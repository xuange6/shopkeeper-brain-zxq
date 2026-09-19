# 执行会话 14：综合平台化

主参考：[Dify](https://github.com/langgenius/dify)。

## 会话使命

将前述能力整理为可供团队运营的控制面和数据面，完成 build/buy/adopt 决策。

## 必做任务

1. 阅读 Dify app/workflow、dataset、model provider、tenant/RBAC、plugin、observability 和 deployment 边界。
2. 列出本系统的控制面：租户、用户、知识资产、Agent/Workflow、模型、工具、Prompt、评测、发布、成本和审计。
3. 明确哪些自研、哪些复用开源平台、哪些购买服务；评估许可、迁移、锁定和升级成本。
4. 建立 dev/staging/prod、配置/secret、数据库迁移、灰度、回滚、备份恢复和 SLO。
5. 进行安全评审、故障演练、容量测试和运营流程验收。

## 验收门槛

- 多租户数据和权限隔离通过验证；
- 知识、Prompt、模型、工具和工作流都有版本及发布记录；
- 关键链路有 SLO、告警、值班与恢复手册；
- 平台选型由需求和总拥有成本支持，而不是由功能列表决定。

结束时交接目标架构、采用决策、迁移计划和生产就绪清单。
