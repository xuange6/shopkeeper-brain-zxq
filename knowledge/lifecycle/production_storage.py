"""Adapters that apply lifecycle changes to the live production projections."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from knowledge.lifecycle.store import LifecycleStore


def _batches(values: Sequence[str], size: int = 256) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


class RevisionProjection:
    def __init__(self, store: LifecycleStore):
        self.store = store

    def context(self, revision_id: str) -> dict[str, Any]:
        row = self.store.fetch_one(
            """SELECT r.*,d.tenant_id,d.visibility,d.acl_json,d.tombstoned,
                      d.document_id,d.source_id
               FROM document_revisions r JOIN documents d ON d.document_id=r.document_id
               WHERE r.revision_id=?""",
            (revision_id,),
        )
        if row is None:
            raise KeyError(revision_id)
        release_id = str(row.get("staging_release_id") or "")
        release = self.store.get_release(release_id) if release_id else self.store.get_active_release()
        if release is None:
            raise KeyError(f"revision {revision_id} is not bound to a release")
        return {
            **row,
            "data_revision_id": str(row.get("data_revision_id") or revision_id),
            "release": release,
        }


class MilvusChunkBackend(RevisionProjection):
    name = "chunks"

    def __init__(self, store: LifecycleStore, client: Any):
        super().__init__(store)
        self.client = client

    def _rows(self, context: dict[str, Any], output_fields: list[str]) -> list[dict[str, Any]]:
        collection = str(context["release"]["chunk_collection"])
        if not self.client.has_collection(collection_name=collection):
            return []
        return list(
            self.client.query(
                collection_name=collection,
                filter=f"revision_id == {json.dumps(context['data_revision_id'])}",
                output_fields=output_fields,
                limit=16384,
            )
        )

    def chunk_ids(self, revision_id: str) -> list[str]:
        context = self.context(revision_id)
        return [str(row["chunk_id"]) for row in self._rows(context, ["chunk_id"])]

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        context = self.context(revision_id)
        rows = self._rows(context, ["*"])
        if not rows:
            raise KeyError(f"no chunks for revision {revision_id}")
        for row in rows:
            row["tenant_id"] = tenant_id
            row["visibility"] = visibility
            row["acl_readers"] = sorted(set(acl_readers))
        self.client.upsert(
            collection_name=str(context["release"]["chunk_collection"]),
            data=rows,
        )
        self.client.flush(collection_name=str(context["release"]["chunk_collection"]))

    def delete_revision(self, revision_id: str) -> None:
        context = self.context(revision_id)
        collection = str(context["release"]["chunk_collection"])
        if self.client.has_collection(collection_name=collection):
            self.client.delete(
                collection_name=collection,
                filter=f"revision_id == {json.dumps(context['data_revision_id'])}",
            )
            self.client.flush(collection_name=collection)

    def contains_revision(self, revision_id: str) -> bool:
        return bool(self._rows(self.context(revision_id), ["revision_id"]))

    def revision_ids(self) -> set[str]:
        rows = self.store.fetch_all(
            """SELECT revision_id FROM document_revisions
               WHERE processing_status IN ('staged','validated','active')
                 AND staging_release_id<>''""",
        )
        return {row["revision_id"] for row in rows if self.contains_revision(row["revision_id"])}


class MilvusEntityBackend(RevisionProjection):
    name = "milvus"

    def __init__(self, store: LifecycleStore, client: Any, chunks: MilvusChunkBackend):
        super().__init__(store)
        self.client = client
        self.chunks = chunks
        self._revision_chunk_ids: dict[str, list[str]] = {}

    def _matching_rows(self, revision_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        context = self.context(revision_id)
        collection = str(context["release"]["entity_collection"])
        ids = self._revision_chunk_ids.get(revision_id) or self.chunks.chunk_ids(revision_id)
        if not ids or not self.client.has_collection(collection_name=collection):
            return context, []
        rows: list[dict[str, Any]] = []
        for batch in _batches(ids):
            rows.extend(
                self.client.query(
                    collection_name=collection,
                    filter=f"source_chunk_id in {json.dumps(batch)}",
                    output_fields=["*"],
                    limit=16384,
                )
            )
        return context, rows

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        context, rows = self._matching_rows(revision_id)
        if not rows:
            raise KeyError(f"no entity projection for revision {revision_id}")
        for row in rows:
            row["tenant_id"] = tenant_id
            row["visibility"] = visibility
            row["acl_readers"] = sorted(set(acl_readers))
        collection = str(context["release"]["entity_collection"])
        self.client.upsert(collection_name=collection, data=rows)
        self.client.flush(collection_name=collection)

    def delete_revision(self, revision_id: str) -> None:
        context = self.context(revision_id)
        collection = str(context["release"]["entity_collection"])
        ids = self.chunks.chunk_ids(revision_id)
        self._revision_chunk_ids[revision_id] = ids
        if not self.client.has_collection(collection_name=collection):
            return
        for batch in _batches(ids):
            self.client.delete(
                collection_name=collection,
                filter=f"source_chunk_id in {json.dumps(batch)}",
            )
        self.client.flush(collection_name=collection)

    def contains_revision(self, revision_id: str) -> bool:
        _, rows = self._matching_rows(revision_id)
        return bool(rows)

    def revision_ids(self) -> set[str]:
        return self.chunks.revision_ids()


class Neo4jDocumentBackend(RevisionProjection):
    name = "neo4j"

    def __init__(self, store: LifecycleStore, driver: Any, chunks: MilvusChunkBackend, *, database: str):
        super().__init__(store)
        self.driver = driver
        self.chunks = chunks
        self.database = database
        self._revision_chunk_ids: dict[str, list[str]] = {}

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        context = self.context(revision_id)
        ids = self._revision_chunk_ids.get(revision_id) or self.chunks.chunk_ids(revision_id)
        if not ids:
            raise KeyError(f"no graph chunks for revision {revision_id}")
        with self.driver.session(database=self.database) as session:
            session.run(
                """MATCH (c:Chunk) WHERE c.graph_version=$version AND c.id IN $ids
                   SET c.tenant_id=$tenant,c.visibility=$visibility,c.acl_readers=$readers
                   WITH c OPTIONAL MATCH (e:Entity)-[:MENTIONED_IN]->(c)
                   SET e.tenant_id=$tenant,e.visibility=$visibility,e.acl_readers=$readers""",
                version=context["release"]["graph_version"], ids=ids,
                tenant=tenant_id, visibility=visibility, readers=sorted(set(acl_readers)),
            ).consume()

    def delete_revision(self, revision_id: str) -> None:
        context = self.context(revision_id)
        ids = self.chunks.chunk_ids(revision_id)
        self._revision_chunk_ids[revision_id] = ids
        with self.driver.session(database=self.database) as session:
            session.run(
                """MATCH (c:Chunk) WHERE c.graph_version=$version AND c.id IN $ids
                   DETACH DELETE c""",
                version=context["release"]["graph_version"], ids=ids,
            ).consume()
            session.run(
                """MATCH (e:Entity) WHERE e.graph_version=$version
                   AND NOT (e)-[:MENTIONED_IN]->() DETACH DELETE e""",
                version=context["release"]["graph_version"],
            ).consume()

    def contains_revision(self, revision_id: str) -> bool:
        context = self.context(revision_id)
        ids = self._revision_chunk_ids.get(revision_id) or self.chunks.chunk_ids(revision_id)
        if not ids:
            return False
        with self.driver.session(database=self.database) as session:
            record = session.run(
                "MATCH (c:Chunk) WHERE c.graph_version=$version AND c.id IN $ids RETURN count(c) AS count",
                version=context["release"]["graph_version"], ids=ids,
            ).single()
            return bool(record and record["count"])

    def revision_ids(self) -> set[str]:
        return self.chunks.revision_ids()


class MinioDocumentBackend(RevisionProjection):
    name = "objects"

    def __init__(self, store: LifecycleStore, client: Any, bucket: str):
        super().__init__(store)
        self.client = client
        self.bucket = bucket

    def _prefix(self, revision_id: str) -> str:
        context = self.context(revision_id)
        namespace = str(context["release"]["object_asset_namespace"]).strip("/")
        return f"{namespace}/documents/{context['document_id']}/assets/"

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        # Objects for protected documents are stored as internal minio:// URIs;
        # authorization occurs before a trusted endpoint signs a download URL.
        self.context(revision_id)

    def delete_revision(self, revision_id: str) -> None:
        for item in self.client.list_objects(self.bucket, prefix=self._prefix(revision_id), recursive=True):
            self.client.remove_object(self.bucket, item.object_name)

    def contains_revision(self, revision_id: str) -> bool:
        return next(iter(self.client.list_objects(self.bucket, prefix=self._prefix(revision_id), recursive=True)), None) is not None

    def revision_ids(self) -> set[str]:
        return set()


class AbsentCacheBackend:
    """Explicit backend for caches that this deployment does not persist."""

    def __init__(self, name: str):
        self.name = name

    def update_acl(self, revision_id: str, **_: Any) -> None:
        return None

    def delete_revision(self, revision_id: str) -> None:
        return None

    def contains_revision(self, revision_id: str) -> bool:
        return False

    def revision_ids(self) -> set[str]:
        return set()


class ControlPlaneBackend(RevisionProjection):
    def __init__(self, store: LifecycleStore, name: str):
        super().__init__(store)
        self.name = name

    def update_acl(self, revision_id: str, **_: Any) -> None:
        self.context(revision_id)

    def delete_revision(self, revision_id: str) -> None:
        self.context(revision_id)

    def contains_revision(self, revision_id: str) -> bool:
        return not bool(self.context(revision_id)["tombstoned"])

    def revision_ids(self) -> set[str]:
        return set()


class ReleaseReferenceBackend(RevisionProjection):
    name = "release_references"

    def update_acl(self, revision_id: str, **_: Any) -> None:
        return None

    def delete_revision(self, revision_id: str) -> None:
        if self.contains_revision(revision_id):
            raise RuntimeError("active release still references the deleted revision; roll back or publish a replacement first")

    def contains_revision(self, revision_id: str) -> bool:
        active = self.store.get_active_release()
        if active is None:
            return False
        return revision_id in set(json.loads(str(active["source_revisions_json"])))

    def revision_ids(self) -> set[str]:
        active = self.store.get_active_release()
        return set(json.loads(str(active["source_revisions_json"]))) if active else set()
