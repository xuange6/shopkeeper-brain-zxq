"""Replay stage-1 reranker scores through the stage-2 deterministic calibration policy."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from knowledge.document_ir.indexing import chunks_to_index_rows
    from knowledge.document_ir.serialization import load_document_ir
    from knowledge.processor.query_process.config import QueryConfig
    from knowledge.processor.query_process.evidence import annotate_evidence, build_evidence_decision
    from knowledge.processor.query_process.nodes.intent_policy import IntentPolicyNode

    report = json.loads(args.report.read_text(encoding="utf-8"))
    cases = {
        row["id"]: row
        for line in args.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for row in [json.loads(line)]
    }
    document = load_document_ir(args.ir)
    rows = {row["chunk_id"]: row for row in chunks_to_index_rows(document, item_name="HAK 180")}
    config = QueryConfig()
    audited = []
    all_scores = []

    for result in report.get("results") or []:
        case = cases[result["case_id"]]
        case_input = case.get("input") or {}
        original_query = str((case_input.get("turns") or [case_input.get("query", "")])[-1])
        decision = IntentPolicyNode.classify(original_query)
        query = decision.get("sanitized_query") or original_query
        categories = set(case.get("category") or [])
        features = {
            "table": "table" in categories,
            "image": "image" in categories,
            "safety": result["case_id"] == "safety_hot_internals",
            "freshness": "freshness" in categories,
        }
        trace = ((result.get("response") or {}).get("diagnostics") or {}).get("retrieval_trace") or {}
        rerank = (trace.get("stages") or {}).get("rerank") or []
        docs = []
        raw_scores = []
        for item in rerank:
            raw = item.get("score")
            if isinstance(raw, (int, float)):
                raw_scores.append(float(raw))
                all_scores.append(float(raw))
            base = dict(rows.get(str(item.get("chunk_id") or "")) or {})
            base.update({key: value for key, value in item.items() if value not in (None, "", [])})
            if item.get("source") == "web":
                base.setdefault("content", str(item.get("title") or ""))
                base["source"] = "web"
            base["score"] = raw
            docs.append(annotate_evidence(base, query, features, config))
        docs.sort(key=lambda item: item["ranking_score"], reverse=True)
        seen = set()
        grouped = []
        for doc in docs:
            if doc["evidence_group_id"] in seen:
                continue
            seen.add(doc["evidence_group_id"])
            grouped.append(doc)
        evidence_decision = build_evidence_decision(grouped[: config.answer_max_evidence], [], query, config)
        audited.append(
            {
                "case_id": result["case_id"],
                "raw_score_distribution": {
                    "count": len(raw_scores),
                    "min": min(raw_scores) if raw_scores else None,
                    "max": max(raw_scores) if raw_scores else None,
                    "mean": statistics.fmean(raw_scores) if raw_scores else None,
                },
                "stage1_top_source": rerank[0].get("source") if rerank else "",
                "stage2_top_source": grouped[0].get("source") if grouped else "",
                "stage2_top_source_type": grouped[0].get("source_type") if grouped else "",
                "canonical_group_count": len(grouped),
                "evidence_decision": evidence_decision,
            }
        )

    payload = {
        "schema_version": "stage2-calibration-audit-v1",
        "source_report": str(args.report),
        "source_index_version": (report.get("metadata") or {}).get("index_version"),
        "stage2_policy_version": config.retrieval_policy_version,
        "global_raw_score_distribution": {
            "count": len(all_scores),
            "min": min(all_scores) if all_scores else None,
            "max": max(all_scores) if all_scores else None,
            "mean": statistics.fmean(all_scores) if all_scores else None,
            "negative_count": sum(score < 0 for score in all_scores),
        },
        "cases": audited,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = [
        "# Stage 2 calibration audit",
        "",
        f"- Source report: `{args.report}`",
        f"- Policy: `{config.retrieval_policy_version}`",
        f"- Raw scores: count={len(all_scores)}, min={payload['global_raw_score_distribution']['min']}, max={payload['global_raw_score_distribution']['max']}, negative={payload['global_raw_score_distribution']['negative_count']}",
        "",
        "## Cases",
        "",
    ]
    for item in audited:
        evidence = item["evidence_decision"]
        markdown.append(
            f"- `{item['case_id']}`: raw_top={evidence.get('top1_raw_score')}, "
            f"confidence={evidence.get('answer_confidence')}, answer={evidence.get('should_answer')}, "
            f"source={item['stage1_top_source']}→{item['stage2_top_source']} ({item['stage2_top_source_type']})"
        )
    args.output.with_suffix(".md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({"json": str(args.output), "cases": len(audited)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
