# 执行会话 01：结构化文档 IR

主参考：[Docling](https://github.com/docling-project/docling)。

## 会话使命

把 PDF、Markdown、表格、图片和扫描件统一为可追溯的文档中间表示，使解析与切分不再丢失结构和来源。

## 必做任务

1. 阅读 Docling 的 document model、conversion pipeline、backend/parser、chunking 与序列化设计。
2. 为本仓库定义 `Document → Section/Block → Chunk` 契约，保留标题路径、页码、坐标、表格、图片、原文范围、parser/version 和稳定 ID。
3. 将 MinerU 输出适配到该 IR，分离“解析、规范化、切分、富化、索引”。
4. 建立解析 fixture 与 golden snapshot，覆盖多栏、跨页表格、脚注、图片说明、OCR 和异常文件。
5. 给解析失败、部分成功、重试、隔离和人工复核定义状态。

## 验收门槛

- 同一文档重复导入得到稳定 ID 和可解释 diff；
- 回答引用可回到原文件页码/块/图片；
- 解析器可替换，后续索引不直接依赖 MinerU 私有格式；
- 阶段 0 数据集证明结构化解析对相关样例的收益和代价。

结束时交接 IR schema、迁移影响和失败样例，不进入检索调参。
