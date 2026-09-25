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

- `stage2-current.core.*` is the approved 2026-09-24 full-pipeline baseline after
  `stage2-industrial-rag-v2` / `intent-policy-v2` hardening. Its source report is
  `evaluation/results/stage2-security-v2-final-candidate.20260924.service.core.json`: 9/9 cases,
  Recall@5/citation correctness/faithfulness/image accuracy/safety all 1.0, p95 10379.839 ms,
  18,906 tokens, CNY 0.00558585, and `cost_status=available`.
- Earlier stage-2 graduation, supplemental FAIL, and preflight-failure reports remain historical
  evidence and must not be rewritten to match this pointer.

- `stage2-acl-v2-lineage-final.20260925.service.full.*` is the final phase-2
  full-suite baseline for the current code and activated ACL/KG index. Its source report is
  `evaluation/results/stage2-acl-v2-lineage-candidate.20260925.service.full.json`: 12/12 cases,
  core 9/9, Recall@5 and evidence-group/section/document Recall@5 0.944444,
  KG Recall@5 0.111111, citation/faithfulness/image/behavior/safety 1.0,
  p95 6093.940 ms, 81,523 tokens, CNY 0.02366160, and gate PASS.
  It was promoted without overwriting `stage2-current.core.*` or any earlier evidence.

- `stage2-industrial-rag-v2.0.0.20260925.service.full.*` is the release baseline
  after signed-token canonicalization and rollback hardening. Its source report is
  `evaluation/results/stage2-acl-v2-release-candidate.20260925.service.full.json`:
  12/12 cases, core 9/9, Recall@5 and evidence-group/section/document Recall@5
  0.944444, KG Recall@5 0.111111, citation/faithfulness/image/behavior/safety 1.0,
  p95 5918.720 ms, 80,275 tokens, CNY 0.02272920, and gate PASS. The matching
  control and every older PASS/FAIL artifact remain immutable historical evidence.

- `stage2-release-contract.core.*` is the deterministic evaluator 3.2 / policy-v2
  CI baseline created alongside the release. It passed 9/9 without external calls
  and replaces only the default contract pointer; older contract baselines remain
  available for historical compatibility audits.
