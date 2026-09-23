# 阶段 1 收尾记录（2026-09-23）

## 交付摘要

- 阶段：结构化文档 IR，继续收尾，不进入阶段 2。
- 达成的用户/系统能力：修复图文切分、表格跨行关系、图片富化入口与图片引用验证；建立可追溯且拒绝不兼容对照的评测合同。
- 未完成项：新版本真实影子索引导入、重复导入验证、新旧索引同条件完整评测、有效计费配置下的成本比较。数据库连通性检查超时，用户确认服务器没有启动。本次未修改任何线上索引。
- 验收判断：代码和离线回归完成，阶段 1 整体验收仍待真实服务验证。不能用单测通过或旧报告的标题归一化分数宣告质量验收通过。

## 代码与设计

### 关键文件及职责

- `knowledge/document_ir/chunking.py`：同一页、同一 section 内图文合并；空描述图片也保留；禁止不同 section 因标题同名而合并；长表保留标题、表头、脚注；修复重叠导致超出预算及换行被吞掉。
- `knowledge/document_ir/adapters/table.py`：正确处理 rowspan 占位和 colspan 列位置，提供可重复纵向标签的行视图；拒绝超大跨度网格。
- `knowledge/document_ir/citations.py`、`knowledge/utils/query_result_utils.py`：图片到内容块的明确映射，来源与检索追踪返回图片地址和块身份。
- `knowledge/processor/import_process/nodes/document_enrich_node.py`：富化重试不重复追加图片地址，不保留上次富化的过期地址。
- `scripts/seed_stage1_corpus.py`：影子导入复用真实富化节点。图片上传必须显式使用 `--enrich-assets` 并取得授权；上传不完整时停止索引。默认准备模式无外部写入。
- `knowledge/processor/query_process/nodes/answer_output.py`、`knowledge/front/chat.html`：展示图片必须来自被引用的来源资产；后端过滤后的空数组不能被前端从正文重新提取图片而绕过。流式未验证正文不加载图片。
- `knowledge/evaluation/{metrics,providers,runner}.py`、`scripts/run_evaluation.py`：评测器 v2、窄范围来源映射、唯一 gold 计分、未知字段拒绝、可比性校验及独立离线基线。
- `evaluation/contract_gate.v2.json`、`evaluation/baselines/stage1-contract-v2.core.{json,md}`、`.github/workflows/ci.yml`：独立离线合同门禁，不覆盖阶段 0 历史基线。
- 测试：`tests/test_document_ir_context.py`、`test_document_ir_tables.py`、`test_frontend_image_contract.py`、`test_seed_stage1_corpus.py`，以及既有 IR、资产安全、工具、评测测试和合成 fixture/golden。

### 最终 IR schema / API / 配置

IR schema 仍为 `shopkeeper.document_ir` **1.1.0**，兼容读取 1.0.0；本次没有另起文档模型。

- Document：`document_id`、显式 `logical_document_key`、`revision_id`，源文件 URI/文件名/MIME/SHA/字节数，parser 名称/版本/backend/artifact schema，状态、错误、原文及 sections/blocks/chunks。
- Section：ID、parent、title、level、title_path、顺序、原文范围、页码、block IDs。
- Block：ID/lineage、类型、文本、所属 section、标题路径、顺序、原文范围、provenance、table/image payload。
- Provenance：当前版本一基页码、page UID、坐标与坐标系/尺寸、原文范围、解析器条目指针。
- Table：原始 HTML、cell row/column、rowspan/colspan、标题、脚注、跨页连接。
- Image：源 URI/本地路径/哈希/MIME、说明、描述、OCR。
- Chunk：ID、document/section/block IDs、文本及上下文、标题路径、原文范围、provenance、表格/图片块引用、顺序。
- 新增引用投影：`citation.images=[{block_id,uri}]`；API source/trace 增加 `image_urls`。已有页码、块、revision 身份不变。
- 切分器版本 **1.2**、资产富化版本 **1.1**。切分规则变化会改变 chunk ID，不能把 chunk ID 当作跨版本页码身份。document ID、block lineage 与 page UID 仍各司其职。
- CLI 新增 `--source-contract`，来源映射文件需绑定数据集 SHA；`--enrich-assets` 仅在 `--index` 下有效。

### 参考项目中实际阅读的模块

本阶段前序实施已阅读 Docling / Docling-core 的以下模块；本次收尾依据仓库真实样例、代码和测试，没有引入新的框架依赖：

- `docling_core/types/doc/document.py`、`types/doc/common/reference.py`、`types/doc/items/node.py`、`types/doc/items/table/table.py`；
- `docling_core/transforms/chunker/doc_chunk.py`、`hierarchical_chunker.py`、`hybrid_chunker.py`；
- `docling_core/test/data/chunker/0b_out_chunks.json`；
- `docling/pipeline/standard_pdf_pipeline.py`、`datamodel/document.py`、`tests/test_extraction.py`；
- `docling/docs/concepts/docling_document.md`、`chunking.md`。

采纳：显式文档模型、块级 provenance、结构切分、解析/切分/富化/索引分离。未采纳：直接复制实现或替换 MinerU。本次不修改召回、RRF、reranker、拒答阈值、提示词或 Agent 架构。

## 证据

- 全量测试：126/126 通过。包含真正执行前端图片选择函数的 Node 测试；编译检查、差异空白检查通过。
- 数据集：原 `shopkeeper-qa-v0.1.0`，core 9 题，问题/事实/阈值原样保留。
- 阶段 0 文件：未覆盖。历史记录的 dataset SHA 与当前文件不一致，v1/v2 评测器也不相同，禁止据此宣告正式门禁通过。
- 新离线 contract：8/9，v2 门禁 PASS；已知权限案例仍失败，不能把这个合同测试称为真实问答成功率。
- 同一份当前真实 IR 上，仅替换切分器做离线对比：205 → 129 片；第 50 页警告区 4 → 1 片；同时包含冷却、锁定灯和取纸步骤的片数 0 → 1；129/129 有来源块与 provenance，最多每片一页；重复处理 chunk IDs 一致。该对比不是端到端问答质量/成本实验。
- 9 月 21 日真实旧索引对照：3/9，答案正确率 0.555556、引用正确率 0.685185、faithfulness 0.462963，p50/p95 2772.615/12061.602 ms，21 次调用、16131 tokens。
- 同日修复前 IR 影子索引：2/9，答案正确率 0.555556、原始引用正确率 0.333333、faithfulness 0.222222，p50/p95 3215.792/14001.666 ms，21 次调用、13062 tokens。
- 上述旧报告使用 v1 口径，存在来源标题偏差、重复 gold 的 NDCG 超过 1 和图片只判非空的问题。只作为缺陷调查历史，不作为 v2 基线。尤其旧影子报告的 `image_accuracy=1` 不证明图片真实可追溯。
- 本次修复后真实质量、延迟、费用：**待测**。未配置可信费率时，历史 `$0` 仅表示无法计算，不表示免费。

### 已知失败样例与边界

- 表格问题：旧、新影子结果都曾因重排最高分低于既有拒答阈值而拒答；本阶段不调阈值，修复表格结构不保证这个问题通过。
- 权限问题仍存在澄清/拒答行为不一致；多轮、注入案例及逐句引用表现需重跑确认。
- 字面事实匹配仍会把部分同义表达判为不匹配；没有为了提高分数扩充原题答案。
- 全局同名“警告”不是充分来源身份。来源映射需人工核对实际事实，绑定旧 chunk ID 与新 document/revision/block IDs；有显式映射时不回退到模糊标题匹配。
- 超长表格的共享上下文加一行超出预算时，回退完整原文切分，避免丢失；此时不承诺每片重复表头/脚注或保持整行。超长图片描述作为不可拆图片块可能超过字符预算。
- 无可访问图片 URI 的本地准备产物不是图片端到端验收。前端不再展示旧历史中没有结构化来源验证的图片。

## 生产边界

- 安全与权限：本轮无远程入库/图像上传，无默认索引切换。收尾验证后，用户授权把修复更新到原代码分支；本记录随代码更新发布。秘密、内部地址、真实 PDF、解析产物、详细报告和本地来源映射不纳入公开提交。
- 可观测性与告警：记录富化版本、图片上传数量、缺少引用来源的图片过滤警告；报告记录 evaluator、数据、来源合同、查询实现、模型与配置指纹。尚无外部告警平台接入。
- 错误处理、重试与幂等：图片上传不完整阻止显式完整影子导入；富化重试上下文不累积；未知评测选择器/缺少必需指标拒绝通过；重复 gold 不膨胀 recall/NDCG。
- 数据迁移：在新影子 collection 中重新切分、富化、嵌入和导入；不可把新 chunk ID 覆盖到旧切分配置的索引中混用。核对文档/块身份、条数、资产与引用后，再运行控制组和候选组。默认 collection 保持不动。
- 回滚：继续读取旧 collection 即可；若以后经批准切换，仅将 collection 配置切回旧索引并重启查询进程。保留旧快照/版本，不需删除新影子索引。旧的“同 key 替换”仍是 delete→insert，非事务，不可宣称原子更新。
- 尚未验证：真实服务重跑、真实图片访问控制与上传、跨机器并发导入、收费准确性、真实 OCR 再解析。Web/LLM 会波动，单次同配置对照仍不证明统计显著因果关系。模型路径/配置有指纹，但未完整哈希多 GB 权重，仍需运营固定模型资产版本。多 attempts 的全部费用累计尚未改造，真实对照使用相同 `attempts=1`，不能将“中位样例成本”当全部尝试费用。

## 下一会话输入

- 必须知道：用户已确认数据库未启动；先恢复服务。当前默认索引与旧基线原样保留。不要把此次离线修复当成阶段 1 全部验收通过。
- 建议 `CURRENT_STAGE.md` 状态：`Active：阶段 1，收尾代码/离线回归完成，真实新旧对照与成本验收待服务恢复`。本轮保留该文件已有的用户修改，未覆盖。
- 推荐启动文件：仍为 `sessions/01-document-ir.md`，同时读取本记录与 `HANDOFF_TEMPLATE.md`；尚不进入阶段 2。

### 复跑入口

离线回归（在仓库根目录、使用项目 Python 环境）：

```powershell
python -m unittest discover -s tests -v
python scripts/run_evaluation.py --provider contract --output evaluation/results/stage1-closeout.contract.json
```

真实服务恢复后，必须先获准上传实际文档图片，再在新的影子 collection 中准备完整输入；不传 `--enrich-assets` 只代表禁止上传，不能据此验收图片链路。下列为占位示例，不是已运行成功记录：

```powershell
python scripts/seed_stage1_corpus.py --source <source.pdf> --content-list <content_list_v2.json> --middle <middle.json> --document-key <logical-key> --item-name <product> --index --collection <new-shadow> --enrich-assets --ir-output <private-ir-output.json>
```

用**相同数据集、来源映射、查询代码、模型、费率和配置**在两个新进程中分别运行；来源映射必须先核对旧索引实际 chunk IDs 和当前 IR revision，不可按分数临时放宽：

```powershell
$env:CHUNKS_COLLECTION='<existing-control>'
$env:INDEX_VERSION='<pinned-control-version>'
python scripts/run_evaluation.py --provider service --source-contract <reviewed-contract.json> --no-gate --write-snapshot <new-control.jsonl> --output <new-control.json>

$env:CHUNKS_COLLECTION='<new-shadow>'
$env:INDEX_VERSION='<pinned-candidate-version>'
python scripts/run_evaluation.py --provider service --source-contract <same-reviewed-contract.json> --baseline <new-control.json> --write-snapshot <new-candidate.jsonl> --output <new-candidate.json>
```

控制组 `--no-gate` 只用于生成新的可审计对照，不是验收放行。检查两组 `baseline_eligibility`、全部失败样例及严格 gate；真实成本未知仍不得声称成本门禁通过。每次使用新的输出路径，不覆盖历史文件；不要挑选最好的一次作为结论。
