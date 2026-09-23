"""Parser-neutral normalization stage."""

from __future__ import annotations

import unicodedata

from knowledge.document_ir.ids import revision_id
from knowledge.document_ir.lineage import seed_lineage
from knowledge.document_ir.models import DocumentIR, ParseError, ParseStatus, RawRange


def normalize_document(document: DocumentIR) -> DocumentIR:
    """Normalize text and derive section spans without parser-specific knowledge."""

    normalized = document.model_copy(deep=True)
    for block in normalized.blocks:
        block.text = _normalize(block.text)
        block.title_path = [_normalize(title) for title in block.title_path if _normalize(title)]
        if block.image:
            block.image.caption = _normalize(block.image.caption)
            block.image.description = _normalize(block.image.description)
            block.image.ocr_text = _normalize(block.image.ocr_text)
        if block.table:
            block.table.captions = [_normalize(value) for value in block.table.captions]
            block.table.footnotes = [_normalize(value) for value in block.table.footnotes]
            for cell in block.table.cells:
                cell.text = _normalize(cell.text)

    blocks_by_id = {block.id: block for block in normalized.blocks}
    for section in normalized.sections:
        section.title = _normalize(section.title)
        section.title_path = [
            _normalize(title) for title in section.title_path if _normalize(title)
        ]
        section_blocks = [
            blocks_by_id[block_id]
            for block_id in section.block_ids
            if block_id in blocks_by_id
        ]
        pages = {
            prov.page_number
            for block in section_blocks
            for prov in block.provenance
            if prov.page_number is not None
        }
        section.page_numbers = sorted(pages)
        ranges = [block.raw_range for block in section_blocks if block.raw_range]
        if ranges:
            basis = ranges[0].basis
            same_basis = [item for item in ranges if item.basis == basis]
            section.raw_range = RawRange(
                start=min(item.start for item in same_basis),
                end=max(item.end for item in same_basis),
                basis=basis,
            )

    body_blocks = [
        block
        for block in normalized.blocks
        if block.content_layer == "body" and block.text.strip()
    ]
    if not body_blocks and normalized.status not in {
        ParseStatus.FAILED,
        ParseStatus.QUARANTINED,
    }:
        normalized.status = ParseStatus.REVIEW_REQUIRED
        if not any(error.code == "no_body_content" for error in normalized.errors):
            normalized.errors.append(
                ParseError(
                    code="no_body_content",
                    message="normalization produced no body blocks",
                    stage="normalize",
                    retryable=True,
                )
            )

    normalized.revision_id = revision_id(
        source_sha256=normalized.source.sha256,
        parser_name=normalized.parser.name,
        parser_version=normalized.parser.version,
        blocks=[
            (
                block.type.value,
                block.text,
                block.section_id,
                block.image.sha256 if block.image else None,
                block.table.html if block.table else None,
                [prov.model_dump(mode="json", exclude_none=True) for prov in block.provenance],
            )
            for block in normalized.blocks
        ],
    )
    normalized.metadata["normalizer"] = "shopkeeper.document_ir.normalize/1.0"
    return seed_lineage(DocumentIR.model_validate(normalized.model_dump()))


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()
