# 执行会话 00：质量基线与回归门禁

主参考：[Promptfoo](https://github.com/promptfoo/promptfoo)；观测参考：[Langfuse](https://github.com/langfuse/langfuse)。

## 会话使命

为现有完整链路建立可重复、可比较、可阻止退化的基线。先测现状，再改系统。

## 必做任务

1. 盘点现有测试、日志、检索结果、引用字段、模型调用与 CI，画出可观测点。
2. 从真实业务能力构建小而有区分度的版本化 golden set，包含无答案、歧义、多轮、跨文档、表格/图片、时效性、权限与 prompt injection。
3. 区分检索评测和生成评测；记录 Recall@k、MRR/nDCG 或 context precision/recall，以及引用正确性、faithfulness、拒答准确率。
4. 记录 p50/p95 延迟、token、调用次数和估算成本；保留模型、Prompt、索引和配置版本。
5. 接入本地一键运行及 CI 门禁；非确定性判分要有重试、阈值和人工复核入口。

## 重点阅读

- Promptfoo 的配置、assertion/provider、red-team、CI 示例和结果持久化；
- Langfuse 的 trace/span/generation、dataset、score 和 prompt version 数据模型；
- 本仓库查询图、SSE/API 输出和现有 tests。

## 验收门槛

- 干净环境可用单一入口复现同一评测；
- 报告能定位到具体问题、召回证据、答案、引用与链路配置；
- 至少有一个人为注入的退化会被门禁捕获；
- 输出首份现状基线，而不是仅提交评测框架空壳。

结束时按 `docs/roadmap/HANDOFF_TEMPLATE.md` 交接，不进入文档解析改造。
