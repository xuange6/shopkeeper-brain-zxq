"""Main LangGraph definition for the query workflow."""

from __future__ import annotations

from typing import Any
import json

from langgraph.graph import END, START, StateGraph

from knowledge.processor.query_process.base import setup_logging
from knowledge.processor.query_process.nodes import (
    AnswerOutputNode,
    ItemNameConfirmNode,
    QueryKgNode,
    RerankNode,
    RrfNode,
    SearchEmbeddingHydeNode,
    SearchEmbeddingNode,
    WebSearchMcpNode,
)
from knowledge.processor.query_process.state import QueryGraphState, create_default_state


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


if load_dotenv:
    load_dotenv()


def route_after_item_confirm(state: QueryGraphState) -> str:
    if state.get("answer"):
        return "answer_output"
    return "multi_search"


def create_query_graph():
    workflow = StateGraph(QueryGraphState)  # type: ignore[arg-type]

    nodes = {
        "item_name_confirm": ItemNameConfirmNode(),
        "multi_search": lambda state: state,
        "search_embedding": SearchEmbeddingNode(),
        "search_embedding_hyde": SearchEmbeddingHydeNode(),
        "query_kg": QueryKgNode(),
        "web_search_mcp": WebSearchMcpNode(),
        "join": lambda state: state,
        "rrf": RrfNode(),
        "rerank": RerankNode(),
        "answer_output": AnswerOutputNode(),
    }

    for name, node in nodes.items():
        workflow.add_node(name, node)

    workflow.add_edge(START, "item_name_confirm")
    workflow.add_conditional_edges(
        "item_name_confirm",
        route_after_item_confirm,
        {
            "multi_search": "multi_search",
            "answer_output": "answer_output",
        },
    )

    workflow.add_edge("multi_search", "search_embedding")
    workflow.add_edge("multi_search", "search_embedding_hyde")
    workflow.add_edge("multi_search", "query_kg")
    workflow.add_edge("multi_search", "web_search_mcp")

    workflow.add_edge("search_embedding", "join")
    workflow.add_edge("search_embedding_hyde", "join")
    workflow.add_edge("query_kg", "join")
    workflow.add_edge("web_search_mcp", "join")

    workflow.add_edge("join", "rrf")
    workflow.add_edge("rrf", "rerank")
    workflow.add_edge("rerank", "answer_output")
    workflow.add_edge("answer_output", END)

    return workflow.compile()


query_app = create_query_graph()


def summarize_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"type": type(state).__name__}

    keys = (
        "task_id",
        "session_id",
        "message_id",
        "original_query",
        "rewritten_query",
        "item_names",
        "answer",
    )
    summary = {key: state.get(key) for key in keys if key in state}
    for key in (
        "embedding_chunks",
        "hyde_embedding_chunks",
        "kg_chunks",
        "kg_triples",
        "kg_seed_nodes",
        "kg_triples_raw",
        "kg_alignments",
        "web_search_docs",
        "rrf_chunks",
        "reranked_docs",
        "history",
    ):
        value = state.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
    if isinstance(state.get("node_timings"), dict):
        summary["node_timings_sec"] = state.get("node_timings")
    return summary


def run_query(
    query: str,
    session_id: str = "",
    item_names: list | None = None,
    is_stream: bool = False,
    task_id: str = "",
) -> dict:
    initial_state = create_default_state(
        task_id=task_id,
        session_id=session_id or "default",
        original_query=query,
        item_names=item_names or [],
        is_stream=is_stream,
    )

    final_state = None
    for event in query_app.stream(initial_state):
        for node_name, node_state in event.items():
            print(
                f"{node_name} summary: "
                f"{json.dumps(summarize_state(node_state), ensure_ascii=False)}"
            )
            final_state = node_state

    return final_state or initial_state


def main() -> None:
    import sys

    setup_logging()
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "万用表怎么测电压？"
    result = run_query(query=query, session_id="test_001")
    print(json.dumps(summarize_state(result), ensure_ascii=False, indent=4))
    print("-" * 50)
    print("Graph structure")
    query_app.get_graph().print_ascii()


if __name__ == "__main__":
    main()
