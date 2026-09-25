"""Read-only verification for an isolated stage-2 ACL vector/KG index."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--entity-collection", required=True)
    parser.add_argument("--graph-version", required=True)
    parser.add_argument("--tenant-id", default="public")
    parser.add_argument("--item-name", required=True)
    parser.add_argument("--expected-chunks", type=int, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def _scalar_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    return int(rows[0].get("count(*)") or rows[0].get("count") or 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / "knowledge" / ".env")

    from knowledge.utils.milvus_utils import get_milvus_client
    from knowledge.utils.neo4j_util import get_neo4j_driver

    milvus = get_milvus_client()
    chunk_description = milvus.describe_collection(collection_name=args.collection)
    entity_description = milvus.describe_collection(
        collection_name=args.entity_collection
    )
    chunk_fields = {
        str(field.get("name")) for field in chunk_description.get("fields", [])
    }
    entity_fields = {
        str(field.get("name")) for field in entity_description.get("fields", [])
    }
    tenant = json.dumps(args.tenant_id, ensure_ascii=False)
    other_tenant = json.dumps(f"{args.tenant_id}-isolation-probe", ensure_ascii=False)
    graph_version = json.dumps(args.graph_version, ensure_ascii=False)

    chunk_count = _scalar_count(
        milvus.query(
            collection_name=args.collection,
            filter=f"tenant_id == {tenant}",
            output_fields=["count(*)"],
        )
    )
    chunk_cross_tenant_count = _scalar_count(
        milvus.query(
            collection_name=args.collection,
            filter=f"tenant_id == {other_tenant}",
            output_fields=["count(*)"],
        )
    )
    entity_count = _scalar_count(
        milvus.query(
            collection_name=args.entity_collection,
            filter=f"tenant_id == {tenant} and graph_version == {graph_version}",
            output_fields=["count(*)"],
        )
    )
    entity_cross_tenant_count = _scalar_count(
        milvus.query(
            collection_name=args.entity_collection,
            filter=(
                f"tenant_id == {other_tenant} and graph_version == {graph_version}"
            ),
            output_fields=["count(*)"],
        )
    )

    driver = get_neo4j_driver()
    with driver.session() as session:
        counts = session.run(
            """
            MATCH (n {tenant_id: $tenant_id, graph_version: $graph_version})
            RETURN
              count(CASE WHEN n:Entity THEN 1 END) AS entities,
              count(CASE WHEN n:Chunk THEN 1 END) AS chunks,
              count(CASE WHEN n.visibility IS NULL OR n.acl_readers IS NULL THEN 1 END)
                AS missing_acl_metadata
            """,
            tenant_id=args.tenant_id,
            graph_version=args.graph_version,
        ).single().data()
        relations = session.run(
            """
            MATCH (a {tenant_id: $tenant_id, graph_version: $graph_version})-[r]->
                  (b {tenant_id: $tenant_id, graph_version: $graph_version})
            RETURN
              count(CASE WHEN type(r) = 'MENTIONED_IN' THEN 1 END) AS mentions,
              count(CASE WHEN type(r) <> 'MENTIONED_IN' THEN 1 END) AS business_relations
            """,
            tenant_id=args.tenant_id,
            graph_version=args.graph_version,
        ).single().data()
        duplicate_entities = session.run(
            """
            MATCH (n:Entity {tenant_id: $tenant_id, graph_version: $graph_version})
            WITH n.name AS name, n.item_name AS item_name, count(*) AS copies
            WHERE copies > 1
            RETURN count(*) AS count
            """,
            tenant_id=args.tenant_id,
            graph_version=args.graph_version,
        ).single()["count"]
        duplicate_chunks = session.run(
            """
            MATCH (n:Chunk {tenant_id: $tenant_id, graph_version: $graph_version})
            WITH n.id AS id, n.item_name AS item_name, count(*) AS copies
            WHERE copies > 1
            RETURN count(*) AS count
            """,
            tenant_id=args.tenant_id,
            graph_version=args.graph_version,
        ).single()["count"]
        constraint_names = {
            row["name"]
            for row in session.run("SHOW CONSTRAINTS YIELD name RETURN name").data()
        }

    checks = {
        "chunk_acl_schema": {"tenant_id", "visibility", "acl_readers"}.issubset(
            chunk_fields
        ),
        "entity_acl_schema": {
            "tenant_id",
            "visibility",
            "acl_readers",
            "graph_version",
        }.issubset(entity_fields),
        "chunk_count": chunk_count == args.expected_chunks,
        "chunk_cross_tenant_zero": chunk_cross_tenant_count == 0,
        "entity_nonempty": entity_count > 0,
        "entity_cross_tenant_zero": entity_cross_tenant_count == 0,
        "neo4j_entity_nonempty": int(counts.get("entities") or 0) > 0,
        "neo4j_chunk_nonempty": int(counts.get("chunks") or 0) > 0,
        "neo4j_relationship_nonempty": int(relations.get("business_relations") or 0) > 0,
        "neo4j_acl_complete": int(counts.get("missing_acl_metadata") or 0) == 0,
        "neo4j_entity_unique": int(duplicate_entities or 0) == 0,
        "neo4j_chunk_unique": int(duplicate_chunks or 0) == 0,
        "neo4j_constraints": {
            "shopkeeper_entity_scope",
            "shopkeeper_chunk_scope",
        }.issubset(constraint_names),
    }
    result = {
        "schema_version": "stage2-index-verification-v1",
        "status": "passed" if all(checks.values()) else "failed",
        "collection": args.collection,
        "entity_collection": args.entity_collection,
        "graph_version": args.graph_version,
        "tenant_id": args.tenant_id,
        "item_name": args.item_name,
        "checks": checks,
        "counts": {
            "milvus_chunks": chunk_count,
            "milvus_chunk_cross_tenant": chunk_cross_tenant_count,
            "milvus_entities": entity_count,
            "milvus_entity_cross_tenant": entity_cross_tenant_count,
            "neo4j_entities": int(counts.get("entities") or 0),
            "neo4j_chunks": int(counts.get("chunks") or 0),
            "neo4j_mentions": int(relations.get("mentions") or 0),
            "neo4j_business_relations": int(relations.get("business_relations") or 0),
            "neo4j_missing_acl_metadata": int(
                counts.get("missing_acl_metadata") or 0
            ),
            "neo4j_duplicate_entities": int(duplicate_entities or 0),
            "neo4j_duplicate_chunks": int(duplicate_chunks or 0),
        },
        "chunk_schema_fields": sorted(chunk_fields),
        "entity_schema_fields": sorted(entity_fields),
        "neo4j_constraints": sorted(constraint_names),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
