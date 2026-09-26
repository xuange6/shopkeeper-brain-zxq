# 阶段 3 交接（2026-09-25）

## 交付摘要

- 阶段：3 企业知识生命周期。
- 达成的用户/系统能力：Source/Document/Revision/SyncRun/Task/Release 持久化；幂等增量、rename/ACL/delete 分类；lease/heartbeat/checkpoint/retry/DLQ/replay/cancel；身份 hash 映射；staging validation、原子 pointer、rollback；三真实数据面删除与 reconciliation；指标、审计、SLO、release guard。
- 阶段结论：生命周期功能与真实数据面 PASS；分布式生产实现已交付，但目标 Kubernetes 集群的 node/AZ/扩缩/托管服务切换仍须上线验收，不能提前标记目标环境 PASS。

## 代码与设计

- 关键文件及职责：见 `STAGE3_ARCHITECTURE_ADR.md` 的“交付与证据”，核心目录为 `knowledge/lifecycle/`。
- 数据模型/API/配置变化：生产主库为 PostgreSQL migrations；新增 `/metrics`、`/api/lifecycle/admin/*`、`LIFECYCLE_DATABASE_URL`、独立 scheduler/worker、动态 active Release 和真实生产存储适配器。
- 参考项目中实际阅读的文件或模块：Onyx connector/db/indexing/celery/deletion/permission sync/swap tests/migrations；Prefect task/flow/state/task engine/concurrency lease/runner/deployment/tests。完整清单和 commit 在 `STAGE3_GITHUB_RESEARCH_20260925.md`。
- 采纳的设计与没有采纳的设计及原因：采纳持久状态、checkpoint、独立 permission attempt、lease/heartbeat、有限重试、crash 区分和测试驱动 swap；未引入 Celery/Prefect Server，lifecycle 继续用 PostgreSQL 队列。Redis 仅用于分布式 API 的短任务状态与 SSE Streams，不是生命周期权威状态或 broker。

## 证据

- 执行的测试和结果：273 passed、66 subtests passed；真实故障演练 15/15 PASS；PostgreSQL 并发/leader failover PASS；Redis 跨进程状态/SSE PASS；真实 Milvus/Neo4j/MinIO ACL/删除验收 PASS。
- 评测数据集版本：`shopkeeper-qa-v0.1.0`，dataset SHA `2ae67e7c4a3c98fa3329cf561553123172bd306b65694ad98dd4da053d724275`，source contract SHA `3839171f53091ac42d220462800a93afabd737064c3fda23920fc20f8b08eae5`。
- 质量、延迟、成本与资源指标：阶段 3 lifecycle ACL 11.052 秒、worker recovery 1.354 秒、rollback 0.035 秒；阶段 2 final service Recall@5 0.944444，引用/Faithfulness/行为/安全/图片 1.0，P50 3606.188 ms、P95 7221.515 ms，81,683 tokens、CNY 0.02392725。
- 已知失败样例：13/15 首次演练、沙箱 service preflight 36 provider errors、首次联网指纹不兼容，以及 contract/replay scope incompatibility 报告均保留。

## 生产边界

- 安全与权限：credential 仅 reference，外部 principal hash，tenant hash 审计；ACL/删除/发布 fail closed；查询继续使用阶段 2 pre-retrieval ACL。
- 可观测性与告警：`/metrics` 和 `config/lifecycle_alerts.json`；审计携带 trace/run/source/document/revision/release/attempt/state/error fields。
- 错误处理、重试与幂等：唯一键、owner lease、有限指数退避、transient/permanent/poison、DLQ 和 checkpoint。
- 数据迁移与回滚办法：见 `STAGE3_MIGRATION_AND_ROLLBACK.md`；不 destructive downgrade，不修改阶段 0–2 资产。
- 尚未验证的假设：真实企业 IdP、目标企业数据 evaluation gate、生产规模长周期 SLO、目标 Kubernetes node/AZ 故障、KEDA 扩缩、托管服务故障切换与跨地域部署。多主机 SQLite 已由 PostgreSQL 方案替代。

## 下一会话输入

- 下一阶段开始前必须知道的事实：阶段 3 生命周期与分布式进程语义已通过；部署到目标 Kubernetes 环境时必须按分布式指南配置外部 HA 服务和 secret manager，并重新跑故障演练与目标数据 gate。阶段 0–3 的历史失败证据与冻结资产不得覆盖或删除。
- `CURRENT_STAGE.md` 已更新为“生命周期实现 PASS / 分布式目标环境待上线验收”。
- 推荐下一会话启动文件：`docs/roadmap/STAGE3_VALIDATION_20260925.md`。
