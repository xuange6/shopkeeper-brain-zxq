# Stage 1 evaluator v2 offline contract baseline

This separate baseline covers deterministic offline production-code contracts only. It does not replace or modify any stage 0 baseline, and is not evidence of live retrieval quality, service latency, or monetary cost.

- Fixed offline configuration; environment model, endpoint, collection and pricing settings are ignored.
- 8/9 cases pass. The existing permission/refusal contract failure remains visible.
- All scored metric denominators and the evaluator fingerprint are recorded in the adjacent JSON artifact.
- Zero-degradation thresholds are in `evaluation/contract_gate.v2.json`.
- Dataset raw SHA is retained for provenance. Only this offline gate compares the JSON semantic SHA, avoiding platform line-ending differences; live comparisons still require identical raw dataset bytes.
- A changed evaluator fingerprint requires deliberate baseline review. Changes to the production code under test must continue to satisfy the fixed expected metrics.

Run `python scripts/run_evaluation.py --provider contract` to execute this gate without external services.
