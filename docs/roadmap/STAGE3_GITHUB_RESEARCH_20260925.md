# 阶段 3 GitHub 源码调研（2026-09-25）

> 2026-09-26 决策更新：后续完整性审计确认 SQLite 不能满足多 worker 生产目标，因此生产控制面已升级为 PostgreSQL 行锁任务队列；本文关于 SQLite 的内容保留为初始调研结论和历史决策依据。

本记录只使用仓库源码、测试和迁移，不以 README、Star 数或框架名作为架构证据。

## 固定版本

- Onyx：`https://github.com/onyx-dot-app/onyx`，`main` commit `e0de28315672dfb72f13431ac51f057f937d6267`。
- Prefect：`https://github.com/PrefectHQ/prefect`，`main` commit `148e99781fb05df258d3efddb5ef4fee0b61c9f2`。

## Onyx

实际阅读的实现与测试：

- `backend/onyx/connectors/interfaces.py`：`LoadConnector`、`PollConnector`、`SlimConnector`、checkpoint 与 credential provider 边界。解决全量、增量、轻量删除扫描及凭据加载分离问题。
- `backend/onyx/db/models.py`、`db/connector.py`、`db/connector_credential_pair.py`、`db/document_set.py`、`db/index_attempt.py`：Connector/Credential/Pair、Document Set、IndexAttempt、SearchSettings 的持久化与关联。
- `backend/onyx/background/celery/tasks/docfetching/tasks.py`、`task_creation_utils.py`、`docprocessing/heartbeat.py`、`connector_deletion/tasks.py`：轮询、后台任务协调、心跳和连接器删除。
- `backend/onyx/indexing/persistent_indexing.py`、`indexing_pipeline.py`：批次持久化、checkpoint、索引写入边界。
- `backend/alembic/versions/03d710ccf29c_add_permission_sync_attempt_tables.py`：权限同步作为独立、可审计 attempt，而不是 Connector 上的布尔值。
- `backend/alembic/versions/2f95e36923e6_add_indexing_coordination.py`：任务 ID、取消、批次进度、心跳和 stall detection 进入数据库。
- `backend/tests/integration/tests/indexing/test_checkpointing.py`、`test_polling.py`、`test_repeated_error_state.py`：checkpoint 恢复、轮询、重复失败恢复。
- `backend/tests/external_dependency_unit/search_settings/test_index_swap_workflow.py`：切换时数据库与文件资产的联动删除。
- `backend/tests/external_dependency_unit/indexing/test_document_deletion_file_cleanup.py`、`test_docfetching_orphan_cleanup.py`：删除传播与孤儿清理。
- `backend/tests/external_dependency_unit/permission_sync/*` 及 `backend/tests/integration/connector_job_tests/*permission_sync*`：权限同步状态、事务和端到端测试。

采纳：Connector 接口分离全量/增量；checkpoint 安全持久化后才推进；权限同步是独立 attempt；索引任务把心跳、取消和批次进度放进数据库；删除和 swap 测试必须包含对象资产。

未采纳：Onyx 的 Celery/Redis/PostgreSQL/Vespa 完整控制面。当前项目是单站点、单 API 服务，直接引入会增加 broker、beat、worker pool 和数据库迁移面；阶段 3 所需语义可由短事务 SQLite 控制面与可替换 backend 接口满足。未来多节点写入量超过 SQLite 单写者边界时迁移 PostgreSQL，接口不变。

## Prefect

实际阅读的实现与测试：

- `src/prefect/tasks.py`：Task 模板与 TaskRun 分离；输入/代码 cache key；有限指数退避、jitter、timeout、条件重试和结果持久化。
- `src/prefect/flows.py`、`src/prefect/states.py`、`src/prefect/server/schemas/states.py`：Flow/Task 只有运行实例持有状态；`Failed` 与基础设施 `Crashed` 分离。
- `src/prefect/task_engine.py`：timeout、AwaitingRetry、缓存和 concurrency lease 的执行路径。
- `src/prefect/server/database/orm_models.py`、`server/orchestration/core_policy.py`：FlowRun/TaskRun 状态持久化和状态编排规则。
- `src/prefect/concurrency/_leases.py`：并发租约的获取与释放。
- `src/prefect/runner/_scheduled_run_poller.py`、`deployments/runner.py`：deployment 调度、worker 轮询和基础设施启动。
- `integration-tests/test_task_retries.py`、`integration-tests/test_worker.py`、`tests/test_task_engine.py`、`tests/runner/test_runner.py`：重试次数、worker 事件、超时和 crash fallback。

采纳：模板与运行实例分离；每次状态变化持久化；以输入/版本构造幂等键；transient/permanent/poison 分类后条件重试；有上限的指数退避；租约、heartbeat、checkpoint 和 crash recovery；并发限制是资源语义，不是进程内锁。

未采纳：Prefect Server/Cloud、Deployment、Work Pool 和事件自动化控制面。阶段 3 没有跨集群调度或多团队编排需求；引入会把本项目的知识状态机映射成第二套通用状态机，增加部署与故障域。若未来需要跨主机调度，可让 Prefect 调用现有幂等生命周期 API，而不是把领域状态迁入 Prefect。

## 对本项目的结论

阶段 2 已提供稳定 document/revision/chunk identity、ACL 查询前过滤和原子环境切换。阶段 3 应新增领域控制面而不重写检索链：SQLite 持久化 Source/Document/Revision/SyncRun/Task/Release；connector 与 storage 使用 Protocol；Milvus/Neo4j/MinIO 仍是数据面；发布 pointer 和领域数据库在验证后切换；所有外部副作用以幂等键、checkpoint 和 reconciliation 收敛。
