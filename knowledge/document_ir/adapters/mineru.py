"""Adapter for MinerU ``content_list_v2`` artifacts.

MinerU remains the PDF parser. This module only translates its output and does
not import MinerU or couple downstream stages to MinerU's private structures.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any

from knowledge.document_ir.adapters.common import build_source, resolve_image_asset, sha256_optional
from knowledge.document_ir.adapters.table import cells_to_text, parse_html_cells
from knowledge.document_ir.ids import logical_document_id, normalized_text, revision_id, stable_id
from knowledge.document_ir.models import (
    BlockType,
    BoundingBox,
    DocumentBlock,
    DocumentIR,
    ImagePayload,
    ParseError,
    ParseStatus,
    ParserDescriptor,
    Provenance,
    RawRange,
    Section,
    TablePayload,
)


_FURNITURE_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text"}
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+\S")
_CAPTION_SECTION_RULE = "mineru.numbered_table_caption_next_sibling/1.0"


class MinerUAdapter:
    name = "mineru"

    def __init__(self, parser_version: str | None = None) -> None:
        self.parser_version = parser_version or self._installed_version()

    @staticmethod
    def _installed_version() -> str:
        try:
            return importlib.metadata.version("mineru")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    @staticmethod
    def find_artifacts(md_path: Path) -> tuple[Path | None, Path | None]:
        stem = md_path.stem.removesuffix("_new")
        parent = md_path.parent
        content_candidates = [
            parent / f"{stem}_content_list_v2.json",
            *sorted(parent.glob("*_content_list_v2.json")),
        ]
        middle_candidates = [
            parent / f"{stem}_middle.json",
            *sorted(parent.glob("*_middle.json")),
        ]
        content = next((path for path in content_candidates if path.is_file()), None)
        middle = next((path for path in middle_candidates if path.is_file()), None)
        return content, middle

    def convert(
        self,
        *,
        source_path: Path,
        content_list_path: Path,
        middle_path: Path | None = None,
        backend: str = "hybrid-auto-engine",
        logical_document_key: str | None = None,
    ) -> DocumentIR:
        source = build_source(source_path, "application/pdf")
        doc_id = logical_document_id(logical_document_key, source.sha256)
        payload = json.loads(content_list_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            pages = payload.get("pages") or payload.get("content") or []
        else:
            pages = payload
        if not isinstance(pages, list):
            raise ValueError("MinerU content_list_v2 must contain a page list")

        page_sizes, middle_metadata = self._load_middle(middle_path)
        root_id = stable_id("sec", doc_id, "root")
        sections = [
            Section(
                id=root_id,
                title=source_path.stem,
                level=0,
                title_path=[],
                order=0,
            )
        ]
        section_by_id = {root_id: sections[0]}
        hierarchy: dict[int, Section] = {}
        current_section = sections[0]
        raw_parts: list[str] = []
        blocks: list[DocumentBlock] = []
        errors: list[ParseError] = []
        section_occurrences: dict[tuple[str, ...], int] = {}
        # Some artifacts flatten every heading to level 1 and absorb a section
        # heading into the following table caption. Recover only an unambiguous
        # next numbered sibling; do not invent a full outline from typography.
        parser_headings = [
            item
            for page in pages if isinstance(page, list)
            for item in page if isinstance(item, dict) and item.get("type") == "title"
        ]
        flat_headings = bool(parser_headings) and all(
            str((item.get("content") or {}).get("level")) == "1"
            for item in parser_headings
        )
        numbered_anchor: tuple[tuple[int, ...], Section, int] | None = None
        recovered_sections = 0

        for page_index, raw_page in enumerate(pages):
            page_number = page_index + 1
            if not isinstance(raw_page, list):
                errors.append(
                    ParseError(
                        code="invalid_page",
                        message=f"page {page_number} is not a list",
                        stage="parse",
                        page_number=page_number,
                    )
                )
                continue
            for item_index, item in enumerate(raw_page):
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "other")
                text, table, image = self._extract_payload(item, content_list_path.parent)
                if not text and not table and not image:
                    continue

                heading_level = self._heading_level(item)
                section_level = heading_level
                section_title = text
                section_inference: dict[str, Any] = {}
                if flat_headings and table and numbered_anchor:
                    caption = self._next_sibling_caption(table, numbered_anchor, page_number)
                    if caption:
                        section_title = caption
                        section_level = numbered_anchor[1].level
                        section_inference = {
                            "rule": _CAPTION_SECTION_RULE,
                            "original_section_id": current_section.id,
                            "anchor_section_id": numbered_anchor[1].id,
                            "parser_heading_level": heading_level,
                            "derived_section_level": section_level,
                            "caption_index": 0,
                        }
                        recovered_sections += 1
                if section_level:
                    title = normalized_text(section_title) or "Untitled"
                    for level in list(hierarchy):
                        if level >= section_level:
                            hierarchy.pop(level, None)
                    parent = next(
                        (
                            hierarchy[level]
                            for level in range(section_level - 1, 0, -1)
                            if level in hierarchy
                        ),
                        sections[0],
                    )
                    title_path = [*parent.title_path, title]
                    key = tuple(title_path)
                    occurrence = section_occurrences.get(key, 0)
                    section_occurrences[key] = occurrence + 1
                    section_id = stable_id(
                        "sec", doc_id, title_path, page_number, occurrence
                    )
                    current_section = Section(
                        id=section_id,
                        parent_id=parent.id,
                        title=title,
                        level=section_level,
                        title_path=title_path,
                        order=len(sections),
                    )
                    sections.append(current_section)
                    section_by_id[section_id] = current_section
                    hierarchy[section_level] = current_section
                    number = self._heading_number(title)
                    if number is not None:
                        numbered_anchor = (number, current_section, page_number)

                start = sum(len(part) for part in raw_parts)
                rendered = text.strip()
                raw_parts.append(rendered)
                end = start + len(rendered)
                raw_parts.append("\n")
                source_pointer = f"/pages/{page_index}/items/{item_index}"
                bbox = self._bbox(item.get("bbox"), page_sizes.get(page_number))
                provenance = Provenance(
                    page_number=page_number,
                    bbox=bbox,
                    raw_range=RawRange(start=start, end=end, basis="parser_text"),
                    source_pointer=source_pointer,
                )
                block_type = self._block_type(item_type, heading_level)
                block_id = stable_id(
                    "blk",
                    doc_id,
                    source_pointer,
                    block_type.value,
                    normalized_text(rendered),
                    item.get("bbox"),
                    image.sha256 if image else None,
                    table.html if table else None,
                )
                block = DocumentBlock(
                    id=block_id,
                    type=block_type,
                    text=rendered,
                    order=len(blocks),
                    section_id=current_section.id,
                    heading_level=heading_level,
                    title_path=list(current_section.title_path),
                    raw_range=provenance.raw_range,
                    provenance=[provenance],
                    table=table,
                    image=image,
                    content_layer=(
                        "furniture" if item_type in _FURNITURE_TYPES else "body"
                    ),
                    source_pointer=source_pointer,
                    metadata={
                        "mineru_type": item_type,
                        "mineru_sub_type": str(item.get("sub_type") or ""),
                        **({"section_inference": section_inference} if section_inference else {}),
                    },
                )
                blocks.append(block)
                section_by_id[current_section.id].block_ids.append(block.id)

        self._link_continued_tables(blocks)
        raw_text = "".join(raw_parts).rstrip("\n")
        status = ParseStatus.SUCCESS
        if errors:
            status = ParseStatus.PARTIAL if blocks else ParseStatus.FAILED
        elif not any(block.content_layer == "body" for block in blocks):
            status = ParseStatus.REVIEW_REQUIRED
            errors.append(
                ParseError(
                    code="no_body_content",
                    message="MinerU produced no body content",
                    stage="parse",
                    retryable=True,
                )
            )

        block_signature = [
            (block.type.value, block.text, block.source_pointer, block.section_id)
            for block in blocks
        ]
        return DocumentIR(
            document_id=doc_id,
            logical_document_key=logical_document_key.strip() if logical_document_key else None,
            revision_id=revision_id(
                source_sha256=source.sha256,
                parser_name=self.name,
                parser_version=self.parser_version,
                blocks=block_signature,
            ),
            source=source,
            parser=ParserDescriptor(
                name=self.name,
                version=self.parser_version,
                backend=backend,
                artifact_schema="content_list_v2",
            ),
            status=status,
            raw_text=raw_text,
            sections=sections,
            blocks=blocks,
            errors=errors,
            metadata={
                "content_list_path": str(content_list_path.resolve()),
                "middle_path": str(middle_path.resolve()) if middle_path else "",
                **middle_metadata,
                "section_recovery": {
                    "rule": _CAPTION_SECTION_RULE,
                    "flat_parser_headings": flat_headings,
                    "recovered_table_sections": recovered_sections,
                },
            },
        )

    @staticmethod
    def _heading_number(title: str) -> tuple[int, ...] | None:
        match = _NUMBERED_HEADING.match(normalized_text(title))
        try:
            return tuple(int(part) for part in match.group(1).split(".")) if match else None
        except ValueError:
            # An untrusted caption with an excessively long integer is not an
            # outline anchor. Preserve it as ordinary source text instead.
            return None

    @classmethod
    def _next_sibling_caption(
        cls,
        table: TablePayload,
        anchor: tuple[tuple[int, ...], Section, int],
        page_number: int,
    ) -> str | None:
        if len(table.captions) != 1:
            return None
        caption = table.captions[0]
        number = cls._heading_number(caption)
        previous, _, previous_page = anchor
        if (
            number is not None
            and len(number) >= 2
            and len(number) == len(previous)
            and number[:-1] == previous[:-1]
            and number[-1] == previous[-1] + 1
            and 0 <= page_number - previous_page <= 1
        ):
            return caption
        return None

    @staticmethod
    def _load_middle(path: Path | None) -> tuple[dict[int, tuple[float, float]], dict[str, Any]]:
        if not path or not path.is_file():
            return {}, {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, {"middle_artifact_valid": False}
        sizes: dict[int, tuple[float, float]] = {}
        for index, page in enumerate(payload.get("pdf_info") or []):
            size = page.get("page_size") if isinstance(page, dict) else None
            if isinstance(size, list) and len(size) >= 2:
                sizes[index + 1] = (float(size[0]), float(size[1]))
        return sizes, {
            "middle_artifact_valid": True,
            "mineru_backend": str(payload.get("_backend") or ""),
            "ocr_enabled": bool(payload.get("_ocr_enable")),
            "vlm_ocr_enabled": bool(payload.get("_vlm_ocr_enable")),
            "mineru_artifact_version": str(payload.get("_version_name") or ""),
            "page_sizes": {
                str(page): [width, height]
                for page, (width, height) in sorted(sizes.items())
            },
        }

    @staticmethod
    def _bbox(value: Any, page_size: tuple[float, float] | None) -> BoundingBox | None:
        if not isinstance(value, list) or len(value) < 4:
            return None
        # content_list_v2 emits coordinates normalized to a 1000x1000 page.
        return BoundingBox(
            left=float(value[0]),
            top=float(value[1]),
            right=float(value[2]),
            bottom=float(value[3]),
            coordinate_space="normalized_1000",
            page_width=1000,
            page_height=1000,
        )

    @staticmethod
    def _heading_level(item: dict[str, Any]) -> int | None:
        if item.get("type") != "title":
            return None
        try:
            return min(6, max(1, int((item.get("content") or {}).get("level") or 1)))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _block_type(item_type: str, heading_level: int | None) -> BlockType:
        if heading_level:
            return BlockType.TITLE if heading_level == 1 else BlockType.HEADING
        mapping = {
            "paragraph": BlockType.PARAGRAPH,
            "text": BlockType.PARAGRAPH,
            "list": BlockType.LIST,
            "table": BlockType.TABLE,
            "image": BlockType.IMAGE,
            "equation": BlockType.FORMULA,
            "footnote": BlockType.FOOTNOTE,
            "page_header": BlockType.PAGE_HEADER,
            "page_footer": BlockType.PAGE_FOOTER,
            "page_number": BlockType.PAGE_NUMBER,
            "page_aside_text": BlockType.OTHER,
        }
        return mapping.get(item_type, BlockType.OTHER)

    def _extract_payload(
        self, item: dict[str, Any], artifact_dir: Path
    ) -> tuple[str, TablePayload | None, ImagePayload | None]:
        item_type = str(item.get("type") or "")
        content = item.get("content") or {}
        if item_type == "table":
            html = str(content.get("html") or "")
            cells = parse_html_cells(html)
            captions = self._text_list(content.get("table_caption"))
            footnotes = self._text_list(content.get("table_footnote"))
            text = "\n".join([*captions, cells_to_text(cells), *footnotes]).strip()
            return (
                text,
                TablePayload(
                    html=html,
                    cells=cells,
                    captions=captions,
                    footnotes=footnotes,
                ),
                None,
            )
        if item_type == "image":
            relative = str((content.get("image_source") or {}).get("path") or "")
            local = resolve_image_asset(relative, artifact_dir)
            description = normalized_text(str(content.get("content") or ""))
            captions = self._text_list(content.get("image_caption"))
            caption = " ".join(captions)
            subtype = str(item.get("sub_type") or "")
            ocr_text = description if subtype == "text_image" else ""
            text = "\n".join(value for value in (caption, description) if value).strip()
            return (
                text,
                None,
                ImagePayload(
                    uri=relative,
                    local_path=str(local) if local else "",
                    sha256=sha256_optional(local) if local else None,
                    mime_type=("image/" + local.suffix.lstrip(".").lower()) if local else "",
                    caption=caption,
                    description=description,
                    ocr_text=ocr_text,
                ),
            )
        key = {
            "title": "title_content",
            "paragraph": "paragraph_content",
            "page_header": "page_header_content",
            "page_footer": "page_footer_content",
            "page_number": "page_number_content",
            "page_aside_text": "page_aside_text_content",
        }.get(item_type)
        if item_type == "list":
            texts = []
            for list_item in content.get("list_items") or []:
                if isinstance(list_item, dict):
                    value = self._text_list(list_item.get("item_content"))
                    texts.extend(value)
            return "\n".join(texts).strip(), None, None
        if key:
            return " ".join(self._text_list(content.get(key))).strip(), None, None
        return " ".join(self._text_list(content)).strip(), None, None

    @classmethod
    def _text_list(cls, value: Any) -> list[str]:
        texts: list[str] = []
        if isinstance(value, str):
            return [normalized_text(value)] if normalized_text(value) else []
        if isinstance(value, list):
            for item in value:
                texts.extend(cls._text_list(item))
        elif isinstance(value, dict):
            direct = value.get("content")
            if isinstance(direct, str) and normalized_text(direct):
                texts.append(normalized_text(direct))
            else:
                for nested in value.values():
                    texts.extend(cls._text_list(nested))
        return texts

    @staticmethod
    def _link_continued_tables(blocks: list[DocumentBlock]) -> None:
        previous: DocumentBlock | None = None
        for block in blocks:
            if block.type != BlockType.TABLE or block.table is None:
                continue
            if previous and previous.table:
                prev_page = previous.provenance[0].page_number if previous.provenance else None
                page = block.provenance[0].page_number if block.provenance else None
                prev_header = [cell.text for cell in previous.table.cells if cell.row == 0]
                header = [cell.text for cell in block.table.cells if cell.row == 0]
                if (
                    prev_page is not None
                    and page == prev_page + 1
                    and previous.section_id == block.section_id
                    and prev_header
                    and prev_header == header
                ):
                    previous.table.continues_to_block_id = block.id
                    block.table.continues_from_block_id = previous.id
            previous = block
