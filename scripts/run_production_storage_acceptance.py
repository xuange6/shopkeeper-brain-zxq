"""Validate production ACL/deletion adapters against real local data services."""

from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from minio import Minio
from neo4j import GraphDatabase
from pymilvus import DataType, MilvusClient

from knowledge.lifecycle.connectors import LocalDirectoryConnector
from knowledge.lifecycle.models import ReleaseManifest, ReleaseState, RevisionState
from knowledge.lifecycle.production_storage import (
    MilvusChunkBackend,
    MilvusEntityBackend,
    MinioDocumentBackend,
    Neo4jDocumentBackend,
)
from knowledge.lifecycle.storage import ACLSynchronizer, DeletionOrchestrator
from knowledge.lifecycle.store import LifecycleStore
from knowledge.lifecycle.sync import SyncEngine


def _create_collection(client: MilvusClient, name: str, fields: str) -> None:
    schema = client.create_schema(enable_dynamic_fields=False)
    if fields == "chunks":
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=128, is_primary=True)
        schema.add_field("revision_id", DataType.VARCHAR, max_length=128)
    else:
        schema.add_field("pk", DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field("source_chunk_id", DataType.VARCHAR, max_length=128)
        schema.add_field("entity_name", DataType.VARCHAR, max_length=128)
    schema.add_field("tenant_id", DataType.VARCHAR, max_length=128)
    schema.add_field("visibility", DataType.VARCHAR, max_length=16)
    schema.add_field(
        "acl_readers", DataType.ARRAY, element_type=DataType.VARCHAR,
        max_capacity=128, max_length=256,
    )
    schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=4)
    indexes = client.prepare_index_params()
    indexes.add_index("dense_vector", index_type="FLAT", metric_type="COSINE")
    client.create_collection(collection_name=name, schema=schema, index_params=indexes)


def main() -> None:
    tag = uuid4().hex[:12]
    milvus = MilvusClient(
        uri=os.getenv("LIFECYCLE_ACCEPTANCE_MILVUS_URL", "http://127.0.0.1:19530")
    )
    neo4j = GraphDatabase.driver(
        os.getenv("LIFECYCLE_ACCEPTANCE_NEO4J_URI", "bolt://127.0.0.1:7687"),
        auth=(
            os.getenv("LIFECYCLE_ACCEPTANCE_NEO4J_USERNAME", "neo4j"),
            os.getenv("LIFECYCLE_ACCEPTANCE_NEO4J_PASSWORD", "shopkeeper-dev"),
        ),
    )
    neo4j.verify_connectivity()
    minio = Minio(
        os.getenv("LIFECYCLE_ACCEPTANCE_MINIO_ENDPOINT", "127.0.0.1:9000").removeprefix("http://"),
        access_key=os.getenv("LIFECYCLE_ACCEPTANCE_MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("LIFECYCLE_ACCEPTANCE_MINIO_SECRET_KEY", "minioadmin"),
        secure=False,
    )
    bucket = os.getenv("LIFECYCLE_ACCEPTANCE_MINIO_BUCKET", "a-bucket")
    if not minio.bucket_exists(bucket):
        minio.make_bucket(bucket)

    chunk_collection = f"kb_lifecycle_chunks_{tag}"
    entity_collection = f"kb_lifecycle_entities_{tag}"
    graph_version = f"lifecycle-{tag}"
    namespace = f"lifecycle/{tag}"
    neo4j_database = os.getenv("LIFECYCLE_ACCEPTANCE_NEO4J_DATABASE", "neo4j")
    _create_collection(milvus, chunk_collection, "chunks")
    _create_collection(milvus, entity_collection, "entities")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        document_path = root / "guide.md"
        document_path.write_text("# Guide\nproduction lifecycle", encoding="utf-8")
        store = LifecycleStore(root / "lifecycle.sqlite3")
        store.upsert_source(
            source_id="production-adapter-source",
            tenant_id="tenant-a",
            connector_type="local_directory",
            configuration_version="v1",
            configuration={"root": str(root), "staging_release_id": tag},
        )
        release = ReleaseManifest(
            tag, (), chunk_collection, entity_collection, graph_version, namespace
        )
        store.create_release(release)
        store.transition_release(
            tag, ReleaseState.STAGING, actor="builder", idempotency_key="stage"
        )
        SyncEngine(store).run(
            "production-adapter-source",
            LocalDirectoryConnector(root),
            idempotency_key="seed",
            owner="acceptance",
            full=True,
        )
        revision = store.fetch_one("SELECT * FROM document_revisions")
        document = store.fetch_one("SELECT * FROM documents")
        assert revision and document
        for state in (RevisionState.PROCESSING, RevisionState.STAGED):
            store.transition_revision(
                revision["revision_id"], state, actor="worker",
                idempotency_key=f"seed:{state.value}",
            )
        data_revision_id = f"data-{tag}"
        store.bind_revision_projection(
            revision["revision_id"],
            data_revision_id=data_revision_id,
            staging_release_id=tag,
        )
        store.attach_revision_to_release(tag, revision["revision_id"])
        chunk_id = f"chunk-{tag}"
        milvus.insert(
            collection_name=chunk_collection,
            data=[{
                "chunk_id": chunk_id,
                "revision_id": data_revision_id,
                "tenant_id": "tenant-a",
                "visibility": "public",
                "acl_readers": [],
                "dense_vector": [0.1, 0.2, 0.3, 0.4],
            }],
        )
        milvus.insert(
            collection_name=entity_collection,
            data=[{
                "source_chunk_id": chunk_id,
                "entity_name": "Guide",
                "tenant_id": "tenant-a",
                "visibility": "public",
                "acl_readers": [],
                "dense_vector": [0.1, 0.2, 0.3, 0.4],
            }],
        )
        milvus.flush(collection_name=chunk_collection)
        milvus.flush(collection_name=entity_collection)
        with neo4j.session(database=neo4j_database) as session:
            session.run(
                """MERGE (c:Chunk {id:$chunk,graph_version:$version})
                   SET c.tenant_id='tenant-a',c.visibility='public',c.acl_readers=[]
                   MERGE (e:Entity {name:'Guide',graph_version:$version})
                   SET e.tenant_id='tenant-a',e.visibility='public',e.acl_readers=[]
                   MERGE (e)-[:MENTIONED_IN]->(c)""",
                chunk=chunk_id, version=graph_version,
            ).consume()
        object_name = f"{namespace}/documents/{document['document_id']}/assets/image.png"
        payload = b"safe-image-fixture"
        minio.put_object(bucket, object_name, BytesIO(payload), len(payload))

        chunks = MilvusChunkBackend(store, milvus)
        entities = MilvusEntityBackend(store, milvus, chunks)
        graph = Neo4jDocumentBackend(
            store, neo4j, chunks,
            database=neo4j_database,
        )
        objects = MinioDocumentBackend(store, minio, bucket)
        backends = (entities, graph, chunks, objects)
        ACLSynchronizer(store, backends).propagate(
            revision["revision_id"],
            tenant_id="tenant-a",
            visibility="private",
            acl_readers=("group:support",),
        )
        chunk_acl = milvus.query(
            collection_name=chunk_collection,
            filter=f"revision_id == {json.dumps(data_revision_id)}",
            output_fields=["visibility", "acl_readers"],
            limit=1,
        )[0]
        entity_acl = milvus.query(
            collection_name=entity_collection,
            filter=f"source_chunk_id == {json.dumps(chunk_id)}",
            output_fields=["visibility", "acl_readers"],
            limit=1,
        )[0]
        chunk_acl = {
            "visibility": str(chunk_acl["visibility"]),
            "acl_readers": list(chunk_acl["acl_readers"]),
        }
        entity_acl = {
            "visibility": str(entity_acl["visibility"]),
            "acl_readers": list(entity_acl["acl_readers"]),
        }
        deletion = store.create_deletion(
            document["document_id"], revision["revision_id"],
            run_id="production-adapter-delete",
            targets=tuple(backend.name for backend in backends),
        )
        result = DeletionOrchestrator(store, backends).run(deletion["deletion_id"])
        if result["state"] != "completed":
            raise AssertionError(result["error_message"])
        print(json.dumps({
            "status": "passed",
            "acl": {
                "chunks": chunk_acl,
                "entities": entity_acl,
            },
            "deletion_state": result["state"],
            "verified_targets": json.loads(result["verification_json"]),
        }, ensure_ascii=False, sort_keys=True))

    neo4j.close()


if __name__ == "__main__":
    main()
