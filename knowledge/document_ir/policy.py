"""Operational policy for parser outcomes."""

from __future__ import annotations

from enum import Enum

from knowledge.document_ir.models import ParseStatus


class ParseAction(str, Enum):
    CONTINUE = "continue"
    CONTINUE_WITH_WARNINGS = "continue_with_warnings"
    RETRY = "retry"
    QUARANTINE = "quarantine"
    MANUAL_REVIEW = "manual_review"
    STOP = "stop"


def action_for_status(
    status: ParseStatus, *, attempt: int = 1, max_attempts: int = 1
) -> ParseAction:
    if status == ParseStatus.SUCCESS:
        return ParseAction.CONTINUE
    if status == ParseStatus.PARTIAL:
        return ParseAction.CONTINUE_WITH_WARNINGS
    if status == ParseStatus.REVIEW_REQUIRED:
        return ParseAction.MANUAL_REVIEW
    if status == ParseStatus.PENDING:
        return ParseAction.STOP
    if status == ParseStatus.FAILED and attempt < max_attempts:
        return ParseAction.RETRY
    if status in {ParseStatus.FAILED, ParseStatus.QUARANTINED}:
        return ParseAction.QUARANTINE
    return ParseAction.STOP
