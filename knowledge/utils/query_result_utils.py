"""Helpers for turning internal retrieval state into public API data."""

from __future__ import annotations

import json
import math
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
                "rerank_raw_score": _json_number(raw_doc.get("rerank_raw_score")),
                "calibrated_relevance": _json_number(raw_doc.get("calibrated_relevance")),
                "ranking_score": _json_number(raw_doc.get("ranking_score")),
                "answer_confidence": _json_number(raw_doc.get("answer_confidence")),
                "authority": _json_number(raw_doc.get("authority")),
                "freshness": _json_number(raw_doc.get("freshness")),
                "source_type": str(raw_doc.get("source_type") or ""),
                "domain": str(raw_doc.get("domain") or ""),
                "retrieved_at": str(raw_doc.get("retrieved_at") or ""),
                "retrieved_date": str(raw_doc.get("retrieved_date") or ""),
                "publication_date": str(raw_doc.get("publication_date") or ""),
                "supported_claims": [
                    str(claim)[:500]
                    for claim in _json_list(raw_doc.get("supported_claims"))
                    if str(claim).strip()
                ],
                "evidence_group_id": str(raw_doc.get("evidence_group_id") or ""),
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
                    "rerank_raw_score": _json_number(doc.get("rerank_raw_score")),
                    "calibrated_relevance": _json_number(doc.get("calibrated_relevance")),
                    "ranking_score": _json_number(doc.get("ranking_score")),
                    "authority": _json_number(doc.get("authority")),
                    "freshness": _json_number(doc.get("freshness")),
                    "structure_match": _json_number(doc.get("structure_match")),
                    "evidence_coverage": _json_number(doc.get("evidence_coverage")),
                    "source_type": str(doc.get("source_type") or ""),
                    "domain": str(doc.get("domain") or ""),
                    "retrieved_at": str(doc.get("retrieved_at") or ""),
                    "retrieved_date": str(doc.get("retrieved_date") or ""),
                    "publication_date": str(doc.get("publication_date") or ""),
                    "evidence_group_id": str(doc.get("evidence_group_id") or ""),
                    "rrf_sources": list(doc.get("rrf_sources") or []),
                }
            )
        stages[stage] = refs

    return {
        "stages": stages,
        "status": dict(state.get("retrieval_status") or {}),
        "plan": dict(state.get("retrieval_plan") or {}),
        "ranking_events": list(state.get("retrieval_trace_events") or []),
        "evidence_decision": dict(state.get("evidence_decision") or {}),
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
        "policy_decision": {
            key: value
            for key, value in (state.get("policy_decision") or {}).items()
            if key != "sanitized_query"
        },
        "context_security": dict(state.get("context_security") or {}),
        "output_security": dict(state.get("output_security") or {}),
        "access_control": dict(state.get("access_control") or {}),
        "web_search": dict(state.get("web_search_diagnostics") or {}),
        "retrieval_plan": dict(state.get("retrieval_plan") or {}),
        "evidence_decision": dict(state.get("evidence_decision") or {}),
        "constraint_completion": {
            key: value
            for key, value in (state.get("constraint_completion") or {}).items()
            if key != "appended_claims"
        },
        "citation_verification": {
            key: value
            for key, value in (state.get("citation_verification") or {}).items()
            if key != "claims"
        },
    }
    model_usage = diagnostics["model_usage"]
    by_operation = model_usage.get("by_operation") if isinstance(model_usage, dict) else {}
    diagnostics["resource_breakdown"] = {
        "query_rewrite": dict((by_operation or {}).get("query_rewrite") or {}),
        "direct_retrieval": {"latency_ms": round(timings.get("search_embedding", 0.0) * 1000, 3)},
        "hyde": {
            **dict((by_operation or {}).get("hyde") or {}),
            "stage_latency_ms": round(timings.get("search_embedding_hyde", 0.0) * 1000, 3),
        },
        "knowledge_graph": {
            **dict((by_operation or {}).get("kg_entity") or {}),
            "stage_latency_ms": round(timings.get("query_kg", 0.0) * 1000, 3),
        },
        "web": {"latency_ms": round(timings.get("web_search_mcp", 0.0) * 1000, 3)},
        "rerank": {"latency_ms": round(timings.get("rerank_node", 0.0) * 1000, 3)},
        "answer": dict((by_operation or {}).get("answer") or {}),
    }
    if include_retrieval_trace:
        # Evaluation/debug traces retain the per-claim binding decisions;
        # ordinary API/history diagnostics stay compact and do not expose the
        # generated draft.  This makes post-generation citation pruning
        # observable without changing the public response contract.
        diagnostics["citation_verification"]["claims"] = list(
            (state.get("citation_verification") or {}).get("claims") or []
        )
        diagnostics["constraint_completion"]["appended_claims"] = list(
            (state.get("constraint_completion") or {}).get("appended_claims") or []
        )
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


def _json_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
        return round(number, 6) if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None
