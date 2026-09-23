from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from fastapi import UploadFile

from knowledge.document_ir.adapters import MarkdownAdapter
from knowledge.document_ir.models import ParseError, ParseStatus
from knowledge.document_ir.normalize import normalize_document
from knowledge.document_ir.serialization import save_document_ir
from knowledge.service.file_import_service import (
    FileImportService,
    UploadTooLargeError,
    UploadValidationError,
)
from knowledge.utils.task_utils import clear_task, get_task_info


class UploadServiceTests(unittest.TestCase):
    def test_rejects_unsupported_extension_before_creating_task(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            service = FileImportService(base_dir=Path(temporary_dir))
            upload = UploadFile(filename="payload.exe", file=BytesIO(b"unsafe"))

            with self.assertRaises(UploadValidationError):
                service.process_file_upload(upload)

            self.assertEqual(list(Path(temporary_dir).iterdir()), [])

    def test_saves_hash_and_uses_collision_safe_minio_key(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            service = FileImportService(base_dir=Path(temporary_dir))
            upload = UploadFile(filename="../manual.md", file=BytesIO(b"# Manual"))

            with patch.object(service, "_upload_to_minio") as upload_to_minio:
                task_id, file_dir, file_path = service.process_file_upload(upload)

            try:
                info = get_task_info(task_id)
                self.assertEqual(info["filename"], "manual.md")
                self.assertEqual(info["size_bytes"], len(b"# Manual"))
                self.assertEqual(len(info["sha256"]), 64)
                self.assertEqual(info["document_id"], "doc_" + info["sha256"][:24])
                self.assertTrue(Path(file_path).is_file())
                self.assertEqual(Path(file_dir).name, task_id)
                upload_to_minio.assert_called_once()
                self.assertEqual(upload_to_minio.call_args.args[1], task_id)
            finally:
                clear_task(task_id)

    def test_oversized_upload_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            service = FileImportService(base_dir=Path(temporary_dir))
            upload = UploadFile(
                filename="large.md",
                file=BytesIO(b"x" * (1024 * 1024 + 1)),
            )

            with patch.dict(os.environ, {"MAX_UPLOAD_MB": "1"}):
                with self.assertRaises(UploadTooLargeError):
                    service.process_file_upload(upload)

            self.assertEqual(list(Path(temporary_dir).iterdir()), [])

    def test_document_key_is_explicit_and_validated_before_saving(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            service = FileImportService(base_dir=Path(temporary_dir))
            upload = UploadFile(filename="manual.md", file=BytesIO(b"# Manual"))
            with self.assertRaises(UploadValidationError):
                service.process_file_upload(upload, "bad\nkey")
            self.assertEqual(list(Path(temporary_dir).iterdir()), [])

    def test_failed_revision_exposes_review_manifest_without_marking_success(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            service = FileImportService(base_dir=Path(temporary_dir))
            upload = UploadFile(filename="manual.md", file=BytesIO(b"# Manual\n\nDifferent body.\n"))
            with patch.object(service, "_upload_to_minio"):
                task_id, file_dir, file_path = service.process_file_upload(
                    upload, document_key="fixture/manual"
                )
            document = normalize_document(
                MarkdownAdapter().convert(Path(file_path), logical_document_key="fixture/manual")
            )
            document.status = ParseStatus.REVIEW_REQUIRED
            document.errors.append(ParseError(
                code="possible_document_key_collision",
                message="different body",
                stage="normalize",
            ))
            save_document_ir(document, Path(file_dir) / "document.ir.json")
            # Test the service's failure handling without constructing the
            # production graph or loading its optional LLM/model dependencies.
            graph_module = ModuleType("knowledge.processor.import_process.main_graph")
            graph_module.run_import_graph = Mock(side_effect=ValueError("revision rejected"))
            try:
                with patch.dict(
                    sys.modules,
                    {"knowledge.processor.import_process.main_graph": graph_module},
                ), patch.object(service.logger, "exception"):
                    service.run_upload_file_task(
                        task_id, file_dir, file_path, document_key="fixture/manual"
                    )
                result = get_task_info(task_id)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["parse_status"], "review_required")
                self.assertEqual(result["parse_errors"][0]["code"], "possible_document_key_collision")
                self.assertEqual(result["document_id"], document.document_id)
            finally:
                clear_task(task_id)


if __name__ == "__main__":
    unittest.main()
