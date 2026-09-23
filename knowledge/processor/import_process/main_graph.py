import json
import os
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from knowledge.processor.import_process.base import setup_logging
from knowledge.processor.import_process.nodes.bge_embedding_chunks_node import BgeEmbeddingNode
from knowledge.processor.import_process.nodes.document_spliter_node import DocumentSplitNode
from knowledge.processor.import_process.nodes.document_enrich_node import DocumentEnrichNode
from knowledge.processor.import_process.nodes.document_normalize_node import DocumentNormalizeNode
from knowledge.processor.import_process.nodes.document_parse_node import DocumentParseNode
from knowledge.processor.import_process.nodes.entry_node import EntryNode
from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode
from knowledge.processor.import_process.nodes.item_name_recognition_load import ItemNameRecognitionNode
from knowledge.processor.import_process.nodes.kg_graph_node import KnowledgeGraphNode
from knowledge.processor.import_process.nodes.pdf_to_md_node import PdfToMdNode
from knowledge.processor.import_process.state import ImportGraphState, create_default_state


TRUTHY_VALUES = {"1", "true", "yes", "on"}
FALSEY_VALUES = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUTHY_VALUES:
        return True
    if normalized in FALSEY_VALUES:
        return False
    return default


def _preview_text(value: Any, limit: int = 80) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _summarize_chunks(chunks: Any) -> dict[str, Any]:
    if not isinstance(chunks, list):
        return {"type": type(chunks).__name__}

    vector_count = 0
    chunk_id_count = 0
    sample_titles = []
    sample_ids = []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if "dense_vector" in chunk or "sparse_vector" in chunk:
            vector_count += 1
        if chunk.get("chunk_id"):
            chunk_id_count += 1
            if len(sample_ids) < 3:
                sample_ids.append(str(chunk.get("chunk_id")))
        if len(sample_titles) < 3:
            sample_titles.append(_preview_text(chunk.get("title") or chunk.get("content")))

    return {
        "count": len(chunks),
        "with_vectors": vector_count,
        "with_chunk_id": chunk_id_count,
        "sample_titles": sample_titles,
        "sample_chunk_ids": sample_ids,
    }


def summarize_state(state: Any) -> dict[str, Any]:
    """Return a small, console-safe state summary without vector payloads."""
    if not isinstance(state, dict):
        return {"type": type(state).__name__}

    summary: dict[str, Any] = {}
    for key in (
        "task_id",
        "is_pdf_read_enabled",
        "is_md_read_enabled",
        "import_file_path",
        "file_dir",
        "pdf_path",
        "md_path",
        "file_title",
        "item_name",
    ):
        if key in state:
            summary[key] = _preview_text(state.get(key))

    if "md_content" in state:
        summary["md_content_chars"] = len(state.get("md_content") or "")
    if "image_contexts" in state:
        image_contexts = state.get("image_contexts")
        summary["image_contexts_count"] = len(image_contexts) if isinstance(image_contexts, list) else 0
    if "image_summaries" in state:
        image_summaries = state.get("image_summaries")
        summary["image_summaries_count"] = len(image_summaries) if isinstance(image_summaries, dict) else 0
    if "chunks" in state:
        summary["chunks"] = _summarize_chunks(state.get("chunks"))
    if "node_timings" in state:
        node_timings = state.get("node_timings")
        if isinstance(node_timings, dict):
            timing_summary = {}
            for key, value in node_timings.items():
                try:
                    timing_summary[str(key)] = round(float(value), 3)
                except (TypeError, ValueError):
                    continue
            summary["node_timings_sec"] = timing_summary
            summary["total_node_time_sec"] = round(sum(timing_summary.values()), 3)

    return summary


def _should_dump_full_state() -> bool:
    return _env_bool("IMPORT_GRAPH_DUMP_FULL_STATE", False)


def import_router(state: ImportGraphState):
    if state.get("is_md_read_enabled"):
        return "document_parse_node"
    if state.get("is_pdf_read_enabled"):
        return "pdf_to_md_node"
    return END


def knowledge_graph_router(state: ImportGraphState):
    if _env_bool("IMPORT_GRAPH_ENABLE_KG", True):
        return "knowledge_graph_node"
    return END


def create_import_graph():
    graph_pipeline = StateGraph(ImportGraphState)  # type: ignore[arg-type]

    nodes = {
        "entry_node": EntryNode(),
        "pdf_to_md_node": PdfToMdNode(),
        "document_parse_node": DocumentParseNode(),
        "document_normalize_node": DocumentNormalizeNode(),
        "document_split_node": DocumentSplitNode(),
        "document_enrich_node": DocumentEnrichNode(),
        "item_name_recognition_node": ItemNameRecognitionNode(),
        "bge_embedding_node": BgeEmbeddingNode(),
        "import_milvus_node": ImportMilvusNode(),
        "knowledge_graph_node": KnowledgeGraphNode(),
    }
    for name, node in nodes.items():
        graph_pipeline.add_node(name, node)

    graph_pipeline.add_edge(START, "entry_node")
    graph_pipeline.add_conditional_edges(
        "entry_node",
        import_router,
        {
            "document_parse_node": "document_parse_node",
            "pdf_to_md_node": "pdf_to_md_node",
            END: END,
        },
    )
    graph_pipeline.add_edge("pdf_to_md_node", "document_parse_node")
    graph_pipeline.add_edge("document_parse_node", "document_normalize_node")
    graph_pipeline.add_edge("document_normalize_node", "document_split_node")
    graph_pipeline.add_edge("document_split_node", "document_enrich_node")
    graph_pipeline.add_edge("document_enrich_node", "item_name_recognition_node")
    graph_pipeline.add_edge("item_name_recognition_node", "bge_embedding_node")
    graph_pipeline.add_edge("bge_embedding_node", "import_milvus_node")
    graph_pipeline.add_conditional_edges(
        "import_milvus_node",
        knowledge_graph_router,
        {
            "knowledge_graph_node": "knowledge_graph_node",
            END: END,
        },
    )
    graph_pipeline.add_edge("knowledge_graph_node", END)

    return graph_pipeline.compile()


graph_app = create_import_graph()


def run_import_graph(
    import_file_path: str,
    file_dir: str,
    task_id: str = "",
    logical_document_key: str = "",
    previous_ir_path: str = "",
):
    init_state = create_default_state(
        task_id=task_id,
        import_file_path=import_file_path,
        file_dir=file_dir,
        logical_document_key=logical_document_key,
        previous_ir_path=previous_ir_path,
    )
    final_state = None

    for event in graph_app.stream(init_state):
        for node_name, node_state in event.items():
            print(
                f"{node_name} summary: "
                f"{json.dumps(summarize_state(node_state), ensure_ascii=False)}"
            )
            if _should_dump_full_state():
                print(json.dumps(node_state, indent=2, ensure_ascii=False))
            final_state = node_state

    return final_state


def main() -> None:
    setup_logging()

    import_file_path = os.getenv("IMPORT_GRAPH_TEST_FILE", "").strip()
    if not import_file_path:
        raise SystemExit("Set IMPORT_GRAPH_TEST_FILE to the document you intend to import")
    file_dir = os.getenv("IMPORT_GRAPH_TEST_DIR", str(Path(import_file_path).parent))

    final_state = run_import_graph(import_file_path=import_file_path, file_dir=file_dir)
    print(json.dumps(summarize_state(final_state), indent=4, ensure_ascii=False))

    print("-" * 50)
    print("Graph structure")
    graph_app.get_graph().print_ascii()


if __name__ == "__main__":
    main()
