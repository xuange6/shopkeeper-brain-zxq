"""Grounded citation construction shared by index and API adapters."""

from __future__ import annotations

from typing import Any

from knowledge.document_ir.models import Chunk, DocumentIR


def build_chunk_citation(document: DocumentIR, chunk: Chunk) -> dict[str, Any]:
    pages = sorted(
        {prov.page_number for prov in chunk.provenance if prov.page_number is not None}
    )
    locations = [
        prov.model_dump(mode="json", exclude_none=True) for prov in chunk.provenance
    ]
    blocks_by_id = {block.id: block for block in document.blocks}
    return {
        "document_id": document.document_id,
        "revision_id": document.revision_id,
        "chunk_id": chunk.id,
        "source_uri": document.source.uri,
        "filename": document.source.filename,
        "page_numbers": pages,
        "page_uids": sorted(
            {prov.page_uid for prov in chunk.provenance if prov.page_uid}
        ),
        "section_id": chunk.section_id,
        "title_path": list(chunk.title_path),
        "block_ids": list(chunk.block_ids),
        "block_lineage_ids": [
            blocks_by_id[block_id].lineage_id
            for block_id in chunk.block_ids
            if block_id in blocks_by_id and blocks_by_id[block_id].lineage_id
        ],
        "locations": locations,
    }
