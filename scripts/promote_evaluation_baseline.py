"""Promote an eligible full-pipeline evaluation report to a baseline artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.evaluation.runner import write_report


def _portable_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def promote(input_path: Path, output_path: Path) -> tuple[Path, Path]:
    report = json.loads(input_path.read_text(encoding="utf-8"))
    metadata = report.get("metadata") or {}
    eligibility = report.get("baseline_eligibility") or {}
    snapshot = report.get("snapshot") or {}

    failures: list[str] = []
    if metadata.get("evaluation_scope") != "full_pipeline":
        failures.append("evaluation_scope must be full_pipeline")
    if not eligibility.get("rag_quality", False):
        reasons = "; ".join(eligibility.get("reasons") or [])
        failures.append(f"rag_quality eligibility is false: {reasons}")
    if snapshot.get("requested") and not snapshot.get("written"):
        failures.append("requested snapshot was not written")
    if not report.get("results"):
        failures.append("report contains no case results")
    if failures:
        raise ValueError("baseline promotion refused: " + "; ".join(failures))

    promoted = dict(report)
    promoted_snapshot = dict(promoted.get("snapshot") or {})
    if promoted_snapshot.get("requested"):
        promoted_snapshot["requested"] = _portable_path(
            Path(promoted_snapshot["requested"])
        )
    promoted["snapshot"] = promoted_snapshot
    promoted["promotion"] = {
        "source_report": _portable_path(input_path),
        "policy": "eligible full-pipeline report with completed cases",
    }
    return write_report(promoted, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    json_path, markdown_path = promote(args.input.resolve(), args.output.resolve())
    print(f"baseline json: {json_path}")
    print(f"baseline markdown: {markdown_path}")


if __name__ == "__main__":
    main()
