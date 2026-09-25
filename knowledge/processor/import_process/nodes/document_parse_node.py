from __future__ import annotations

import os
from pathlib import Path

from knowledge.document_ir.adapters.common import build_source
from knowledge.document_ir.adapters import MarkdownAdapter, MinerUAdapter
from knowledge.document_ir.ids import logical_document_id, revision_id, stable_id
from knowledge.document_ir.models import (
    DocumentIR,
    ParseError,
    ParseStatus,
    ParserDescriptor,
    Section,
)
from knowledge.document_ir.policy import action_for_status
from knowledge.document_ir.serialization import save_document_ir
from knowledge.processor.import_process.base import BaseNode
from knowledge.processor.import_process.exceptions import FileProcessingError, ValidationError
from knowledge.processor.import_process.state import ImportGraphState


class DocumentParseNode(BaseNode):
    """Convert parser/source-specific output into the unified DocumentIR."""

    name = "document_parse"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        source_path = Path(state.get("import_file_path") or "")
        if not source_path.is_file():
            raise ValidationError("import_file_path is missing", node_name=self.name)

        max_attempts = self._max_attempts()
        last_error: Exception | None = None
        document: DocumentIR | None = None
        for attempt in range(1, max_attempts + 1):
            state["parse_attempts"] = attempt
            try:
                document = self._convert(state, source_path)
                break
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts:
                    self.logger.warning(
                        "document adapter attempt %d/%d failed, retrying: %s",
                        attempt,
                        max_attempts,
                        exc,
                    )
        if document is None:
            quarantined = self._quarantine_document(
                source_path, last_error, state.get("logical_document_key") or None
            )
            output_path = Path(state.get("file_dir") or source_path.parent) / "document.ir.json"
            save_document_ir(quarantined, output_path)
            state["document_ir"] = quarantined
            state["document_id"] = quarantined.document_id
            state["revision_id"] = quarantined.revision_id
            state["parse_status"] = quarantined.status.value
            state["parse_action"] = action_for_status(quarantined.status).value
            state["parse_errors"] = [
                error.model_dump(mode="json") for error in quarantined.errors
            ]
            state["ir_path"] = str(output_path)
            raise FileProcessingError(
                f"document quarantined after {max_attempts} parse attempts: {last_error}",
                node_name=self.name,
                cause=last_error,
            )

        state["document_ir"] = document
        state["document_id"] = document.document_id
        state["revision_id"] = document.revision_id
        state["parse_status"] = document.status.value
        state["parse_action"] = action_for_status(
            document.status,
            attempt=state["parse_attempts"],
            max_attempts=max_attempts,
        ).value
        state["parse_errors"] = [error.model_dump(mode="json") for error in document.errors]
        output_path = Path(state.get("file_dir") or source_path.parent) / "document.ir.json"
        save_document_ir(document, output_path)
        state["ir_path"] = str(output_path)
        self.logger.info(
            "DocumentIR parsed: document_id=%s status=%s blocks=%d sections=%d",
            document.document_id,
            document.status.value,
            len(document.blocks),
            len(document.sections),
        )
        return state

    def _convert(self, state: ImportGraphState, source_path: Path) -> DocumentIR:
        if state.get("is_pdf_read_enabled"):
            md_path = Path(state.get("md_path") or "")
            content_path, middle_path = MinerUAdapter.find_artifacts(md_path)
            if content_path is None:
                raise FileProcessingError(
                    f"MinerU content_list_v2 artifact not found beside {md_path}",
                    node_name=self.name,
                )
            return MinerUAdapter().convert(
                source_path=source_path,
                content_list_path=content_path,
                middle_path=middle_path,
                logical_document_key=state.get("logical_document_key") or None,
            )
        if state.get("is_md_read_enabled"):
            document = MarkdownAdapter().convert(
                source_path, logical_document_key=state.get("logical_document_key") or None
            )
            state["md_content"] = document.raw_text
            return document
        raise ValidationError("unsupported document input", node_name=self.name)

    @staticmethod
    def _max_attempts() -> int:
        try:
            return max(1, min(5, int(os.getenv("DOCUMENT_PARSE_MAX_ATTEMPTS", "2"))))
        except ValueError:
            return 2

    @staticmethod
    def _quarantine_document(
        source_path: Path, error: Exception | None, logical_document_key: str | None = None
    ) -> DocumentIR:
        source = build_source(source_path)
        doc_id = logical_document_id(logical_document_key, source.sha256)
        root_id = stable_id("sec", doc_id, "root")
        parser_name = "mineru" if source_path.suffix.lower() == ".pdf" else "markdown"
        parser_version = (
            MinerUAdapter().parser_version if parser_name == "mineru" else MarkdownAdapter.version
        )
        message = str(error or "unknown parser failure")
        return DocumentIR(
            document_id=doc_id,
            logical_document_key=logical_document_key.strip() if logical_document_key else None,
            revision_id=revision_id(
                source_sha256=source.sha256,
                parser_name=parser_name,
                parser_version=parser_version,
                blocks=["quarantined", message],
            ),
            source=source,
            parser=ParserDescriptor(
                name=parser_name,
                version=parser_version,
                backend="quarantine",
            ),
            status=ParseStatus.QUARANTINED,
            sections=[
                Section(
                    id=root_id,
                    title=source_path.stem,
                    level=0,
                    title_path=[],
                    order=0,
                )
            ],
            errors=[
                ParseError(
                    code="parser_failure",
                    message=message,
                    stage="parse",
                    retryable=True,
                    details={"exception_type": type(error).__name__ if error else ""},
                )
            ],
        )
