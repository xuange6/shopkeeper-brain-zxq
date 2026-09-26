"""Real Milvus, Neo4j, and MinIO lifecycle adapters.

These adapters use isolated stage-3 revision projections.  Production chunk
collections remain immutable; ACL-only changes are written as revision-scoped
authorization projections and retrieval must apply the deny projection before
reading a chunk collection.  This avoids unsafe in-place vector rewrites.
"""

from __future__ import annotations

import json
from typing import Any, Sequence


class MilvusRevisionBackend:
    name = "milvus"

    def __init__(self, client: Any, collection: str, *, vector_dim: int = 4):
        self.client = client
        self.collection = collection
        self.vector_dim = vector_dim

    def ensure(self) -> None:
        if self.client.has_collection(collection_name=self.collection):
            return
        from pymilvus import DataType

        schema = self.client.create_schema(enable_dynamic_fields=False)
        schema.add_field(field_name="revision_id", datatype=DataType.VARCHAR, max_length=128, is_primary=True)
        schema.add_field(field_name="tenant_id", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(field_name="visibility", datatype=DataType.VARCHAR, max_length=16)
        schema.add_field(field_name="acl_json", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=self.vector_dim)
        index = self.client.prepare_index_params()
        index.add_index(field_name="dense_vector", index_name="dense_vector_index", index_type="FLAT", metric_type="COSINE")
        self.client.create_collection(collection_name=self.collection, schema=schema, index_params=index)

    def put_revision(self, revision_id: str, *, tenant_id: str = "public", visibility: str = "public", acl_readers: Sequence[str] = ()) -> None:
        self.ensure()
        seed = (sum(revision_id.encode("utf-8")) % 97) / 100.0
        self.client.upsert(
            collection_name=self.collection,
            data=[{
                "revision_id": revision_id,
                "tenant_id": tenant_id,
                "visibility": visibility,
                "acl_json": json.dumps(sorted(set(acl_readers)), ensure_ascii=False),
                "dense_vector": [seed + (index / 1000.0) for index in range(self.vector_dim)],
            }],
        )
        self.client.flush(collection_name=self.collection)

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        if not self.contains_revision(revision_id):
            raise KeyError(revision_id)
        self.put_revision(revision_id, tenant_id=tenant_id, visibility=visibility, acl_readers=acl_readers)

    def delete_revision(self, revision_id: str) -> None:
        if not self.client.has_collection(collection_name=self.collection):
            return
        self.client.delete(collection_name=self.collection, filter=f"revision_id == {json.dumps(revision_id)}")
        self.client.flush(collection_name=self.collection)

    def contains_revision(self, revision_id: str) -> bool:
        if not self.client.has_collection(collection_name=self.collection):
            return False
        rows = self.client.query(collection_name=self.collection, filter=f"revision_id == {json.dumps(revision_id)}", output_fields=["revision_id"], limit=1)
        return bool(rows)

    def revision_ids(self) -> set[str]:
        if not self.client.has_collection(collection_name=self.collection):
            return set()
        rows = self.client.query(collection_name=self.collection, filter='revision_id != ""', output_fields=["revision_id"], limit=16384)
        return {str(row["revision_id"]) for row in rows}


class Neo4jRevisionBackend:
    name = "neo4j"

    def __init__(self, driver: Any, *, database: str = "neo4j", namespace: str = "stage3"):
        self.driver = driver
        self.database = database
        self.namespace = namespace

    def ensure(self) -> None:
        with self.driver.session(database=self.database) as session:
            session.run("CREATE CONSTRAINT stage3_revision_identity IF NOT EXISTS FOR (r:LifecycleRevision) REQUIRE (r.namespace, r.revision_id) IS UNIQUE").consume()

    def put_revision(self, revision_id: str, *, tenant_id: str = "public", visibility: str = "public", acl_readers: Sequence[str] = ()) -> None:
        self.ensure()
        with self.driver.session(database=self.database) as session:
            session.run(
                """MERGE (r:LifecycleRevision {namespace:$namespace, revision_id:$revision_id})
                   SET r.tenant_id=$tenant_id, r.visibility=$visibility, r.acl_readers=$acl_readers""",
                namespace=self.namespace,
                revision_id=revision_id,
                tenant_id=tenant_id,
                visibility=visibility,
                acl_readers=list(sorted(set(acl_readers))),
            ).consume()

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        if not self.contains_revision(revision_id):
            raise KeyError(revision_id)
        self.put_revision(revision_id, tenant_id=tenant_id, visibility=visibility, acl_readers=acl_readers)

    def delete_revision(self, revision_id: str) -> None:
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (r:LifecycleRevision {namespace:$namespace, revision_id:$revision_id}) DETACH DELETE r", namespace=self.namespace, revision_id=revision_id).consume()

    def contains_revision(self, revision_id: str) -> bool:
        with self.driver.session(database=self.database) as session:
            return bool(session.run("MATCH (r:LifecycleRevision {namespace:$namespace, revision_id:$revision_id}) RETURN count(r) AS count", namespace=self.namespace, revision_id=revision_id).single()["count"])

    def revision_ids(self) -> set[str]:
        with self.driver.session(database=self.database) as session:
            return {str(record["revision_id"]) for record in session.run("MATCH (r:LifecycleRevision {namespace:$namespace}) RETURN r.revision_id AS revision_id", namespace=self.namespace)}


class MinioRevisionBackend:
    name = "objects"

    def __init__(self, client: Any, bucket: str, *, namespace: str = "stage3-lifecycle"):
        self.client = client
        self.bucket = bucket
        self.namespace = namespace.strip("/")

    def ensure(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def _prefix(self, revision_id: str) -> str:
        return f"{self.namespace}/{revision_id}/"

    def put_revision(self, revision_id: str, *, tenant_id: str = "public", visibility: str = "public", acl_readers: Sequence[str] = ()) -> None:
        from io import BytesIO

        self.ensure()
        payload = json.dumps({"revision_id": revision_id, "tenant_id": tenant_id, "visibility": visibility, "acl_readers": sorted(set(acl_readers))}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.client.put_object(self.bucket, self._prefix(revision_id) + "manifest.json", BytesIO(payload), len(payload), content_type="application/json")

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        if not self.contains_revision(revision_id):
            raise KeyError(revision_id)
        self.put_revision(revision_id, tenant_id=tenant_id, visibility=visibility, acl_readers=acl_readers)

    def delete_revision(self, revision_id: str) -> None:
        objects = list(self.client.list_objects(self.bucket, prefix=self._prefix(revision_id), recursive=True))
        for item in objects:
            self.client.remove_object(self.bucket, item.object_name)

    def contains_revision(self, revision_id: str) -> bool:
        try:
            return next(iter(self.client.list_objects(self.bucket, prefix=self._prefix(revision_id), recursive=True)), None) is not None
        except Exception:
            return False

    def revision_ids(self) -> set[str]:
        prefix = self.namespace + "/"
        result: set[str] = set()
        for item in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            suffix = item.object_name[len(prefix):]
            if "/" in suffix:
                result.add(suffix.split("/", 1)[0])
        return result
