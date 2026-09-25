"""Evaluation orchestration, reporting, and regression gate comparison."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

from knowledge.evaluation.metrics import (
    EVALUATOR_VERSION, _source_matches, aggregate_results, evaluate_case,
)
from knowledge.evaluation.providers import EvaluationProvider, write_jsonl
from knowledge.evaluation.usage import USAGE_ACCOUNTING_VERSION, combine_model_usage


REQUIRED_CATEGORIES = {
    "business_qa",
    "ambiguity",
    "no_answer",
    "multi_turn",
    "cross_document",
    "table",
    "image",
    "freshness",
    "permission",
    "prompt_injection",
}


def load_dataset(path: Path) -> list[Dict[str, Any]]:
    cases: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError(f"{path}:{line_number}: case id is required")
        case_id = str(item["id"])
        if case_id in seen:
            raise ValueError(f"{path}:{line_number}: duplicate case id {case_id}")
        if not isinstance(item.get("input"), dict) or not isinstance(
            item.get("expected"), dict
        ):
            raise ValueError(f"{path}:{line_number}: input and expected are required")
        seen.add(case_id)
        cases.append(item)
    covered = {category for case in cases for category in case.get("category") or []}
    missing = REQUIRED_CATEGORIES - covered
    if missing:
        raise ValueError(f"dataset misses required categories: {sorted(missing)}")
    return cases


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_sha256(path: Path) -> str:
    """Source fingerprints ignore checkout EOLs; dataset byte hashes do not."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_source_contract(path: Path | None, dataset_path: Path) -> tuple[dict, str]:
    """Load explicit evidence aliases tied to the exact unchanged dataset bytes."""
    if path is None:
        return {}, "none"
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != "1.0" or contract.get("dataset_sha256") != _sha256(dataset_path):
        raise ValueError("source contract schema/dataset SHA mismatch")
    cases = contract.get("cases")
    if not isinstance(cases, dict):
        raise ValueError("source contract cases must be an object")
    dataset_cases = {case["id"]: case for case in load_dataset(dataset_path)}
    for case_id, rules in cases.items():
        if case_id not in dataset_cases or not isinstance(rules, dict):
            raise ValueError(f"invalid source contract case: {case_id}")
        if set(rules) - {"source_aliases", "fact_source_aliases"}:
            raise ValueError(f"unknown source contract rule: {case_id}")
        for rule_name in ("source_aliases", "fact_source_aliases"):
            aliases = rules.get(rule_name, [])
            if not isinstance(aliases, list):
                raise ValueError("source aliases must be lists")
            for alias in aliases:
                actual = alias.get("actual") if isinstance(alias, dict) else None
                # Never approve a broad title-only or file-only equivalence.
                if not isinstance(actual, dict) or not _source_matches(actual, actual):
                    raise ValueError("alias actual selector is invalid")
                if not (actual.get("document_id") or actual.get("file_title")) or not any(
                    actual.get(field) for field in ("block_ids", "block_lineage_ids", "section_id", "chunk_id")
                ):
                    raise ValueError("alias must pin a document and a block/section/chunk")
                expected = dataset_cases[case_id].get("expected") or {}
                if rule_name == "source_aliases":
                    if alias.get("expected") not in (expected.get("relevant_sources") or []):
                        raise ValueError("alias expected selector is not present in dataset case")
                elif alias.get("expected_title") not in {
                    title for fact in expected.get("facts", []) for title in fact.get("source_titles", [])
                }:
                    raise ValueError("alias fact title is not present in dataset case")
    return cases, _sha256(path)


def _git(command: Sequence[str], cwd: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", *command], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def collect_metadata(
    root: Path,
    dataset_path: Path,
    provider_name: str,
    dataset_version: str,
    evaluation_scope: str,
) -> Dict[str, Any]:
    from knowledge.processor.query_process.config import QueryConfig

    from knowledge.evaluation.providers import contract_query_config

    is_contract = provider_name == "contract"
    config = contract_query_config() if is_contract else QueryConfig()
    prompt_path = root / "knowledge/processor/query_process/prompt.py"
    dirty = _git(["status", "--porcelain"], root)
    evaluator_files = {
        name: _source_sha256(root / "knowledge/evaluation" / name)
        for name in ("metrics.py", "runner.py", "providers.py", "usage.py")
    }
    query_files = {
        str(path.relative_to(root)).replace("\\", "/"): _source_sha256(path)
        for path in sorted((root / "knowledge/processor/query_process").rglob("*.py"))
    }
    for name in ("query_result_utils.py", "embedding_utils.py", "bge_rerank_util.py", "llm_utils.py"):
        query_files[f"knowledge/utils/{name}"] = _source_sha256(root / "knowledge/utils" / name)
    query_files["knowledge/service/query_service.py"] = _source_sha256(
        root / "knowledge/service/query_service.py"
    )
    query_files["knowledge/security/access_control.py"] = _source_sha256(
        root / "knowledge/security/access_control.py"
    )
    query_files["knowledge/schema/query_schema.py"] = _source_sha256(
        root / "knowledge/schema/query_schema.py"
    )
    excluded_config = {
        "openai_api_base", "openai_api_key", "default_model", "item_model",
        "milvus_url", "chunks_collection", "item_name_collection", "entity_name_collection",
        "neo4j_uri", "neo4j_username", "neo4j_password", "neo4j_database", "mcp_dashscope_base_url",
    }
    if is_contract:
        pricing = {}
        pricing_known = False
    else:
        try:
            from knowledge.observability.pricing import load_pricing, pricing_metadata

            pricing_config = load_pricing()
            pricing = pricing_metadata(pricing_config, model=config.default_model)
            configured_models = pricing_config.get("models") or {}
            pricing_known = bool(config.default_model) and all(
                model in configured_models
                for model in {config.default_model, config.item_model or config.default_model}
            )
        except Exception as exc:
            pricing = {"status": "unavailable", "reason": type(exc).__name__}
            pricing_known = False
    return {
        "evaluation_schema_version": "3.0",
        "evaluator_version": EVALUATOR_VERSION,
        "usage_accounting_version": USAGE_ACCOUNTING_VERSION,
        "usage_scope": "all_turns_all_attempts",
        "evaluator_fingerprint": _fingerprint(evaluator_files),
        "evaluator_modules": evaluator_files,
        "query_pipeline_sha256": _fingerprint(query_files),
        "dataset_version": dataset_version,
        "dataset_sha256": _sha256(dataset_path),
        "dataset_semantic_sha256": _fingerprint(load_dataset(dataset_path)),
        "provider": provider_name,
        "evaluation_scope": evaluation_scope,
        "git_commit": _git(["rev-parse", "HEAD"], root),
        "git_dirty": dirty not in {"", "unknown"},
        "prompt_sha256": "not-used-by-offline-contract" if is_contract else _source_sha256(prompt_path),
        "model": config.default_model or "not-configured",
        "item_model": config.item_model or "not-configured",
        "runtime_configuration_sha256": _fingerprint({
            "endpoints": {field: getattr(config, field) for field in (
                "openai_api_base", "milvus_url", "neo4j_uri", "neo4j_database", "mcp_dashscope_base_url",
            )},
            "model_runtime": {} if is_contract else {name: os.getenv(name, "") for name in (
                "MODEL", "ITEM_MODEL", "LLM_DEFAULT_MODEL", "LLM_DEFAULT_TEMPERATURE",
                "BGE_M3", "BGE_M3_PATH", "MODELSCOPE_CACHE", "BGE_DEVICE", "BGE_FP16",
                "BGE_RERANKER_LARGE", "BGE_RERANKER_DEVICE", "BGE_RERANKER_FP16",
            )},
        }),
        "pricing_configuration_sha256": (
            "not-applicable" if is_contract else str(pricing.get("fingerprint") or _fingerprint(pricing))
        ),
        "pricing": pricing,
        "cost_status": "not_applicable" if is_contract else ("available" if pricing_known else "unavailable"),
        "index_version": "offline-contract" if is_contract else os.getenv("INDEX_VERSION", "unversioned"),
        "collections": {
            "chunks": config.chunks_collection or "not-configured",
            "items": config.item_name_collection or "not-configured",
            "entities": config.entity_name_collection or "not-configured",
        },
        "access_policy": {
            "version": config.acl_policy_version,
            "enforcement": "pre_retrieval",
            "identity_source": "signed_server_context",
            "default_tenant": os.getenv("ACCESS_DEFAULT_TENANT", "public"),
        },
        "kg_graph_version": config.kg_graph_version,
        "query_config": {key: value for key, value in asdict(config).items() if key not in excluded_config},
    }


def _median_attempt(attempts: list[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(attempts, key=lambda result: float(result.get("score") or 0.0))
    selected = dict(ordered[len(ordered) // 2])
    scores = [float(result.get("score") or 0.0) for result in attempts]
    selected["attempt_scores"] = scores
    selected["unstable"] = max(scores) - min(scores) >= 0.2 if scores else False
    return selected


def run_evaluation(
    *,
    root: Path,
    dataset_path: Path,
    provider: EvaluationProvider,
    suite: str = "core",
    attempts: int = 1,
    write_snapshot_path: Path | None = None,
    source_contract_path: Path | None = None,
) -> Dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    if write_snapshot_path is not None and attempts != 1:
        raise ValueError("snapshot recording requires --attempts 1")
    if write_snapshot_path is not None and write_snapshot_path.exists():
        raise ValueError("refusing to overwrite an existing evaluation snapshot")
    if source_contract_path is None and provider.name != "contract" and os.getenv("EVALUATION_SOURCE_CONTRACT_FILE"):
        source_contract_path = Path(os.environ["EVALUATION_SOURCE_CONTRACT_FILE"])
    source_contract, source_contract_sha = load_source_contract(source_contract_path, dataset_path)
    all_cases = load_dataset(dataset_path)
    cases = (
        [case for case in all_cases if case.get("ci_core")]
        if suite == "core"
        else all_cases
    )
    if not cases:
        raise ValueError(f"suite {suite!r} selected no cases")

    raw_results: list[Dict[str, Any]] = []
    evaluated: list[Dict[str, Any]] = []
    for case in cases:
        case_attempts = []
        for attempt in range(1, attempts + 1):
            raw = provider.run(case, attempt=attempt)
            raw_results.append(raw)
            case_attempts.append(evaluate_case(case, raw, source_contract.get(case["id"])))
        evaluated.append(_median_attempt(case_attempts))

    dataset_version = str(all_cases[0].get("dataset_version") or "unversioned")
    evaluation_scope = str(getattr(provider, "evaluation_scope", "unknown"))
    metadata = collect_metadata(
        root,
        dataset_path,
        provider.name,
        dataset_version,
        evaluation_scope,
    )
    metadata["source_contract_sha256"] = source_contract_sha
    metadata["attempts"] = attempts
    source_scope = getattr(provider, "source_evaluation_scope", None)
    if source_scope:
        metadata["replay_source_evaluation_scope"] = str(source_scope)
    if getattr(provider, "snapshot_sha256", None):
        metadata["replay_snapshot_sha256"] = provider.snapshot_sha256
    summary = aggregate_results(evaluated)
    # Quality/latency retain the documented representative-attempt semantics;
    # billing/resource totals must include every executed attempt and turn.
    summary["representative_model_usage"] = summary["model_usage"]
    summary["model_usage"] = combine_model_usage(
        (raw.get("response") or {}).get("diagnostics", {}).get("model_usage")
        for raw in raw_results
    )
    summary["executed_attempt_count"] = len(raw_results)
    execution_health = {
        "provider_error_count": sum(bool(raw.get("error")) for raw in raw_results),
        "failed_model_call_count": summary["model_usage"]["failed_call_count"],
        "incomplete_turn_count": sum(
            value is not True
            for raw in raw_results
            for value in (raw.get("response") or {}).get("diagnostics", {}).get("evaluation_usage", {}).get("turn_pipeline_complete", [])
        ),
        "usage_complete": summary["model_usage"]["usage_complete"],
    }
    if provider.name == "replay":
        metadata["usage_scope"] = "recorded_snapshot"
        metadata["cost_status"] = "unavailable"
    if not summary["model_usage"]["usage_complete"] and provider.name != "contract":
        metadata["cost_status"] = "unavailable"
    if (
        provider.name not in {"contract", "replay"}
        and summary["model_usage"].get("cost_status") != "available"
    ):
        metadata["cost_status"] = "unavailable"
    batch_budget = float((metadata.get("query_config") or {}).get("evaluation_max_batch_cost") or 0.0)
    native_cost = float(summary["model_usage"].get("cost") or 0.0)
    cost_available = (
        metadata.get("cost_status") == "available"
        and summary["model_usage"].get("cost_status") == "available"
    )
    summary["cost_budget"] = {
        "limit": batch_budget,
        "actual": native_cost,
        "currency": summary["model_usage"].get("currency", ""),
        "status": (
            "not_applicable" if provider.name == "contract"
            else "available" if cost_available
            else "unavailable"
        ),
        "passed": (
            bool(batch_budget > 0 and native_cost <= batch_budget)
            if cost_available
            else None
        ),
    }
    eligibility_reasons = _rag_quality_eligibility_reasons(
        summary=summary,
        results=evaluated,
        metadata=metadata,
    )
    if execution_health["provider_error_count"]:
        eligibility_reasons.append("one or more executed attempts failed (including non-representative attempts)")
    if execution_health["failed_model_call_count"]:
        eligibility_reasons.append("one or more executed model calls failed")
    if execution_health["incomplete_turn_count"]:
        eligibility_reasons.append("one or more conversation turns had an incomplete pipeline")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": suite,
        "metadata": metadata,
        "summary": summary,
        "execution_health": execution_health,
        "baseline_eligibility": {
            "rag_quality": not eligibility_reasons,
            "reasons": eligibility_reasons,
        },
        "runtime_preflight": (
            provider.preflight()
            if callable(getattr(provider, "preflight", None))
            else {"passed": True, "checks": [], "failures": []}
        ),
        "snapshot": {
            "requested": str(write_snapshot_path) if write_snapshot_path else "",
            "written": False,
            "reason": "",
        },
        "results": evaluated,
        "manual_review": [
            {
                "case_id": result["case_id"],
                "reasons": result["failures"]
                + (["score variance across attempts"] if result.get("unstable") else []),
            }
            for result in evaluated
            if result.get("failures") or result.get("unstable")
        ],
    }
    if write_snapshot_path is not None:
        if eligibility_reasons:
            report["snapshot"]["reason"] = (
                "not written because the run is ineligible for a RAG quality baseline: "
                + "; ".join(eligibility_reasons)
            )
        else:
            write_jsonl(write_snapshot_path, raw_results)
            report["snapshot"]["written"] = True
            report["snapshot"]["sha256"] = _sha256(write_snapshot_path)
    return report


def _rag_quality_eligibility_reasons(
    *,
    summary: Dict[str, Any],
    results: Sequence[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    is_full_pipeline = metadata.get("evaluation_scope") == "full_pipeline"
    if not is_full_pipeline:
        reasons.append("provider did not execute the full retrieval-to-answer pipeline")
    if metadata.get("index_version") in {"", "unversioned", None}:
        reasons.append("INDEX_VERSION is not pinned")
    if "recall@5" not in (summary.get("metrics") or {}):
        reasons.append("retrieval recall is unavailable")
    if is_full_pipeline and float(
        (summary.get("metrics") or {}).get("pipeline_complete", 0.0)
    ) < 1.0:
        reasons.append("one or more pipeline runs failed or silently degraded")
    if any(result.get("error") for result in results):
        reasons.append("one or more provider calls failed")
    if is_full_pipeline and metadata.get("cost_status") != "available":
        reasons.append("versioned monetary cost is unavailable")
    cost_budget = summary.get("cost_budget") or {}
    if is_full_pipeline and cost_budget.get("status") == "available" and cost_budget.get("passed") is not True:
        reasons.append("evaluation batch monetary budget exceeded")
    return reasons


def compare_with_baseline(
    candidate: Dict[str, Any],
    baseline: Dict[str, Any],
    gate_config: Dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    candidate_metadata = candidate.get("metadata") or {}
    baseline_metadata = baseline.get("metadata") or {}
    is_contract = candidate_metadata.get("provider") == baseline_metadata.get("provider") == "contract"
    for field in (
        "evaluation_schema_version", "evaluator_version", "evaluator_fingerprint",
        "dataset_version", "dataset_sha256", "source_contract_sha256", "evaluation_scope",
        "provider", "prompt_sha256", "model", "item_model", "query_config", "attempts",
        "usage_accounting_version", "usage_scope",
        "query_pipeline_sha256", "runtime_configuration_sha256", "pricing_configuration_sha256",
    ):
        if is_contract and field == "query_pipeline_sha256":
            # This is precisely the production code exercised by an offline
            # contract, not a fixed external dependency of a live A/B run.
            continue
        if is_contract and field == "dataset_sha256":
            field = "dataset_semantic_sha256"
        current_value = candidate_metadata.get(field)
        baseline_value = baseline_metadata.get(field)
        if current_value is None or baseline_value is None or current_value == "" or baseline_value == "":
            failures.append(f"incompatible baseline: missing comparison contract field {field}")
            continue
        if current_value != baseline_value:
            failures.append(
                f"incompatible baseline: {field} {current_value!r} != {baseline_value!r}"
            )
    live_comparison = (
        candidate_metadata.get("provider") in {"service", "http"}
        and baseline_metadata.get("provider") in {"service", "http"}
    )
    if live_comparison:
        # The chunks collection and index version are the intentional A/B
        # variable. Other collections must remain fixed, and both sides must
        # still pin an explicit version rather than using an implicit default.
        for label, metadata in (("candidate", candidate_metadata), ("baseline", baseline_metadata)):
            if metadata.get("index_version") in {None, "", "unversioned"}:
                failures.append(f"incompatible baseline: {label} index_version is not pinned")
            collections = metadata.get("collections")
            if not isinstance(collections, dict) or not collections.get("chunks"):
                failures.append(f"incompatible baseline: {label} chunks collection is missing")
        candidate_collections = candidate_metadata.get("collections") or {}
        baseline_collections = baseline_metadata.get("collections") or {}
        for collection in ("items", "entities"):
            if candidate_collections.get(collection) != baseline_collections.get(collection):
                failures.append(
                    f"incompatible baseline: {collection} collection changed outside the A/B variable"
                )
    if candidate.get("suite") is None or candidate.get("suite") != baseline.get("suite"):
        failures.append("incompatible baseline: missing or different suite")

    candidate_rows = candidate.get("results") or []
    baseline_rows = baseline.get("results") or []
    candidate_cases = {item.get("case_id") for item in candidate_rows}
    baseline_cases = {item.get("case_id") for item in baseline_rows}
    if baseline_rows and candidate_cases != baseline_cases:
        missing = sorted(str(item) for item in baseline_cases - candidate_cases)
        added = sorted(str(item) for item in candidate_cases - baseline_cases)
        failures.append(
            f"incompatible baseline case set: missing={missing}, added={added}"
        )
    if not candidate_rows or not baseline_rows:
        failures.append("incompatible baseline: case results are missing")
    if len(candidate_rows) != len(candidate_cases) or len(baseline_rows) != len(baseline_cases):
        failures.append("incompatible baseline: duplicate case IDs")
    if failures:
        # Report incompatibility, not spurious numerical regressions under a
        # different evaluator. Frozen v1 baselines must be preserved, not relabelled.
        return failures

    candidate_summary = candidate.get("summary") or {}
    baseline_summary = baseline.get("summary") or {}
    max_drop = gate_config.get("metric_max_drop") or {}
    for metric, allowed_drop in max_drop.items():
        if metric not in (candidate_summary.get("metrics") or {}):
            failures.append(f"required gate metric unavailable in candidate: {metric}")
            continue
        if metric not in (baseline_summary.get("metrics") or {}):
            failures.append(f"required gate metric unavailable in baseline: {metric}")
            continue
        candidate_count = candidate_summary.get("metric_case_counts", {}).get(metric)
        baseline_count = baseline_summary.get("metric_case_counts", {}).get(metric)
        if not isinstance(candidate_count, int) or not isinstance(baseline_count, int) or min(candidate_count, baseline_count) <= 0:
            failures.append(f"required metric denominator unavailable: {metric}")
            continue
        if candidate_count != baseline_count:
            failures.append(f"incompatible metric denominator: {metric}")
            continue
        current = float(candidate_summary.get("metrics", {}).get(metric, 0.0))
        previous = float(baseline_summary.get("metrics", {}).get(metric, 0.0))
        if not math.isfinite(current) or not math.isfinite(previous):
            failures.append(f"required gate metric is not finite: {metric}")
            continue
        minimum = previous - float(allowed_drop)
        if current + 1e-12 < minimum:
            failures.append(
                f"{metric} regressed: {current:.4f} < baseline {previous:.4f} "
                f"- allowance {float(allowed_drop):.4f}"
            )

    allowed_pass_drop = float(gate_config.get("pass_rate_max_drop", 0.0))
    if "pass_rate" not in candidate_summary or "pass_rate" not in baseline_summary:
        failures.append("required pass_rate is unavailable")
    current_pass = float(candidate_summary.get("pass_rate") or 0.0)
    previous_pass = float(baseline_summary.get("pass_rate") or 0.0)
    if not math.isfinite(current_pass) or not math.isfinite(previous_pass):
        failures.append("required pass_rate is not finite")
    if current_pass + 1e-12 < previous_pass - allowed_pass_drop:
        failures.append(
            f"pass_rate regressed: {current_pass:.4f} < baseline {previous_pass:.4f} "
            f"- allowance {allowed_pass_drop:.4f}"
        )

    latency_ratio = float(gate_config.get("latency_p95_max_increase_ratio", 0.25))
    current_latency = float(
        candidate_summary.get("latency_ms", {}).get("p95") or 0.0
    )
    previous_latency = float(
        baseline_summary.get("latency_ms", {}).get("p95") or 0.0
    )
    if "latency_p95_max_increase_ratio" in gate_config and (
        not math.isfinite(previous_latency) or not math.isfinite(current_latency)
        or previous_latency <= 0 or current_latency <= 0
    ):
        failures.append("required positive latency p95 is unavailable")
    if "latency_p95_max_increase_ratio" in gate_config and previous_latency > 0 and current_latency > previous_latency * (1 + latency_ratio):
        failures.append(
            f"latency p95 regressed: {current_latency:.1f}ms > "
            f"{previous_latency * (1 + latency_ratio):.1f}ms"
        )

    native_allowances = gate_config.get("cost_max_increase_by_currency")
    if isinstance(native_allowances, dict):
        candidate_usage = candidate_summary.get("model_usage", {})
        baseline_usage = baseline_summary.get("model_usage", {})
        currency = str(candidate_usage.get("currency") or "")
        previous_currency = str(baseline_usage.get("currency") or "")
        cost_known = (
            candidate_metadata.get("cost_status") == "available"
            and baseline_metadata.get("cost_status") == "available"
            and currency == previous_currency
            and currency in native_allowances
        )
        if not cost_known:
            failures.append(
                "cost comparison unavailable: matching versioned pricing and currency are required"
            )
        else:
            current_cost = candidate_usage.get("cost")
            previous_cost = baseline_usage.get("cost")
            valid = all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0
                for value in (current_cost, previous_cost)
            )
            if not valid:
                failures.append("required monetary cost is unavailable or invalid")
            else:
                allowance = float(native_allowances[currency])
                if float(current_cost) > float(previous_cost) + allowance + 1e-12:
                    failures.append(
                        f"cost regressed: {float(current_cost):.6f} {currency} > "
                        f"{float(previous_cost) + allowance:.6f} {currency}"
                    )
    else:
        cost_allowance = float(gate_config.get("cost_max_increase_usd", 0.0))
        cost_known = candidate_metadata.get("cost_status") == baseline_metadata.get("cost_status") == "estimated"
        if "cost_max_increase_usd" in gate_config and not cost_known:
            failures.append("cost comparison unavailable: configured positive pricing is required; zero is not evidence of free usage")
        current_cost = float(
            candidate_summary.get("model_usage", {}).get("estimated_cost_usd") or 0.0
        )
        previous_cost = float(
            baseline_summary.get("model_usage", {}).get("estimated_cost_usd") or 0.0
        )
        if "cost_max_increase_usd" in gate_config and cost_known and (
            "estimated_cost_usd" not in candidate_summary.get("model_usage", {})
            or "estimated_cost_usd" not in baseline_summary.get("model_usage", {})
            or not math.isfinite(current_cost) or not math.isfinite(previous_cost)
            or min(current_cost, previous_cost) < 0
        ):
            failures.append("required monetary cost is unavailable or invalid")
        if cost_known and current_cost > previous_cost + cost_allowance + 1e-12:
            failures.append(
                f"estimated cost regressed: ${current_cost:.6f} > "
                f"${previous_cost + cost_allowance:.6f}"
            )
    return failures


def write_report(report: Dict[str, Any], json_path: Path) -> tuple[Path, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    metadata = report.get("metadata") or {}
    eligibility = report.get("baseline_eligibility") or {}
    usage = summary.get("model_usage") or {}
    if usage.get("cost_status") == "available":
        cost_text = f"{float(usage.get('cost') or 0):.6f} {usage.get('currency', '')}"
    else:
        cost_text = "unavailable"
    lines = [
        "# Shopkeeper Brain evaluation report",
        "",
        f"- Dataset: `{metadata.get('dataset_version', '')}`",
        f"- Dataset SHA-256: `{metadata.get('dataset_sha256', '')}`",
        f"- Evaluator: `{metadata.get('evaluator_version', '')}` / `{metadata.get('evaluator_fingerprint', '')}`",
        f"- Source contract SHA-256: `{metadata.get('source_contract_sha256', '')}`",
        f"- Provider: `{metadata.get('provider', '')}`",
        f"- Evaluation scope: `{metadata.get('evaluation_scope', '')}`",
        f"- Git commit: `{metadata.get('git_commit', '')}` (dirty={metadata.get('git_dirty')})",
        f"- Model / item model: `{metadata.get('model', '')}` / `{metadata.get('item_model', '')}`",
        f"- Index version: `{metadata.get('index_version', '')}`",
        f"- Cases: {summary.get('passed_count', 0)}/{summary.get('case_count', 0)} passed",
        f"- Latency p50/p95: {summary.get('latency_ms', {}).get('p50', 0)} / {summary.get('latency_ms', {}).get('p95', 0)} ms",
        f"- Model calls/tokens/cost: {usage.get('call_count', 0)} / {usage.get('total_tokens', 0)} / {cost_text}",
        f"- Cost status: `{metadata.get('cost_status', 'unavailable')}` (unavailable/zero does not mean free)",
        f"- Pricing fingerprint/currency: `{metadata.get('pricing_configuration_sha256', '')}` / `{usage.get('currency', '')}`",
        f"- Usage scope: `{metadata.get('usage_scope', 'unknown')}`; executed attempts: {summary.get('executed_attempt_count', 'unknown')}",
        f"- Valid RAG quality baseline: `{bool(eligibility.get('rag_quality'))}`",
        "",
        "## Baseline eligibility",
        "",
    ]
    reasons = eligibility.get("reasons") or []
    if reasons:
        lines.extend(f"- {reason}" for reason in reasons)
    else:
        lines.append("- Eligible for RAG quality comparison")
    lines.extend([
        "",
        "## Metrics",
        "",
    ])
    for name, value in (summary.get("metrics") or {}).items():
        lines.append(f"- {name}: {float(value):.4f}")
    lines.extend(["", "## Failed and review cases", ""])
    reviews = report.get("manual_review") or []
    if not reviews:
        lines.append("- None")
    else:
        for item in reviews:
            reasons = "; ".join(item.get("reasons") or ["manual review requested"])
            lines.append(f"- `{item['case_id']}`: {reasons}")
    lines.extend(["", "## Case results", ""])
    for result in report.get("results") or []:
        status = "PASS" if result.get("passed") else "FAIL"
        lines.append(
            f"- `{result['case_id']}` — {status}, score={float(result.get('score') or 0):.4f}, latency={float(result.get('latency_ms') or 0):.1f} ms"
        )
    return "\n".join(lines) + "\n"
