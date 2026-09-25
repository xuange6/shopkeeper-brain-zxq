# 当前阶段

## 阶段 2：Completed / PASS

执行文件：`sessions/02-industrial-rag.md`

阶段 2 的遗留缺口属于阶段 2 自身验收范围，现已在阶段 2 内完成修复和复验。2026-09-25 使用当前代码、真实 Milvus/Neo4j/Web/模型服务及原 `evaluation/gate.json`，完成 12 个用例、每例 3 次的 control/candidate 对照；candidate 为 12/12，核心用例为 9/9，gate PASS，允许阶段 2 毕业。阶段 3 尚未开始。

阶段 0、阶段 1 和阶段 2 早期的基线、快照、FAIL 报告及索引均保留。旧报告只代表其生成时的代码和环境，不被改写为当前结果。

## 最终验收结果

- 自动化测试：发布前最终完整运行 230/230 PASS。
- control：12/12；candidate：12/12；两组核心用例均为 9/9；执行次数均为 36，运行错误为 0。
- candidate 的 Recall@5、evidence-group/section/document Recall@5 均为 0.944444；引用正确率、Faithfulness、图片正确率、行为准确率和安全均为 1.0。
- KG 不再是空通道：正式集 `knowledge_graph_recall@5=0.111111`；发布前专用真实探针召回 20 个 KG evidence，且每个探针 evidence 都有 document/section/chunk 血缘及 `knowledge_graph` 来源类型。
- candidate P50 2989.878 ms，P95 5918.720 ms；相对同轮 control 的 6072.658 ms，P95 降低 2.53%，满足最大增加 25% 的门禁。
- control 为 80,482 tokens、CNY 0.02296815；candidate 为 80,275 tokens、CNY 0.02272920，分别降低 0.26% 和 1.04%。`cost_status=available`，真实 CNY 批次预算通过。
- 费率指纹：`dff7920ae0a2f1374786cb4e00169c25ed772923f8c493952fe3af7f348eab24`；发布代码 query pipeline 指纹：`56fdc795661541578271caf3a370eb12b4822d5a0a16597e9f1242f81718b07c`。
- 当前激活集合为 `kb_chunks_ir_stage2_release_20260925`；实体集合为 `kb_graph_entities_stage2_release_20260925`；图版本为 `stage2-acl-kg-release-20260925`。ACL-compatible rollback 集合为 `kb_chunks_ir_stage2_release_control_20260925`。

## 已关闭的阶段 2 缺口

- 可信 principal/tenant/ACL 由服务端签名上下文提供，请求正文不能自报身份；direct、HyDE、实体对齐、Neo4j 扩展和 chunk 回填均在召回前过滤。
- KG schema、版本、约束、实体集合和图数据已重建并实测；KG 回填保留完整 citation lineage。
- 表格负 reranker 原始分不再直接触发拒答；拒答由校准相关性、分差、authority、结构命中和覆盖率共同决定。
- 本地产品/安全资料优先，Web 只按时效意图或本地证据不足路由；普通网页不能证明产品能力，官方域名、日期、漂移和失败可观测。
- 引用按 claim 绑定并在输出前核验；公开 source 只声明自己实际支持的 claims。
- 权限窃取、Prompt Injection、正常业务、知识不足、实体歧义和时效查询走独立策略；注入中的合法业务部分在清洗后继续回答。
- chunk、canonical evidence-group、section 和 document 指标并存，结构变化通过 dataset-SHA 固定的精确 source contract 审计，不通过复制 chunk 提分。
- 发布清单和原子切换脚本已经真实完成 candidate → rollback → candidate 往返；每次应用前备份 `.env`，且只允许修改四个索引/图谱白名单键。

## 保留风险

- Web 与生成模型仍是外部动态依赖；三次实验降低了偶然性，但不能消除长期漂移，阶段 3 应纳入发布监控和回滚。
- 当前确定性安全边界经过对抗回归，但不声称覆盖所有未知攻击；专用 guard model 可作为后续 shadow 信号，不能替代确定性授权。
- 费率配置有来源和生效日期；供应商、区域或模型价格变化时必须更新配置并生成新指纹。
- 当前正式数据集中只有一个用例产生可匹配的 KG Top-5 命中；已增加独立真实 KG 探针，后续应扩充版本化 KG golden set，但这不再是空图或空通道问题。

## 最终证据

- `docs/roadmap/STAGE2_ARCHITECTURE_ADR.md`
- `docs/roadmap/STAGE2_VALIDATION_20260925.md`
- `evaluation/results/stage2-acl-v2-release-control.20260925.service.full.json`
- `evaluation/results/stage2-acl-v2-release-candidate.20260925.service.full.json`
- `evaluation/results/stage2-acl-v2-release-kg-lineage-probe.20260925.service.json`
- `evaluation/baselines/stage2-industrial-rag-v2.0.0.20260925.service.full.json`
- `evaluation/source_contracts/stage2-ir-v4.json`
- `output/stage2-acl-release.index-audit.20260925.json`
- `output/stage2-acl-release-candidate.index-verification.20260925.json`
- `output/stage2-acl-release-control.index-verification.20260925.json`
- `config/releases/stage2-industrial-rag-v2.json`

## 下一步

阶段 2 已毕业，阶段 3 可作为新的独立阶段启动；不得把阶段 2 的历史 FAIL 报告删除或回写为 PASS。
