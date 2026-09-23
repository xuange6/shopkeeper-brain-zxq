"""Helpers for turning internal retrieval state into public API data."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from urllib.parse import urlsplit


_TRACE_FIELDS = {
    "direct": "embedding_chunks",
    "hyde": "hyde_embedding_chunks",
    "knowledge_graph": "kg_chunks",
    "web": "web_search_docs",
    "rrf": "rrf_chunks",
    "rerank": "reranked_docs",
}


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

        citation = _json_object(raw_doc.get("citation"))
        stable_id = raw_doc.get("stable_id") or raw_doc.get("chunk_id")
        pages = citation.get("page_numbers") or _json_list(raw_doc.get("page_numbers"))
        block_ids = citation.get("block_ids") or _json_list(raw_doc.get("block_ids"))
        block_lineage_ids = citation.get("block_lineage_ids") or _json_list(raw_doc.get("block_lineage_ids"))
        page_uids = citation.get("page_uids") or _json_list(raw_doc.get("page_uids"))
        title_path = citation.get("title_path") or _json_list(raw_doc.get("title_path"))
        sources.append(
            {
                "index": len(sources) + 1,
                "source": str(raw_doc.get("source") or "local"),
                "title": str(raw_doc.get("title") or ""),
                "file_title": str(raw_doc.get("file_title") or ""),
                "parent_title": str(raw_doc.get("parent_title") or ""),
                "chunk_id": str(stable_id or ""),
                "document_id": str(raw_doc.get("document_id") or citation.get("document_id") or ""),
                "revision_id": str(raw_doc.get("revision_id") or citation.get("revision_id") or ""),
                "source_uri": str(raw_doc.get("source_uri") or citation.get("source_uri") or ""),
                "section_id": str(raw_doc.get("section_id") or citation.get("section_id") or ""),
                "page_numbers": pages,
                "page_uids": page_uids,
                "block_ids": block_ids,
                "block_lineage_ids": block_lineage_ids,
                "title_path": title_path,
                "locations": list(citation.get("locations") or []),
                "image_urls": source_image_urls(raw_doc),
                "url": str(raw_doc.get("url") or ""),
                "score": score,
                "preview": content[:280] + ("…" if len(content) > 280 else ""),
            }
        )
    return sources


def build_retrieval_trace(state: Dict[str, Any]) -> Dict[str, Any]:
    """Build an evaluation-safe trace of candidates at each retrieval boundary.

    Full chunk text, vectors, prompts, HyDE text, and credentials are deliberately
    excluded. The retained composite identity is sufficient for golden-label
    matching and for locating where a relevant chunk was lost.
    """

    stages: Dict[str, List[Dict[str, Any]]] = {}
    for stage, field in _TRACE_FIELDS.items():
        raw_docs = state.get(field) or []
        refs: List[Dict[str, Any]] = []
        for raw_doc in raw_docs if isinstance(raw_docs, list) else []:
            if not isinstance(raw_doc, dict):
                continue
            entity = raw_doc.get("entity")
            doc = entity if isinstance(entity, dict) else raw_doc
            score = (
                raw_doc.get("distance")
                if raw_doc.get("distance") is not None
                else doc.get("score", doc.get("rrf_score", doc.get("retrieval_score")))
            )
            try:
                score = round(float(score), 6) if score is not None else None
            except (TypeError, ValueError):
                score = None
            refs.append(
                {
                    "rank": len(refs) + 1,
                    "source": str(doc.get("source") or ("web" if stage == "web" else "local")),
                    "chunk_id": str(doc.get("stable_id") or doc.get("chunk_id") or doc.get("id") or ""),
                    "document_id": str(doc.get("document_id") or ""),
                    "revision_id": str(doc.get("revision_id") or ""),
                    "section_id": str(doc.get("section_id") or ""),
                    "page_numbers": _json_list(doc.get("page_numbers")),
                    "block_ids": _json_list(doc.get("block_ids")),
                    "image_urls": source_image_urls(doc),
                    "file_title": str(doc.get("file_title") or ""),
                    "title": str(doc.get("title") or ""),
                    "parent_title": str(doc.get("parent_title") or ""),
                    "url": str(doc.get("url") or ""),
                    "score": score,
                    "rrf_sources": list(doc.get("rrf_sources") or []),
                }
            )
        stages[stage] = refs

    return {
        "stages": stages,
        "status": dict(state.get("retrieval_status") or {}),
    }


def build_query_diagnostics(
    task_id: str,
    state: Dict[str, Any],
    elapsed: float | None = None,
    include_retrieval_trace: bool = False,
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
    diagnostics = {
        "trace_id": task_id,
        "retrieval_counts": counts,
        "node_timings": timings,
        "total_time": round(elapsed, 3) if elapsed is not None else None,
        "model_usage": dict(state.get("model_usage") or {}),
    }
    if include_retrieval_trace:
        diagnostics["retrieval_trace"] = build_retrieval_trace(state)
    return diagnostics


def source_image_urls(doc: Dict[str, Any]) -> List[str]:
    """Expose only asset references, never arbitrary links found in prose.

    IR citations carry exact block-to-asset associations. Legacy rows have no
    such field, so their Markdown image syntax is the compatibility boundary.
    This does not fetch remote URLs or infer images from source/page URLs.
    """
    citation = _json_object(doc.get("citation"))
    if "images" in citation:
        values = [item.get("uri") for item in _json_list(citation.get("images")) if isinstance(item, dict)]
    else:
        values = list(_json_list(doc.get("image_urls")))
        values.extend(re.findall(
            r"!\[[^\]]*\]\(\s*<?(https?://[^\s<>\)]+)>?(?:\s+[^\)]*)?\)",
            str(doc.get("content") or ""),
        ))
    result: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password
                and not any(character.isspace() for character in value)
            )
        except ValueError:
            valid = False
        if valid and value not in result:
            result.append(value)
    return result


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
