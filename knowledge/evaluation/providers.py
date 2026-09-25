"""Evaluation providers for frozen replay, local service, and HTTP runs."""

from __future__ import annotations

import json
import hashlib
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Protocol
from uuid import uuid4

from knowledge.evaluation.usage import attach_turn_usage


class EvaluationProvider(Protocol):
    name: str

    def run(self, case: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]: ...


def contract_query_config():
    """Fixed offline inputs: developer .env values never affect CI contracts."""
    from knowledge.processor.query_process.config import QueryConfig

    return QueryConfig(
        max_context_chars=12000, rerank_max_top_k=15, rerank_min_top_k=6,
        rerank_gap_ratio=0.25, rerank_gap_abs=0.5, refusal_min_score=0.3,
        retrieval_policy_version="stage2-industrial-rag-v2",
        security_policy_version="intent-policy-v2",
        acl_policy_version="retrieval-acl-v1",
        security_max_input_chars=4000, security_inspect_encodings=True,
        security_max_decoded_payloads=4, security_fuzzy_threshold=0.82,
        security_context_guard_enabled=True, security_output_guard_enabled=True,
        rerank_calibration_center=-1.8, rerank_calibration_scale=0.9,
        confidence_relevance_weight=0.35, confidence_margin_weight=0.15,
        confidence_authority_weight=0.15, confidence_structure_weight=0.15,
        confidence_coverage_weight=0.20, refusal_min_confidence=0.46,
        answer_max_evidence=6, citation_min_overlap=0.08,
        require_claim_citations=True,
        rrf_k=60, rrf_kg_weight=0.7, rrf_direct_weight=1.0,
        rrf_hyde_weight=0.9, rrf_max_results=20,
        embedding_search_limit=10, hyde_search_limit=10,
        enable_hyde=True, enable_knowledge_graph=True, enable_web=True,
        web_fallback_min_local_candidates=1,
        web_fallback_min_local_coverage=0.25, web_search_limit=3,
        direct_timeout_ms=8000, hyde_timeout_ms=15000,
        kg_timeout_ms=12000, web_timeout_ms=12000,
        local_authority=1.0, web_default_authority=0.45,
        web_official_authority=0.9, web_official_domains="",
        web_freshness_require_official=True,
        web_official_query_expansion=True,
        web_official_query_domain_limit=3,
        request_max_model_calls=4, request_max_tokens=12000,
        request_max_cost=0.05, evaluation_max_batch_cost=0.5,
        kg_entity_align_min_score=None, kg_graph_version="offline-contract",
        openai_api_base="", openai_api_key="",
        default_model="offline-contract", item_model="offline-contract",
        milvus_url="", chunks_collection="offline-contract",
        item_name_collection="offline-contract", entity_name_collection="offline-contract",
        neo4j_uri="", neo4j_username="", neo4j_password="", neo4j_database="offline-contract",
        mcp_dashscope_base_url="",
    )


class ReplayProvider:
    name = "replay"
    evaluation_scope = "recorded_output"

    def __init__(self, snapshot_path: Path):
        self.snapshot_path = snapshot_path
        snapshot_rows = _read_jsonl(snapshot_path)
        self._results = {item["case_id"]: item for item in snapshot_rows}
        if len(self._results) != len(snapshot_rows):
            raise ValueError("replay snapshot contains duplicate case IDs")
        self.snapshot_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        source_scopes = {
            str(item.get("evaluation_scope"))
            for item in snapshot_rows
            if item.get("evaluation_scope")
        }
        self.source_evaluation_scope = (
            next(iter(source_scopes)) if len(source_scopes) == 1 else "unknown"
        )

    def run(self, case: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]:
        result = dict(self._results.get(case["id"]) or {})
        if not result:
            return {
                "case_id": case["id"],
                "response": {},
                "latency_ms": 0.0,
                "attempt": attempt,
                "error": f"case missing from snapshot: {case['id']}",
            }
        result["attempt"] = attempt
        result.setdefault("evaluation_scope", self.evaluation_scope)
        return result


class ContractProvider:
    """Exercise production fusion/refusal/source contracts with frozen inputs.

    This provider is intentionally offline. It replaces infrastructure and model
    calls, not the RRF, grounding/refusal, citation-source, or image extraction
    code whose regressions should block CI.
    """

    name = "contract"
    evaluation_scope = "contract"

    def run(self, case: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]:
        from knowledge.processor.query_process.config import QueryConfig
        from knowledge.processor.query_process.nodes.answer_output import AnswerOutputNode
        from knowledge.processor.query_process.nodes.intent_policy import IntentPolicyNode
        from knowledge.processor.query_process.nodes.rrf import RrfNode
        from knowledge.processor.query_process.state import create_default_state
        from knowledge.utils.task_utils import clear_task, create_task

        started = time.perf_counter()
        task_id = f"contract-{case['id']}-{uuid4().hex[:8]}"
        create_task(task_id)
        try:
            case_input = case.get("input") or {}
            expected = case.get("expected") or {}
            item_names = list(case_input.get("item_names") or [])
            state = create_default_state(
                task_id=task_id,
                session_id="",
                original_query=str(
                    (case_input.get("turns") or [case_input.get("query", "")])[-1]
                ),
                rewritten_query=str(
                    (case_input.get("turns") or [case_input.get("query", "")])[-1]
                ),
                item_names=item_names,
                is_stream=False,
            )

            state = IntentPolicyNode(config=contract_query_config()).process(state)

            if state.get("answer"):
                pass
            elif not item_names:
                state["answer"] = (
                    "抱歉，我无法识别您询问的具体产品名称，"
                    "请提供更准确的产品名称或型号。"
                )
            elif expected.get("behavior") == "answer":
                docs = _contract_docs(expected)
                state["reranked_docs"] = RrfNode._rrf_merge(
                    {"direct": (docs, 1.0), "hyde": (list(reversed(docs)), 1.0)},
                    smoothing_factor=60,
                    top_n=20,
                )
                state["answer"] = _contract_answer(expected)

            node = AnswerOutputNode(config=contract_query_config())
            final_state = node.process(state)
            response = {
                "answer": final_state.get("answer", ""),
                "sources": final_state.get("sources", []),
                "image_urls": final_state.get("image_urls", []),
                "diagnostics": {
                    "trace_id": task_id,
                    "retrieval_counts": {
                        "rrf": len(final_state.get("reranked_docs") or []),
                        "rerank": len(final_state.get("reranked_docs") or []),
                    },
                    "node_timings": {},
                    "total_time": round(time.perf_counter() - started, 6),
                    "model_usage": {
                        "call_count": 0,
                        "failed_call_count": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "estimated_token_count": False,
                        "estimated_cost_usd": 0.0,
                        "latency_ms": 0.0,
                        "models": [],
                    },
                },
            }
            return {
                "case_id": case["id"],
                "response": response,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "attempt": attempt,
                "error": "",
                "evaluation_scope": self.evaluation_scope,
            }
        except Exception as exc:
            return {
                "case_id": case["id"],
                "response": {},
                "latency_ms": (time.perf_counter() - started) * 1000,
                "attempt": attempt,
                "error": str(exc),
            }
        finally:
            clear_task(task_id)


class ServiceProvider:
    """Run the real in-process query graph with configured integrations."""

    name = "service"
    evaluation_scope = "full_pipeline"

    def __init__(self, strict_preflight: bool = True):
        self.strict_preflight = strict_preflight
        self._preflight: Dict[str, Any] | None = None

    def preflight(self) -> Dict[str, Any]:
        if self._preflight is None:
            from knowledge.evaluation.preflight import check_service_runtime

            self._preflight = check_service_runtime()
        return self._preflight

    def run(self, case: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]:
        from knowledge.service.query_service import QueryService
        from knowledge.evaluation.metrics import _pipeline_complete

        preflight = self.preflight()
        if self.strict_preflight and not preflight["passed"]:
            return {
                "case_id": case["id"],
                "response": {"diagnostics": {"runtime_preflight": preflight}},
                "latency_ms": 0.0,
                "attempt": attempt,
                "error": "runtime preflight failed: " + "; ".join(preflight["failures"]),
                "evaluation_scope": self.evaluation_scope,
            }

        service = QueryService()
        case_input = case.get("input") or {}
        session_id = f"eval-{case['id']}-{uuid4().hex[:10]}"
        queries = list(case_input.get("turns") or [case_input.get("query", "")])
        response: Dict[str, Any] = {}
        turn_usages: list[dict | None] = []
        turn_health: list[bool] = []
        started = time.perf_counter()
        try:
            for query in queries:
                task_id = service.generate_task_id()
                service.submit_query(task_id, is_stream=False)
                service.run_query_graph(
                    task_id=task_id,
                    session_id=session_id,
                    user_query=str(query),
                    is_stream=False,
                    item_names=list(case_input.get("item_names") or []),
                    include_evaluation_trace=True,
                )
                response = {
                    "answer": service.get_answer(task_id),
                    "sources": service.get_sources(task_id),
                    "image_urls": service.get_image_urls(task_id),
                    "diagnostics": service.get_diagnostics(task_id),
                }
                turn_usages.append((response.get("diagnostics") or {}).get("model_usage"))
                turn_health.append(bool(_pipeline_complete(
                    case, response, (response.get("diagnostics") or {}).get("retrieval_trace", {})
                )))
                error = service.get_error(task_id)
                if error:
                    raise RuntimeError(error)
            return {
                "case_id": case["id"],
                "response": attach_turn_usage(response, turn_usages, len(queries), turn_health),
                "latency_ms": (time.perf_counter() - started) * 1000,
                "attempt": attempt,
                "error": "",
                "evaluation_scope": self.evaluation_scope,
            }
        except Exception as exc:
            return {
                "case_id": case["id"],
                "response": attach_turn_usage(response, turn_usages, len(queries), turn_health),
                "latency_ms": (time.perf_counter() - started) * 1000,
                "attempt": attempt,
                "error": str(exc),
                "evaluation_scope": self.evaluation_scope,
            }


class HttpProvider:
    name = "http"
    evaluation_scope = "answer_only"

    def __init__(self, base_url: str, timeout_seconds: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def run(self, case: Dict[str, Any], attempt: int = 1) -> Dict[str, Any]:
        case_input = case.get("input") or {}
        session_id = f"eval-{case['id']}-{uuid4().hex[:10]}"
        queries = list(case_input.get("turns") or [case_input.get("query", "")])
        response: Dict[str, Any] = {}
        turn_usages: list[dict | None] = []
        started = time.perf_counter()
        error = ""
        try:
            for query in queries:
                payload = json.dumps(
                    {
                        "query": query,
                        "session_id": session_id,
                        "item_names": case_input.get("item_names") or [],
                        "is_stream": False,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"{self.base_url}/query",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as http_response:
                    response = json.loads(http_response.read().decode("utf-8"))
                turn_usages.append((response.get("diagnostics") or {}).get("model_usage"))
                if response.get("error"):
                    raise RuntimeError(str(response["error"]))
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
            error = str(exc)
        return {
            "case_id": case["id"],
            "response": attach_turn_usage(response, turn_usages, len(queries)),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "attempt": attempt,
            "error": error,
            "evaluation_scope": self.evaluation_scope,
        }


def _read_jsonl(path: Path) -> list[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    items = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        items.append(item)
    return items


def _contract_docs(expected: Dict[str, Any]) -> list[Dict[str, Any]]:
    facts = [item for item in expected.get("facts") or [] if isinstance(item, dict)]
    fact_text = "；".join(
        str((fact.get("any") or [""])[0]) for fact in facts if fact.get("any")
    )
    docs = []
    for index, source in enumerate(expected.get("relevant_sources") or [], 1):
        doc = dict(source)
        doc.setdefault("source", "local")
        doc.setdefault("chunk_id", f"contract-{index}")
        doc["content"] = fact_text or "评测契约证据"
        if expected.get("requires_image"):
            doc["image_urls"] = ["https://example.invalid/evaluation/control-panel.png"]
            doc["content"] += "\n![control panel](https://example.invalid/evaluation/control-panel.png)"
        docs.append(doc)
    return docs


def _contract_answer(expected: Dict[str, Any]) -> str:
    facts = [item for item in expected.get("facts") or [] if isinstance(item, dict)]
    values = [
        str((fact.get("any") or [""])[0]).strip()
        for fact in facts
        if (fact.get("any") or [""])[0]
    ]
    answer = "；".join(
        f"{value}。[1]" if expected.get("requires_citation") else value
        for value in values
    )
    if not answer and expected.get("behavior") == "answer":
        answer = "请以当前可核验的公开来源为准"
    if expected.get("requires_image"):
        answer += "\n【图片】\nhttps://example.invalid/evaluation/control-panel.png"
    return answer


def write_jsonl(path: Path, items: list[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
