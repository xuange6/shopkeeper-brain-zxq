"""Conservative page/block identity across explicitly related document revisions.

Page numbers and parser item indexes are locations, not identity. Ambiguous matches
are deliberately left unmatched rather than inventing continuity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher

from knowledge.document_ir.ids import normalized_text, stable_id
from knowledge.document_ir.models import DocumentBlock, DocumentIR


def _body_by_page(document: DocumentIR) -> dict[int, list[DocumentBlock]]:
    pages: dict[int, list[DocumentBlock]] = defaultdict(list)
    for block in document.blocks:
        if block.content_layer != "body":
            continue
        for number in {p.page_number for p in block.provenance if p.page_number}:
            pages[number].append(block)
    return dict(pages)


def _page_text(blocks: list[DocumentBlock]) -> str:
    return normalized_text(" ".join(
        f"{b.type.value}:{b.text}:{b.image.sha256 if b.image else ''}"
        for b in blocks
    ))


def _block_signature(block: DocumentBlock) -> tuple[str, tuple[str, ...], str]:
    return block.type.value, tuple(block.title_path), normalized_text(block.text)


def seed_lineage(document: DocumentIR) -> DocumentIR:
    """Assign deterministic identities without claiming ambiguous duplicate content."""

    result = document.model_copy(deep=True)
    pages = _body_by_page(result)
    page_text = {number: _page_text(blocks) for number, blocks in pages.items()}
    page_counts = Counter(page_text.values())
    page_ids = {
        number: stable_id(
            "page",
            result.document_id,
            text if text and page_counts[text] == 1 else (result.revision_id, number),
        )
        for number, text in page_text.items()
    }
    signatures = [_block_signature(block) for block in result.blocks]
    counts = Counter(signatures)
    for block in result.blocks:
        signature = _block_signature(block)
        if block.lineage_id is None:
            block.lineage_id = stable_id(
                "lin",
                result.document_id,
                signature if counts[signature] == 1 else (result.revision_id, block.order),
            )
        for provenance in block.provenance:
            if provenance.page_number in page_ids and provenance.page_uid is None:
                provenance.page_uid = page_ids[provenance.page_number]
    return DocumentIR.model_validate(result.model_dump())


def _unique_best_matches(
    old: dict[int, str], new: dict[int, str], *, threshold: float, margin: float
) -> dict[int, int]:
    """Return new-index -> old-index matches only when both sides agree uniquely."""

    scores = {
        (new_index, old_index): SequenceMatcher(None, new_text, old_text).ratio()
        for new_index, new_text in new.items()
        for old_index, old_text in old.items()
        if new_text and old_text
    }
    result: dict[int, int] = {}
    for new_index in new:
        ranking = sorted(
            ((score, old_index) for (candidate, old_index), score in scores.items() if candidate == new_index),
            reverse=True,
        )
        if not ranking or ranking[0][0] < threshold:
            continue
        if len(ranking) > 1 and ranking[0][0] - ranking[1][0] < margin:
            continue
        score, old_index = ranking[0]
        reverse = sorted(
            ((value, candidate) for (candidate, old), value in scores.items() if old == old_index),
            reverse=True,
        )
        if reverse[0][1] != new_index or (len(reverse) > 1 and reverse[0][0] - reverse[1][0] < margin):
            continue
        result[new_index] = old_index
    return result


def align_document(before: DocumentIR, after: DocumentIR) -> DocumentIR:
    """Inherit page/block lineage only for confidently matched revisions.

    Call after normalization, before chunking. An explicit logical key is required
    when source bytes differ; filenames are never used as an identity assertion.
    """

    if before.document_id != after.document_id:
        raise ValueError("cannot align different logical documents")
    if before.source.sha256 != after.source.sha256 and not (
        before.logical_document_key
        and before.logical_document_key == after.logical_document_key
    ):
        raise ValueError("cross-revision alignment requires the same explicit logical key")

    old = seed_lineage(before)
    result = seed_lineage(after)
    old_pages = _body_by_page(old)
    new_pages = _body_by_page(result)
    page_matches = _unique_best_matches(
        {number: _page_text(blocks) for number, blocks in old_pages.items()},
        {number: _page_text(blocks) for number, blocks in new_pages.items()},
        threshold=0.70,
        margin=0.08,
    )
    old_page_uids = {
        number: next(
            (p.page_uid for b in blocks for p in b.provenance if p.page_number == number and p.page_uid),
            None,
        )
        for number, blocks in old_pages.items()
    }
    for block in result.blocks:
        for provenance in block.provenance:
            old_number = page_matches.get(provenance.page_number)
            if old_number is not None:
                provenance.page_uid = old_page_uids[old_number]

    old_candidates = {block.order: block for block in old.blocks if block.content_layer == "body"}
    new_candidates = {block.order: block for block in result.blocks if block.content_layer == "body"}
    used_old: set[int] = set()
    matched: dict[int, int] = {}

    # Exact matches are global but must be unique. This survives inserted pages
    # and changed parser item indices without relying on either as identity.
    old_signatures: dict[tuple, list[int]] = defaultdict(list)
    new_signatures: dict[tuple, list[int]] = defaultdict(list)
    for index, block in old_candidates.items():
        old_signatures[_block_signature(block)].append(index)
    for index, block in new_candidates.items():
        new_signatures[_block_signature(block)].append(index)
    for signature, new_indexes in new_signatures.items():
        old_indexes = old_signatures.get(signature, [])
        if len(new_indexes) == len(old_indexes) == 1:
            matched[new_indexes[0]] = old_indexes[0]
            used_old.add(old_indexes[0])

    # Changed text can inherit lineage only when the page/section/type and
    # similarity produce an unambiguous mutual-best match.
    remaining_old = {index: block for index, block in old_candidates.items() if index not in used_old}
    remaining_new = {index: block for index, block in new_candidates.items() if index not in matched}
    scores: dict[tuple[int, int], float] = {}
    for new_index, new_block in remaining_new.items():
        for old_index, old_block in remaining_old.items():
            if new_block.type != old_block.type or new_block.title_path != old_block.title_path:
                continue
            new_uids = {p.page_uid for p in new_block.provenance if p.page_uid}
            old_uids = {p.page_uid for p in old_block.provenance if p.page_uid}
            if new_uids and old_uids and not new_uids.intersection(old_uids):
                continue
            ratio = SequenceMatcher(
                None, normalized_text(new_block.text), normalized_text(old_block.text)
            ).ratio()
            if ratio >= (0.78 if new_uids else 0.85):
                scores[new_index, old_index] = ratio
    for new_index in remaining_new:
        ranking = sorted(
            ((score, old_index) for (candidate, old_index), score in scores.items() if candidate == new_index),
            reverse=True,
        )
        if not ranking or (len(ranking) > 1 and ranking[0][0] - ranking[1][0] < 0.08):
            continue
        old_index = ranking[0][1]
        reverse = sorted(
            ((score, candidate) for (candidate, old), score in scores.items() if old == old_index),
            reverse=True,
        )
        if reverse[0][1] == new_index and (len(reverse) == 1 or reverse[0][0] - reverse[1][0] >= 0.08):
            matched[new_index] = old_index

    for new_index, old_index in matched.items():
        new_candidates[new_index].lineage_id = old_candidates[old_index].lineage_id
    result.metadata["alignment"] = {
        "previous_revision_id": old.revision_id,
        "matched_pages": {str(new): old_number for new, old_number in sorted(page_matches.items())},
        "matched_blocks": len(matched),
        "unmatched_blocks": len(new_candidates) - len(matched),
        "method": "unique-content-and-mutual-best-v1",
    }
    return DocumentIR.model_validate(result.model_dump())
