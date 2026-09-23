# 阶段 1 代码交付与迁移说明

本次代码提交对应结构化文档 IR，实现状态不等于正式发布验收。
质量门槛仍未通过；不应因代码上传就切换默认索引或推进检索调参。
本说明只使用合成样例，不包含业务原文、内部部署地址或真实评测明细。

## 代码范围与模型

导入流程为：解析适配、规范化、结构切分、资产富化、向量化和索引投影。
PDF 仍用 MinerU；不引入 Docling 运行依赖，也不重写检索排序或 Agent。
参考设计与功能比较见 [版本区别说明](STAGE1_VERSION_COMPARISON.md)。
那份说明记录的是此前的文档单独发布；本次在独立代码分支补充实现和测试。

`knowledge/document_ir/` 定义 `shopkeeper.document_ir`，schema 版本 `1.1.0`，
可兼容读取旧 `1.0.0` 数据：

- `DocumentIR`：文档 ID、逻辑 key、修订 ID、来源 URI/文件哈希、解析器信息、
  解析状态/错误、解析原文，以及 sections、blocks、chunks。
- `Section`：父章节、标题级别/路径、阅读顺序、原文范围与块引用。
- `DocumentBlock`：块类型、内容、章节、来源位置、跨版本 lineage，表格或图片数据。
- `Provenance`：当前页码、页面身份、坐标、页面尺寸、原文范围和解析制品指针。
- `Chunk`：当前切分配置下的稳定 ID、文本与带上下文文本、原始块引用、来源位置。

来源缺失时不虚构位置；Markdown 通常不具有 PDF 的物理页码。
同字节文件和同处理配置可重现身份；跨修订需要显式逻辑 key 和上一版 IR。
只有可信匹配才继承页面/块身份；切分配置变化时不承诺切片 ID 不变。

本地图片引用必须位于受信任的文档或制品目录内，并使用允许的栅格图片后缀。
适配器拒绝绝对引用和越界路径，富化阶段根据任务目录再次检查；远程链接
只保留引用，不由这些步骤下载。目录和后缀检查不等于图片内容安全扫描，
导入目录仍须由服务控制，不能让不可信调用方直接指定服务器目录。

## 本地验证

使用已有 Python 环境和仓库依赖执行：

```text
python -m unittest discover -s tests -v
python -m unittest tests.test_document_ir tests.test_document_ir_tools -v
```

测试使用合成 fixtures 与假的存储/模型对象，不要求连接真实数据库或调用模型。
CI 增补 Milvus、对象存储及相关导入模块 SDK，用于测试接口；不需要模型权重。
合成 PDF 的生成依赖和样例边界见
[fixture 说明](../../tests/fixtures/document_ir/README.md)。

历史基线的数据集一致性问题没有通过重写基线绕过：

```text
python scripts/audit_stage0_baseline.py
```

不一致时命令返回非零；自动化单测通过不代表正式质量门禁通过。
标题兼容诊断只用于定位格式差异，不是修改官方评分规则或基线。

## 不连接外部服务的转换

Markdown 示例：

```text
python scripts/migrate_document_ir.py tests/fixtures/document_ir/complex.md --output output/example.ir.json --document-key example/manual
```

从已有 MinerU 制品生成 IR，不重新调用 MinerU、LLM 或视觉模型：

```text
python scripts/seed_stage1_corpus.py --source tests/fixtures/document_ir/complex_layout.pdf --content-list tests/fixtures/document_ir/complex_mineru_content_list_v2.json --middle tests/fixtures/document_ir/complex_mineru_middle.json --ir-output output/example-pdf.ir.json --document-key example/pdf-manual
```

准备脚本不再内置任何真实业务文件路径、产品名称或固定文档 key。
`--source` 和 `--content-list` 必填；`--middle`、`--document-key`、
`--item-name` 可选。不指定 key 时使用源文件哈希识别文档，不自动跨修订绑定。

## 修订更新与错误处理

上传接口可接收 `document_key`；调用方从第一版开始保存并复用它。
服务维护上一成功版本的 IR 记录，在新版本写入前执行正文连续性检查。
无 key 的旧文档后来补 key，不会自动完成身份迁移。

离线比较需使用与上一版相同的 key：

```text
python scripts/migrate_document_ir.py revised.md --output output/revised.ir.json --document-key example/manual --compare output/example.ir.json
```

明显的 key 身份冲突会停止入库并产生待复核状态；这是启发式防误操作，
不是权限校验或语义同一性的证明。没有上一版 IR 时不能执行连续性检查。

- `success`：继续处理；`partial`：携带警告继续。
- `review_required`：需要复核，停止正常导入。
- 解析失败在预算内重试；耗尽后写入 `quarantined` IR 并停止。
- 任务状态保留文档/修订 ID、阶段错误、IR 路径与节点耗时。
- `DOCUMENT_PARSE_MAX_ATTEMPTS` 控制解析尝试次数；
  `DOCUMENT_IR_MIN_REVISION_OVERLAP` 控制修订保护阈值。
  配置值不应代替人工判断，更不能用于绕过业务权限。

## 索引迁移与回滚

1. 备份代码与配置，保留当前查询集合、原文件和旧 IR。
2. 在独立影子集合生成新索引；向量数据库和模型端点必须由操作者在本地配置并授权。
3. 核对文档/切片数量、ID、来源引用及问答质量。
4. 质量验证通过后，才在部署流程中切换 `CHUNKS_COLLECTION`。
5. 如需回滚，切回保留的旧集合，并恢复匹配的代码和配置。

准备脚本仅在显式提供 `--index --collection YOUR_SHADOW_COLLECTION` 时调用
embedding 并写入集合，且拒绝目标等于当前查询集合。先准备影子集合，
再由独立部署步骤切换；不要先把当前查询集合改成待写入的影子集合。
该脚本用于准备测试索引，不代替带上一版注册记录的正式修订更新流程。

新集合将数据库内部主键与稳定切片 ID 分离。旧集合只通过必要字段适配保持查询兼容，
不要求原地改写其 schema。正式迁移优先使用影子集合。
商品名投影也按文档 ID 替换，不按同名文件覆盖其他文档；无文档身份的旧记录
不会自动合并或清理，迁移时需明确处理。

当前同文档替换是先删旧切片再写新切片，不是事务：删除失败会中止，
删除成功后的写入失败仍可能导致缺失，不能保证自动恢复旧索引。
同 key 导入只有进程内串行保护；多实例并发、租户权限、完整版本生命周期和
前端一键更新不属于本次已完成能力。部署前仍需相应保护。

## 发布边界

仅发布本阶段代码、合成 fixtures、通用脚本、测试与公开说明。
真实文档、解析产物、运行环境文件、模型权重、服务评测快照和内部详细报告均不包含。
原阶段 0 基线不覆盖，当前阶段状态文件中的其他本地改动也不在本次提交范围。
