# Evaluation baseline

阶段 0 将检索评测与回答评测放在同一份逐样例报告中，但不混成一个分数。

## 一条命令

真实 RAG 质量评测默认执行完整主链路：

```powershell
$env:INDEX_VERSION = "<已发布索引版本>"
python scripts/run_evaluation.py
```

命令先做只读运行时预检，再执行 Direct/HyDE/KG/Web、RRF、真实 reranker 与回答生成。
只有索引版本已固定、必需依赖可用、每个阶段未降级且报告包含真实候选轨迹时，
`baseline_eligibility.rag_quality` 才会为 `true`。首次建立基线用 `--no-gate`，人工复核后
再晋升为基线；已有基线时默认门禁会比较相同数据集和相同 case 集。

稳定、无外部费用的 CI 契约集：

```powershell
python scripts/run_evaluation.py --provider contract --baseline evaluation/baselines/stage0-contract.core.json
```

`contract` 只检查生产 RRF、拒答、来源整形、引用、图片与评分器契约。它不执行真实召回
和真实 reranker，因此报告不会生成 Recall/MRR，也不能晋升为 RAG 质量基线。

重放快照并重新判分：

```powershell
python scripts/run_evaluation.py --provider replay `
  --snapshot evaluation/snapshots/stage0-full-pipeline.core.jsonl `
  --baseline evaluation/baselines/stage0-current.core.json
```

Replay 不执行检索、RRF 或 reranker。只有快照本身包含此前真实运行保存的逐阶段候选轨迹时，
它才能复算检索指标；无轨迹的旧快照只能复判答案，不能证明检索质量。Replay 自身仍标记为
`recorded_output`，不能晋升为新 RAG 基线；当快照声明来源为 `full_pipeline` 时，可以用来验证
来源快照是否忠实重算出已批准基线。

连接当前进程配置的完整主链路并保存候选快照：

```powershell
python scripts/run_evaluation.py --provider service --suite full --no-gate --attempts 2
```

也可评测已启动的服务：

```powershell
python scripts/run_evaluation.py --provider http --suite full --base-url http://127.0.0.1:8000 --no-gate
```

只有人工复核失败样例、数据版本、模型、Prompt、索引与配置后，才应使用
`--write-snapshot` 记录新的重放输入，并显式替换基线文件。不要在同一次候选运行中自动抬高基线。
若运行时预检、逐阶段完整性或索引版本检查失败，runner 会保留诊断报告但拒绝写 snapshot。

当前阶段 0 语料可从仓库内已解析的 HAK 180 chunks 幂等恢复：

```powershell
python scripts/seed_stage0_corpus.py
```

该脚本只写 Milvus，不自动构建 Neo4j 图。需要回滚种子数据时，分别在
`kb_chunks_v2` 按 `file_title == "hak180使用说明书"`、在 `kb_item_names_v2` 按
`item_name == "HAK 180"` 做精确过滤删除；不要清空 collection。

合格候选经人工复核后，用受保护的晋升入口生成 JSON 与 Markdown 基线；入口会拒绝非
`full_pipeline`、`rag_quality=false`、没有结果或请求了却未写成快照的报告：

```powershell
python scripts/promote_evaluation_baseline.py `
  --input evaluation/results/stage0-full-pipeline.core.json `
  --output evaluation/baselines/stage0-current.core.json
```

## 数据和指标

- `datasets/shopkeeper_qa.v0.1.0.jsonl` 是版本化 golden set，覆盖业务问答、歧义、无答案、多轮、跨文档、表格、图片、时效性、权限与 prompt injection。
- `service` 是唯一默认的完整主链路 provider；`contract` 是低成本代码契约；`replay` 是冻结输出的复判器。
- 检索层记录 Recall@5、Precision@5、MRR、nDCG@5。
- 检索报告同时记录 Direct、HyDE、KG、Web、RRF、rerank 各阶段候选身份和分路 Recall，能定位正确 chunk 在哪一步丢失。
- 没有 relevant source 的拒答样例不进入 Recall/Precision/MRR/nDCG 分母，单独统计 `empty_retrieval_accuracy`。
- 回答层记录行为/拒答准确率、答案事实覆盖、引用正确性、faithfulness、安全性和图片返回。
- 报告保留逐样例答案、来源、失败原因、p50/p95 延迟、模型调用次数、token 与估算成本。
- token 缺少服务端 usage 时按字符数估算，并在 `estimated_token_count` 标记；单价未配置时成本为 0，不冒充真实费用。

非确定性完整集建议至少 `--attempts 2`。runner 选取中位数尝试；分数跨度达到 0.2、断言失败或 provider 出错的样例都会进入 `manual_review`。

## 基线更新纪律

1. 固定 dataset SHA-256，不在调参时顺手改答案；数据修改必须升版本。
2. 完整运行记录 `git_commit`、Prompt SHA-256、模型、集合名、`INDEX_VERSION` 与查询参数。
3. 先审查逐样例退化，再决定是否替换 snapshot/baseline。
4. `evaluation/results/` 是本地运行产物；经审查的首份基线位于 `evaluation/baselines/`。
5. `stage0-current.core` 是 2026-09-19 经审查晋升的首份完整主链路质量基线；其低分与已知失败必须保留，后续改造以门禁比较而不是覆盖历史。
