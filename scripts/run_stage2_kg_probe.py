"""Run a real stage-2 knowledge-graph retrieval probe and persist the evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.evaluation.providers import ServiceProvider
from knowledge.processor.query_process.config import QueryConfig


DEFAULT_OUTPUT = (
    ROOT
    / "evaluation/results/stage2-acl-v2-final-kg-lineage-probe.20260925.service.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        default="HAK 180 的部件和操作步骤之间有什么关系？",
    )
    parser.add_argument("--item-name", default="HAK 180")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def evaluate_probe(
    result: dict[str, Any],
    config: QueryConfig,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    response = result.get("response") or {}
    diagnostics = response.get("diagnostics") or {}
    trace = diagnostics.get("retrieval_trace") or {}
    stages = trace.get("stages") or {}
    statuses = trace.get("status") or {}
    kg_rows = stages.get("knowledge_graph") or []
    kg_status = statuses.get("query_kg") or {}
    counts = diagnostics.get("retrieval_counts") or {}

    assertions = {
        "runtime_preflight_passed": bool(preflight.get("passed")),
        "provider_error_empty": not bool(result.get("error")),
        "kg_stage_status_ok": kg_status.get("status") == "ok",
        "kg_retrieval_count_positive": int(counts.get("knowledge_graph") or 0) > 0,
        "kg_trace_nonempty": bool(kg_rows),
        "kg_trace_has_document_lineage": bool(kg_rows)
        and all(
            row.get("document_id") and row.get("section_id")
            for row in kg_rows
            if isinstance(row, dict)
        ),
        "kg_trace_has_graph_source_type": bool(kg_rows)
        and all(
            row.get("source_type") == "knowledge_graph"
            for row in kg_rows
            if isinstance(row, dict)
        ),
        "versioned_graph_selected": bool(config.kg_graph_version)
        and config.kg_graph_version != "legacy",
        "versioned_entity_collection_selected": bool(config.entity_name_collection),
    }
    compact_rows = [
        {
            key: row.get(key)
            for key in (
                "rank",
                "chunk_id",
                "document_id",
                "section_id",
                "file_title",
                "title",
                "score",
                "source_type",
            )
        }
        for row in kg_rows[:10]
        if isinstance(row, dict)
    ]
    return {
        "schema_version": "stage2-kg-probe-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": "service",
        "evaluation_scope": "full_pipeline_live_probe",
        "passed": all(assertions.values()),
        "assertions": assertions,
        "configuration": {
            "chunks_collection": config.chunks_collection,
            "entity_name_collection": config.entity_name_collection,
            "kg_graph_version": config.kg_graph_version,
            "acl_policy_version": config.acl_policy_version,
            "retrieval_policy_version": config.retrieval_policy_version,
        },
        "latency_ms": round(float(result.get("latency_ms") or 0.0), 3),
        "error": str(result.get("error") or ""),
        "answer": str(response.get("answer") or ""),
        "retrieval_counts": counts,
        "kg_status": kg_status,
        "kg_trace": compact_rows,
        "access_control": diagnostics.get("access_control") or {},
        "runtime_preflight": preflight,
        "model_usage": diagnostics.get("model_usage") or {},
    }


def main() -> int:
    args = parse_args()
    config = QueryConfig.from_env()
    case = {
        "id": "stage2_kg_relationship_live_probe",
        "input": {
            "query": args.query,
            "item_names": [args.item_name],
        },
        "expected": {},
    }
    provider = ServiceProvider(strict_preflight=True)
    preflight = provider.preflight()
    result = provider.run(case)
    report = evaluate_probe(result, config, preflight)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"kg probe: {'PASS' if report['passed'] else 'FAIL'}; "
        f"kg_count={report['retrieval_counts'].get('knowledge_graph', 0)}; "
        f"output={output}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
