# 当前阶段

## 阶段 0 执行完成，待总控审查 — 质量基线与回归门禁

执行文件：`sessions/00-quality-baseline.md`

选择它作为起点，是因为后续每一次“更先进”的改造都必须回答三个问题：质量是否提高、代价是多少、哪些样例退化。没有基线，替换切分、检索、语义路由或 Agent 架构都只能靠感觉判断。

### 本阶段完成定义

- 建立覆盖业务问答、歧义、无答案、时效性、表格/图片、权限和注入攻击的版本化数据集；
- 一条命令执行端到端回归，并保存配置、模型、索引版本和结果；
- 至少统计检索与回答两层指标，包括 Recall/Precision、引用正确性、faithfulness、拒答和延迟/成本；
- CI 对稳定且低成本的核心集执行门禁，较贵的完整集可定时执行；
- 记录当前主链路的首份基线报告，不为得到好分数而先改检索逻辑。

### 冻结范围

本阶段允许为可测试性增加接口、fixture、日志字段和评测适配器。除阻断评测的缺陷外，不重写解析、检索或 Agent 架构。

### 完成后的动作

执行会话按 `HANDOFF_TEMPLATE.md` 提交交接记录。总控会话审查证据后，把 Active 更新为阶段 1。

### 当前执行状态（2026-09-19）

阶段 0 的执行项已经完成，但本文件不提前把 Active 切换到阶段 1，等待总控审查交接证据。
Milvus、Neo4j、MongoDB、Web MCP、LLM、embedding 和 reranker 预检均已通过；HAK 180
版本化语料已用幂等 fixture 恢复到 Milvus。真实 core 主链路 9 个样例全部完成且无静默降级，
`baseline_eligibility.rag_quality=true`，快照和首份基线已写入：

- `evaluation/snapshots/stage0-full-pipeline.core.jsonl`
- `evaluation/baselines/stage0-current.core.json`
- `evaluation/baselines/stage0-current.core.md`

当前现状为 2/9 通过、Recall@5=0.8333、MRR/nDCG@5=0.8333、p50=3132.621 ms、
p95=15583.487 ms；低分与失败样例原样保留。Neo4j 可连接但 HAK 180 图数据为空，
因此 knowledge_graph_recall@5=0，这是后续阶段的输入，不在阶段 0 为提高分数而改造。
离线 contract 门禁 8/9 且 PASS，真实快照 replay 门禁 2/9 且 PASS，测试 27/27 通过。
