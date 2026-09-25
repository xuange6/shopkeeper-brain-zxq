"""Structure-preserving adapter for Markdown source files."""

from __future__ import annotations

import re
from pathlib import Path

from knowledge.document_ir.adapters.common import build_source, resolve_image_asset, sha256_optional
from knowledge.document_ir.ids import logical_document_id, normalized_text, revision_id, stable_id
from knowledge.document_ir.models import (
    BlockType,
    DocumentBlock,
    DocumentIR,
    ImagePayload,
    ParseError,
    ParseStatus,
    ParserDescriptor,
    Provenance,
    RawRange,
    Section,
    TableCell,
    TablePayload,
)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_IMAGE_RE = re.compile(r'^\s*!\[(?P<alt>[^]]*)\]\((?P<uri>[^)\s]+)(?:\s+["\'].*?["\'])?\)\s*$')
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


class MarkdownAdapter:
    name = "markdown"
    version = "1.0"

    def convert(self, source_path: Path, *, logical_document_key: str | None = None) -> DocumentIR:
        source = build_source(source_path, "text/markdown")
        doc_id = logical_document_id(logical_document_key, source.sha256)
        text = source_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        offsets: list[int] = []
        cursor = 0
        for line in lines:
            offsets.append(cursor)
            cursor += len(line)

        root_id = stable_id("sec", doc_id, "root")
        root = Section(
            id=root_id,
            title=source_path.stem,
            level=0,
            title_path=[],
            order=0,
        )
        sections = [root]
        section_by_id = {root_id: root}
        hierarchy: dict[int, Section] = {}
        current = root
        occurrences: dict[tuple[str, ...], int] = {}
        blocks: list[DocumentBlock] = []
        index = 0

        while index < len(lines):
            raw_line = lines[index]
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                index += 1
                continue

            heading = _HEADING_RE.match(line)
            if heading:
                level = len(heading.group(1))
                title = normalized_text(heading.group(2))
                for old_level in list(hierarchy):
                    if old_level >= level:
                        hierarchy.pop(old_level, None)
                parent = next(
                    (
                        hierarchy[parent_level]
                        for parent_level in range(level - 1, 0, -1)
                        if parent_level in hierarchy
                    ),
                    root,
                )
                title_path = [*parent.title_path, title]
                occurrence = occurrences.get(tuple(title_path), 0)
                occurrences[tuple(title_path)] = occurrence + 1
                current = Section(
                    id=stable_id("sec", doc_id, title_path, occurrence),
                    parent_id=parent.id,
                    title=title,
                    level=level,
                    title_path=title_path,
                    order=len(sections),
                )
                sections.append(current)
                section_by_id[current.id] = current
                hierarchy[level] = current
                block = self._block(
                    doc_id=doc_id,
                    block_type=BlockType.TITLE if level == 1 else BlockType.HEADING,
                    rendered=title,
                    source_text=line,
                    start=offsets[index],
                    end=offsets[index] + len(line),
                    start_line=index,
                    end_line=index,
                    section=current,
                    order=len(blocks),
                    heading_level=level,
                )
                blocks.append(block)
                current.block_ids.append(block.id)
                index += 1
                continue

            if line.lstrip().startswith(("```", "~~~")):
                marker = line.lstrip()[:3]
                end_index = index + 1
                while end_index < len(lines):
                    if lines[end_index].lstrip().startswith(marker):
                        end_index += 1
                        break
                    end_index += 1
                block = self._range_block(
                    lines,
                    offsets,
                    index,
                    end_index,
                    doc_id,
                    current,
                    len(blocks),
                    BlockType.CODE,
                )
                blocks.append(block)
                current.block_ids.append(block.id)
                index = end_index
                continue

            image_match = _IMAGE_RE.match(line)
            if image_match:
                uri = image_match.group("uri")
                local = resolve_image_asset(uri, source_path.parent)
                alt = normalized_text(image_match.group("alt"))
                image = ImagePayload(
                    uri=uri,
                    local_path=str(local) if local else "",
                    sha256=sha256_optional(local) if local else None,
                    mime_type="",
                    caption=alt,
                    description=alt,
                )
                block = self._block(
                    doc_id=doc_id,
                    block_type=BlockType.IMAGE,
                    rendered=alt or uri,
                    source_text=line,
                    start=offsets[index],
                    end=offsets[index] + len(line),
                    start_line=index,
                    end_line=index,
                    section=current,
                    order=len(blocks),
                    image=image,
                )
                blocks.append(block)
                current.block_ids.append(block.id)
                index += 1
                continue

            if index + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(
                lines[index + 1].rstrip("\r\n")
            ):
                end_index = index + 2
                while end_index < len(lines) and "|" in lines[end_index]:
                    if not lines[end_index].strip():
                        break
                    end_index += 1
                cells = self._markdown_table_cells(lines[index:end_index])
                rendered = "\n".join(
                    " | ".join(cell.text for cell in cells if cell.row == row)
                    for row in sorted({cell.row for cell in cells})
                )
                table = TablePayload(cells=cells)
                block = self._range_block(
                    lines,
                    offsets,
                    index,
                    end_index,
                    doc_id,
                    current,
                    len(blocks),
                    BlockType.TABLE,
                    rendered=rendered,
                    table=table,
                )
                blocks.append(block)
                current.block_ids.append(block.id)
                index = end_index
                continue

            end_index = index + 1
            while end_index < len(lines):
                candidate = lines[end_index].rstrip("\r\n")
                if (
                    not candidate.strip()
                    or _HEADING_RE.match(candidate)
                    or candidate.lstrip().startswith(("```", "~~~"))
                    or _IMAGE_RE.match(candidate)
                    or (
                        end_index + 1 < len(lines)
                        and _TABLE_SEPARATOR_RE.match(
                            lines[end_index + 1].rstrip("\r\n")
                        )
                    )
                ):
                    break
                end_index += 1
            stripped = line.lstrip()
            block_type = (
                BlockType.LIST
                if re.match(r"^(?:[-+*]|\d+[.)])\s+", stripped)
                else BlockType.PARAGRAPH
            )
            block = self._range_block(
                lines,
                offsets,
                index,
                end_index,
                doc_id,
                current,
                len(blocks),
                block_type,
            )
            blocks.append(block)
            current.block_ids.append(block.id)
            index = end_index

        status = ParseStatus.SUCCESS if blocks else ParseStatus.REVIEW_REQUIRED
        errors = []
        if not blocks:
            errors.append(
                ParseError(
                    code="empty_document",
                    message="Markdown document has no content blocks",
                    stage="parse",
                    retryable=False,
                )
            )
        return DocumentIR(
            document_id=doc_id,
            logical_document_key=logical_document_key.strip() if logical_document_key else None,
            revision_id=revision_id(
                source_sha256=source.sha256,
                parser_name=self.name,
                parser_version=self.version,
                blocks=[(b.type.value, b.text, b.source_pointer) for b in blocks],
            ),
            source=source,
            parser=ParserDescriptor(
                name=self.name,
                version=self.version,
                backend="native",
                artifact_schema="commonmark-subset",
            ),
            status=status,
            raw_text=text,
            sections=sections,
            blocks=blocks,
            errors=errors,
        )

    def _range_block(
        self,
        lines: list[str],
        offsets: list[int],
        start_index: int,
        end_index: int,
        doc_id: str,
        section: Section,
        order: int,
        block_type: BlockType,
        *,
        rendered: str | None = None,
        table: TablePayload | None = None,
    ) -> DocumentBlock:
        raw = "".join(lines[start_index:end_index]).rstrip("\r\n")
        end = offsets[end_index - 1] + len(lines[end_index - 1].rstrip("\r\n"))
        return self._block(
            doc_id=doc_id,
            block_type=block_type,
            rendered=rendered if rendered is not None else raw.strip(),
            source_text=raw,
            start=offsets[start_index],
            end=end,
            start_line=start_index,
            end_line=end_index - 1,
            section=section,
            order=order,
            table=table,
        )

    @staticmethod
    def _block(
        *,
        doc_id: str,
        block_type: BlockType,
        rendered: str,
        source_text: str,
        start: int,
        end: int,
        start_line: int,
        end_line: int,
        section: Section,
        order: int,
        heading_level: int | None = None,
        table: TablePayload | None = None,
        image: ImagePayload | None = None,
    ) -> DocumentBlock:
        pointer = f"/lines/{start_line + 1}-{end_line + 1}"
        raw_range = RawRange(start=start, end=end, basis="source_text")
        return DocumentBlock(
            id=stable_id(
                "blk", doc_id, pointer, block_type.value, normalized_text(source_text),
                image.sha256 if image else None,
                table.html if table else None,
            ),
            type=block_type,
            text=rendered,
            order=order,
            section_id=section.id,
            heading_level=heading_level,
            title_path=list(section.title_path),
            raw_range=raw_range,
            provenance=[Provenance(raw_range=raw_range, source_pointer=pointer)],
            table=table,
            image=image,
            source_pointer=pointer,
        )

    @staticmethod
    def _markdown_table_cells(lines: list[str]) -> list[TableCell]:
        cells: list[TableCell] = []
        logical_row = 0
        for line_index, raw in enumerate(lines):
            line = raw.strip().strip("|")
            if line_index == 1 and _TABLE_SEPARATOR_RE.match(raw.rstrip("\r\n")):
                continue
            values = [value.strip() for value in line.split("|")]
            for column, value in enumerate(values):
                cells.append(
                    TableCell(
                        row=logical_row,
                        column=column,
                        text=value,
                        is_header=logical_row == 0,
                    )
                )
            logical_row += 1
        return cells
