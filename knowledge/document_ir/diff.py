"""Explainable comparisons between two versions of the same document."""

from __future__ import annotations

from pydantic import BaseModel, Field

from knowledge.document_ir.ids import normalized_text
from knowledge.document_ir.lineage import align_document, seed_lineage
from knowledge.document_ir.models import DocumentBlock, DocumentIR


class ModifiedBlock(BaseModel):
    before_id: str
    after_id: str
    anchor: str
    before_text: str
    after_text: str
    before_asset_sha256: str | None = None
    after_asset_sha256: str | None = None


class PageMove(BaseModel):
    page_uid: str
    before_number: int
    after_number: int


class StructuralBlockChange(BaseModel):
    before_id: str
    after_id: str
    anchor: str
    before_section_id: str
    after_section_id: str
    before_title_path: list[str]
    after_title_path: list[str]


class DocumentDiff(BaseModel):
    same_document: bool
    unchanged_block_ids: list[str] = Field(default_factory=list)
    unchanged_lineage_ids: list[str] = Field(default_factory=list)
    added_block_ids: list[str] = Field(default_factory=list)
    removed_block_ids: list[str] = Field(default_factory=list)
    modified_blocks: list[ModifiedBlock] = Field(default_factory=list)
    page_moves: list[PageMove] = Field(default_factory=list)
    structural_changes: list[StructuralBlockChange] = Field(default_factory=list)


def diff_documents(before: DocumentIR, after: DocumentIR) -> DocumentDiff:
    same_document = before.document_id == after.document_id
    old = seed_lineage(before)
    new = align_document(old, after) if same_document else seed_lineage(after)
    before_by_id = {block.id: block for block in old.blocks}
    after_by_id = {block.id: block for block in new.blocks}
    before_by_lineage = {block.lineage_id: block for block in old.blocks}
    after_by_lineage = {block.lineage_id: block for block in new.blocks}
    shared = set(before_by_lineage) & set(after_by_lineage) if same_document else set()
    # "Unchanged" means confirmed identity plus unchanged content. A repeated
    # block coinciding at the same parser position in an ambiguous new source
    # is not both unchanged and added/removed. Section corrections are separate.
    unchanged = sorted(
        block_id for block_id in set(before_by_id) & set(after_by_id)
        if before_by_id[block_id].lineage_id in shared
        and before_by_id[block_id].lineage_id == after_by_id[block_id].lineage_id
        and _content_signature(before_by_id[block_id]) == _content_signature(after_by_id[block_id])
    )
    unchanged_lineage = sorted(
        lineage for lineage in shared
        if _content_signature(before_by_lineage[lineage])
        == _content_signature(after_by_lineage[lineage])
    )
    removed = {block.id for lineage, block in before_by_lineage.items() if lineage not in shared}
    added = {block.id for lineage, block in after_by_lineage.items() if lineage not in shared}
    modified: list[ModifiedBlock] = []
    structural_changes: list[StructuralBlockChange] = []
    for lineage in sorted(shared):
        before_block = before_by_lineage[lineage]
        after_block = after_by_lineage[lineage]
        if (
            before_block.section_id != after_block.section_id
            or before_block.title_path != after_block.title_path
        ):
            structural_changes.append(
                StructuralBlockChange(
                    before_id=before_block.id,
                    after_id=after_block.id,
                    anchor=lineage,
                    before_section_id=before_block.section_id,
                    after_section_id=after_block.section_id,
                    before_title_path=list(before_block.title_path),
                    after_title_path=list(after_block.title_path),
                )
            )
        if _content_signature(before_block) != _content_signature(after_block):
            modified.append(
                ModifiedBlock(
                    before_id=before_block.id,
                    after_id=after_block.id,
                    anchor=lineage,
                    before_text=before_block.text,
                    after_text=after_block.text,
                    before_asset_sha256=(
                        before_block.image.sha256 if before_block.image else None
                    ),
                    after_asset_sha256=(
                        after_block.image.sha256 if after_block.image else None
                    ),
                )
            )

    def pages(document: DocumentIR) -> dict[str, int]:
        return {
            provenance.page_uid: provenance.page_number
            for block in document.blocks
            for provenance in block.provenance
            if provenance.page_uid and provenance.page_number is not None
        }

    before_pages = pages(old)
    after_pages = pages(new)
    moves = [
        PageMove(page_uid=uid, before_number=before_pages[uid], after_number=after_pages[uid])
        for uid in sorted(set(before_pages) & set(after_pages))
        if before_pages[uid] != after_pages[uid]
    ]

    return DocumentDiff(
        same_document=same_document,
        unchanged_block_ids=unchanged,
        unchanged_lineage_ids=unchanged_lineage,
        added_block_ids=sorted(added),
        removed_block_ids=sorted(removed),
        modified_blocks=modified,
        page_moves=moves,
        structural_changes=structural_changes,
    )


def _content_signature(block: DocumentBlock) -> tuple[str, str, str, str | None, str | None]:
    return (
        block.type.value,
        block.content_layer,
        normalized_text(block.text),
        block.image.sha256 if block.image else None,
        block.table.html if block.table else None,
    )
