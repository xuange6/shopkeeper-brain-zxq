"""Read-only diagnostic for legacy Markdown titles versus structured-IR titles.

This does not alter the frozen dataset, baseline, snapshots, or official gate.
It removes superficial Markdown heading markers and the legacy numeric suffix
used for split table chunks, then re-evaluates recorded responses. The result
is supplementary evidence, not a promoted quality baseline.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.evaluation.metrics import aggregate_results, evaluate_case
from knowledge.evaluation.runner import load_dataset


_HEADING_PREFIX = re.compile(r"^#+\s*")
_LEGACY_TABLE_PART = re.compile(r"^(\d+(?:\.\d+)+\s+.+)-[1-9]\d*$")


def canonical_title(value: str) -> str:
    title = _HEADING_PREFIX.sub("", str(value or "").strip())
    table_part = _LEGACY_TABLE_PART.match(title)
    return (table_part.group(1) if table_part else title).strip()


def _normalize_source(source: dict) -> None:
    for field in ("title", "parent_title"):
        if source.get(field):
            source[field] = canonical_title(source[field])


def normalized_case(case: dict) -> dict:
    result = copy.deepcopy(case)
    expected = result.get("expected") or {}
    for source in expected.get("relevant_sources") or []:
        _normalize_source(source)
        # Stage 0 sometimes labeled a heading as parent_title, while the IR
        # exposes that same heading as title. This is diagnostic equivalence.
        if source.get("parent_title") and not source.get("title"):
            source["title"] = source.pop("parent_title")
    for fact in expected.get("facts") or []:
        fact["source_titles"] = [
            canonical_title(title) for title in fact.get("source_titles") or []
        ]
    return result


def normalized_result(result: dict) -> dict:
    normalized = copy.deepcopy(result)
    response = normalized.get("response") or {}
    for source in response.get("sources") or []:
        if isinstance(source, dict):
            _normalize_source(source)
    trace = (response.get("diagnostics") or {}).get("retrieval_trace") or {}
    for stage in (trace.get("stages") or {}).values():
        if isinstance(stage, list):
            for source in stage:
                if isinstance(source, dict):
                    _normalize_source(source)
    return normalized


def diagnose(dataset: Path, reports: list[Path]) -> dict:
    cases = {case["id"]: case for case in load_dataset(dataset) if case.get("ci_core")}
    output = {
        "diagnostic_only": True,
        "warning": "Heading/legacy table-part compatibility only; not the official gate or a replacement baseline.",
        "reports": [],
    }
    for path in reports:
        report = json.loads(path.read_text(encoding="utf-8"))
        results = report.get("results") or []
        if {row["case_id"] for row in results} != set(cases):
            raise ValueError(f"{path} does not contain exactly the core case IDs")
        evaluated = [
            evaluate_case(normalized_case(cases[row["case_id"]]), normalized_result(row))
            for row in results
        ]
        summary = aggregate_results(evaluated)
        output["reports"].append({
            "path": str(path),
            "dataset_sha256": report.get("metadata", {}).get("dataset_sha256"),
            "collection": report.get("metadata", {}).get("collections", {}).get("chunks"),
            "official_passed_count": report["summary"]["passed_count"],
            "diagnostic_passed_count": summary["passed_count"],
            "official_metrics": report["summary"]["metrics"],
            "diagnostic_metrics": summary["metrics"],
            "per_case": [
                {
                    "case_id": item["case_id"],
                    "passed": item["passed"],
                    "recall@5": item["metrics"].get("recall@5"),
                    "citation_correctness": item["metrics"].get("citation_correctness"),
                    "faithfulness": item["metrics"].get("faithfulness"),
                }
                for item in evaluated
            ],
        })
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument(
        "--dataset", type=Path,
        default=ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = diagnose(args.dataset, args.reports)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
