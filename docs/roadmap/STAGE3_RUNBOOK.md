# 阶段 3 运维 Runbook

## 启动与检查

企业主生产按 `docs/DISTRIBUTED_PRODUCTION_DEPLOYMENT.md` 部署多可用区 Kubernetes 和外部 HA 数据服务。以下直接进程/Compose 步骤只用于本地、边缘或灾备演练。

1. 启动仓库固定的 Compose 基础设施：`docker compose up -d`，确认 PostgreSQL、Milvus、Neo4j、MinIO 健康。
2. 设置 `LIFECYCLE_DATABASE_URL=postgresql://...`、随机高强度 `LIFECYCLE_ADMIN_TOKEN`、数据服务地址和 secret manager reference。生产不得回退到 `LIFECYCLE_DB_PATH`。
3. 分别启动 API、scheduler 和至少两个 worker：`python -m knowledge.main`、`python -m knowledge.lifecycle.runtime scheduler`、`python -m knowledge.lifecycle.runtime worker`。它们可位于不同主机，但必须连接同一 PostgreSQL。
4. 访问 `/health` 与 `/metrics`。有活动版本后 `active_release_health` 必须为 1；凭据不得出现在响应。
5. 管理接口统一位于 `/api/lifecycle/admin/*`，必须携带 `X-Lifecycle-Admin-Token`。未配置 token 时接口按设计返回 503。
6. 对生产 source 只配置 credential reference。配置任意层级含 password/secret/token/api_key/access_key 时创建会被拒绝。

## 同步与任务

- 为每个外部事件使用稳定 idempotency key；重复发送安全。
- 一个 source 同时只允许一个有效 sync lease。冲突不是失败，不得强行覆盖 cursor。
- Worker 每个长步骤写 checkpoint 并续租；启动时自动恢复过期 sync/task lease。PostgreSQL claim 使用 `SKIP LOCKED`，同 source 由 source 行锁和唯一索引排他。
- transient 使用有限指数退避；permanent 直接 failed；poison 和耗尽重试进入 DLQ。
- 重放 DLQ 前先修复根因，调用 `replay_dead_letter` 后 attempt 清零但保留原 task identity/audit。
- 取消先置 `cancellation_requested`，handler 在 checkpoint 边界检查并释放外部资源。

## 发布

1. 先创建 building Release，把 `staging_release_id` 写入 Source 配置；parse 完成后 worker 自动把 staged revision 纳入 manifest。进入 validating 后 manifest 不可变。
2. 新 revision 只写该 Release 的 chunk collection、entity collection、graph version 和 object namespace。
3. `release_validate` 固定校验文档/chunk 数、ACL 完整性、资产后端、KG lineage、评测 gate 和 manifest hash；缺项或异常均 failed。
4. 状态自动到 `ready` 后才可排队 `release_activate`。查询进程按请求读取 PostgreSQL active Release，无需重启。
5. 发布后至少观察两个满足样本量的 canary/shadow 窗口。单信号 pause；两个不同信号连续异常才自动 rollback。
6. 人工接管时启用 manual hold；自动动作后遵守 900 秒 cooldown。

## 删除与对账

- 删除任务先 tombstone，查询立即 fail closed，再传播 metadata/revision/chunks/Milvus/Neo4j/objects/search cache/permission cache/release references。
- 失败任务保留 completed target checkpoint；重试不得重新创建已删资产。
- verified 要求每个 backend 均查询不到 revision；完成前不得清除审计。
- Scheduler 每 15 分钟排队一次 reconciliation。数据库存在而存储缺失时隔离 release；存储孤儿且数据库不存在/已删除时可由显式 repair 任务删除；active release 引用删除 revision 时删除任务 fail closed，必须先发布替代版本或回滚。

## 告警与 SLO

- ACL 撤权 30 秒；删除 60 秒；增量同步 300 秒；worker 恢复 15 秒；切换 10 秒；回滚 30 秒；最大重试窗口 900 秒；孤儿检测周期 900 秒。
- 真实本地验收：ACL 11.052 秒、worker 1.354 秒、rollback 0.035 秒，均满足。
- `dead_letter_total` 增量、`oldest_task_age`、同步失败率、ACL/删除延迟、orphan count 和依赖错误率按 `config/lifecycle_alerts.json` 告警。

## 故障处理

- Milvus/Neo4j/MinIO 不可用：保持 revision staged、task retrying，禁止 release ready；服务恢复后从 checkpoint 重放并查询验证。
- Worker 丢失：等待 lease 过期，reaper 转 retrying；严禁手工把 running 改 succeeded。
- Validation 失败：candidate failed，active 不动。修复后从 building 重建，不能在 active collection 上重建。
- 回滚：确认 previous release 完整，执行 publisher rollback，再做查询、ACL 和资产验证。
- PostgreSQL 故障：停止 scheduler/worker 写入，恢复数据库备份/PITR 和 active pointer 镜像；运行 reconciliation；不得根据数据面猜测并自动激活 release。

## 可重复验收

执行以下三项：

- `python scripts/run_stage3_acceptance.py`：15 项基础设施故障演练。
- `python scripts/run_postgres_lifecycle_acceptance.py`：真实 PostgreSQL migration、并发 claim 和同 source 排他。
- `python scripts/run_production_storage_acceptance.py`：真实 Milvus chunks/entity、Neo4j、MinIO ACL 与删除验证。

脚本使用随机隔离 collection/graph/object namespace，不修改阶段 0–2 集合。生产晋级还必须使用目标环境数据生成新的 evaluation gate 报告。
