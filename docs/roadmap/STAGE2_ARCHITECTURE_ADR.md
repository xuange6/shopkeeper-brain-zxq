# ADR：阶段 2 工业级 RAG 决策链

- 状态：Final Accepted（完整阶段 2 门禁通过）
- 初始日期：2026-09-24；最终复验：2026-09-25
- 策略版本：`stage2-industrial-rag-v2` / `intent-policy-v2` / `retrieval-acl-v1`

## 决策

查询链拆为十个可独立观测的决策面：

1. `IntentPolicy` 先对输入做 NFKC、HTML entity、零宽/控制字符和空白规范化，并在严格大小/数量上限内检查 URL、Base64 和 hex 载荷；随后结合精确规则、压缩 token、变形词相似度和上下文豁免产生多标签风险信号。这不是单层正则匹配。
2. 策略同时区分权限/秘密窃取、Prompt Injection、正常业务、知识不足、实体歧义、时效问题与安全教育。注入内容被移除后，合法业务片段必须重新接受完整安全检查；若仍包含凭据窃取，权限拒绝优先。通用 LLM 的分类结果不具备授权效力。
3. API 请求正文不能自报 tenant、role 或 ACL；可信 principal 来自服务端签名上下文。direct、HyDE、实体对齐、Neo4j 扩展和 KG chunk 回填均在召回前组合 tenant、visibility、reader ACL 和 graph version filter。`RetrievalPlan` 再根据意图和查询特征选择 direct、HyDE、KG、Web 通道、预算、超时及权重。普通产品与安全说明采用 Local-first；本地证据不足才允许 Web fallback；时效问题可要求 Web。
4. 各检索通道保留原始候选和失败/跳过原因。加权 RRF 使用集中配置，不再把所有问题无条件 fan-out 到所有通道。
5. 文档、历史与 KG 都被视为不可信输入。候选在 rerank 前执行间接 Prompt Injection 检查并记录 `security_filter`；通过的内容在组装答案上下文时再次检查，避免恶意片段进入模型提示词。
6. rerank 同时保留模型原始分数和 sigmoid 校准置信度；排序再加入 authority、freshness、source type、结构命中与 evidence coverage。Web 普通页面不能靠动态排名挤掉本地产品说明或警告。
7. 拒答只使用组合后的 `evidence_decision`：Top-1 校准分、Top-1/Top-2 间隔、来源权威性、结构匹配和证据覆盖率。未经校准的 reranker 原始分不再作为生产拒答阈值。
8. 对用途、边界和安全类问题，在回答生成后、引用核验前，从最高排名证据中确定性补全明确写出的同级结构约束；补全内容仍必须逐 claim 通过后续证据核验，不能凭常识扩写。
9. 回答拆为 claim，每个 claim 必须通过具体 evidence 核验；证据保留 document、revision、section、page、block、chunk、lineage 和 source URI。未获支持的 claim/citation 在输出前删除或降级。最终文本再经过凭据 DLP；流式模型 token 在验证完成前缓冲，不能先向客户端发送后再拦截。
10. 所有模型调用按 query rewrite、HyDE、KG、rerank 和 answer 分项记录次数、输入/输出 token、延迟与费用，并执行单请求和评测批次预算。

## 配置与可观测性

策略参数集中在 `knowledge/processor/query_process/config.py`；费率在 `config/model_pricing.json`，业务节点不包含价格常量。安全诊断仅保存规则 ID、标签、计数与规范化元数据，不保存解码后的潜在秘密。评测快照保存策略/评测器/费率指纹、币种、生效日期、模型和索引版本。

公开响应只暴露最终引用；诊断信息另外保存 retrieval plan、各阶段候选、排序变化、校准输入、过滤理由、最终 evidence decision、citation verification 和分操作资源统计。这样既不把无效候选冒充引用，也不丢失故障复盘所需的完整 trace。

## 评测语义

继续保留 chunk 级 Recall/Precision/MRR/nDCG 用于检索诊断，同时增加 canonical evidence-group、section 和 document 级 Recall/Precision。canonical evidence 优先由 source URI 或 document+section 生成，重复警告片段只算一个证据组；不会重新制造重复 chunk 来提高 Precision。

确定性 contract 只验证行为与数据契约，不作为真实 RAG 质量基线。毕业门禁来自同数据、同费率配置下的真实 control/candidate 完整管道对照；补充用例即使不属于核心毕业集，其失败报告也必须保留。

## 索引与回滚

阶段 2 最终 candidate 使用独立 chunk 集合 `kb_chunks_ir_stage2_acl_v2_20260925`、实体集合 `kb_graph_entities_stage2_acl_v2_20260925` 和图版本 `stage2-acl-kg-v2-20260925`。导入器保留稳定 document/revision/section/block/chunk 身份，KG 导入对空结果和部分失败 fail-closed，并建立 tenant+graph+item+entity/chunk 作用域唯一约束。阶段 0、阶段 1 和阶段 2 早期索引及失败报告均未覆盖。最终真实门禁通过后，当前配置切换到该版本；发布监控、灰度和原子回滚属于阶段 3，但不再以此推迟阶段 2 的 ACL/KG 正确性。

## 决策验证

2026-09-24 的 9/9 核心 PASS 和 11/12 补充 FAIL 保留为中间证据，见 `STAGE2_VALIDATION_20260924.md`。遗留项在阶段 2 内关闭后，2026-09-25 使用当前代码执行真实 service control/candidate：两组均为 12/12、核心 9/9、每组 36 次执行且错误为 0；candidate 在未降低 `evaluation/gate.json` 门槛的情况下 PASS。candidate 的 Recall@5 与 evidence-group/section/document Recall@5 均为 0.944444，引用、Faithfulness、图片、行为和安全均为 1.0，KG Recall@5 为 0.111111；P95 相对 control 降低 16.90%，token 降低 0.51%，费用降低 3.63%。完整测试为 224/224。详细证据见 `STAGE2_VALIDATION_20260925.md`。

## 外部实现参考

- RAGFlow `rag/nlp/search.py`：借鉴其混合检索权重、候选池、归一化 rerank 分数以及检索与过滤分层，但保留本项目已有图和数据模型。
- RAGFlow `agent/tools/retrieval.py`：借鉴工具层显式暴露 threshold、top-N、candidate count、keyword weight、KG 与 metadata filter 的做法。
- RAGFlow `docs/guides/dataset/run_retrieval_test.md`：借鉴上线前独立验证检索配置的流程。
- Haystack `haystack/components/evaluators/document_mrr.py`：借鉴按内容、ID 或 metadata field 比较文档的思路，扩展为本项目 evidence-group/section/document 多粒度指标。
- OWASP [LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)：采用规范化、编码检测、输入/输出检查、最小权限和分层防御原则；没有把黑名单宣称为完备防护。
- OWASP [AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html)：采用“不可信外部内容不能直接成为指令”和输出验证原则。

上述参考是具体文件级设计借鉴，没有引入框架重写现有系统。

## 安全边界

`intent-policy-v2` 是低延迟、可审计、默认拒绝的确定性策略边界，不保证识别所有自然语言攻击。`retrieval-acl-v1` 独立承担授权边界：只有服务端签名身份能声明 tenant、subject、role 和 group，所有本地通道在召回前 fail-closed 过滤；日志只记录主体哈希和计数。未来可增加专用 guard model 作为 shadow/升级信号，但不能让概率模型单独批准读取敏感资源。外部身份提供者接入和权限生命周期可在阶段 3 扩展，当前阶段 2 已完成可信查询契约和检索前 ACL enforcement。
