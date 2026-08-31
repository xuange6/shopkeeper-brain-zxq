from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import UploadFile

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
                self.assertEqual(info["document_id"], info["sha256"][:24])
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


if __name__ == "__main__":
    unittest.main()
