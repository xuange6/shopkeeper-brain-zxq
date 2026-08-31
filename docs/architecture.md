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

任务状态和 SSE 队列目前是进程内实现，适合单机、单 Worker 演示。生产部署的下一步是：

1. Redis 持久任务 + Pub/Sub；
2. Celery / RQ / Dramatiq worker；
3. document_id + version + staging/active/retired 两阶段发布；
4. Prometheus 指标与 OpenTelemetry trace；
5. 外部依赖超时、重试和熔断。

## 配置原则

经验参数统一由环境变量覆盖，默认值只是可解释的起点。不要把示例中的召回率、QPS 或用户规模当作本项目实测结论；所有数字都应从自己的验证集和部署环境中产生。
