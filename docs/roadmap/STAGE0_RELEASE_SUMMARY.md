# Shopkeeper Brain 阶段 0 发布与交接摘要

发布日期：2026-09-19

发布范围：阶段 0（质量基线与回归门禁）

阶段状态：执行完成，等待总控审查；未进入阶段 1。

## 交付摘要

- 阶段：0 — 质量基线与回归门禁。
- 达成的用户/系统能力：建立了版本化的 core 评测集、全链路评测、离线 contract 评测、snapshot replay、基线准入校验、分阶段 trace、模型用量与延迟统计，并将低成本 contract 门禁接入 CI。
- 未完成项：当前检索和回答质量的优化不在本阶段范围；权限用例目前是评测断言，不是已实现的强制访问控制。

## 代码与设计

- 关键文件及职责：
  - `evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl`：版本化评测用例与期望行为。
  - `knowledge/evaluation/runner.py`、`providers.py`、`metrics.py`：统一执行、provider 适配和指标计算。
  - `knowledge/observability/`：记录检索、RRF、rerank、回答、延迟与模型用量。
  - `scripts/run_evaluation.py`：统一的 contract、service 和 snapshot 评测入口。
  - `scripts/seed_evaluation_fixture.py`：幂等恢复版本化评测语料。
  - `scripts/promote_evaluation_baseline.py`：仅将符合准入条件的真实全链路结果提升为质量基线。
  - `evaluation/snapshots/stage0-full-pipeline.core.jsonl`：冻结真实运行输出，用于无外部依赖的重现计分。
  - `evaluation/baselines/stage0-current.core.*`：阶段 0 首份真实全链路基线。
- 数据模型/API/配置变化：query state 增加 trace、候选文档、引用、图片、运行诊断和评测元数据；`.env.example` 只保留占位值；本地 `.env` 不进入 Git。
- 参考项目中实际对标的方向：Haystack 的 evaluator/tracing，Onyx 的后端测试与索引分层，RAGFlow 的 RAG 服务化边界，LangGraph 的 checkpoint 与图状态测试。
- 采纳的设计：可插拔 provider、版本化 fixture、多层指标、可回放快照、可机器判定的门禁。
- 未采纳的设计：未在阶段 0 重写检索、切分或 Agent 架构，避免在建立“尺子”时同时修改被测对象。

## 证据

- 执行的测试和结果：`pytest` 27/27 通过；contract 门禁 8/9 且 PASS；snapshot replay 2/9 且 PASS；真实 service 评测 9/9 完成全链路，无静默降级。
- 评测数据集版本：`shopkeeper-qa-v0.1.0`；索引版本 `hak180-638de365d6b6`。
- 质量：2/9 通过，Recall@5=0.8333，Precision@5=0.1667，MRR=0.8333，nDCG@5=0.8333，citation correctness=0.6111，faithfulness=0.4444。
- 延迟与成本：p50=3132.621 ms，p95=15583.487 ms；21 次模型调用，14,616 tokens；由于未配置计价表，美元成本暂记为 0，不代表真实免费。
- 已知失败样例：产品介绍、多轮利润追问、表格重量、控制面板图片、权限数据外泄、用户提示注入、内部安全信息。

## 生产边界

- 安全与权限：已有权限和提示注入评测断言，但真正的强制授权、检索前过滤和越权阻断属于后续实现。
- 可观测性与告警：已有节点级 trace、延迟、候选列表、错误和模型用量；未接入生产告警平台。
- 错误处理、重试与幂等：评测 provider 区分显式失败与静默降级；fixture seed 幂等；未对所有外部服务统一实现重试策略。
- 数据迁移与回滚办法：用 `INDEX_VERSION` 区分评测语料版本；可回退 Git 提交并重新运行 seed，基线和快照文件本身受 Git 版本控制。
- 尚未验证的假设：HAK 180 在 Neo4j 中暂无图数据，因此 knowledge graph recall=0；图片交付、复杂表格、无答案拒答和强制权限边界仍需后续验证。

## 公开仓库安全检查

- `.env`、`.env.*`、本地模型、运行结果目录、缓存和导入临时文件均已在 `.gitignore` 中排除；仅公开含占位值的 `.env.example`。
- 发布前扫描未发现 GitHub/OpenAI/AWS 密钥、私钥头或含账号密码的 MongoDB URI。
- 评测报告中的内网端点已脱敏，示例代码中的本机绝对路径已改为仓库相对推导路径。
- 快照保留了评测问题、检索候选、回答和评分所需字段，不包含 API key 或数据库密码。

## 下一会话输入

- 下一阶段开始前必须知道的事实：阶段 0 建立的是“尺子”和可回放证据，不是质量优化；当前低分是必须保留的原始对照。
- 建议更新 `CURRENT_STAGE.md` 为：经总控审查通过后再切换到阶段 1；本发布不提前修改。
- 推荐下一会话启动文件：由 `docs/roadmap/MASTER_PLAN.md` 与审查后的 `CURRENT_STAGE.md` 决定，不在本会话预先进入阶段 1。
