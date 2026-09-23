"""Structure-aware chunking over DocumentIR blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.document_ir.ids import normalized_text, stable_id
from knowledge.document_ir.models import (
    BlockType,
    Chunk,
    DocumentBlock,
    DocumentIR,
    Provenance,
    RawRange,
)


@dataclass(frozen=True)
class ChunkingConfig:
    max_characters: int = 1200
    min_characters: int = 300
    overlap_characters: int = 120
    merge_peers: bool = True
    repeat_table_header: bool = True


_ATOMIC_TYPES = {BlockType.TABLE, BlockType.IMAGE, BlockType.CODE, BlockType.FORMULA}
_HEADING_TYPES = {BlockType.TITLE, BlockType.HEADING}


def chunk_document(
    document: DocumentIR, config: ChunkingConfig | None = None
) -> DocumentIR:
    """Attach deterministic chunks while preserving block-level grounding.

    The first pass follows document structure. Oversized chunks are split only
    afterwards, and undersized peers merge only when their title paths match.
    """

    config = config or ChunkingConfig()
    result = document.model_copy(deep=True)
    blocks_by_section: dict[str, list[DocumentBlock]] = {}
    for block in result.blocks:
        if block.content_layer == "furniture" or block.type in _HEADING_TYPES:
            continue
        if not block.text.strip():
            continue
        blocks_by_section.setdefault(block.section_id, []).append(block)

    candidates: list[list[tuple[DocumentBlock, str, int]]] = []
    for section in result.sections:
        current: list[tuple[DocumentBlock, str, int]] = []
        current_length = 0
        for block in blocks_by_section.get(section.id, []):
            pieces = _split_block(block, config)
            for piece_index, piece in enumerate(pieces):
                piece_length = len(piece)
                is_atomic = block.type in _ATOMIC_TYPES or len(pieces) > 1
                if current and (
                    is_atomic
                    or current_length + 2 + piece_length > config.max_characters
                ):
                    candidates.append(current)
                    current = []
                    current_length = 0
                current.append((block, piece, piece_index))
                current_length += piece_length + (2 if current_length else 0)
                if is_atomic:
                    candidates.append(current)
                    current = []
                    current_length = 0
        if current:
            candidates.append(current)

    chunker_signature = stable_id(
        "cfg",
        "shopkeeper.structure_aware/1.1",
        config.max_characters,
        config.min_characters,
        config.overlap_characters,
        config.merge_peers,
        config.repeat_table_header,
    )
    chunks = [
        _build_chunk(result, candidate, index, chunker_signature)
        for index, candidate in enumerate(candidates)
    ]
    if config.merge_peers:
        chunks = _merge_peers(result, chunks, config, chunker_signature)
    result.chunks = chunks
    result.metadata["chunker"] = {
        "name": "shopkeeper.structure_aware",
        "version": "1.1",
        "signature": chunker_signature,
        "max_characters": config.max_characters,
        "min_characters": config.min_characters,
        "overlap_characters": config.overlap_characters,
        "merge_peers": config.merge_peers,
        "repeat_table_header": config.repeat_table_header,
    }
    return DocumentIR.model_validate(result.model_dump())


def _split_block(block: DocumentBlock, config: ChunkingConfig) -> list[str]:
    if len(block.text) <= config.max_characters:
        return [block.text]
    if block.type == BlockType.TABLE and block.table and block.table.cells:
        rows: dict[int, list[str]] = {}
        for cell in block.table.cells:
            rows.setdefault(cell.row, []).append(cell.text)
        lines = [" | ".join(rows[row]) for row in sorted(rows)]
        if not lines:
            return _semantic_split(block.text, config)
        header = lines[0]
        pieces: list[str] = []
        current = header
        for line in lines[1:]:
            candidate = current + "\n" + line
            if len(candidate) > config.max_characters and current != header:
                pieces.append(current)
                current = (header + "\n" + line) if config.repeat_table_header else line
            else:
                current = candidate
        if current:
            pieces.append(current)
        return [piece for value in pieces for piece in _semantic_split(value, config)]
    if block.type == BlockType.CODE:
        return _line_split(block.text, config.max_characters)
    return _semantic_split(block.text, config)


def _semantic_split(text: str, config: ChunkingConfig) -> list[str]:
    if len(text) <= config.max_characters:
        return [text]
    units = [unit for unit in re.split(r"(?<=[。！？；.!?;])|\n+", text) if unit]
    if len(units) <= 1:
        return _line_split(text, config.max_characters, config.overlap_characters)
    pieces: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > config.max_characters:
            if current:
                pieces.append(current.strip())
                current = ""
            pieces.extend(
                _line_split(unit, config.max_characters, config.overlap_characters)
            )
        elif len(current) + len(unit) <= config.max_characters:
            current += unit
        else:
            pieces.append(current.strip())
            overlap = current[-config.overlap_characters :] if config.overlap_characters else ""
            current = overlap + unit
    if current.strip():
        pieces.append(current.strip())
    return pieces


def _line_split(text: str, size: int, overlap: int = 0) -> list[str]:
    if size <= 0:
        return [text]
    overlap = min(max(0, overlap), max(0, size // 4))
    step = max(1, size - overlap)
    return [text[index : index + size] for index in range(0, len(text), step)]


def _build_chunk(
    document: DocumentIR,
    candidate: list[tuple[DocumentBlock, str, int]],
    ordinal: int,
    chunker_signature: str,
) -> Chunk:
    blocks: list[DocumentBlock] = []
    for block, _, _ in candidate:
        if not blocks or blocks[-1].id != block.id:
            blocks.append(block)
    text = "\n\n".join(piece for _, piece, _ in candidate).strip()
    title_path = list(blocks[0].title_path)
    contextual = "\n".join([*title_path, text]).strip()
    block_ids = [block.id for block in blocks]
    piece_signature = [(block.id, piece_index, normalized_text(piece)) for block, piece, piece_index in candidate]
    chunk_id = stable_id("chk", document.revision_id, chunker_signature, piece_signature)
    return Chunk(
        id=chunk_id,
        document_id=document.document_id,
        section_id=blocks[0].section_id,
        block_ids=block_ids,
        text=text,
        contextual_text=contextual,
        title_path=title_path,
        raw_ranges=_unique_ranges(blocks),
        provenance=_unique_provenance(blocks),
        table_block_ids=[block.id for block in blocks if block.type == BlockType.TABLE],
        image_block_ids=[block.id for block in blocks if block.type == BlockType.IMAGE],
        part=ordinal + 1,
    )


def _merge_peers(
    document: DocumentIR, chunks: list[Chunk], config: ChunkingConfig, chunker_signature: str
) -> list[Chunk]:
    if not chunks:
        return []
    merged: list[Chunk] = []
    current = chunks[0]
    for following in chunks[1:]:
        combined_length = len(current.text) + 2 + len(following.text)
        if (
            len(current.text) < config.min_characters
            and current.title_path == following.title_path
            and not current.table_block_ids
            and not current.image_block_ids
            and not following.table_block_ids
            and not following.image_block_ids
            and combined_length <= config.max_characters
        ):
            block_ids = list(dict.fromkeys([*current.block_ids, *following.block_ids]))
            text = current.text + "\n\n" + following.text
            current = current.model_copy(
                update={
                    "id": stable_id(
                        "chk", document.revision_id, chunker_signature, block_ids, normalized_text(text)
                    ),
                    "block_ids": block_ids,
                    "text": text,
                    "contextual_text": "\n".join([*current.title_path, text]).strip(),
                    "raw_ranges": _dedupe_models([*current.raw_ranges, *following.raw_ranges]),
                    "provenance": _dedupe_models([*current.provenance, *following.provenance]),
                }
            )
        else:
            merged.append(current)
            current = following
    merged.append(current)
    return [chunk.model_copy(update={"part": index + 1}) for index, chunk in enumerate(merged)]


def _unique_ranges(blocks: list[DocumentBlock]) -> list[RawRange]:
    return _dedupe_models([block.raw_range for block in blocks if block.raw_range])


def _unique_provenance(blocks: list[DocumentBlock]) -> list[Provenance]:
    return _dedupe_models([prov for block in blocks for prov in block.provenance])


def _dedupe_models(items):
    result = []
    seen = set()
    for item in items:
        key = item.model_dump_json(exclude_none=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
