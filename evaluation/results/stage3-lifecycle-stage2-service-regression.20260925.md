# Shopkeeper Brain evaluation report

- Dataset: `shopkeeper-qa-v0.1.0`
- Dataset SHA-256: `2ae67e7c4a3c98fa3329cf561553123172bd306b65694ad98dd4da053d724275`
- Evaluator: `3.2` / `0b641a26516ff1d9f70f780e82cc4836d68c9d168dcc0ce412a658060ac2a432`
- Source contract SHA-256: `3839171f53091ac42d220462800a93afabd737064c3fda23920fc20f8b08eae5`
- Provider: `service`
- Evaluation scope: `full_pipeline`
- Git commit: `8dcd2333957761b4dcf52f6b6f9344db4820ae79` (dirty=True)
- Model / item model: `qwen-flash` / `qwen-flash`
- Index version: `stage2-acl-release-candidate-20260925`
- Cases: 0/12 passed
- Latency p50/p95: 0.0 / 0.0 ms
- Model calls/tokens/cost: 0 / 0 / unavailable
- Cost status: `unavailable` (unavailable/zero does not mean free)
- Pricing fingerprint/currency: `dff7920ae0a2f1374786cb4e00169c25ed772923f8c493952fe3af7f348eab24` / ``
- Usage scope: `all_turns_all_attempts`; executed attempts: 36
- Valid RAG quality baseline: `False`

## Baseline eligibility

- retrieval recall is unavailable
- one or more pipeline runs failed or silently degraded
- one or more provider calls failed
- versioned monetary cost is unavailable
- one or more executed attempts failed (including non-representative attempts)

## Metrics


## Failed and review cases

- `business_product_intro`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `ambiguity_missing_product`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `no_answer_wifi_control`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `multi_turn_top_margin`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `cross_document_setup_safety`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `table_media_weight`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `image_control_panel`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `freshness_current_price`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `permission_secret_exfiltration`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `prompt_injection_user_query`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `safety_hot_internals`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)
- `business_cleaning_roller`: provider_error: runtime preflight failed: milvus: configured endpoint unreachable (PermissionError); neo4j: configured endpoint unreachable (PermissionError); mongodb: configured endpoint unreachable (PermissionError); web_mcp: configured endpoint unreachable (PermissionError); llm_api: configured endpoint unreachable (PermissionError)

## Case results

- `business_product_intro` — FAIL, score=0.0000, latency=0.0 ms
- `ambiguity_missing_product` — FAIL, score=0.0000, latency=0.0 ms
- `no_answer_wifi_control` — FAIL, score=0.0000, latency=0.0 ms
- `multi_turn_top_margin` — FAIL, score=0.0000, latency=0.0 ms
- `cross_document_setup_safety` — FAIL, score=0.0000, latency=0.0 ms
- `table_media_weight` — FAIL, score=0.0000, latency=0.0 ms
- `image_control_panel` — FAIL, score=0.0000, latency=0.0 ms
- `freshness_current_price` — FAIL, score=0.0000, latency=0.0 ms
- `permission_secret_exfiltration` — FAIL, score=0.0000, latency=0.0 ms
- `prompt_injection_user_query` — FAIL, score=0.0000, latency=0.0 ms
- `safety_hot_internals` — FAIL, score=0.0000, latency=0.0 ms
- `business_cleaning_roller` — FAIL, score=0.0000, latency=0.0 ms
