# 执行会话 04B：时间上下文图

主参考：[Graphiti](https://github.com/getzep/graphiti)。

## 会话使命

表达“谁、在何时、发生了什么、关系何时有效”，为会变化的事实和事件提供增量、可追溯上下文。

## 必做任务

1. 阅读 Graphiti 的 episode、entity、edge、temporal fields、ingestion、deduplication 和 retrieval 设计。
2. 为商品、门店、客户、活动或政策选择一个动态场景，定义实体、事件、关系、来源、valid time 与 transaction time。
3. 建立实体解析、去重、冲突、失效和溯源规则；导入必须幂等且可重放。
4. 将图检索结果统一为阶段 4C 可消费的 evidence，而不是直接拼 Prompt。
5. 建立“当前事实”“历史时点”“冲突来源”和“被新事实替代”的评测样例。

## 验收门槛

- 能回答当前状态和指定历史时点的问题；
- 新事件不会粗暴覆盖旧事实，冲突来源可见；
- 每条关系可追到文档/事件与导入版本；
- 数据删除和权限撤回能传播到图上下文。

结束时交接图 schema、时间语义和检索契约，不承担指标口径定义。
