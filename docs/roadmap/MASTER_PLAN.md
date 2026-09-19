# Shopkeeper Brain 工业化主路线

更新时间：2026-09-19

## 目标

把当前具备多路 RAG 原型能力的仓库，演进为可评测、可治理、可观测、可持续更新的企业知识与 Agent 系统。路线以交付能力为主线，而不是依次体验热门框架。

## 当前基线判断

仓库已有文档解析、标题切分、BGE-M3 dense/sparse、Milvus、Neo4j、HyDE、Web 搜索、RRF、reranker、LangGraph 和 SSE。主要差距集中在：

- 缺少固定数据集、离线回归、线上反馈闭环和发布门禁；
- 文档结构、表格、图片、页码、来源位置还没有统一的中间表示和可追溯契约；
- 查询路径固定并行展开，缺少意图、预算、权限和风险驱动的上下文规划；
- 文档版本、增量同步、权限继承、删除传播和索引发布生命周期不完整；
- `knowledge/semantic` 尚未成为查询主链路中的业务语义层；
- 任务、状态、审计、监控和故障恢复仍有单机原型边界。

## 主线阶段

| 阶段 | 产出能力 | 主参考项目 | 进入下一阶段的硬门槛 |
|---|---|---|---|
| 0 | 质量基线与回归门禁 | [Promptfoo](https://github.com/promptfoo/promptfoo) | 固定数据集可重复运行；核心指标、失败样例和基线报告可追踪 |
| 1 | 结构化文档 IR | [Docling](https://github.com/docling-project/docling) | PDF/Markdown 的层级、表格、图片、页码与来源可统一表达和回放 |
| 2 | 工业 RAG | [RAGFlow](https://github.com/infiniflow/ragflow) | 检索策略可配置；混合召回、重排、引用和拒答有离线证据 |
| 3 | 企业知识生命周期 | [Onyx](https://github.com/onyx-dot-app/onyx) | 增量同步、版本、ACL、删除传播、重建和发布状态可验证 |
| 4A | 业务语义层 | [Cube](https://github.com/cube-js/cube) | 核心业务概念、指标、维度和同义词有版本化定义与确定性解析 |
| 4B | 时间上下文图 | [Graphiti](https://github.com/getzep/graphiti) | 实体、事件、关系及有效时间可增量更新并用于检索 |
| 4C | 上下文服务 | [LlamaIndex](https://github.com/run-llama/llama_index) | 查询计划能按意图、权限、预算路由语义/RAG/图/工具并输出统一证据包 |
| 5 | 长期记忆 | [Mem0](https://github.com/mem0ai/mem0) | 用户、会话、组织记忆分域；写入、冲突、TTL、遗忘和隐私可控 |
| 6 | 单 Agent 可靠运行时 | [LangGraph](https://github.com/langchain-ai/langgraph) | 状态持久化、恢复、超时、重试、人工中断和幂等可通过故障测试 |
| 7 | 工具治理 | [ToolHive](https://github.com/stacklok/toolhive) | 工具注册、鉴权、隔离、策略、审批和审计形成统一入口 |
| 8 | 多智能体协作 | [DeerFlow](https://github.com/bytedance/deer-flow) | 只有单 Agent 基线证明多 Agent 提升质量或成本收益时才采用 |
| 9 | Deep Research | [Open Deep Research](https://github.com/langchain-ai/open_deep_research) | 规划、搜索、阅读、交叉验证、引用和预算控制能端到端评测 |
| 10 | 高效推理 | [vLLM](https://github.com/vllm-project/vllm) | 用真实负载得到吞吐、首 token、尾延迟、显存和成本基线 |
| 11 | Code Agent | [OpenHands](https://github.com/All-Hands-AI/OpenHands) | 沙箱、事件流、工作区权限、补丁审查和任务评测完整 |
| 12 | 自进化 | [OpenEvolve](https://github.com/codelion/openevolve) | 候选生成、评测、选择、回滚均在沙箱中，有明确收益门槛 |
| 13 | Agentic RL | [Agent Lightning](https://github.com/microsoft/agent-lightning) | 轨迹与训练解耦，奖励可验证，离线收益足以覆盖复杂度 |
| 14 | 综合平台化 | [Dify](https://github.com/langgenius/dify) | 租户、权限、资产、发布、观测、成本和运营形成统一管理面 |

## 横向工业化轨道

这条轨道从阶段 0 开始持续建设，不另等一个“大运维阶段”：

- 评测与红队：[Promptfoo](https://github.com/promptfoo/promptfoo)
- 后台任务和恢复：[Prefect](https://github.com/PrefectHQ/prefect)
- 模型网关和成本路由：[LiteLLM](https://github.com/BerriAI/litellm)
- Trace、Prompt、数据集与反馈：[Langfuse](https://github.com/langfuse/langfuse)
- 工具运行与治理：[ToolHive](https://github.com/stacklok/toolhive)
- 最终管理面参考：[Dify](https://github.com/langgenius/dify)

每阶段都要同时检查安全、可观测性、错误处理、配置、迁移、回滚、成本和运维手册。

## 学习方法

每个参考项目只做四件事：

1. 找到它解决本阶段问题的核心数据模型和边界；
2. 追一条真实请求或任务从入口到持久层的调用链；
3. 阅读相应测试、CI、故障处理和部署配置；
4. 在本仓库实现最小纵向切片，用阶段 0 的数据集证明效果。

完成实现后再读第二个同类项目做反证。禁止把“跑通 Demo”“增加一个依赖”或“README 写了支持”当成阶段完成。

## 顺序与并行规则

- 主依赖顺序：`0 → 1 → 2 → 3 → 4A → 4B → 4C → 5 → 6 → 7`。
- `4A` 管业务定义，`4B` 管动态事实，`4C` 决定本轮调用哪些上下文；三者职责不能合并成一个模糊的“语义模块”。
- 阶段 8–14 是能力扩展，应根据业务压力和评测证据选择，不要求为了路线完整而全部上线。
- 阶段 10 可在推理成本成为瓶颈时提前；阶段 14 可在多团队、多租户和运营需求明确时提前做管理面设计。
