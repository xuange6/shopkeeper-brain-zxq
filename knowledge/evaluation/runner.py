"""Evaluation orchestration, reporting, and regression gate comparison."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

from knowledge.evaluation.metrics import aggregate_results, evaluate_case
from knowledge.evaluation.providers import EvaluationProvider, write_jsonl


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

    config = QueryConfig()
    prompt_path = root / "knowledge/processor/query_process/prompt.py"
    dirty = _git(["status", "--porcelain"], root)
    return {
        "evaluation_schema_version": "1.1",
        "dataset_version": dataset_version,
        "dataset_sha256": _sha256(dataset_path),
        "provider": provider_name,
        "evaluation_scope": evaluation_scope,
        "git_commit": _git(["rev-parse", "HEAD"], root),
        "git_dirty": dirty not in {"", "unknown"},
        "prompt_sha256": _sha256(prompt_path),
        "model": config.default_model or "not-configured",
        "item_model": config.item_model or "not-configured",
        "index_version": os.getenv("INDEX_VERSION", "unversioned"),
        "collections": {
            "chunks": config.chunks_collection or "not-configured",
            "items": config.item_name_collection or "not-configured",
            "entities": config.entity_name_collection or "not-configured",
        },
        "query_config": {
            "embedding_search_limit": config.embedding_search_limit,
            "hyde_search_limit": config.hyde_search_limit,
            "rrf_k": config.rrf_k,
            "rrf_max_results": config.rrf_max_results,
            "rerank_min_top_k": config.rerank_min_top_k,
            "rerank_max_top_k": config.rerank_max_top_k,
            "refusal_min_score": config.refusal_min_score,
        },
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
) -> Dict[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
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
            case_attempts.append(evaluate_case(case, raw))
        evaluated.append(_median_attempt(case_attempts))

    if write_snapshot_path is not None and attempts != 1:
        raise ValueError("snapshot recording requires --attempts 1")

    dataset_version = str(all_cases[0].get("dataset_version") or "unversioned")
    evaluation_scope = str(getattr(provider, "evaluation_scope", "unknown"))
    metadata = collect_metadata(
        root,
        dataset_path,
        provider.name,
        dataset_version,
        evaluation_scope,
    )
    source_scope = getattr(provider, "source_evaluation_scope", None)
    if source_scope:
        metadata["replay_source_evaluation_scope"] = str(source_scope)
    summary = aggregate_results(evaluated)
    eligibility_reasons = _rag_quality_eligibility_reasons(
        summary=summary,
        results=evaluated,
        metadata=metadata,
    )
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": suite,
        "metadata": metadata,
        "summary": summary,
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
    return reasons


def compare_with_baseline(
    candidate: Dict[str, Any],
    baseline: Dict[str, Any],
    gate_config: Dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    candidate_metadata = candidate.get("metadata") or {}
    baseline_metadata = baseline.get("metadata") or {}
    for field in ("dataset_version", "dataset_sha256", "evaluation_scope"):
        current_value = candidate_metadata.get(field)
        baseline_value = baseline_metadata.get(field)
        if (
            field == "evaluation_scope"
            and current_value == "recorded_output"
            and candidate_metadata.get("replay_source_evaluation_scope")
            == baseline_value
        ):
            # A replay is still ineligible to become a RAG baseline, but it may
            # verify that an already captured full-pipeline snapshot reproduces
            # the metrics of the baseline it came from.
            continue
        if baseline_value is not None and current_value != baseline_value:
            failures.append(
                f"incompatible baseline: {field} {current_value!r} != {baseline_value!r}"
            )

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

    candidate_summary = candidate.get("summary") or {}
    baseline_summary = baseline.get("summary") or {}
    max_drop = gate_config.get("metric_max_drop") or {}
    for metric, allowed_drop in max_drop.items():
        if metric not in (candidate_summary.get("metrics") or {}):
            continue
        if metric not in (baseline_summary.get("metrics") or {}):
            continue
        current = float(candidate_summary.get("metrics", {}).get(metric, 0.0))
        previous = float(baseline_summary.get("metrics", {}).get(metric, 0.0))
        minimum = previous - float(allowed_drop)
        if current + 1e-12 < minimum:
            failures.append(
                f"{metric} regressed: {current:.4f} < baseline {previous:.4f} "
                f"- allowance {float(allowed_drop):.4f}"
            )

    allowed_pass_drop = float(gate_config.get("pass_rate_max_drop", 0.0))
    current_pass = float(candidate_summary.get("pass_rate") or 0.0)
    previous_pass = float(baseline_summary.get("pass_rate") or 0.0)
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
    if previous_latency > 0 and current_latency > previous_latency * (1 + latency_ratio):
        failures.append(
            f"latency p95 regressed: {current_latency:.1f}ms > "
            f"{previous_latency * (1 + latency_ratio):.1f}ms"
        )

    cost_allowance = float(gate_config.get("cost_max_increase_usd", 0.0))
    current_cost = float(
        candidate_summary.get("model_usage", {}).get("estimated_cost_usd") or 0.0
    )
    previous_cost = float(
        baseline_summary.get("model_usage", {}).get("estimated_cost_usd") or 0.0
    )
    if current_cost > previous_cost + cost_allowance + 1e-12:
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
    lines = [
        "# Shopkeeper Brain evaluation report",
        "",
        f"- Dataset: `{metadata.get('dataset_version', '')}`",
        f"- Dataset SHA-256: `{metadata.get('dataset_sha256', '')}`",
        f"- Provider: `{metadata.get('provider', '')}`",
        f"- Evaluation scope: `{metadata.get('evaluation_scope', '')}`",
        f"- Git commit: `{metadata.get('git_commit', '')}` (dirty={metadata.get('git_dirty')})",
        f"- Model / item model: `{metadata.get('model', '')}` / `{metadata.get('item_model', '')}`",
        f"- Index version: `{metadata.get('index_version', '')}`",
        f"- Cases: {summary.get('passed_count', 0)}/{summary.get('case_count', 0)} passed",
        f"- Latency p50/p95: {summary.get('latency_ms', {}).get('p50', 0)} / {summary.get('latency_ms', {}).get('p95', 0)} ms",
        f"- Model calls/tokens/cost: {summary.get('model_usage', {}).get('call_count', 0)} / {summary.get('model_usage', {}).get('total_tokens', 0)} / ${summary.get('model_usage', {}).get('estimated_cost_usd', 0):.6f}",
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
