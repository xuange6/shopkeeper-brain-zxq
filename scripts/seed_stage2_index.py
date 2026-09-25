"""Seed isolated stage-2 vector and optional knowledge-graph indexes.

The command preserves stage-1 document/chunk identities, citations, and asset
URIs. Vector data and graph data are written only to explicitly named shadow
versions. Knowledge-graph extraction is opt-in because it calls the configured
LLM once per chunk; Web search, MinerU, and object storage are never called.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ir-input", type=Path, required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--item-name", default="HAK 180")
    parser.add_argument("--tenant-id", default="public")
    parser.add_argument("--visibility", choices=("public", "tenant", "private"), default="public")
    parser.add_argument("--acl-reader", action="append", default=[])
    parser.add_argument("--with-kg", action="store_true")
    parser.add_argument("--entity-collection", default="")
    parser.add_argument("--graph-version", default="")
    parser.add_argument("--audit-output", type=Path)
    return parser.parse_args(argv)


def _emit_audit(result: dict, output: Path | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from knowledge.document_ir.indexing import chunks_to_index_rows
    from knowledge.document_ir.serialization import load_document_ir
    from knowledge.processor.import_process.config import get_config
    from knowledge.processor.import_process.nodes.bge_embedding_chunks_node import BgeEmbeddingNode
    from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode
    from knowledge.processor.import_process.nodes.kg_graph_node import KnowledgeGraphNode

    current = get_config()
    if args.collection == current.chunks_collection:
        raise SystemExit("refusing to modify the current CHUNKS_COLLECTION; use a stage-2 shadow collection")
    if args.with_kg:
        if not args.entity_collection or not args.graph_version:
            raise SystemExit("--with-kg requires --entity-collection and --graph-version")
        if args.entity_collection == current.entity_name_collection:
            raise SystemExit(
                "refusing to modify the current ENTITY_NAME_COLLECTION; use a shadow collection"
            )
        if args.graph_version == current.kg_graph_version:
            raise SystemExit(
                "refusing to rebuild the active KG_GRAPH_VERSION; use a shadow graph version"
            )
    document = load_document_ir(args.ir_input.resolve())
    rows = chunks_to_index_rows(
        document,
        item_name=args.item_name,
        tenant_id=args.tenant_id,
        visibility=args.visibility,
        acl_readers=args.acl_reader,
    )
    if not rows or len({row["chunk_id"] for row in rows}) != len(rows):
        raise SystemExit("IR projection is empty or contains duplicate stable chunk IDs")

    shadow = replace(
        current,
        chunks_collection=args.collection,
        entity_name_collection=(args.entity_collection or current.entity_name_collection),
        kg_graph_version=(args.graph_version or current.kg_graph_version),
    )
    state = {
        "document_ir": document,
        "chunks": rows,
        "item_name": args.item_name,
        "tenant_id": args.tenant_id,
        "visibility": args.visibility,
        "acl_readers": list(args.acl_reader),
        "graph_version": args.graph_version or current.kg_graph_version,
    }
    state = BgeEmbeddingNode(config=shadow).process(state)
    state = ImportMilvusNode(config=shadow).process(state)
    result = {
        "schema_version": "stage2-index-seed-v2",
        "collection": args.collection,
        "document_id": document.document_id,
        "revision_id": document.revision_id,
        "source_sha256": document.source.sha256,
        "chunk_count": len(state.get("chunks") or []),
        "unique_chunk_count": len({row["chunk_id"] for row in state.get("chunks") or []}),
        "asset_uri_count": sum(
            len((json.loads(row["citation"]) or {}).get("images") or [])
            for row in rows
        ),
        "access_policy": {
            "tenant_id": args.tenant_id,
            "visibility": args.visibility,
            "acl_reader_count": len(args.acl_reader),
        },
        "knowledge_graph": {
            "status": "not_requested",
            "entity_collection": args.entity_collection or "",
            "graph_version": args.graph_version or "",
        },
    }
    if args.with_kg:
        try:
            state = KnowledgeGraphNode(config=shadow).process(state)
            result["knowledge_graph"] = {
                "status": "passed",
                "entity_collection": args.entity_collection,
                "graph_version": args.graph_version,
                **(state.get("kg_import_stats") or {}),
            }
        except Exception as exc:
            result["knowledge_graph"] = {
                "status": "failed",
                "entity_collection": args.entity_collection,
                "graph_version": args.graph_version,
                "error_type": type(exc).__name__,
                "error": str(exc)[:1000],
                **(state.get("kg_import_stats") or {}),
            }
            _emit_audit(result, args.audit_output)
            raise

    _emit_audit(result, args.audit_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
