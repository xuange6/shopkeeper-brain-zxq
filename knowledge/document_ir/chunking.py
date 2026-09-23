"""Structure-aware chunking over DocumentIR blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.document_ir.adapters.table import cells_to_rows
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


_STANDALONE_TYPES = {BlockType.TABLE, BlockType.CODE, BlockType.FORMULA}
_HEADING_TYPES = {BlockType.TITLE, BlockType.HEADING}


def chunk_document(
    document: DocumentIR, config: ChunkingConfig | None = None
) -> DocumentIR:
    """Attach deterministic chunks while preserving block-level grounding.

    The first pass follows document structure. Oversized chunks are split only
    afterwards. Images stay with surrounding instructions on the same page and
    in the same section; an image itself is never split. Repeated heading text
    does not make two different sections interchangeable.
    """

    config = config or ChunkingConfig()
    result = document.model_copy(deep=True)
    blocks_by_section: dict[str, list[DocumentBlock]] = {}
    for block in result.blocks:
        if block.content_layer == "furniture" or block.type in _HEADING_TYPES:
            continue
        if not block.text.strip() and block.image is None:
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
                is_atomic = block.type in _STANDALONE_TYPES or len(pieces) > 1
                if current and (
                    is_atomic
                    or _page_numbers(current[-1][0].provenance) != _page_numbers(block.provenance)
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
        "shopkeeper.structure_aware/1.2",
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
        "version": "1.2",
        "signature": chunker_signature,
        "max_characters": config.max_characters,
        "min_characters": config.min_characters,
        "overlap_characters": config.overlap_characters,
        "merge_peers": config.merge_peers,
        "repeat_table_header": config.repeat_table_header,
    }
    return DocumentIR.model_validate(result.model_dump())


def _split_block(block: DocumentBlock, config: ChunkingConfig) -> list[str]:
    if block.type == BlockType.IMAGE:
        return [block.text]
    if len(block.text) <= config.max_characters:
        return [block.text]
    if block.type == BlockType.TABLE and block.table and block.table.cells:
        return _split_table(block, config)
    if block.type == BlockType.CODE:
        return _line_split(block.text, config.max_characters)
    return _semantic_split(block.text, config)


def _split_table(block: DocumentBlock, config: ChunkingConfig) -> list[str]:
    """Budget every row slice with its captions, units, and footnotes.

    If shared context plus one row cannot fit, fall back to splitting the full
    original block text, not a cells-only reconstruction. This preserves all
    source information, but deliberately does not promise repeated context or
    intact rows in that exceptional case.
    """
    table = block.table
    lines = cells_to_rows(table.cells) if table else []
    if not lines:
        return _semantic_split(block.text, config)

    def render(rows: list[str]) -> str:
        return "\n".join(part for part in [*table.captions, *rows, *table.footnotes] if part)

    header = lines[0]
    if len(render([header])) > config.max_characters:
        return _semantic_split(block.text, config)
    for index, line in enumerate(lines[1:]):
        row_context = [header, line] if config.repeat_table_header or index == 0 else [line]
        if len(render(row_context)) > config.max_characters:
            return _semantic_split(block.text, config)

    pieces: list[str] = []
    current = [header]
    for line in lines[1:]:
        if len(render([*current, line])) > config.max_characters:
            pieces.append(render(current))
            current = [header] if config.repeat_table_header else []
        current.append(line)
    if current:
        pieces.append(render(current))
    return pieces


def _semantic_split(text: str, config: ChunkingConfig) -> list[str]:
    if len(text) <= config.max_characters:
        return [text]
    units = [unit for unit in re.split(r"(?<=[。！？；.!?;\n])", text) if unit]
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
            overlap_length = min(config.overlap_characters, config.max_characters - len(unit))
            overlap = current[-overlap_length:] if overlap_length > 0 else ""
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
            and current.section_id == following.section_id
            and current.title_path == following.title_path
            and _page_numbers(current.provenance) == _page_numbers(following.provenance)
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


def _page_numbers(provenance: list[Provenance]) -> frozenset[int]:
    return frozenset(prov.page_number for prov in provenance if prov.page_number is not None)


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
