# Stage 0 reference alignment

Reviewed on 2026-09-19. The goal is behavioral alignment, not dependency adoption.

## RAGFlow

- Reference: [retrieval test guide](https://github.com/infiniflow/ragflow/blob/main/docs/guides/dataset/run_retrieval_test.md)
  and [retrieval benchmark](https://github.com/infiniflow/ragflow/blob/main/rag/benchmark.py).
- Boundary adopted: retrieval quality is measured by running a query against an indexed dataset and
  checking the chunks that actually return. Hybrid retrieval and reranking configuration belong in
  the run record.
- Local implementation: only `service` full-pipeline traces can emit Recall/MRR/nDCG. Contract-injected
  evidence and output replay are ineligible for RAG quality promotion.

## Haystack

- Reference: [evaluation overview](https://docs.haystack.deepset.ai/docs/evaluation) and
  [DocumentMRREvaluator](https://docs.haystack.deepset.ai/docs/documentmrrevaluator).
- Boundary adopted: statistical retrieval evaluation compares ground-truth documents with retrieved
  documents independently from answer evaluation.
- Local implementation: manually labeled `relevant_sources` are compared with rerank candidates;
  Direct, HyDE, KG, Web and RRF are also scored separately for loss localization.

## Onyx

- Reference: [OpenSearch retrieval benchmark](https://github.com/onyx-dot-app/onyx/blob/main/backend/scripts/debugging/opensearch/benchmark_retrieval.py).
- Boundary adopted: retain query mode, returned document/chunk identity, sample count and latency
  distribution instead of reporting a single opaque end-to-end score.
- Local implementation: route-specific compact identities, attempts, p50/p95, model usage and error
  status are persisted per run.

## LangGraph

- Reference: [streaming state updates](https://langchain-ai.github.io/langgraphjs/how-tos/stream-values/).
- Boundary adopted: graph nodes produce state updates; parallel branches need explicit reducers;
  observability should inspect graph outputs rather than replace graph execution.
- Local implementation: `retrieval_status` is reduced across parallel routes and the evaluation trace
  is built from the final graph state only for in-process evaluation.

## Promptfoo

- Reference: [assertions](https://www.promptfoo.dev/docs/configuration/expected-outputs/) and
  [persisted output formats](https://www.promptfoo.dev/docs/configuration/outputs/).
- Boundary adopted: provider execution, assertions, row-level failure reasons, aggregate statistics
  and CI exit status remain distinct.
- Local implementation: contract/replay/service/http declare explicit scopes, reports retain raw
  provider errors, and incompatible datasets or case sets cannot be compared.

## Langfuse

- Reference: [experiment data model](https://langfuse.com/docs/evaluation/experiments/data-model) and
  [datasets](https://langfuse.com/docs/evaluation/experiments/datasets).
- Boundary adopted: dataset item, experiment run, trace/observation and score are separate records;
  versioned inputs do not make model output deterministic.
- Local implementation: dataset hash/version, trace ID, per-stage observations, per-case scores,
  aggregate run metrics and manual-review reasons are persisted separately.

## Deliberately not adopted in stage 0

- No RAGFlow/Haystack/Onyx runtime is embedded into the product.
- No Promptfoo Node runtime or hosted Langfuse service is required for CI.
- No parser, indexing architecture, ACL lifecycle or Agent runtime redesign is pulled forward from
  later roadmap stages.
