"""Helpers for turning internal retrieval state into public API data."""

from __future__ import annotations

from typing import Any, Dict, List


def build_source_references(raw_docs: Any) -> List[Dict[str, Any]]:
    """Return compact, JSON-safe evidence records without prompt/vector payloads."""

    sources: List[Dict[str, Any]] = []
    if not isinstance(raw_docs, list):
        return sources

    for raw_doc in raw_docs:
        if not isinstance(raw_doc, dict):
            continue
        content = str(raw_doc.get("content") or "").strip()
        score = raw_doc.get("score")
        try:
            score = round(float(score), 4) if score is not None else None
        except (TypeError, ValueError):
            score = None

        sources.append(
            {
                "index": len(sources) + 1,
                "source": str(raw_doc.get("source") or "local"),
                "title": str(raw_doc.get("title") or ""),
                "file_title": str(raw_doc.get("file_title") or ""),
                "parent_title": str(raw_doc.get("parent_title") or ""),
                "chunk_id": str(raw_doc.get("chunk_id") or ""),
                "url": str(raw_doc.get("url") or ""),
                "score": score,
                "preview": content[:280] + ("…" if len(content) > 280 else ""),
            }
        )
    return sources


def build_query_diagnostics(
    task_id: str,
    state: Dict[str, Any],
    elapsed: float | None = None,
) -> Dict[str, Any]:
    """Build a compact retrieval trace suitable for API responses and history."""

    count_fields = {
        "embedding": "embedding_chunks",
        "hyde": "hyde_embedding_chunks",
        "knowledge_graph": "kg_chunks",
        "web": "web_search_docs",
        "rrf": "rrf_chunks",
        "rerank": "reranked_docs",
    }
    counts = {
        label: len(state.get(field) or [])
        for label, field in count_fields.items()
    }
    timings = {
        str(name): round(float(seconds), 3)
        for name, seconds in (state.get("node_timings") or {}).items()
        if isinstance(seconds, (int, float))
    }
    return {
        "trace_id": task_id,
        "retrieval_counts": counts,
        "node_timings": timings,
        "total_time": round(elapsed, 3) if elapsed is not None else None,
    }
