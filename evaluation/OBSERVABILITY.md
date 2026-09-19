# Stage 0 observable points and design decisions

## Current query path

```text
POST /query
  -> QueryService (task_id / total_time / terminal error)
  -> item_name_confirm (history + explicit item constraint)
  -> parallel retrieval
       -> direct Milvus hybrid
       -> HyDE + Milvus hybrid
       -> entity extraction + Milvus alignment + Neo4j
       -> Web MCP
  -> RRF (candidate identities and contributing channels)
  -> rerank (ordered evidence and scores)
  -> answer_output (refusal or answer + citations + images)
  -> QueryService (public sources + diagnostics + SSE final)
```

Before stage 0 the public response already exposed `trace_id`, per-channel result counts,
node timings, final sources, and total time. Stage 0 adds aggregate model call count, failed
call count, input/output/total tokens, model names, model latency and estimated USD cost.
Raw prompts, API credentials, vectors and full retrieved chunks are intentionally excluded
from diagnostics.

## Evaluation record mapping

- Dataset item: versioned input, expected behavior, relevant source identity, supported facts,
  forbidden output and per-case thresholds.
- Provider result: answer, sources, images, diagnostics, latency and provider error.
- Full-pipeline retrieval trace: Direct/HyDE/KG/Web candidates, RRF output, rerank output,
  route status and compact evidence identity. Chunk text, vectors, prompts and credentials are excluded.
- Score: retrieval and answer metrics attached to the case result with a human-readable reason.
- Run: dataset/Prompt hashes, Git revision, model, index version, collection names and effective
  query parameters plus aggregate quality, latency, token and cost metrics.
- Review queue: assertion failures, provider errors and runs whose repeated scores vary by at
  least 0.2.

This mirrors the useful boundaries in Promptfoo's provider response/evaluator result and
Langfuse's dataset item/run/observation/score model without introducing either service as a
runtime dependency.

## Reference files actually reviewed

Promptfoo:

- `site/docs/configuration/reference.md`: provider response fields, token/cost/latency,
  thresholds, retry/error filters and CI options.
- `site/docs/configuration/expected-outputs/index.md`: deterministic/custom assertions and
  evaluating saved model outputs.
- `site/docs/configuration/outputs.md`: JSON/JSONL/JUnit persistence and assertion-level
  failure detail.
- `src/evaluator/runtime.ts`: persisted evaluation/result boundary and result writer contract.

Langfuse:

- `content/docs/evaluation/experiments/datasets.mdx`: versioned dataset items and links back to
  source traces/observations.
- experiment data-model documentation: dataset run items, traces/observations and attached
  scores.
- `packages/shared/prisma/schema.prisma`: observation token/cost/prompt linkage and score
  records.

Adopted: immutable versioned inputs, provider adapters, deterministic assertions, separate
retrieval/answer scores, persisted raw case output, run metadata, and an explicit review queue.

Not adopted in stage 0: Promptfoo's Node runtime/database/UI and model-graded assertions, or a
Langfuse deployment/SDK. The repository needs a zero-service CI gate first; optional platform
export can be added later without changing the local record schema.

## Operational boundaries

- Snapshots can contain user questions, answers and source previews. Treat them as internal
  evaluation data and review before committing; secrets and raw prompts are not recorded.
- Model token counts are marked estimated when provider usage metadata is absent. Cost is only
  meaningful after configuring both per-million-token rates.
- `INDEX_VERSION=unversioned` is a visible warning, not a valid production release identity.
- A replay pass proves the deterministic grader and frozen snapshot did not regress. It does not
  prove retrieval executed. Release evidence requires a `service` run with a pinned index and a
  complete retrieval trace. The current HTTP response intentionally omits evaluation-only traces.
- Provider failures are retained at case level. Snapshot replacement and baseline promotion are
  manual so a broken environment cannot silently redefine success.

## Alignment with high-star reference projects

The reviewed source links and adopted boundaries are recorded in
[`REFERENCE_ALIGNMENT.md`](REFERENCE_ALIGNMENT.md).

- RAGFlow: retrieval testing must run against the parsed/indexed dataset and verify that intended
  chunks are actually recovered before answer quality is interpreted. This project now gives only
  full-pipeline runs retrieval metrics; injected contract documents cannot produce Recall.
- Haystack: statistical document evaluators compare labeled ground-truth documents with retrieved
  documents. The local golden set uses manually labeled source identities and the evaluator consumes
  the rerank-stage candidates, not the final formatted citation list.
- Onyx: retrieval benchmarks distinguish query modes, retain returned document/chunk identity and
  sample latency repeatedly. The local report preserves route-specific identities, p50/p95 latency,
  attempts and instability flags.
- LangGraph: node updates remain in graph state and are reduced across parallel branches. Evaluation
  trace construction happens after the graph completes, so the runner observes real node outputs
  without replacing the graph.
- Promptfoo: providers, assertions, persisted result rows and CI failure reasons remain separate.
  Contract, replay, service and HTTP providers now declare different evaluation scopes.
- Langfuse: dataset items, runs, observations and scores are separate objects. The local JSON mirrors
  those boundaries with dataset metadata, trace IDs, per-stage observations, per-case metrics and
  aggregate run scores.
