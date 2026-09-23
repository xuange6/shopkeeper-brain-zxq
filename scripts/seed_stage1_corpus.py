"""Prepare Stage-1 IR from existing MinerU artifacts; optionally seed a shadow Milvus collection.

Default mode performs no external writes and does not run MinerU, LLM, or vision.
Use --index --collection NEW_NAME only after confirming the target Milvus service.
Image uploads require separate authorization and the explicit --enrich-assets
flag. Without it, indexing retains local/remote references without uploading.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


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
    parser.add_argument("--ir-output", type=Path, help="save the final (post-enrichment when indexed) IR")
    parser.add_argument("--index", action="store_true", help="embed and write a shadow Milvus collection")
    parser.add_argument("--collection", help="new shadow collection name; required with --index")
    parser.add_argument(
        "--enrich-assets", action="store_true",
        help="upload local images to configured object storage; requires --index and separate upload authorization",
    )
    args = parser.parse_args(argv)
    if args.index and not args.collection:
        parser.error("--index requires --collection NEW_SHADOW_NAME")
    if args.enrich_assets and not args.index:
        parser.error("--enrich-assets requires --index; default preparation never uploads assets")
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
        if args.enrich_assets and not shadow.minio_bucket:
            raise SystemExit("--enrich-assets requires a configured image storage bucket")

    document = prepare(
        args.source.resolve(),
        args.content_list.resolve(),
        args.middle.resolve() if args.middle else None,
        logical_document_key=args.document_key,
    )
    uploaded_images = 0
    if args.index:
        from knowledge.processor.import_process.nodes.bge_embedding_chunks_node import BgeEmbeddingNode
        from knowledge.processor.import_process.nodes.document_enrich_node import DocumentEnrichNode
        from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode

        # Reuse the production enrichment/projection path. The explicit flag is
        # an independent consent boundary from Milvus indexing: existing --index
        # invocations must not silently acquire image-upload side effects.
        enrichment_config = shadow if args.enrich_assets else replace(shadow, minio_bucket="")
        with TemporaryDirectory(prefix="shopkeeper-stage1-ir-") as temporary:
            state = {
                "document_ir": document,
                "file_dir": str(args.content_list.resolve().parent),
                "import_file_path": str(args.source.resolve()),
                "item_name": args.item_name,
                "ir_path": str(args.ir_output.resolve() if args.ir_output else Path(temporary) / "document.ir.json"),
            }
            state = DocumentEnrichNode(config=enrichment_config).process(state)
            document = state["document_ir"]
            uploaded_images = document.metadata["enrichment"]["uploaded_image_count"]
            expected_uploads = sum(bool(block.image and block.image.local_path) for block in document.blocks)
            if args.enrich_assets and uploaded_images != expected_uploads:
                raise SystemExit(
                    f"asset enrichment incomplete ({uploaded_images}/{expected_uploads} local images); "
                    "shadow indexing aborted"
                )
            state = BgeEmbeddingNode(config=shadow).process(state)
            ImportMilvusNode(config=shadow).process(state)
    elif args.ir_output:
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
        "indexed": args.index,
        "asset_uploads_requested": args.enrich_assets,
        "uploaded_images": uploaded_images,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
