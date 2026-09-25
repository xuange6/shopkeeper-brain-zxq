# 执行会话 02：工业 RAG

主参考：[RAGFlow](https://github.com/infiniflow/ragflow)。实现调研以当前版本的 `rag/nlp/search.py`、`agent/tools/retrieval.py`、`docs/guides/dataset/run_retrieval_test.md`、`docs/guides/dataset/configuration.md` 及相应测试为准，不以 README 功能列表代替源码证据。

## 会话使命

将现有固定 fan-out 查询图改造成可配置、可诊断、可评测的检索系统。

## 必做任务

1. 追踪 RAGFlow 从 dataset/chunk 到 retrieval/rerank/citation 的真实调用链，核对 hybrid keyword/vector 权重、rerank candidate count、metadata filter、threshold、top-k/top-n 和 KG 开关如何进入执行层。
2. 以阶段 1 IR 为输入，为现有 dense、sparse、HyDE、KG、Web 定义统一协议和版本化 `RetrievalPlan`；每一路包含触发条件、预算、超时、错误分类和降级。
3. 将 query rewrite、召回、标准化、去重、融合、重排、截断、引用和拒答拆成可观测步骤；保存候选的原始分数、校准分数、排名变化和淘汰原因。
4. 拆开确定性离线实验与真实在线实验：冻结 Web/模型输出验证检索因果，真实链路用于验证外部依赖、延迟和成本。
5. 扩充阶段 1 的失败集，至少覆盖精确编号、同义词、表格、图片、多轮、跨文档、空答案、权限、注入、Web 波动和通道故障。
6. 比较单路、hybrid、HyDE、KG、Web、RRF/加权融合和 rerank 的增量贡献；禁止为了某一道题临时调阈值。
7. 建立索引版本、检索策略版本、模型/Prompt/费率指纹、灰度、重放和回滚契约。

## 验收门槛

- 检索策略可由配置/查询计划选择，不再对所有问题无条件并发全部通道；
- 结果包含来源、结构位置、各阶段分数、排名变化和最终入选/淘汰原因；
- tenant/ACL/metadata filter 在召回前生效，并有越权与日志泄漏测试；
- 空证据、低置信度、超时、部分通道失败和全部通道失败都有确定行为；
- 用同一 evaluator 和冻结输入证明策略收益，并用真实服务报告质量、延迟、token、调用量和可信成本；
- 阶段 1 的 Precision/MRR/nDCG/引用退化得到修复或可审计归因，不能通过降低门槛隐藏。

## 2026-09-24 中间验收（历史）

状态：**Completed / PASS**。

已交付并复验 `stage2-industrial-rag-v2`：独立意图与安全策略、查询级 RetrievalPlan、加权融合、原始/校准/组合置信度分离、Local-first/Web-fallback、来源 authority/freshness/domain/type 信号、canonical evidence 去重、结构约束补全、Claim-Evidence 引用核验、分操作 token/延迟/费用和多粒度指标。参数集中在查询配置和费率配置中，完整 trace 写入评测快照。

安全层 v2 不是把通用 LLM 放到授权边界，也不是单层正则：先做 NFKC/控制字符规范化、受限 Base64/hex/URL 解码、间隔词和变形词检测，再进行多标签策略判断；Prompt Injection 清洗后重新检查合法业务部分，权限窃取优先拒绝。来自文档、历史和图谱的内容在 rerank 与提示词组装前接受间接注入隔离，模型输出通过凭据 DLP 后才一次性释放，避免流式分片先泄漏后拦截。

安全加固后的最终真实核心对照使用同一代码、模型、Prompt、运行配置、费率和数据契约，仅索引不同：control 8/9，candidate 9/9；candidate 的 Recall@5、引用、Faithfulness、图片、行为和安全均为 1.0，P95 从 12656.597 ms 降至 10379.839 ms，成本从 CNY 0.00524475 增至 CNY 0.00558585，增量 CNY 0.00034110，严格 gate PASS。完整测试 195/195 通过，版本化安全语料 15/15 通过。

独立索引 `kb_chunks_ir_stage2_20260924_v1` 和报告均单独保存，阶段 0/1 证据未覆盖。补充 12 例对照保留为 FAIL，用于跟踪动态 Web 官方来源缺失、语义等价文本计分和 KG schema 缺失风险；没有降低门槛或修改期望来制造通过。

架构决策见 `../STAGE2_ARCHITECTURE_ADR.md`，该次验证和失败归因见 `../STAGE2_VALIDATION_20260924.md`。本节保留当时结论；其后发现的 ACL、KG、Web 和补充门禁缺口仍属于阶段 2，不能顺延给阶段 3。

## 2026-09-25 最终完成状态

状态：**Completed / PASS / GRADUATED**。

阶段 2 重开后的缺口已在本阶段关闭：可信签名 principal/tenant/ACL 进入请求契约，所有本地检索通道执行召回前过滤；重建版本化实体索引和 Neo4j 图并对空图/部分失败 fail-closed；KG evidence 回填完整 document/section/page/block/chunk/source URI 血缘；官方 Web 路由、能力型权威证据保护、公开 claim-evidence binding 和 dataset-SHA 固定的精确 source contract 均已复验。

当前代码的真实 service control/candidate 各执行 12 个用例 × 3 次，均为 12/12、核心 9/9、运行错误 0；candidate 使用未降低门槛的 `evaluation/gate.json` 得到 PASS。candidate Recall@5 与 evidence-group/section/document Recall@5 均为 0.944444，KG Recall@5 为 0.111111，引用、Faithfulness、图片、行为和安全均为 1.0。P95 为 6093.940 ms，相对 control 降低 16.90%；81,523 tokens，CNY 0.02366160，`cost_status=available`。最终完整自动化测试 224/224 PASS。

独立真实 KG lineage 探针召回 22 个 KG evidence，全部具备 document/section 血缘和 `knowledge_graph` 来源类型；索引/ACL 校验中跨租户结果、缺失 ACL 和作用域重复均为 0。最终 baseline 为 `evaluation/baselines/stage2-acl-v2-lineage-final.20260925.service.full.json`。

完整证据、历史 FAIL 清单和保留风险见 `../STAGE2_VALIDATION_20260925.md`。阶段 2 已毕业，阶段 3 尚未开始。
