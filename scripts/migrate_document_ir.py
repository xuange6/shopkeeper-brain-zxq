"""Build or compare stage-1 DocumentIR artifacts without invoking MinerU.

Examples:
    python scripts/migrate_document_ir.py manual.md --output document.ir.json
    python scripts/migrate_document_ir.py manual.pdf \
      --content-list mineru_content_list_v2.json --middle mineru_middle.json \
      --output document.ir.json --compare previous.ir.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.document_ir.adapters import MarkdownAdapter, MinerUAdapter
from knowledge.document_ir.chunking import ChunkingConfig, chunk_document
from knowledge.document_ir.diff import diff_documents
from knowledge.document_ir.lineage import align_document
from knowledge.document_ir.normalize import normalize_document
from knowledge.document_ir.revision_guard import (
    DEFAULT_MIN_REVISION_OVERLAP,
    assess_revision_continuity,
)
from knowledge.document_ir.serialization import load_document_ir, save_document_ir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--content-list", type=Path)
    parser.add_argument("--middle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--document-key", help="explicit stable source key for revised versions")
    parser.add_argument(
        "--min-revision-overlap", type=float, default=DEFAULT_MIN_REVISION_OVERLAP,
        help="minimum body overlap before treating --compare as the same document",
    )
    parser.add_argument("--max-characters", type=int, default=1200)
    parser.add_argument("--min-characters", type=int, default=300)
    parser.add_argument("--overlap-characters", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    if source.suffix.lower() in {".md", ".markdown"}:
        document = MarkdownAdapter().convert(source, logical_document_key=args.document_key)
    elif source.suffix.lower() == ".pdf":
        if not args.content_list:
            raise SystemExit("--content-list is required for PDF migration")
        document = MinerUAdapter().convert(
            source_path=source,
            content_list_path=args.content_list.resolve(),
            middle_path=args.middle.resolve() if args.middle else None,
            logical_document_key=args.document_key,
        )
    else:
        raise SystemExit(f"unsupported source type: {source.suffix}")

    document = normalize_document(document)
    if args.compare:
        previous = load_document_ir(args.compare.resolve())
        if previous.document_id != document.document_id:
            raise SystemExit("--compare requires the same explicit --document-key for changed source bytes")
        try:
            continuity = assess_revision_continuity(
                previous, document, minimum_overlap=args.min_revision_overlap
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if not continuity.accepted:
            raise SystemExit(
                f"possible document-key collision: body overlap {continuity.overlap:.3f} "
                f"is below {args.min_revision_overlap:.3f}; no IR was written"
            )
        document = align_document(previous, document)
        document.metadata["revision_continuity"] = continuity.as_metadata()
    document = chunk_document(
        document,
        ChunkingConfig(
            max_characters=max(1, args.max_characters),
            min_characters=max(0, args.min_characters),
            overlap_characters=max(0, args.overlap_characters),
        ),
    )
    output = args.output.resolve()
    save_document_ir(document, output)
    summary = {
        "output": str(output),
        "document_id": document.document_id,
        "revision_id": document.revision_id,
        "status": document.status.value,
        "sections": len(document.sections),
        "blocks": len(document.blocks),
        "chunks": len(document.chunks),
    }
    if args.compare:
        difference = diff_documents(previous, document)
        summary["diff"] = difference.model_dump(mode="json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
