"""Prepare Stage-1 IR from existing MinerU artifacts; optionally seed a shadow Milvus collection.

Default mode performs no external writes and does not run MinerU, LLM, or vision.
Use --index --collection NEW_NAME only after confirming the target Milvus service.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def prepare(
    source: Path,
    content_list: Path,
    middle: Path | None = None,
    *,
    logical_document_key: str | None = None,
):
    from knowledge.document_ir.adapters import MinerUAdapter
    from knowledge.document_ir.chunking import chunk_document
    from knowledge.document_ir.normalize import normalize_document

    return chunk_document(normalize_document(MinerUAdapter().convert(
        source_path=source,
        content_list_path=content_list,
        middle_path=middle,
        logical_document_key=logical_document_key,
    )))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--content-list", type=Path, required=True)
    parser.add_argument("--middle", type=Path)
    parser.add_argument("--document-key", help="optional stable key assigned by the caller")
    parser.add_argument("--item-name", default="", help="optional product label for index rows")
    parser.add_argument("--ir-output", type=Path)
    parser.add_argument("--index", action="store_true", help="embed and write a shadow Milvus collection")
    parser.add_argument("--collection", help="new shadow collection name; required with --index")
    args = parser.parse_args(argv)
    if args.index and not args.collection:
        parser.error("--index requires --collection NEW_SHADOW_NAME")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shadow = None
    if args.index:
        from knowledge.processor.import_process.config import get_config

        current = get_config()
        if args.collection == current.chunks_collection:
            raise SystemExit("refusing to modify the current CHUNKS_COLLECTION; use a shadow collection")
        shadow = replace(current, chunks_collection=args.collection)

    document = prepare(
        args.source.resolve(),
        args.content_list.resolve(),
        args.middle.resolve() if args.middle else None,
        logical_document_key=args.document_key,
    )
    if args.ir_output:
        from knowledge.document_ir.serialization import save_document_ir

        save_document_ir(document, args.ir_output.resolve())
    summary = {
        "document_id": document.document_id,
        "revision_id": document.revision_id,
        "source_sha256": document.source.sha256,
        "sections": len(document.sections),
        "blocks": len(document.blocks),
        "chunks": len(document.chunks),
        "grounded_chunks": sum(
            bool(chunk.block_ids and chunk.provenance) for chunk in document.chunks
        ),
        "collection": args.collection or "",
        "indexed": False,
    }
    if args.index:
        from knowledge.document_ir.indexing import chunks_to_index_rows
        from knowledge.processor.import_process.nodes.bge_embedding_chunks_node import BgeEmbeddingNode
        from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode

        state = {"chunks": chunks_to_index_rows(document, item_name=args.item_name)}
        BgeEmbeddingNode(config=shadow).process(state)
        ImportMilvusNode(config=shadow).process(state)
        summary["indexed"] = True
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
