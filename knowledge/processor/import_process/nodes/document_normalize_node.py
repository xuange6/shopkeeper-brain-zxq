from __future__ import annotations

import os
from pathlib import Path

from knowledge.document_ir.models import DocumentIR, ParseError, ParseStatus
from knowledge.document_ir.lineage import align_document
from knowledge.document_ir.normalize import normalize_document
from knowledge.document_ir.revision_guard import (
    DEFAULT_MIN_REVISION_OVERLAP,
    assess_revision_continuity,
)
from knowledge.document_ir.serialization import load_document_ir, save_document_ir
from knowledge.processor.import_process.base import BaseNode
from knowledge.processor.import_process.exceptions import ValidationError
from knowledge.processor.import_process.state import ImportGraphState


class DocumentNormalizeNode(BaseNode):
    name = "document_normalize"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        raw_document = state.get("document_ir")
        if raw_document is None:
            raise ValidationError("document_ir is missing", node_name=self.name)
        document = (
            raw_document
            if isinstance(raw_document, DocumentIR)
            else DocumentIR.model_validate(raw_document)
        )
        normalized = normalize_document(document)
        output_path = Path(state.get("ir_path") or Path(state["file_dir"]) / "document.ir.json")
        previous_path = state.get("previous_ir_path")
        if previous_path:
            previous = load_document_ir(Path(previous_path))
            configured = os.getenv("DOCUMENT_IR_MIN_REVISION_OVERLAP", "").strip()
            try:
                minimum_overlap = (
                    float(configured) if configured else DEFAULT_MIN_REVISION_OVERLAP
                )
                continuity = assess_revision_continuity(
                    previous, normalized, minimum_overlap=minimum_overlap
                )
            except ValueError as exc:
                raise ValidationError(str(exc), node_name=self.name) from exc
            if not continuity.accepted:
                message = (
                    "可能误用了相同 document_key：新旧正文重合度 "
                    f"{continuity.overlap:.3f} 低于 {minimum_overlap:.3f}；"
                    "已停止索引替换，请核对文档身份或使用新 key"
                )
                normalized.status = ParseStatus.REVIEW_REQUIRED
                normalized.errors.append(
                    ParseError(
                        code="possible_document_key_collision",
                        message=message,
                        stage="normalize",
                        retryable=False,
                        details=continuity.as_metadata(),
                    )
                )
                normalized.metadata["revision_continuity"] = continuity.as_metadata()
                save_document_ir(normalized, output_path)
                state["document_ir"] = normalized
                state["parse_status"] = normalized.status.value
                state["parse_errors"] = [error.model_dump(mode="json") for error in normalized.errors]
                raise ValidationError(message, node_name=self.name)
            normalized = align_document(previous, normalized)
            normalized.metadata["revision_continuity"] = continuity.as_metadata()
        state["document_ir"] = normalized
        state["revision_id"] = normalized.revision_id
        state["parse_status"] = normalized.status.value
        save_document_ir(normalized, output_path)
        return state
