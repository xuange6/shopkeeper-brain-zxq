"""Stable, parser-neutral document intermediate representation."""

from knowledge.document_ir.diff import DocumentDiff, diff_documents
from knowledge.document_ir.models import (
    BoundingBox,
    Chunk,
    DocumentBlock,
    DocumentIR,
    DocumentSource,
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

__all__ = [
    "BoundingBox",
    "Chunk",
    "DocumentBlock",
    "DocumentDiff",
    "DocumentIR",
    "DocumentSource",
    "ImagePayload",
    "ParseError",
    "ParseStatus",
    "ParserDescriptor",
    "Provenance",
    "RawRange",
    "Section",
    "TableCell",
    "TablePayload",
    "diff_documents",
]
