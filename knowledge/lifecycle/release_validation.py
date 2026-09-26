"""Fail-closed validation gates for a production knowledge release."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from knowledge.lifecycle.models import ReleaseManifest
from knowledge.lifecycle.publisher import REQUIRED_VALIDATION_CHECKS
from knowledge.lifecycle.store import LifecycleStore


def _source_rows(store: LifecycleStore, release: dict[str, Any]) -> list[dict[str, Any]]:
    revision_ids = list(json.loads(str(release["source_revisions_json"])))
    if not revision_ids or len(revision_ids) != len(set(revision_ids)):
        return []
    rows: list[dict[str, Any]] = []
    for revision_id in revision_ids:
        row = store.fetch_one(
            """SELECT r.*,d.tenant_id,d.visibility,d.acl_json,d.tombstoned
               FROM document_revisions r JOIN documents d ON d.document_id=r.document_id
               WHERE r.revision_id=?""",
            (revision_id,),
        )
        if row is None:
            return []
        rows.append(row)
    return rows


def build_release_checks(
    store: LifecycleStore,
    *,
    milvus: Any,
    neo4j: Any,
    minio: Any,
    minio_bucket: str,
    neo4j_database: str,
) -> Mapping[str, Callable[[dict[str, Any]], bool]]:
    def document_count(release: dict[str, Any]) -> bool:
        rows = _source_rows(store, release)
        return bool(rows) and all(
            not row["tombstoned"]
            and row["processing_status"] in {"staged", "validated", "active"}
            and bool(row.get("data_revision_id"))
            for row in rows
        )

    def chunk_count(release: dict[str, Any]) -> bool:
        rows = _source_rows(store, release)
        collection = str(release["chunk_collection"])
        if not rows or not milvus.has_collection(collection_name=collection):
            return False
        for row in rows:
            matches = milvus.query(
                collection_name=collection,
                filter=f"revision_id == {json.dumps(row['data_revision_id'])}",
                output_fields=["chunk_id"],
                limit=1,
            )
            if not matches:
                return False
        return True

    def acl(release: dict[str, Any]) -> bool:
        collection = str(release["chunk_collection"])
        for row in _source_rows(store, release):
            matches = milvus.query(
                collection_name=collection,
                filter=f"revision_id == {json.dumps(row['data_revision_id'])}",
                output_fields=["tenant_id", "visibility", "acl_readers"],
                limit=16384,
            )
            expected_readers = sorted(json.loads(str(row["acl_json"])))
            if not matches or any(
                str(item.get("tenant_id")) != str(row["tenant_id"])
                or str(item.get("visibility")) != str(row["visibility"])
                or sorted(item.get("acl_readers") or []) != expected_readers
                for item in matches
            ):
                return False
        return True

    def assets(release: dict[str, Any]) -> bool:
        namespace = str(release["object_asset_namespace"]).strip("/")
        path = PurePosixPath(namespace)
        return bool(namespace) and ".." not in path.parts and minio.bucket_exists(minio_bucket)

    def kg_lineage(release: dict[str, Any]) -> bool:
        rows = _source_rows(store, release)
        collection = str(release["chunk_collection"])
        ids: list[str] = []
        for row in rows:
            ids.extend(
                str(item["chunk_id"])
                for item in milvus.query(
                    collection_name=collection,
                    filter=f"revision_id == {json.dumps(row['data_revision_id'])}",
                    output_fields=["chunk_id"],
                    limit=16384,
                )
            )
        if not ids:
            return False
        with neo4j.session(database=neo4j_database) as session:
            result = session.run(
                """MATCH (c:Chunk) WHERE c.graph_version=$version AND c.id IN $ids
                   RETURN count(DISTINCT c) AS count""",
                version=release["graph_version"], ids=ids,
            ).single()
        return bool(result and int(result["count"]) == len(set(ids)))

    def evaluation_gate(release: dict[str, Any]) -> bool:
        report_path = Path(str(release.get("evaluation_report") or ""))
        if not report_path.is_file():
            return False
        report = json.loads(report_path.read_text(encoding="utf-8"))
        metadata = report.get("metadata") or {}
        collections = metadata.get("collections") or {}
        return bool(
            (report.get("gate") or {}).get("passed")
            and collections.get("chunks") == release["chunk_collection"]
            and collections.get("entities") == release["entity_collection"]
            and metadata.get("kg_graph_version") == release["graph_version"]
        )

    def manifest(release: dict[str, Any]) -> bool:
        payload = json.loads(str(release["manifest_json"]))
        try:
            candidate = ReleaseManifest(
                release_id=str(payload["release_id"]),
                source_revisions=tuple(payload["source_revisions"]),
                chunk_collection=str(payload["chunk_collection"]),
                entity_collection=str(payload["entity_collection"]),
                graph_version=str(payload["graph_version"]),
                object_asset_namespace=str(payload["object_asset_namespace"]),
                schema_version=str(payload["schema_version"]),
                evaluation_report=str(payload.get("evaluation_report") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return candidate.manifest_hash == release["manifest_hash"]

    checks = {
        "document_count": document_count,
        "chunk_count": chunk_count,
        "acl": acl,
        "assets": assets,
        "kg_lineage": kg_lineage,
        "evaluation_gate": evaluation_gate,
        "manifest": manifest,
    }
    assert set(checks) == set(REQUIRED_VALIDATION_CHECKS)
    return checks
