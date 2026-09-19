# 执行会话 03：企业知识生命周期

主参考：[Onyx](https://github.com/onyx-dot-app/onyx)；任务参考：[Prefect](https://github.com/PrefectHQ/prefect)。

## 会话使命

让知识从一次上传变为可持续同步、授权、发布、回滚和删除的企业资产。

## 必做任务

1. 阅读 Onyx connector、document set、permission sync、indexing pipeline、background jobs 和 deletion 相关实现。
2. 设计 source/document/version/index-run 状态机，以及 staging、active、failed、retired 状态。
3. 支持幂等增量导入、变更检测、断点续跑、重试、死信、重建、原子发布和回滚。
4. 将 ACL/tenant/source metadata 从导入传播到每个检索通道；验证删除能传播到对象存储、向量库和图数据库。
5. 为任务积压、失败率、处理时延、孤儿数据和权限同步建立指标。

## 验收门槛

- 同一来源重复同步不产生重复知识；
- 发布中失败不会污染 active 索引；
- 删除与权限撤回在约定窗口内对查询生效；
- Worker 重启后任务可恢复，状态和错误可审计。

结束时交接状态机、恢复演练和数据迁移方法。
