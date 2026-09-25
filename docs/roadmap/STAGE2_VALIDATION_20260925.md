# 阶段 2 最终验证记录（2026-09-25）

## 结论

阶段 2 **允许毕业**。遗留 ACL、KG、Web 官方来源、能力型拒答、引用公开绑定和结构化评测问题均在阶段 2 内关闭。当前代码的真实 service control/candidate 均执行 12 个用例 × 3 次；candidate 为 12/12、核心用例 9/9，使用未降低门槛的 `evaluation/gate.json` 得到 PASS。阶段 3 尚未开始。

2026-09-24 的验证文档、早期 9/12、11/12 和 preflight/引用失败报告均保留为历史证据。本记录是新的最终证据，不回写旧报告。

## 架构和代码交付

- `knowledge/security/access_control.py`、`knowledge/api/query_router.py`、`knowledge/schema/query_schema.py`：身份和 ACL 只接受服务端签名上下文；查询正文禁止 tenant/role 自报；会话历史绑定 tenant 和主体哈希。
- direct、HyDE、KG entity alignment、Neo4j 一跳扩展和 KG chunk 回填：统一执行 tenant、visibility、ACL 和图版本的召回前过滤。
- `knowledge/processor/query_process/nodes/query_kg.py`、`knowledge/processor/import_process/nodes/kg_graph_node.py`：版本化 Entity/Chunk schema、作用域唯一约束、非空/部分失败关闭、实体索引与图谱真实回填；KG evidence 保留 document、revision、section、page、block、lineage、chunk 和 source URI。
- `knowledge/processor/query_process/nodes/retrieval_plan.py`、`rrf.py`、`rerank.py`、`evidence.py`：可配置多路路由、加权融合、rerank 原始分与校准分分离、组合式拒答、能力问题权威证据保护和 canonical evidence 去重。
- `knowledge/processor/query_process/nodes/web_search_mcp.py`：Local-first/Web-fallback、时效路由、官方域名扩展与过滤，以及失败、漂移、发布日期和抓取时间诊断。
- `knowledge/processor/query_process/nodes/intent_policy.py`、`security.py`：权限窃取、Prompt Injection、业务问题、知识不足、实体歧义和时效意图分离；规范化、受限编码检查、间接注入隔离和输出 DLP 分层执行。
- `knowledge/processor/query_process/nodes/answer_output.py`、`knowledge/utils/query_result_utils.py`：逐 claim citation verification；公开 source 只携带核验通过的 `supported_claims`，并保留完整结构位置和 Web 日期。
- `knowledge/observability/pricing.py`、`model_usage.py`、`knowledge/evaluation/usage.py`：query rewrite、HyDE、KG entity 和 answer 分操作记录调用、token、延迟和费用；请求与批次预算独立执行。
- `knowledge/evaluation/metrics.py`、`runner.py`：chunk 指标继续用于诊断，并增加 evidence-group、section、document 指标；精确 source contract 由 dataset SHA 固定，禁止模糊映射和复制 chunk 提分。
- `scripts/seed_stage2_index.py`、`verify_stage2_index.py`、`run_stage2_kg_probe.py`：独立建索引、跨租户/约束核验和可重复真实 KG 验收。
- `config/releases/stage2-industrial-rag-v2.json`、`scripts/switch_rag_release.py`：候选/回滚资源集中声明，只修改四个白名单键，默认 dry-run，应用前备份并原子替换；发布前已完成 candidate → rollback → candidate 真实往返。

## 索引、ACL 与 KG 验证

candidate 独立资源：

- chunk collection：`kb_chunks_ir_stage2_release_20260925`
- rollback chunk collection：`kb_chunks_ir_stage2_release_control_20260925`
- entity collection：`kb_graph_entities_stage2_release_20260925`
- graph version：`stage2-acl-kg-release-20260925`
- index version：`stage2-acl-release-candidate-20260925`
- ACL policy：`retrieval-acl-v1`

发布前在全新资源中重建 129 个 chunk、129 个唯一 chunk ID、83 个资产 URI；KG 抽取 129/129 完成且无失败。只读验收中 Milvus chunk 129、entity 906；Neo4j entity 584、chunk 122、MENTIONED_IN 906、业务关系 738；candidate 和 rollback 两个 chunk collection 均通过，跨租户结果、缺失 ACL、重复实体和重复 chunk 均为 0，两个作用域唯一约束存在。

发布前真实 KG lineage 探针通过 9 项断言：运行预检成功、无 provider error、`query_kg=ok`、KG count > 0、trace 非空、document/section 血缘完整、来源类型为 `knowledge_graph`、版本化 graph/entity collection 均已选中。一次请求召回 20 个 KG evidence、2 个种子实体和 4 条关系；总延迟 28200.925 ms，3,139 tokens，CNY 0.00125520，费用可计算。

证据：

- `output/stage2-acl-release.index-audit.20260925.json`
- `output/stage2-acl-release-candidate.index-verification.20260925.json`
- `output/stage2-acl-release-control.index-verification.20260925.json`
- `evaluation/results/stage2-acl-v2-release-kg-lineage-probe.20260925.service.json`

## 自动化验证

- 最终完整测试：230/230 PASS。
- ACL/发布回滚定向回归：24/24 PASS。
- 新增/扩充测试覆盖签名身份、请求体身份注入、跨租户 Milvus/Neo4j filter、历史隔离、KG fail-closed、KG citation lineage、Web 路由、校准拒答、能力型证据保护、claim-evidence 公开绑定、多粒度指标、来源契约、真实费率和预算。
- 完整测试第一次运行曾因 Windows 临时目录原子重命名出现一次 `WinError 5`，单用例立即重跑通过，最终完整套件再次运行通过；未把该环境抖动计为业务成功。

## 当前代码的真实 control/candidate

共同条件：同一数据集、源证据契约、当前代码、模型、Prompt、实体集合、KG 图、Web 配置、费率和 evaluator；仅 chunk collection/index version 不同。每组 12 个用例 × 3 次，共 36 次执行，错误均为 0，均具备 RAG baseline eligibility。

公共指纹：

- query pipeline：`56fdc795661541578271caf3a370eb12b4822d5a0a16597e9f1242f81718b07c`
- source contract：`3839171f53091ac42d220462800a93afabd737064c3fda23920fc20f8b08eae5`
- pricing：`dff7920ae0a2f1374786cb4e00169c25ed772923f8c493952fe3af7f348eab24`
- dataset：`2ae67e7c4a3c98fa3329cf561553123172bd306b65694ad98dd4da053d724275`

Control：

- 12/12，核心 9/9；Recall@5 0.944444；引用正确率、Faithfulness、图片、行为和安全均为 1.0。
- P50 2724.640 ms；P95 6072.658 ms。
- 66 次模型调用；合计 80,482 tokens。
- CNY 0.02296815，`cost_status=available`。

Candidate：

- 12/12，核心 9/9；gate PASS；失败用例 0；manual review 0。
- Recall@5、evidence-group/section/document Recall@5 均为 0.944444；KG Recall@5 0.111111；引用正确率、Faithfulness、图片、行为和安全均为 1.0。
- P50 2989.878 ms；P95 5918.720 ms。
- 66 次模型调用；输入 72,358、输出 7,917、合计 80,275 tokens。
- CNY 0.02272920，`cost_status=available`；批次费用低于 CNY 0.5 预算。

Candidate 相对同轮 control：P95 降低 2.53%；token 减少 207（0.26%）；费用减少 CNY 0.00023895（1.04%）。价格为阿里云百炼北京地域 `qwen-flash`、CNY/每百万 token，来源 URL、生效日和观察日都写入评测 metadata。

分操作 candidate 消耗：HyDE 7,894 tokens / CNY 0.00729285；answer 71,058 / CNY 0.01510020；query rewrite 651 / CNY 0.00017460；KG entity 672 / CNY 0.00016155。检索和 rerank 的非模型阶段延迟另存于每请求 diagnostics。

证据：

- `evaluation/results/stage2-acl-v2-release-control.20260925.service.full.json`
- `evaluation/results/stage2-acl-v2-release-candidate.20260925.service.full.json`
- `evaluation/baselines/stage2-industrial-rag-v2.0.0.20260925.service.full.json`

## 逐项失败根因和修复证据

1. `table_media_weight`：旧逻辑把负 reranker logit 当拒答阈值，虽召回正确表格仍拒答。现保留 raw score 仅用于诊断，通过 sigmoid 校准相关性，并组合 Top-1、分差、authority、结构匹配和证据覆盖率决策。最终三次均回答 350 g/m² 与 90 g/m²并绑定表格 evidence。
2. `safety_hot_internals`：旧动态 Web 普通结果挤占本地警告。现产品与安全意图 Local-first，本地充分时 Web 跳过；时效问题才要求 Web，普通页面权威值低于本地说明书。最终三次均引用本地警告并通过。
3. `multi_turn_top_margin`/跨文档引用：旧模式先生成后附相关引用，可能有事实正确但引用不完整。现逐 claim 核验并给 source 标记实际 `supported_claims`；不能支持的 claim 删除或降级。修复前单次 snapshot 的 `cross_document_setup_safety` 引用正确率 0.8 被保留，修复后 control/candidate 为 1.0。
4. 安全路由：旧权限窃取被当产品歧义、注入被当知识不足。现权限越权独立拒绝；Prompt Injection 清洗后对合法业务重新执行策略和检索，最终既不泄漏又回答 5 mm 上边距。
5. 能力型无答案：通用网页曾被误当成“支持 Wi-Fi”的能力依据。现能力问题必须由本地或官方 Web 覆盖核心能力词；三次均以 `unsupported_capability_evidence` 安全拒答。
6. 成本不可计算：费率从业务逻辑移至 `config/model_pricing.json`，按模型/区域/币种/单位/生效日生成指纹并分操作计量。最终两组均为 available，预算和成本门禁可计算。
7. 分块粒度偏差：新 IR 合并重复警告后，旧 chunk title 不再一一对应。现保留 chunk 指标，并增加 canonical evidence-group/section/document 指标；`stage2-ir-v4.json` 只允许 dataset-SHA 固定的 document+section+chunk 精确迁移，不做模糊匹配，数据集和 gate 未改动。
8. ACL/KG：旧请求没有可信主体，Neo4j 是空图。现服务端签名身份进入所有本地检索前过滤，重建版本化实体和关系；真实 KG probe 证明非空召回。最后发现 KG trace 只含 chunk ID，随后补齐全量血缘字段并新增回归测试；修复前 probe 报告原样保留。

## 历史失败证据保留

- `docs/roadmap/STAGE2_VALIDATION_20260924.md`：2026-09-24 中间状态与遗留问题。
- `evaluation/results/stage2-acl-v2-control.20260925.service.full.json`：首次补全实验 9/12。
- `evaluation/results/stage2-acl-v2-candidate-snapshot.20260925.service.full.json`：公开 claim binding 修复前 11/12。
- `evaluation/results/stage2-acl-v2-final-kg-probe.20260925.service.json`：KG 已命中但 trace 血缘尚不完整的修复前证据。

## 尚存风险

- Web/模型长期漂移仍需阶段 3 发布监控、灰度和回滚处理。
- 对抗安全回归不能证明覆盖未知攻击；授权仍必须保持确定性和 fail-closed。
- 费率变更需运维更新配置并触发新指纹，旧价格报告不能跨生效期直接比较。
- KG golden case 数量仍少；后续扩充独立版本化数据集，但不得修改当前冻结数据集来回写历史指标。

这些风险不再属于未实现的阶段 2 验收能力。最终结论：**阶段 2 毕业；允许开始阶段 3，但阶段 3 尚未启动。**
