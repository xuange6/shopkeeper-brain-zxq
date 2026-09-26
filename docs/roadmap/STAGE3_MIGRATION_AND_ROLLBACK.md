# 阶段 3 数据迁移与回滚

## 向前迁移

生产迁移位于 `migrations/postgres/`。`0001_stage3_lifecycle` 创建领域表，`0002_source_task_exclusion` 为 running task 增加同 source 部分唯一索引；构造 `PostgresLifecycleStore` 时按版本幂等执行并记录到 `schema_migrations`。根目录/`migrations/sqlite/` 只服务本地兼容后端。

阶段 2 活跃集合不会复制、覆盖或删除。首次上线步骤：

1. 备份 `knowledge/.env`、active release pointer，并为 PostgreSQL 创建可验证的全量备份/PITR 恢复点。
2. 设置 `LIFECYCLE_DATABASE_URL` 后应用 migration，注册每个 Source；credential 只写批准的 secret manager reference URI。
3. 把阶段 2 当前 active 集合作为 bootstrap Release manifest，状态保持 active。
4. 对已有文档生成稳定 SourceItem/Document/Revision 映射；先 dry-run 对账，再写控制面。
5. 后续 revision 只构建 staging；完整验证后切 pointer。

## 应用回滚

代码回滚不删除 lifecycle tables。旧代码可以忽略这些表，阶段 2 `.env` 仍指向原 active collections。先暂停 source/worker，再恢复 `.env` 备份并重启查询服务。

## Release 回滚

`ReleasePublisher.rollback` 需要 candidate 的 `previous_release_id` 指向完整 retired release。它先原子恢复 previous pointer，随后把 candidate 标记 rolled_back、previous 标记 active。之后必须运行查询、ACL、KG lineage 和对象资产验证。

## Schema 回滚

迁移不提供自动 destructive downgrade。回滚应用版本时保留 PostgreSQL schema，让旧版本忽略新增表/索引；确需恢复时使用上线前备份或 PITR 到新数据库并切换 DSN。不得直接 DROP 表；含审计、DLQ 或 deletion checkpoint 的数据库必须长期保留。

## 失败恢复

- 数据面成功、控制面失败：reconciliation 将数据识别为 storage orphan，隔离或删除。
- 控制面成功、数据面失败：revision 保持 staged/failed，release 不可 ready；任务从 checkpoint 重试。
- pointer 已切、控制面提交失败：恢复 previous pointer并运行 reconciliation；禁止流量继续使用无 active 审计的 pointer。
