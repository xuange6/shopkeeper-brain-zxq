# ADR：阶段 3 企业知识生命周期

- 状态：Accepted / Production implementation validated locally
- 日期：2026-09-26
- 控制面 schema：PostgreSQL `0001_stage3_lifecycle` + `0002_source_task_exclusion`

## 决策

采用“持久化领域控制面 + 不可变数据面发布”架构：

`Source → SyncRun → Document/Revision → durable Task → staging stores → validation → atomic Release pointer → active → retire/delete/reconcile`

生产控制面使用 PostgreSQL；scheduler 和任意数量 worker 通过数据库任务队列协作，claim 使用行锁与 `SKIP LOCKED`，同时锁 source 行并用部分唯一索引双重保证同 source 排他。SQLite WAL 仅作为本地开发/测试兼容后端。控制面持久化 source cursor、文档身份、revision fingerprint、任务租约/checkpoint/DLQ、release manifest、删除进度、身份映射、指标事件和审计。Milvus、Neo4j、MinIO 是数据面，新增内容只写 release staging 命名空间；active collection 禁止破坏性重建。

没有引入 Prefect 或 Celery，也没有把 Redis 作为 lifecycle broker。生命周期任务语义继续由 PostgreSQL 租约队列完整覆盖。分布式 API 另行使用 Redis Hash/Streams 保存短查询状态和 SSE 重放，使 POST 与 stream 请求可以落在不同 Pod；Redis 故障不会改变 Source/Revision/Release 权威状态。仓储和 connector 协议不绑定 PostgreSQL，方便未来按规模替换。

查询服务每个请求从控制面读取唯一 active Release，因此发布后无需重启进程或刷新全局配置缓存。Kubernetes 生产设置 `LIFECYCLE_RELEASE_POINTER_MODE=database`，不写节点本地 pointer；文件 pointer 只保留给本地/灾备演练。

企业生产部署采用多可用区 Kubernetes：API HPA 3–12、两个 scheduler（PostgreSQL advisory lock 单活）、KEDA worker 2–30、PDB、topology spread、NetworkPolicy、非 root/read-only root filesystem。PostgreSQL、Redis、Milvus、Neo4j、MongoDB 和对象存储均为外部高可用服务，不在应用清单里伪装成单副本生产数据库。详见 `docs/DISTRIBUTED_PRODUCTION_DEPLOYMENT.md`。

## 数据模型

- `sources`：tenant、connector type、credential reference、配置版本、cursor、同步/权限策略、状态和调度时间。普通配置拒绝 `password/secret/token/api_key` 字段。
- `documents`：稳定业务 identity、source item/path、active revision、tenant/ACL、tombstone、首次/最后发现时间。
- `document_revisions`：content/metadata/ACL hash、parser fingerprint、来源修改时间、状态、change kind、lineage、控制面 revision 到数据面 revision/release 的绑定、激活/退役时间。唯一键阻止重复 revision。
- `sync_runs`：触发、cursor 前后、计数、状态、重试、lease/heartbeat/checkpoint、错误分类和时间。`source_id + idempotency_key` 唯一；同 source 仅一个活动租约。
- `lifecycle_tasks`：payload、状态、attempt/max、lease、heartbeat、available_at、checkpoint、取消、错误分类和 DLQ。
- `index_releases`：完整 manifest、各数据面命名空间、evaluation report、manifest hash、previous release、激活/回滚时间；仅一个 active。
- `identity_mappings`：只保存外部 principal hash 与内部 principal；完整外部身份不进入日志。
- `deletion_jobs`：detected→tombstoned→propagating→verified→completed/failed，保存逐 backend checkpoint 与验证结果。
- `audit_events`：trace/run/source/tenant hash/document/revision/release/attempt、状态前后、错误分类；幂等唯一键阻止重复审计。

## 状态机与事务边界

合法迁移由 `knowledge/lifecycle/state_machines.py` 单点定义，非法迁移抛出 `InvalidTransition`。

- SyncRun：`pending→running→succeeded|partially_failed|retrying|failed|cancelled|dead_lettered`；scheduler/worker/reaper/operator 触发。租约获取、状态写入和审计同事务；cursor 只与成功终态同事务推进。
- Revision：`discovered→processing→staged→validated→active→retired|deleted`，失败可回到 processing；sync/worker/publisher/deletion 触发。active revision 替换和旧 revision retire 同事务。
- Release：`building→staging→validating→ready→activating→active`，失败保持旧 active；`active→rolling_back→rolled_back` 并恢复 previous；builder/validator/publisher/rollback/operator 触发。
- Task：`pending|retrying→running→succeeded|retrying|failed|cancelled|dead_lettered`，DLQ 可由 operator 安全 replay。lease owner 是 worker 写入 checkpoint/终态的前置条件。
- Deletion：`detected→tombstoned→propagating→verified→completed`，任何外部失败进入 failed，重试从已完成 backend checkpoint 继续。

每个迁移必须带 actor 与 idempotency key，并在同一数据库事务记录审计；外部 I/O 不伪装成数据库原子事务，而由 checkpoint、验证和 reconciliation 实现最终收敛。

## 增量与权限

Local connector 支持全量快照、cursor、content/metadata/ACL 独立 fingerprint。相同事件、相同 revision 和任务均有唯一键。唯一的“旧路径缺失 + 新路径出现 + content hash 相同”被识别为 rename，保留 document identity；歧义时不猜测。ACL-only revision 只有在前一 revision 已绑定真实数据投影时才跳过解析，否则强制重建，避免对不存在的数据宣称撤权成功。

外部 IdP 使用 `IdentityConnector`；验收使用 `StaticIdentityConnector`，后续可替换企业 IdP。撤权按 permission/search cache → Milvus entity → Neo4j → Milvus chunks → objects 顺序 fail closed；查询继续在 Milvus/Neo4j 召回前应用 tenant/ACL 过滤。私有资产只写 `minio://` 内部 URI，不生成可直接访问的 HTTP URL。原始外部 principal 只计算 hash，不写审计。

## 发布、监控与自动回滚

Release 必须通过 document/chunk、ACL、资产、KG lineage、evaluation gate 和 manifest 七个固定检查，缺一即 failed；通过后自动进入 ready。激活在控制面事务内同时切唯一 active Release、激活所含 revision、退役旧 revision。`AtomicReleasePointer` 使用同目录临时文件和 `os.replace` 保存兼容镜像。Validation 失败时旧 active 不动；回滚恢复 previous Release。

`ReleaseGuard` 需要至少 50 个样本、连续两个窗口和至少两个不同信号才自动 rollback；单信号只 pause，单个 LLM 随机结果不能触发回滚。支持 15 分钟 cooldown 和人工 hold。阈值/告警在 `config/lifecycle_alerts.json`。

## 安全与边界

- credential 仅接受 `secret://`、`vault://`、`env://`、`keyring://` reference；配置中任意嵌套 password/secret/token/api_key/access_key 都会被拒绝。
- tenant 只记录 hash；ACL、删除、切换失败均 fail closed。
- 管理 API 在未配置 token 时返回 503，错误 token 返回 401，并且不回显 credential reference。
- PostgreSQL 方案支持多进程/多主机，但不是跨地域共识系统；跨地域需单写 region 或升级控制面。
