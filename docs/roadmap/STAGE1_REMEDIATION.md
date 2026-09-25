# 阶段 1 修复及复验记录（2026-09-23）

## 交付摘要

- 阶段：针对阶段 1 v3 验证发现的问题执行修复；本记录不替代总控对阶段切换的决定。
- 已实现：正确上传图片 MIME；保守纠正表格被错误归属到上一章节的问题；区分正文变动与章节结构修正，避免稳定块同时被报告为未变及新增/删除；完整统计所有评测轮次与尝试的消耗，防止早轮/非代表尝试失败被成功结果掩盖。
- 已实测：新 v4 影子索引导入和完整重复导入，83 张图片的存储/HTTP/浏览器解码，所有入库引用及最终服务返回的来源定位。
- 结论：上述缺陷修复通过验证，**严格整体门禁仍 FAIL**。不能把章节和 MIME 修复说成已解决重排、拒答或所有问答问题。
- 未完成：可靠的检索/回答质量非退化、真实单价下的费用验收，以及冻结外部输入的检索因果实验。原始数据集、gold、质量阈值、检索策略和提示词未改。

## 代码与设计

### 修改文件与职责

- `knowledge/document_ir/adapters/mineru.py`：恢复明确编号且具有可靠相邻章节锚点的表格所属章节。
- `knowledge/document_ir/lineage.py`：在源文件相同或页对应已确认时，优先继承内容一致的稳定块身份；重复内容仍按全局歧义保守处理。
- `knowledge/document_ir/diff.py`：新增独立结构差异，避免章节修正被误报成正文新增/删除或相互矛盾的身份结论。
- `knowledge/processor/import_process/nodes/document_enrich_node.py`：MinIO 上传显式传 `content_type`，与 IR MIME 保持一致。
- `knowledge/evaluation/usage.py`：汇总各轮/各尝试资源，校验缺失、非法值和完整性。
- `knowledge/evaluation/providers.py`：保留多轮全量消耗和逐轮链路状态；HTTP 业务错误返回可审计结果，不丢弃已知消耗。
- `knowledge/evaluation/metrics.py`：沿用原质量计算；资源汇总使用安全聚合器，版本更新为 2.1。
- `knowledge/evaluation/runner.py`：质量仍取代表尝试，实际消耗统计全部尝试；任意已执行尝试失败或早轮降级阻止成为可用质量基线。
- `scripts/run_evaluation.py`：离线默认基线指向独立的 v2.1 合同基线。
- `evaluation/baselines/stage1-contract-v2.1.core.{json,md}`：新增版本化离线基线；与原 v2 的质量指标、失败样例完全相同，没有覆盖原 v2 或阶段 0。
- `tests/test_document_ir_asset_safety.py`、`tests/test_document_ir_caption_sections.py`、`tests/test_document_ir_structural_diff.py`、`tests/test_evaluation.py`、`tests/test_evaluation_usage.py`：新增真实缺陷回归与兼容性测试。
- 本文件：脱敏修复说明。详细 IR、来源合同、模型输出、真实图片 URL、报告和验证脚本只保留在忽略目录中。

现有 `CURRENT_STAGE.md` 和 `sessions/02-industrial-rag.md` 在本轮开始前已被其他工作修改为阶段 2；本轮保留这两份修改，不覆盖，也不把文件内的阶段声明当成扩大本轮权限的依据。

### 数据模型/API/配置

IR schema 仍为 `shopkeeper.document_ir` **1.1.0**，可读取 1.0.0；切分器仍为 1.2。Document 的逻辑身份/版本/源文件/解析器、Section 的标题层级、Block 的类型和来源位置、Chunk 的来源块/页码/坐标/范围/图表引用等字段不变。

表格章节恢复不是对所有平级标题重建语义层级。只有以下条件同时成立才启用：

1. MinerU 所有显式标题明确给出 level=1；缺失或异常层级不视为可靠平级证据。
2. 表格只有一个“数字章节号 + 标题”的 caption。
3. 最近明确编号章节位于同页或上一页，caption 恰为同父编号的下一个兄弟。

原始块类型、正文、坐标、范围、source pointer 和 block ID 不变；记录 `section_inference`、原章节、锚点及规则 `mineru.numbered_table_caption_next_sibling/1.0`。不覆盖已有有效层级，不猜测含糊标题。

真实说明书的两个修正是第 32 页菜单表与第 48 页介质送入表：sections **134→136**，blocks **674→674**，chunks **129→129**。document ID 与全部 block ID 不变；结构变化导致 revision 和 chunk IDs 正常更新。重复同一新版本导入仍要求稳定。

`DocumentDiff` 兼容新增 `structural_changes`：逐项保存 `before_id`、`after_id`、身份锚点 `anchor`、前后 `section_id` 和 `title_path`。章节修正与正文修改分别记录；身份继承要求相同源文件，或已确认的页对应和相同块内容，不能仅凭重复内容碰巧位于相同解析位置认定同一块。此差异/身份修复不重算现有 document/revision/block/chunk ID，不改变检索正文，故无需重跑已完成的 v4 查询实验。

资产富化版本升级为 **1.2**。MIME 来自已验证本地路径的固定栅格扩展名映射，不信任 IR 声明或系统 MIME 注册表；未知类型保守回退，原路径白名单仍拒绝 HTML/SVG。

评测 schema/evaluator 为 **2.1**，新增 `usage_accounting_version`、`usage_scope=all_turns_all_attempts`、执行完整性和代表尝试用量。旧报告不能直接当作新版基线。缺失用量或单价仍不能算作免费；本轮不填写猜测费率，也不修改 `.env`。

### 实际参考与取舍

本轮重新阅读 Docling 的 [HierarchicalChunker 源码](https://github.com/docling-project/docling-core/blob/main/docling_core/transforms/chunker/hierarchical_chunker.py) 与 [DoclingDocument 架构说明](https://github.com/docling-project/docling/blob/main/docs/concepts/docling_document.md)，确认标题上下文、caption 与来源节点应显式传递。采用独立文档结构及可追溯修复；没有复制实现、替换 MinerU，或把 Docling 的表格序列化方式直接移植来迎合某一道题。

computer-use 技能用于浏览器显示验证；遵循其中优先使用浏览器工具的指引，在专用本地测试页加载授权的 MinIO 图片，未操作系统设置。

## 证据

### 工程和真实链路

- 全量测试：159/159 PASS（本轮新增 33 项）；编译检查及差异空白检查通过。
- 真实 v3→v4 结构差异：674 块正文及稳定身份不变；新增、删除、正文修改、页移动均为 0；结构变化为 6 块（第 32、48 页各 1 张表及 2 个页眉/页脚等辅助块）。此前误报的 57 新增 + 57 删除已消除；重复 lineage 和“既未变又新增/删除”的矛盾均为 0。
- v2.1 离线 contract：8/9、gate PASS；已知权限行为案例仍失败。原 v2 与 v2.1 的所有离线质量指标及失败集逐项相同，新增基线不是降低标准。
- 新 v4 影子索引真实导入两次：均为 129 行、129 唯一 chunk IDs，两份 IR 完全相同，业务行指纹相同。默认旧索引始终 336 行，未切换。
- 图片 83/83：对象存在、本地 SHA 与 IR SHA 相符、长度和 ETag 对应；HTTP HEAD 均为 200；对象与响应 MIME 均修正为 `image/jpeg`。
- 浏览器专用图片页：83/83 图片 `complete=true` 且 naturalWidth/naturalHeight > 0；视口截图能看到设备及面板图。这是实际解码显示检查，不冒充正式聊天页面的全部交互端到端测试。
- 入库引用 129/129 与 IR citation 投影完全一致，文档/版本、源文件 SHA、章节、块、页码/page UID、坐标和原文范围全部正确；129/129 单页。
- v4 最终服务响应中 40/40 条本地来源逐字段匹配 IR，10/10 次实际引用可回溯。源 URI 指向本机文件，不代表其他机器浏览器能直接下载该原 PDF。

### 新旧同条件对照

继续使用原 `shopkeeper-qa-v0.1.0` core 9 题。数据集 SHA：
`2ae67e7c4a3c98fa3329cf561553123172bd306b65694ad98dd4da053d724275`。

新来源合同仅将 16 处实际来源 revision 绑定换到结构修复后的 revision；8 个新 IR 块选择器仍唯一命中，原 gold、具体块、事实和阈值未扩大。旧合同和 v3 证据保留。

两组 evaluator、查询实现、Prompt、模型、配置、来源合同和价格配置相同，在两个新进程各运行一次（attempts=1）。旧控制组完成后导入并复导入，再运行候选。以下为本轮 **旧控制组 → v4 候选**，不是拿旧版漏计的 21 次调用与新版全量 24 次调用直接比较：

- 样例通过数：4/9 → 4/9；答案正确率：0.555556 → 0.555556。
- Recall@5：1.000000 → 1.000000；Precision@5：0.233333 → 0.200000。
- MRR：0.916667 → 0.888889；nDCG@5：0.938488 → 0.916667。
- 引用正确率：0.685185 → 0.666667；faithfulness：0.574074 → 0.629630。
- 图片指标：0.888889 → 1.000000；直接召回：0.666667 → 1.000000；HyDE Recall@5 两组均 1.000000。
- p50：2689.104 → 2459.484 ms；p95：11296.615 → 11567.269 ms。
- 全部模型调用：24 → 24；输入 tokens：17808 → 18393；输出 tokens：1926 → 2619；总 tokens：19734 → 21012。
- 两组失败调用、provider 错误、早轮链路不完整均为 0；用量完整，token 为 API 返回数而非字符估算。
- 费用：未知 → 未知；尚未收到实际输入/输出费率。这里不包括建索引、MinIO 存储和机器资源费用，也没有采集峰值内存/显存。

严格 gate 仍失败：Precision@5、MRR、nDCG@5、引用正确率下降，以及费用不可计算。延迟未触发本轮门槛。阶段 0 原始基线不被覆盖；v1/v2/v2.1 不能直接混比成本。

### 失败归因与下一步修正边界

- 表格问题：API 来源标题已经从前一章节改成正确章节；重排分从 v3 的 -1.270089 改善到 v4 的 -0.859826，但仍低于现有 0.3 阈值。正确表格仍排名第一；解决拒答需要检查分数与拒答策略，而不是继续改原文或伪造章节。
- 上边距：两个附加引用实际支持卡纸/膜松弛说明，但不在原主事实 gold 内。不能简单把它们设成主事实等价来源来涨分；需要独立、逐声明的标注及版本化评测设计。
- 面板题：图片已可追溯、可显示，但固定 gold 下引用分仍为 0.666667。
- 权限题：仍为澄清而非预期拒绝；注入题有正确证据却生成知识不足的拒答。这涉及前门和回答行为，不是文档丢失。
- 安全题：答案通过且支持更完整；旧索引同一个 gold 对应两片，新索引合一片，chunk 粒度 Precision 对结构变化敏感。不能重新拆出重复片段凑分。Web 片段又会变化，MRR/nDCG 不能单独归因于 IR。
- 后续应将“冻结外部输入的检索诊断”与“真实服务全链路评测”并列。冻结实验需重新捕获完整 Web/改写/HyDE 输入，必须独立 scope，不能拿旧快照缺失的原文补造夹具，也不能用冻结实验费用冒充线上费用。

## 生产边界

- 安全与权限：沿用明确授权的 MinIO 与说明书、新建 v4 影子索引；没有切换默认索引、改桶权限、上传 GitHub 或泄露配置。相同图片对象按同路径重传以修正 MIME，字节内容/哈希不变，旧影子对这些对象的链接仍有效。
- 可观测性：新增全尝试执行健康和用量完整性；保留失败报告、私有来源合同和逐项审计。尚未接入外部告警平台。
- 幂等与错误：完整重导入没有重复业务记录；图片失败阻止完整 seed 入库；HTTP/多轮失败不再丢弃已知用量；任意已执行失败不被成功代表尝试掩盖。索引替换仍为 delete→insert，非原子事务。
- 迁移：新规则会产生新 revision/chunk ID，使用新 collection 重新适配、富化、嵌入和索引，不能只修改旧行标题混用两个版本。
- 回滚：默认未切换，无需回滚。保留旧 collection、v3/v4 IR 与快照；后续若获准切换失败，可恢复旧 collection 配置并重启查询进程。已纠正的图片 MIME 对旧链接兼容，无需恢复错误类型。
- 尚未验证：跨机器并发/崩溃原子性、真实文件改版迁移、重新 OCR、非空 Neo4j 图兼容、正式聊天界面完整交互、可信价格下成本、外部输入冻结后的因果结果。

## 下一会话输入

- 必须知道：阶段 1 这轮明确缺陷已修复并有 v4 证据，但严格整体门禁仍失败，不能声称全部完成。现有路线图由其他工作改为阶段 2，须区分业务放行与质量门禁通过。
- 建议阶段说明：结构 IR 修复与重复导入/图片/定位验收通过；问答策略和费用验收仍待处理。若授权阶段 2，则明确携带本轮失败项，不删除旧报告或放宽门槛。
- 推荐输入：本记录、`STAGE1_LIVE_VALIDATION.md`、`HANDOFF_TEMPLATE.md`；阶段 1 限定范围仍看 `sessions/01-document-ir.md`，只有获得相应范围授权后才执行 `sessions/02-industrial-rag.md`。
- 私有证据：`evaluation/results/stage1-{control,ir-shadow}-v4.20260923.service.core.{json,md}`、对应快照、`evaluation/results/stage1-remediation-v4.final.contract.{json,md}`、`output/stage1-remediation-v4.source-contract.json`、`output/stage1-ir-shadow-v4{,.repeat}.document.ir.json`、`output/stage1-live-assets-citations-v4.20260923.json`、`output/stage1-remediation-v4.import-browser-audit.json`、`output/stage1-final-response-audit-v4.20260923.json`。不要将真实语料/模型输出/内部地址随代码公开。
