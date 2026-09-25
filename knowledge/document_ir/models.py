"""Versioned data contract for structured documents.

The model deliberately owns no parser, chunker, enrichment, or database logic.
Adapters populate blocks and provenance; later stages normalize, chunk, enrich,
and project the model into storage-specific rows.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "1.1.0"


class IrModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParseStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    REVIEW_REQUIRED = "review_required"


class BlockType(str, Enum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    CODE = "code"
    FORMULA = "formula"
    FOOTNOTE = "footnote"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"
    PAGE_NUMBER = "page_number"
    OTHER = "other"


class RawRange(IrModel):
    """Half-open character range in ``DocumentIR.raw_text`` or source text."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    basis: Literal["source_text", "parser_text"] = "source_text"

    @model_validator(mode="after")
    def validate_order(self) -> "RawRange":
        if self.end < self.start:
            raise ValueError("raw range end must be >= start")
        return self


class BoundingBox(IrModel):
    left: float
    top: float
    right: float
    bottom: float
    coordinate_space: Literal["points", "pixels", "normalized_1000"] = (
        "normalized_1000"
    )
    page_width: float | None = Field(default=None, gt=0)
    page_height: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "BoundingBox":
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("bbox coordinates must be left/top/right/bottom ordered")
        return self


class Provenance(IrModel):
    """Grounding location for a block or chunk.

    Page numbers are one-based at this public boundary, regardless of parser
    conventions. ``source_pointer`` identifies the exact parser artifact item.
    """

    page_number: int | None = Field(default=None, ge=1)
    page_uid: str | None = None
    bbox: BoundingBox | None = None
    raw_range: RawRange | None = None
    source_pointer: str = ""


class DocumentSource(IrModel):
    uri: str
    filename: str
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ParserDescriptor(IrModel):
    name: str
    version: str
    backend: str = ""
    artifact_schema: str = ""


class ParseError(IrModel):
    code: str
    message: str
    stage: Literal["parse", "normalize", "chunk", "enrich", "index"]
    retryable: bool = False
    page_number: int | None = Field(default=None, ge=1)
    details: dict[str, Any] = Field(default_factory=dict)


class TableCell(IrModel):
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    text: str
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    is_header: bool = False


class TablePayload(IrModel):
    html: str = ""
    cells: list[TableCell] = Field(default_factory=list)
    captions: list[str] = Field(default_factory=list)
    footnotes: list[str] = Field(default_factory=list)
    continues_from_block_id: str | None = None
    continues_to_block_id: str | None = None


class ImagePayload(IrModel):
    uri: str = ""
    local_path: str = ""
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mime_type: str = ""
    caption: str = ""
    description: str = ""
    ocr_text: str = ""


class DocumentBlock(IrModel):
    id: str
    lineage_id: str | None = None
    type: BlockType
    text: str
    order: int = Field(ge=0)
    section_id: str
    heading_level: int | None = Field(default=None, ge=1, le=6)
    title_path: list[str] = Field(default_factory=list)
    raw_range: RawRange | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    table: TablePayload | None = None
    image: ImagePayload | None = None
    content_layer: Literal["body", "furniture"] = "body"
    source_pointer: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Section(IrModel):
    id: str
    parent_id: str | None = None
    title: str
    level: int = Field(ge=0, le=6)
    title_path: list[str] = Field(default_factory=list)
    order: int = Field(ge=0)
    block_ids: list[str] = Field(default_factory=list)
    raw_range: RawRange | None = None
    page_numbers: list[int] = Field(default_factory=list)


class Chunk(IrModel):
    id: str
    document_id: str
    section_id: str
    block_ids: list[str]
    text: str
    contextual_text: str
    title_path: list[str] = Field(default_factory=list)
    raw_ranges: list[RawRange] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    table_block_ids: list[str] = Field(default_factory=list)
    image_block_ids: list[str] = Field(default_factory=list)
    part: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentIR(IrModel):
    schema_name: Literal["shopkeeper.document_ir"] = "shopkeeper.document_ir"
    schema_version: Literal["1.0.0", "1.1.0"] = SCHEMA_VERSION
    document_id: str
    logical_document_key: str | None = None
    revision_id: str
    source: DocumentSource
    parser: ParserDescriptor
    status: ParseStatus
    raw_text: str = ""
    sections: list[Section] = Field(default_factory=list)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    errors: list[ParseError] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> "DocumentIR":
        section_ids = [section.id for section in self.sections]
        block_ids = [block.id for block in self.blocks]
        chunk_ids = [chunk.id for chunk in self.chunks]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("section IDs must be unique")
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block IDs must be unique")
        lineage_ids = [block.lineage_id for block in self.blocks if block.lineage_id]
        if len(lineage_ids) != len(set(lineage_ids)):
            raise ValueError("block lineage IDs must be unique within a revision")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("chunk IDs must be unique")

        page_to_uid: dict[int, str] = {}
        uid_to_page: dict[str, int] = {}
        for block in self.blocks:
            for provenance in block.provenance:
                if provenance.page_number is None or provenance.page_uid is None:
                    continue
                number, uid = provenance.page_number, provenance.page_uid
                if number in page_to_uid and page_to_uid[number] != uid:
                    raise ValueError(f"page {number} has conflicting page UIDs")
                if uid in uid_to_page and uid_to_page[uid] != number:
                    raise ValueError(f"page UID {uid} belongs to multiple pages")
                page_to_uid[number] = uid
                uid_to_page[uid] = number

        known_sections = set(section_ids)
        known_blocks = set(block_ids)
        for section in self.sections:
            if section.parent_id and section.parent_id not in known_sections:
                raise ValueError(f"unknown parent section: {section.parent_id}")
            missing = set(section.block_ids) - known_blocks
            if missing:
                raise ValueError(f"section references unknown blocks: {sorted(missing)}")
        for block in self.blocks:
            if block.section_id not in known_sections:
                raise ValueError(f"block references unknown section: {block.section_id}")
        for chunk in self.chunks:
            if chunk.document_id != self.document_id:
                raise ValueError("chunk document_id does not match document")
            if chunk.section_id not in known_sections:
                raise ValueError(f"chunk references unknown section: {chunk.section_id}")
            missing = set(chunk.block_ids) - known_blocks
            if missing:
                raise ValueError(f"chunk references unknown blocks: {sorted(missing)}")
        return self

    def canonical_dict(self) -> dict[str, Any]:
        """Return deterministic JSON-compatible data for snapshots and storage."""

        return self.model_dump(mode="json", exclude_none=True)
