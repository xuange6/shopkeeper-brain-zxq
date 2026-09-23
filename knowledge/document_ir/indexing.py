"""Projection from parser-neutral IR chunks to the current vector index rows."""

from __future__ import annotations

import json
from typing import Any

from knowledge.document_ir.citations import build_chunk_citation
from knowledge.document_ir.models import DocumentIR


def chunks_to_index_rows(document: DocumentIR, item_name: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in document.chunks:
        citation = build_chunk_citation(document, chunk)
        pages = citation["page_numbers"]
        title = chunk.title_path[-1] if chunk.title_path else ""
        parent_title = (
            chunk.title_path[-2]
            if len(chunk.title_path) > 1
            else document.source.filename.rsplit(".", 1)[0]
        )
        rows.append(
            {
                "chunk_id": chunk.id,
                "stable_id": chunk.id,
                "document_id": document.document_id,
                "revision_id": document.revision_id,
                "content": chunk.contextual_text,
                "raw_content": chunk.text,
                "title": title,
                "parent_title": parent_title,
                "title_path": json.dumps(chunk.title_path, ensure_ascii=False),
                "part": chunk.part,
                "file_title": document.source.filename.rsplit(".", 1)[0],
                "item_name": item_name,
                "source_uri": document.source.uri,
                "source_sha256": document.source.sha256,
                "section_id": chunk.section_id,
                "block_ids": json.dumps(chunk.block_ids, ensure_ascii=False),
                "page_numbers": json.dumps(pages),
                "page_uids": json.dumps(citation["page_uids"]),
                "block_lineage_ids": json.dumps(citation["block_lineage_ids"]),
                "citation": json.dumps(citation, ensure_ascii=False, sort_keys=True),
                "parser_name": document.parser.name,
                "parser_version": document.parser.version,
                "ir_schema_version": document.schema_version,
                "has_table": bool(chunk.table_block_ids),
                "has_image": bool(chunk.image_block_ids),
            }
        )
    return rows
