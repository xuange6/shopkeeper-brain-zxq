"""
文档切分节点

按 Markdown 标题切分文档，支持二次切分和短内容合并。
融合了原生层级追踪与 LangChain 递归切分算法。
"""

import re
import os
import json
import sys
from pathlib import Path
from typing import List, Tuple, Optional

# 允许直接运行本文件：python knowledge/processor/import_process/nodes/document_spliter_node.py
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
        )
    )
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:
    class RecursiveCharacterTextSplitter:
        """langchain_text_splitters 未安装时的轻量兜底切分器。"""

        def __init__(
                self,
                chunk_size: int,
                chunk_overlap: int = 0,
                separators: Optional[List[str]] = None,
        ):
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap
            self.separators = separators or ["\n\n", "\n", " "]

        def split_text(self, text: str) -> List[str]:
            return self._split_recursive(text.strip(), self.separators)

        def _split_recursive(self, text: str, separators: List[str]) -> List[str]:
            if not text:
                return []
            if len(text) <= self.chunk_size:
                return [text]
            if not separators:
                return [
                    text[i:i + self.chunk_size]
                    for i in range(0, len(text), self.chunk_size)
                ]

            separator = separators[0]
            raw_parts = text.split(separator)
            if len(raw_parts) == 1:
                return self._split_recursive(text, separators[1:])

            parts = [
                part + separator if index < len(raw_parts) - 1 else part
                for index, part in enumerate(raw_parts)
                if part
            ]

            chunks: List[str] = []
            current = ""
            for part in parts:
                if len(part) > self.chunk_size:
                    if current:
                        chunks.append(current.strip())
                        current = ""
                    chunks.extend(self._split_recursive(part, separators[1:]))
                elif len(current) + len(part) <= self.chunk_size:
                    current += part
                else:
                    if current:
                        chunks.append(current.strip())
                    current = part

            if current:
                chunks.append(current.strip())

            return chunks

from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.processor.import_process.exceptions import DocumentSplitError
from knowledge.document_ir.chunking import ChunkingConfig, chunk_document
from knowledge.document_ir.indexing import chunks_to_index_rows
from knowledge.document_ir.models import DocumentIR
from knowledge.document_ir.serialization import save_document_ir


class DocumentSplitNode(BaseNode):
    """
    文档切分节点

    处理流程：
    1. 读取 MD 内容
    2. 按 Markdown 标题进行一级切分（title 与 body 分离，并发放 parent_title 身份证）
    3. 处理无标题情况兜底
    4. 对超长章节进行二次切分 (引入 LangChain Recursive Split)
    5. 合并过短的相邻章节 (基于 parent_title 同宗同源合并)
    6. 组装最终 content = title + body
    7. 备份与状态更新
    """

    name = "document_split"

    # ------------------------------------------------------------------ #
    #                           主流程                                     #
    # ------------------------------------------------------------------ #

    def process(self, state: ImportGraphState) -> ImportGraphState:
        config = self.config

        if state.get("document_ir") is not None:
            return self._process_document_ir(state, config)

        # Step 1: 获取输入
        content, file_title, max_length = self._get_inputs(state, config)
        if not content:
            raise DocumentSplitError("md_content 为空", node_name=self.name)

        # Step 2: 按标题一级切分 (带层级追踪)
        sections, has_title = self._split_by_headings(content, file_title)

        # Step 3: 处理全文无标题的情况
        if not has_title:
            sections = [{
                "title": "无标题",
                "body": content,
                "file_title": file_title,
                "parent_title": file_title
            }]
            self.logger.info("全文无标题，作为单个 chunk 处理")

        # Step 4: 二次切分 + 合并短章节
        sections = self._split_and_merge(
            sections,
            max_length,
            config.min_content_length,
            config.overlap_sentences,
        )

        # Step 5: 组装最终 content（title + body），清理内部字段
        sections = self._assemble_content(sections)

        # Step 6: 日志统计
        self._log_summary(content, sections, max_length)

        # Step 7: 备份
        state["chunks"] = sections
        self._backup_chunks(state, sections)

        return state

    def _process_document_ir(self, state: ImportGraphState, config) -> ImportGraphState:
        """Chunk unified IR and project it to the legacy index-row boundary."""

        raw_document = state.get("document_ir")
        document = (
            raw_document
            if isinstance(raw_document, DocumentIR)
            else DocumentIR.model_validate(raw_document)
        )
        if not document.blocks:
            raise DocumentSplitError("document_ir.blocks 为空", node_name=self.name)

        chunked = chunk_document(
            document,
            ChunkingConfig(
                max_characters=config.max_content_length,
                min_characters=config.min_content_length,
                overlap_characters=max(0, config.overlap_sentences) * 120,
                merge_peers=True,
                repeat_table_header=True,
            ),
        )
        state["document_ir"] = chunked
        state["chunks"] = chunks_to_index_rows(
            chunked,
            item_name=state.get("item_name", ""),
        )
        output_path = Path(
            state.get("ir_path") or Path(state.get("file_dir") or ".") / "document.ir.json"
        )
        save_document_ir(chunked, output_path)
        state["ir_path"] = str(output_path)
        self._log_summary(chunked.raw_text, state["chunks"], config.max_content_length)
        self._backup_chunks(state, state["chunks"])
        return state

    # ------------------------------------------------------------------ #
    #                       Step 1: 获取输入                               #
    # ------------------------------------------------------------------ #

    def _get_inputs(
            self, state: ImportGraphState, config
    ) -> Tuple[Optional[str], Optional[str], int]:
        self.log_step("step_1", "获取输入")

        content = state.get("md_content", "")
        if content:
            # 统一换行符，避免正则匹配出 Bug
            content = content.replace("\r\n", "\n").replace("\r", "\n")

        file_title = state.get("file_title", "")
        max_length = config.max_content_length

        return content, file_title, max_length

    # ------------------------------------------------------------------ #
    #                  Step 2: 按标题一级切分 (带层级追踪)                   #
    # ------------------------------------------------------------------ #

    def _split_by_headings(
            self, content: str, file_title: str
    ) -> Tuple[List[dict], bool]:
        """
        按 Markdown 标题行切分，title 与 body 分开存储。
        新增特性：向上追踪层级，寻找最近的高级标题作为 parent_title。
        """
        self.log_step("step_2", "按标题切分并追踪层级")

        # 使用括号分组，(#{1,6}) 捕获井号数量即层级，(.+) 捕获标题内容
        heading_re = re.compile(r"^\s*(#{1,6})\s+(.+)")
        lines = content.split("\n")

        sections: List[dict] = []
        current_title = ""
        current_level = 0
        body_lines: List[str] = []
        has_title = False
        in_fence = False  # 代码围栏标记

        # 记录 1-6 级标题的最新足迹（索引 0 不用）
        hierarchy = [""] * 7

        def _flush():
            """将当前积累的内容保存为一个 section，并计算 parent_title"""
            body = "\n".join(body_lines).strip()
            if current_title or body:
                # 向上寻找最近的父标题作为 parent_title
                parent_title = ""
                for lvl in range(current_level - 1, 0, -1):
                    if hierarchy[lvl]:
                        parent_title = hierarchy[lvl]
                        break

                # 如果没找到父标题（自己就是 H1，或者文档开头无标题段落），自己或文件标题兜底
                if not parent_title:
                    parent_title = current_title if current_title else file_title

                sections.append({
                    "title": current_title,
                    "body": body,
                    "file_title": file_title,
                    "parent_title": parent_title,
                })

        for line in lines:
            # 检测代码围栏（``` 或 ~~~），防止误切代码内部的 # 注释
            if line.strip().startswith("```") or line.strip().startswith("~~~"):
                in_fence = not in_fence

            match = heading_re.match(line) if not in_fence else None

            if match:
                has_title = True
                _flush()  # 先把上一段结算落盘

                # 获取当前标题的级别 (1-6)
                level = len(match.group(1))
                current_level = level
                current_title = line.strip()
                hierarchy[level] = current_title

                # 出现新的上级标题，其下属的旧子标题足迹要清空
                for i in range(level + 1, 7):
                    hierarchy[i] = ""

                body_lines = []  # 清空正文缓存，开始收集当前新标题下的内容
            else:
                body_lines.append(line)

        # 处理文档最后一段
        _flush()

        return sections, has_title

    # ------------------------------------------------------------------ #
    #                Step 4: 二次切分 + 合并短章节                          #
    # ------------------------------------------------------------------ #

    def _split_and_merge(
            self,
            sections: List[dict],
            max_length: int,
            min_length: int,
            overlap_sentences: int,
    ) -> List[dict]:
        self.log_step("step_4", "二次切分和合并")

        if max_length <= 0:
            return sections

        # 4a: 对超长章节做二次切分
        split_result: List[dict] = []
        for section in sections:
            split_result.extend(
                self._split_long_section(section, max_length, overlap_sentences)
            )

        # 4b: 合并过短的相邻章节（仅限同一 parent_title 下的子片段）
        return self._merge_short_sections(split_result, min_length)

    def _split_long_section(
            self,
            section: dict,
            max_length: int,
            overlap_sentences: int = 1,
    ) -> List[dict]:
        """
        引入 LangChain 的 RecursiveCharacterTextSplitter 对超长 body 优雅降级切分
        """
        title = section.get("title", "")
        body = section.get("body", "")
        file_title = section.get("file_title", "")
        parent_title = section.get("parent_title", title)

        # title 作为前缀会占用一部分空间
        title_prefix = f"{title}\n\n" if title else ""
        total = len(title_prefix) + len(body)

        if total <= max_length:
            return [section]

        # 计算留给正文的实际可用字符数
        available = max_length - len(title_prefix)
        if available <= 0:
            return [section]

        if RecursiveCharacterTextSplitter is None:
            raise DocumentSplitError(
                "缺少 langchain_text_splitters 依赖，无法进行超长章节二次切分",
                node_name=self.name,
            )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=available,
            # 中文句长差异很大，用约 120 字/句转换为字符重叠，并限制在
            # chunk 的 1/4 内，兼顾上下文连续性和检索去重成本。
            chunk_overlap=min(
                max(0, overlap_sentences) * 120,
                max(0, available // 4),
            ),
            # 优雅降级策略：优先按双换行切，再按单换行，最后按标点和空格
            separators=["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " "]
        )

        pieces = splitter.split_text(body)

        # 防御性代码：万一没切开，原样返回
        if len(pieces) <= 1:
            return [section]

        # 组装切片结果
        sub_sections = []
        for i, piece in enumerate(pieces):
            sub_sections.append({
                "title": f"{title}-{i + 1}" if title else f"chunk-{i + 1}",
                "body": piece.strip(),
                "file_title": file_title,
                "parent_title": parent_title,
                "part": i + 1,
            })

        return sub_sections

    def _merge_short_sections(
            self, sections: List[dict], min_length: int
    ) -> List[dict]:
        """
        合并过短的相邻子片段（仅限同一 parent_title 下的片段）。
        """
        if not sections:
            return []

        merged: List[dict] = []
        current = sections[0]

        for next_sec in sections[1:]:
            cur_body_len = len(current.get("body", ""))

            # 同宗同源检验（依赖 Step 2 发放的 parent_title）
            same_parent = (
                    current.get("parent_title")
                    and current["parent_title"] == next_sec.get("parent_title")
            )

            if cur_body_len < min_length and same_parent:
                # 合并: 将 next_sec 的 body 追加到 current
                current["body"] = (
                        current.get("body", "").rstrip()
                        + "\n\n"
                        + next_sec.get("body", "").lstrip()
                ).strip()
                # 标题回退为父标题（表示这是一个大综合块）
                current["title"] = current.get("parent_title", current.get("title", ""))
                # 更新 part 编号
                if "part" in next_sec:
                    current["part"] = next_sec["part"]
            else:
                merged.append(current)
                current = next_sec

        merged.append(current)
        return merged

    # ------------------------------------------------------------------ #
    #               Step 5: 组装最终 content                               #
    # ------------------------------------------------------------------ #

    def _assemble_content(self, sections: List[dict]) -> List[dict]:
        """
        将 title + body 组装为最终的 content 字段，
        清理内部临时字段 body，保留 parent_title 和 part 供下游使用。
        """
        self.log_step("step_5", "组装 content")

        result: List[dict] = []
        for sec in sections:
            title = sec.get("title", "")
            body = sec.get("body", "")

            # 组装: title 在最前面，body 紧随其后
            if title and body:
                content = f"{title}\n\n{body}"
            else:
                content = title or body

            chunk = {
                "title": title,
                "content": content.strip(),
                "file_title": sec.get("file_title", ""),
            }

            # 保留二次切分产生的字段，供下游合并/溯源使用
            if "parent_title" in sec:
                chunk["parent_title"] = sec["parent_title"]
            if "part" in sec:
                chunk["part"] = sec["part"]

            result.append(chunk)

        return result

    # ------------------------------------------------------------------ #
    #                       日志 & 备份                                    #
    # ------------------------------------------------------------------ #

    def _log_summary(self, raw_content: str, sections: List[dict], max_length: int):
        self.log_step("step_6", "输出统计")

        lines_count = raw_content.count("\n") + 1
        self.logger.info(f"原文档行数: {lines_count}")
        self.logger.info(f"最终切分章节数: {len(sections)}")
        self.logger.info(f"最大切片长度: {max_length}")

        if sections:
            self.logger.info("章节预览:")
            for i, sec in enumerate(sections[:5]):
                title = sec.get("title", "")[:50]
                self.logger.info(f"  {i + 1}. {title}...")
            if len(sections) > 5:
                self.logger.info(f"  ... 还有 {len(sections) - 5} 个章节")

    def _backup_chunks(self, state: ImportGraphState, sections: List[dict]):
        self.log_step("step_7", "备份切片")

        # 优先使用 file_dir，兼容 local_dir (避免因为字段命名引发写入失败)
        local_dir = state.get("file_dir", state.get("local_dir", ""))
        if not local_dir:
            self.logger.debug("未设置 file_dir/local_dir，跳过备份")
            return

        try:
            os.makedirs(local_dir, exist_ok=True)
            output_path = os.path.join(local_dir, "chunks.json")
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(sections, f, ensure_ascii=False, indent=2)
            self.logger.info(f"已备份到: {output_path}")
        except Exception as e:
            self.logger.warning(f"备份失败: {e}")


# ================================================================== #
#                        兼容 & 测试                                   #
# ================================================================== #

# 兼容当前项目已有的文件/类名拼写
DocumentSpliterNode = DocumentSplitNode
DocumentSplitterNode = DocumentSplitNode

# 实例化节点
node_document_split = DocumentSplitNode()
node_document_spliter = node_document_split
node_document_splitter = node_document_split


if __name__ == "__main__":
    setup_logging()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sample_document_path = os.getenv("DOCUMENT_SPLIT_TEST_FILE", "").strip()
    if not sample_document_path:
        raise SystemExit("Set DOCUMENT_SPLIT_TEST_FILE to the Markdown document you intend to split")

    with open(sample_document_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    state = {
        "file_title": os.path.splitext(os.path.basename(sample_document_path))[0],
        "md_content": content,
        "file_dir": os.path.dirname(sample_document_path)
    }

    result_state = node_document_split.process(state)

    print("\n" + "=" * 50)
    print("切片执行完毕，最终状态字典概览：")
    print("=" * 50)

    preview_chunks = result_state.get("chunks", [])[:10]
    print(json.dumps(preview_chunks, ensure_ascii=False, indent=4))
    print(f"\n...... (共生成 {len(result_state.get('chunks', []))} 个 Chunks, 详情请查看 chunks.json)")
