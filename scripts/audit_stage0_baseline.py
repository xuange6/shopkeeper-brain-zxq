"""Read-only audit of the immutable stage-0 dataset, baseline, and replay IDs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def audit(dataset: Path, baseline: Path, snapshot: Path) -> dict:
    actual_sha = hashlib.sha256(dataset.read_bytes()).hexdigest()
    baseline_data = json.loads(baseline.read_text(encoding="utf-8"))
    recorded_sha = baseline_data["metadata"]["dataset_sha256"]
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    dataset_ids = [case["id"] for case in cases if case.get("ci_core")]
    snapshot_ids = [json.loads(line)["case_id"] for line in snapshot.read_text(encoding="utf-8").splitlines() if line.strip()]
    baseline_ids = [row["case_id"] for row in baseline_data.get("results") or []]
    ids_match = dataset_ids == snapshot_ids == baseline_ids
    return {
        "dataset_sha256_actual": actual_sha,
        "dataset_sha256_recorded": recorded_sha,
        "dataset_bytes_match": actual_sha == recorded_sha,
        "case_ids_match": ids_match,
        "core_case_count": len(dataset_ids),
        "baseline_eligible": bool((baseline_data.get("baseline_eligibility") or {}).get("rag_quality")),
        "comparison_authorized": actual_sha == recorded_sha and ids_match,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl")
    parser.add_argument("--baseline", type=Path, default=ROOT / "evaluation/baselines/stage0-current.core.json")
    parser.add_argument("--snapshot", type=Path, default=ROOT / "evaluation/snapshots/stage0-full-pipeline.core.jsonl")
    args = parser.parse_args()
    result = audit(args.dataset, args.baseline, args.snapshot)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["comparison_authorized"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
