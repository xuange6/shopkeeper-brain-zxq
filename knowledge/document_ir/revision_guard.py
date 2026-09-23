"""Fail-closed check before replacing an indexed logical document.

This is a collision guard, not a proof that two documents are semantically the
same. The caller-supplied logical key remains the primary identity assertion.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from knowledge.document_ir.ids import normalized_text
from knowledge.document_ir.models import BlockType, DocumentIR


DEFAULT_MIN_REVISION_OVERLAP = 0.40
_SHINGLE_LENGTH = 5
_SHORT_BODY_CHARACTERS = 100
_EXCLUDED_TYPES = {
    BlockType.TITLE,
    BlockType.HEADING,
    BlockType.PAGE_HEADER,
    BlockType.PAGE_FOOTER,
    BlockType.PAGE_NUMBER,
}


@dataclass(frozen=True)
class RevisionContinuity:
    accepted: bool
    reason: str
    overlap: float | None
    minimum_overlap: float
    previous_revision_id: str
    previous_units: int
    incoming_units: int
    shared_units: int
    method: str = "body-character-5gram-dice-v1"

    def as_metadata(self) -> dict[str, str | float | int | bool | None]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "overlap": self.overlap,
            "minimum_overlap": self.minimum_overlap,
            "previous_revision_id": self.previous_revision_id,
            "previous_units": self.previous_units,
            "incoming_units": self.incoming_units,
            "shared_units": self.shared_units,
            "method": self.method,
        }


def _body_units(document: DocumentIR) -> Counter[tuple[str, str]]:
    units: Counter[tuple[str, str]] = Counter()
    for block in document.blocks:
        if block.content_layer != "body" or block.type in _EXCLUDED_TYPES:
            continue
        text = normalized_text(block.text).casefold()
        if block.table and not text:
            text = normalized_text(" ".join(cell.text for cell in block.table.cells)).casefold()
        if text:
            if len(text) < _SHINGLE_LENGTH:
                units[("short_text", text)] += 1
            else:
                for start in range(len(text) - _SHINGLE_LENGTH + 1):
                    units[("text", text[start : start + _SHINGLE_LENGTH])] += 1
        if block.image and block.image.sha256:
            units[("image_sha256", block.image.sha256)] += 1
    return units


def _body_text(document: DocumentIR) -> str:
    parts: list[str] = []
    for block in document.blocks:
        if block.content_layer != "body" or block.type in _EXCLUDED_TYPES:
            continue
        text = normalized_text(block.text)
        if block.table and not text:
            text = normalized_text(" ".join(cell.text for cell in block.table.cells))
        if text:
            parts.append(text.casefold())
    return "\n".join(parts)


def assess_revision_continuity(
    before: DocumentIR,
    after: DocumentIR,
    *,
    minimum_overlap: float = DEFAULT_MIN_REVISION_OVERLAP,
) -> RevisionContinuity:
    """Reject an obvious document-key collision before any index mutation.

    Matching document IDs alone are insufficient: two unrelated uploads can be
    assigned the same key by mistake. Headings and page furniture are excluded
    because shared templates should not make different manuals look related.
    """

    if not 0 <= minimum_overlap <= 1:
        raise ValueError("minimum revision overlap must be between 0 and 1")
    if before.document_id != after.document_id:
        raise ValueError("cannot compare revisions of different logical documents")
    if before.source.sha256 == after.source.sha256:
        return RevisionContinuity(
            accepted=True,
            reason="same_source_bytes",
            overlap=None,
            minimum_overlap=minimum_overlap,
            previous_revision_id=before.revision_id,
            previous_units=0,
            incoming_units=0,
            shared_units=0,
            method="source-sha256",
        )
    if not before.logical_document_key or before.logical_document_key != after.logical_document_key:
        raise ValueError("changed source requires the same explicit logical document key")

    previous_text = _body_text(before)
    incoming_text = _body_text(after)
    if previous_text and incoming_text and max(len(previous_text), len(incoming_text)) <= _SHORT_BODY_CHARACTERS:
        previous_count = len(previous_text)
        incoming_count = len(incoming_text)
        matcher = SequenceMatcher(None, previous_text, incoming_text, autojunk=False)
        shared = sum(size for _, _, size in matcher.get_matching_blocks())
        method = "short-body-sequence-v1"
    else:
        previous = _body_units(before)
        incoming = _body_units(after)
        previous_count = sum(previous.values())
        incoming_count = sum(incoming.values())
        shared = sum((previous & incoming).values())
        method = "body-character-5gram-dice-v1"
    denominator = previous_count + incoming_count
    overlap = 2 * shared / denominator if denominator else 0.0
    accepted = bool(previous_count and incoming_count and overlap >= minimum_overlap)
    return RevisionContinuity(
        accepted=accepted,
        reason="body_overlap_accepted" if accepted else "possible_document_key_collision",
        overlap=round(overlap, 6),
        minimum_overlap=minimum_overlap,
        previous_revision_id=before.revision_id,
        previous_units=previous_count,
        incoming_units=incoming_count,
        shared_units=shared,
        method=method,
    )
