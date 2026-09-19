# Baseline status

- `stage0-contract.core.*` is the deterministic CI contract baseline. It does not measure retrieval quality.
- `stage0-current.core.*` is the approved 2026-09-19 full-pipeline baseline. It was captured with
  `INDEX_VERSION=hak180-638de365d6b6`, passed runtime preflight, completed every pipeline stage,
  and has `baseline_eligibility.rag_quality=true`.
- Its deliberately unoptimized current-state result is 2/9 passing, Recall@5 0.8333,
  Precision@5 0.1667, MRR/nDCG@5 0.8333, p50 3132.621 ms, and p95 15583.487 ms.
- The matching frozen replay input is `evaluation/snapshots/stage0-full-pipeline.core.jsonl`.
  Replay verifies reproducible scoring but remains ineligible to create a new RAG quality baseline.
- A future candidate can be promoted only when `baseline_eligibility.rag_quality` is `true`, the
  dataset/case set is unchanged, and failures have been reviewed.

Known baseline gaps include an empty HAK 180 Neo4j graph (`knowledge_graph_recall@5=0`), missing
image delivery for the control-panel case, weak table-answer/refusal behavior, citation errors, and
the permission case. These are evidence for later stages, not reasons to rewrite the stage-0 baseline.
