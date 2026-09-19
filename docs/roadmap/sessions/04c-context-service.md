# 执行会话 04C：上下文服务

主参考：[LlamaIndex](https://github.com/run-llama/llama_index)。

## 会话使命

把业务语义、文档证据、时间图、记忆和工具组织为统一的上下文规划与供应层。

## 必做任务

1. 阅读 LlamaIndex retriever、router、query engine、node/postprocessor、workflow 与 response synthesizer 的接口分层。
2. 定义 `QueryPlan`、`ContextRequest`、`Evidence`、`ContextBundle` 和 `ContextPolicy`，包含身份、权限、时间、预算、来源和 token 限额。
3. 由查询计划按需路由 semantic/RAG/graph/web/tool，支持并行、依赖、超时、降级、去重和证据排序。
4. 将证据选择与最终生成解耦；所有上下文片段保留 provenance、权限和选择原因。
5. 对路由正确性、上下文污染、越权、token 预算、延迟和缺失依赖建立测试。

## 验收门槛

- 简单问题不再无条件 fan-out 所有通道；
- 答案能解释用了哪些上下文、为何选择以及哪些来源失败；
- 越权证据不能进入 bundle 或日志泄漏；
- 阶段 0 数据证明新路由的质量/延迟/成本权衡。

结束时交接统一上下文协议。这是语义层进入 Agent 之前的边界。
