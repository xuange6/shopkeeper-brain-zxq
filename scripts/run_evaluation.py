"""Single entry point for Shopkeeper Brain's stage-0 evaluation gate."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import json


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.evaluation.providers import (
    ContractProvider,
    HttpProvider,
    ReplayProvider,
    ServiceProvider,
)
from knowledge.evaluation.runner import (
    compare_with_baseline,
    run_evaluation,
    write_report,
)


DEFAULT_DATASET = ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl"
DEFAULT_SNAPSHOT = ROOT / "evaluation/snapshots/stage0-current.core.jsonl"
DEFAULT_BASELINE = ROOT / "evaluation/baselines/stage0-current.core.json"
DEFAULT_CONTRACT_BASELINE = ROOT / "evaluation/baselines/stage2-release-contract.core.json"
DEFAULT_GATE = ROOT / "evaluation/gate.json"
DEFAULT_CONTRACT_GATE = ROOT / "evaluation/contract_gate.v2.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("contract", "replay", "service", "http"),
        default="service",
    )
    parser.add_argument("--suite", choices=("core", "full"), default="core")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--gate-config", type=Path)
    parser.add_argument("--source-contract", type=Path, help="Reviewed, dataset-SHA-pinned evidence selectors.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Run the service provider even when configured dependencies are unavailable.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-snapshot", type=Path)
    parser.add_argument("--gate", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.provider == "contract":
        provider = ContractProvider()
    elif args.provider == "replay":
        provider = ReplayProvider(_resolve(args.snapshot))
    elif args.provider == "http":
        provider = HttpProvider(args.base_url, timeout_seconds=args.timeout_seconds)
    else:
        provider = ServiceProvider(strict_preflight=not args.skip_preflight)

    report = run_evaluation(
        root=ROOT,
        dataset_path=_resolve(args.dataset),
        provider=provider,
        suite=args.suite,
        attempts=args.attempts,
        write_snapshot_path=_resolve(args.write_snapshot) if args.write_snapshot else None,
        source_contract_path=_resolve(args.source_contract) if args.source_contract else None,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = _resolve(args.output) if args.output else ROOT / f"evaluation/results/{stamp}.{args.provider}.{args.suite}.json"

    gate_failures: list[str] = []
    if args.gate:
        baseline_path = args.baseline or (
            DEFAULT_CONTRACT_BASELINE
            if args.provider == "contract"
            else DEFAULT_BASELINE
        )
        baseline = json.loads(_resolve(baseline_path).read_text(encoding="utf-8"))
        gate_path = args.gate_config or (DEFAULT_CONTRACT_GATE if args.provider == "contract" else DEFAULT_GATE)
        gate_config = json.loads(_resolve(gate_path).read_text(encoding="utf-8"))
        gate_failures = compare_with_baseline(report, baseline, gate_config)
        if args.provider in {"service", "http"} and not (
            baseline.get("baseline_eligibility") or {}
        ).get("rag_quality", False):
            gate_failures.append(
                "selected baseline is not an approved full-pipeline RAG quality baseline"
            )
        if args.provider in {"service", "http"} and not report[
            "baseline_eligibility"
        ]["rag_quality"]:
            gate_failures.extend(
                "invalid RAG quality run: " + reason
                for reason in report["baseline_eligibility"]["reasons"]
            )
    report["gate"] = {
        "enabled": bool(args.gate),
        "passed": (not gate_failures) if args.gate else None,
        "failures": gate_failures,
    }
    json_path, markdown_path = write_report(report, output)

    summary = report["summary"]
    print(
        f"evaluation: {summary['passed_count']}/{summary['case_count']} cases passed; "
        f"gate={('PASS' if not gate_failures else 'FAIL') if args.gate else 'SKIP'}"
    )
    print(f"json: {json_path}")
    print(f"markdown: {markdown_path}")
    for failure in gate_failures:
        print(f"gate failure: {failure}")
    return 1 if gate_failures else 0


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
