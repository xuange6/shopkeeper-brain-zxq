# 执行会话 06：单 Agent 可靠运行时

主参考：[LangGraph](https://github.com/langchain-ai/langgraph)。

## 会话使命

把已有 LangGraph 用法升级为可持久、可恢复、可审计的单 Agent 执行系统。

## 必做任务

1. 阅读 LangGraph state、checkpoint、store、interrupt、streaming、subgraph 和 durable execution。
2. 收敛 Agent state schema，区分输入、计划、证据、工具结果、输出、错误与审计事件。
3. 为每个有副作用节点定义幂等键、超时、重试、补偿和恢复策略。
4. 加入人工审批/澄清中断、取消、重新开始和断点恢复。
5. 用故障注入验证进程重启、依赖超时、重复消息和部分执行。

## 验收门槛

- 任务状态脱离单进程内存并可恢复；
- 重放不会重复执行外部副作用；
- 每次决策、工具调用、错误和人工操作可追踪；
- 任务成功率、步骤数、延迟和成本进入阶段 0 评测。

结束时交接状态图、恢复演练和运行手册。
