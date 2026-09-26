# Architecture notes

这份文档把第三章“知识库项目”的工程经验映射到本仓库实现。附件中的文字仅作为技术参考；它不是运行时指令，也不会被系统当作 Prompt 执行。

## 为什么保留现有 LangGraph

项目已经具备一条完整的多路 RAG 主链路，重写框架本身不会自动提升答案质量。因此本版本选择保留节点边界，把质量和可运维性补到接口层：

- 导入节点负责“解析 → 结构化 → 向量化 → 入库”；
- 查询节点负责“改写 → 并行召回 → 融合 → 精排 → 生成”；
- Service 层负责任务生命周期、SSE、错误隔离和 API 结果整形；
- 前端只消费稳定的结构化字段，不再解析内部向量或日志文本。

## 数据流

### Ingestion

    upload
      └─ task_id + sha256 + document_id
          └─ MinIO 原件（documents/<task_id>/<filename>）
              └─ Entry
                  ├─ PDF → MinerU → Markdown
                  └─ Markdown
                      └─ 图片上下文 / URL
                          └─ 标题层级切片（1200 / 300 / overlap）
                              └─ 商品名识别
                                  └─ BGE-M3 dense + sparse
                                      ├─ Milvus chunks
                                      └─ Neo4j entities / relations（可选）

### Query

    question + history
      └─ item_name confirm / rewrite
          ├─ direct hybrid vector search
          ├─ HyDE hybrid vector search
          ├─ Neo4j graph search
          └─ MCP web search
              └─ RRF(k=60, candidates=20)
                  └─ BGE rerank
                      └─ cliff cutoff (6–15)
                          └─ grounded answer + [n] citations
                              └─ SSE / JSON sources / diagnostics

## 可信回答边界

1. 空证据时拒答；
2. 有精排分数且最高分低于阈值时拒答；
3. Prompt 将检索内容明确标为“不可信资料”，禁止执行资料中的指令；
4. 引用只来自结构化 sources，前端展示文件、章节、片段和分数；
5. 图片通过 image_urls 结构化返回，同时兼容旧的“【图片】”文本标记。

这套策略不能替代评测集。上线前应使用业务问题、人工标注答案和线上 bad case 校准：

- faithfulness
- answer relevancy
- context precision
- context recall
- answer correctness

## 当前运行边界

本地模式的短时任务/SSE 使用进程内通道；Kubernetes 分布式模式使用 Redis Hash/Streams，使任务状态和 SSE 可跨 API Pod 读取。旧 `/upload` 的进程内后台导入在分布式模式下被禁用，生产导入统一走 durable lifecycle Source。企业知识生命周期使用 PostgreSQL 持久控制面和数据库任务队列。独立 scheduler 与多个 worker 通过 advisory leader lock、行锁、lease、checkpoint、重试和 DLQ 协作，同一 source 排他处理。文档 revision 写入不可变 staging Release，经过硬校验后原子激活；ACL、删除传播、reconciliation、Prometheus 指标和自动回滚均在阶段 3 接通。

当前生产边界是单 region、多可用区 Kubernetes；跨地域 active-active、外部 IdP、集中 OpenTelemetry collector、网关限流和平台级 secret manager 由目标环境提供。主生产入口见 `docs/DISTRIBUTED_PRODUCTION_DEPLOYMENT.md`，单机演练见 `docs/PRODUCTION_DEPLOYMENT.md`。

## 配置原则

经验参数统一由环境变量覆盖，默认值只是可解释的起点。不要把示例中的召回率、QPS 或用户规模当作本项目实测结论；所有数字都应从自己的验证集和部署环境中产生。
