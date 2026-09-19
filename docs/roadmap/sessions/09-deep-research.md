# 执行会话 09：Deep Research

主参考：[Open Deep Research](https://github.com/langchain-ai/open_deep_research)。

## 会话使命

实现可计划、可追溯、会交叉验证且受预算约束的深度研究能力。

## 必做任务

1. 阅读其 planning、search、research loop、source handling、synthesis 与配置实现。
2. 定义研究计划、子问题、停止条件、来源质量、时效性、冲突和引用契约。
3. 复用 Context Service 与工具治理，实施域名/来源策略、预算、缓存、并发和超时。
4. 把事实声明与证据绑定，支持来源冲突提示和证据不足结论。
5. 构建需要多源、多跳、时效核验的问题集，与普通 RAG 对照。

## 验收门槛

- 每个关键结论可追到实际访问的来源；
- 能识别冲突、过期和低质量来源；
- 达到停止条件或预算后可返回部分结果与缺口；
- 质量收益和额外时间/成本均有报告。

结束时交接研究轨迹、来源策略和失败样例。
