"""Seed the versioned stage-0 evaluation corpus into the configured stores.

The repository already contains the parser output for the HAK 180 manual.  This
script deliberately reuses those chunks so the quality-baseline setup does not
depend on rerunning document parsing or vision calls.  Inserts are guarded by
the document title, making the default Milvus seed idempotent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENV_PATH = REPO_ROOT / "knowledge" / ".env"
DEFAULT_CHUNKS_PATH = (
    REPO_ROOT
    / "knowledge"
    / "processor"
    / "import_process"
    / "import_temp_Dir"
    / "hak180使用说明书"
    / "hybrid_auto"
    / "chunks.json"
)
FILE_TITLE = "hak180使用说明书"
ITEM_NAME = "HAK 180"


def _load_chunks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"stage-0 chunks must be a non-empty list: {path}")

    chunks: list[dict[str, Any]] = []
    for index, raw_chunk in enumerate(payload):
        if not isinstance(raw_chunk, dict) or not raw_chunk.get("content"):
            raise ValueError(f"invalid chunk at index {index}: {path}")
        chunk = dict(raw_chunk)
        chunk["file_title"] = FILE_TITLE
        chunk["item_name"] = ITEM_NAME
        chunks.append(chunk)
    return chunks


def _document_exists(client: Any, collection_name: str) -> bool:
    if not client.has_collection(collection_name=collection_name):
        return False
    rows = client.query(
        collection_name=collection_name,
        filter=f"file_title == {json.dumps(FILE_TITLE, ensure_ascii=False)}",
        output_fields=["chunk_id"],
        limit=1,
    )
    return bool(rows)


def _item_exists(client: Any, collection_name: str) -> bool:
    if not client.has_collection(collection_name=collection_name):
        return False
    rows = client.query(
        collection_name=collection_name,
        filter=f"item_name == {json.dumps(ITEM_NAME, ensure_ascii=False)}",
        output_fields=["item_name"],
        limit=1,
    )
    return bool(rows)


def seed(*, chunks_path: Path, with_kg: bool = False) -> dict[str, Any]:
    load_dotenv(dotenv_path=ENV_PATH, override=False)

    # Import after loading the explicit environment file. Several legacy
    # modules read their configuration at import time.
    from knowledge.processor.import_process.config import get_config
    from knowledge.processor.import_process.nodes.bge_embedding_chunks_node import (
        BgeEmbeddingNode,
    )
    from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode
    from knowledge.processor.import_process.nodes.item_name_recognition_load import (
        ItemNameRecognitionNode,
    )
    from knowledge.processor.import_process.nodes.kg_graph_node import KnowledgeGraphNode
    from knowledge.utils.milvus_utils import get_milvus_client

    config = get_config()
    client = get_milvus_client()
    summary: dict[str, Any] = {
        "file_title": FILE_TITLE,
        "item_name": ITEM_NAME,
        "chunks_source": str(chunks_path),
        "chunks_inserted": 0,
        "item_inserted": False,
        "kg_requested": with_kg,
        "kg_completed": False,
    }

    state: dict[str, Any]
    if _document_exists(client, config.chunks_collection):
        summary["chunks_status"] = "already_present"
        rows = client.query(
            collection_name=config.chunks_collection,
            filter=f"file_title == {json.dumps(FILE_TITLE, ensure_ascii=False)}",
            output_fields=[
                "chunk_id",
                "content",
                "title",
                "parent_title",
                "part",
                "file_title",
                "item_name",
            ],
            limit=1000,
        )
        state = {"chunks": list(rows), "file_title": FILE_TITLE, "item_name": ITEM_NAME}
    else:
        chunks = _load_chunks(chunks_path)
        state = {"chunks": chunks, "file_title": FILE_TITLE, "item_name": ITEM_NAME}
        BgeEmbeddingNode().process(state)
        ImportMilvusNode().process(state)
        summary["chunks_inserted"] = len(state["chunks"])
        summary["chunks_status"] = "inserted"

    if _item_exists(client, config.item_name_collection):
        summary["item_status"] = "already_present"
    else:
        recognizer = ItemNameRecognitionNode()
        dense_vector, sparse_vector = recognizer._generate_vectors(ITEM_NAME)
        recognizer._save_to_milvus(
            state=state,
            file_title=FILE_TITLE,
            item_name=ITEM_NAME,
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            config=config,
        )
        summary["item_inserted"] = True
        summary["item_status"] = "inserted"

    if with_kg:
        KnowledgeGraphNode().process(state)
        summary["kg_completed"] = True

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    parser.add_argument(
        "--with-kg",
        action="store_true",
        help="also rebuild the HAK 180 knowledge graph (uses many LLM calls)",
    )
    args = parser.parse_args()
    result = seed(chunks_path=args.chunks.resolve(), with_kg=args.with_kg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
