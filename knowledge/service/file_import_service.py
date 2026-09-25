"""File import service for upload and background processing."""

from __future__ import annotations

import logging
import os
import hashlib
import json
import shutil
from contextlib import nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Tuple
from uuid import uuid4

from fastapi import UploadFile

from knowledge.core.paths import get_local_base_dir
from knowledge.document_ir.ids import logical_document_id, stable_id
from knowledge.service.task_service import TaskService
from knowledge.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    create_task,
    set_task_result,
)


class UploadValidationError(ValueError):
    """上传文件不符合知识库导入约束。"""


class UploadTooLargeError(UploadValidationError):
    """上传文件超过大小上限。"""


class FileImportService:
    DEFAULT_ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown"}
    COPY_CHUNK_SIZE = 1024 * 1024

    def __init__(self, base_dir: Path | None = None, task_service: TaskService | None = None):
        self.base_dir = base_dir or get_local_base_dir()
        self._task_service = task_service or TaskService()
        self.logger = logging.getLogger(self.__class__.__name__)
        self._registry_guard = Lock()
        self._document_locks: dict[str, Lock] = {}

    @staticmethod
    def _document_key(value: str | None) -> str:
        key = (value or "").strip()
        if len(key) > 256 or any(ord(char) < 32 for char in key):
            raise UploadValidationError("document_key 长度不得超过 256，且不能包含控制字符")
        return key

    def _lock_for(self, key: str) -> Lock:
        with self._registry_guard:
            return self._document_locks.setdefault(key, Lock())

    def _registry_path(self, key: str) -> Path:
        return self.base_dir / "_document_ir_registry" / f"{stable_id('key', key)}.json"

    def _previous_ir_path(self, key: str) -> str:
        registry_path = self._registry_path(key)
        if not registry_path.is_file():
            return ""
        record = json.loads(registry_path.read_text(encoding="utf-8"))
        if record.get("document_id") != logical_document_id(key, ""):
            raise ValueError("document registry identity mismatch")
        ir_path = Path(record["ir_path"]).resolve()
        if not ir_path.is_relative_to(self.base_dir.resolve()) or not ir_path.is_file():
            raise ValueError("document registry points outside import storage or to a missing IR")
        return str(ir_path)

    def _record_ir(self, key: str, final_state: dict) -> None:
        ir_path = Path(str(final_state.get("ir_path") or "")).resolve()
        if (
            not ir_path.is_relative_to(self.base_dir.resolve())
            or not ir_path.is_file()
            or final_state.get("document_id") != logical_document_id(key, "")
        ):
            raise ValueError("import did not produce a valid IR for this document key")
        destination = self._registry_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "document_id": final_state["document_id"],
            "revision_id": final_state["revision_id"],
            "ir_path": str(ir_path),
        }
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=destination.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            temporary = Path(handle.name)
        os.replace(temporary, destination)

    def _build_task_paths(self, task_id: str, original_filename: str) -> Tuple[Path, Path]:
        safe_name = Path(original_filename).name or "uploaded_file"
        task_dir = self.base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        import_file_path = task_dir / safe_name
        return task_dir, import_file_path

    def _upload_to_minio(self, local_path: Path, task_id: str) -> None:
        bucket_name = os.getenv("MINIO_BUCKET_NAME", "").strip()
        if not bucket_name:
            return

        from knowledge.utils.minio_util import get_minio_client

        minio_client = get_minio_client()
        if not minio_client:
            self.logger.warning("MinIO client unavailable, skip object upload.")
            return

        object_name = f"documents/{task_id}/{local_path.name}"
        try:
            minio_client.fput_object(
                bucket_name,
                object_name,
                str(local_path),
            )
        except Exception as exc:
            self.logger.warning("MinIO upload failed for %s: %s", local_path, exc)

    @staticmethod
    def _max_upload_bytes() -> int:
        try:
            max_mb = int(os.getenv("MAX_UPLOAD_MB", "100"))
        except ValueError:
            max_mb = 100
        return max(1, max_mb) * 1024 * 1024

    @classmethod
    def _allowed_extensions(cls) -> set[str]:
        configured = os.getenv("ALLOWED_UPLOAD_EXTENSIONS", "").strip()
        if not configured:
            return cls.DEFAULT_ALLOWED_EXTENSIONS
        return {
            value if value.startswith(".") else f".{value}"
            for raw_value in configured.split(",")
            if (value := raw_value.strip().lower())
        }

    def _validate_filename(self, original_filename: str) -> str:
        safe_name = Path(original_filename).name.strip()
        if not safe_name:
            raise UploadValidationError("文件名不能为空")
        if Path(safe_name).suffix.lower() not in self._allowed_extensions():
            allowed = ", ".join(sorted(self._allowed_extensions()))
            raise UploadValidationError(f"暂不支持该文件类型，可上传：{allowed}")
        return safe_name

    def _save_upload(self, file: UploadFile, target_path: Path) -> tuple[int, str]:
        max_bytes = self._max_upload_bytes()
        total_bytes = 0
        digest = hashlib.sha256()

        file.file.seek(0)
        with target_path.open("wb") as target:
            while True:
                chunk = file.file.read(self.COPY_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadTooLargeError(
                        f"文件超过 {max_bytes // (1024 * 1024)} MB 上传上限"
                    )
                digest.update(chunk)
                target.write(chunk)

        if total_bytes == 0:
            raise UploadValidationError("不能上传空文件")
        return total_bytes, digest.hexdigest()

    def process_file_upload(self, file: UploadFile, document_key: str | None = None):
        safe_name = self._validate_filename(file.filename or "")
        document_key = self._document_key(document_key)
        task_id = uuid4().hex
        file_dir, import_file_path = self._build_task_paths(
            task_id,
            safe_name,
        )

        try:
            size_bytes, sha256 = self._save_upload(file, import_file_path)
        except Exception:
            # 只清理本次新建的 task 目录，不触碰任何既有文档。
            shutil.rmtree(file_dir, ignore_errors=True)
            raise

        create_task(task_id)
        set_task_result(task_id, "filename", safe_name)
        set_task_result(task_id, "size_bytes", size_bytes)
        set_task_result(task_id, "sha256", sha256)
        set_task_result(task_id, "document_id", logical_document_id(document_key, sha256))
        if document_key:
            set_task_result(task_id, "logical_document_key", document_key)

        self._upload_to_minio(import_file_path, task_id)

        return task_id, str(file_dir), str(import_file_path)

    def run_upload_file_task(
        self, task_id: str, file_dir: str, import_file_path: str,
        document_key: str | None = None,
    ) -> None:
        try:
            self._task_service.update_task_status(task_id, TASK_STATUS_PROCESSING)
            # Importing the graph constructs all LangGraph nodes. Keep it lazy so
            # starting the web application does not initialize the import pipeline.
            from knowledge.processor.import_process.main_graph import run_import_graph

            key = self._document_key(document_key)
            with self._lock_for(key) if key else nullcontext():
                final_state = run_import_graph(
                    import_file_path=import_file_path,
                    file_dir=file_dir,
                    task_id=task_id,
                    logical_document_key=key,
                    previous_ir_path=self._previous_ir_path(key) if key else "",
                )
                if key and isinstance(final_state, dict):
                    self._record_ir(key, final_state)
            if isinstance(final_state, dict):
                set_task_result(task_id, "item_name", final_state.get("item_name", ""))
                set_task_result(task_id, "chunk_count", len(final_state.get("chunks") or []))
                set_task_result(task_id, "document_id", final_state.get("document_id", ""))
                set_task_result(task_id, "revision_id", final_state.get("revision_id", ""))
                set_task_result(task_id, "parse_status", final_state.get("parse_status", ""))
                set_task_result(task_id, "parse_errors", final_state.get("parse_errors") or [])
                set_task_result(task_id, "ir_path", final_state.get("ir_path", ""))
                set_task_result(
                    task_id,
                    "image_count",
                    len(final_state.get("image_summaries") or {}),
                )
                set_task_result(task_id, "node_timings", final_state.get("node_timings") or {})
            self._task_service.update_task_status(task_id, TASK_STATUS_COMPLETED)
        except Exception as exc:
            # A failed graph can still leave a useful quarantine/review manifest.
            # Surface it through the task API without changing the failed status.
            manifest_path = Path(file_dir) / "document.ir.json"
            if manifest_path.is_file():
                try:
                    from knowledge.document_ir.serialization import load_document_ir

                    manifest = load_document_ir(manifest_path)
                    set_task_result(task_id, "document_id", manifest.document_id)
                    set_task_result(task_id, "revision_id", manifest.revision_id)
                    set_task_result(task_id, "parse_status", manifest.status.value)
                    set_task_result(
                        task_id, "parse_errors",
                        [error.model_dump(mode="json") for error in manifest.errors],
                    )
                    set_task_result(task_id, "ir_path", str(manifest_path))
                except Exception as manifest_error:
                    self.logger.warning("[%s] could not read failed IR manifest: %s", task_id, manifest_error)
            set_task_result(task_id, "error", str(exc))
            self._task_service.update_task_status(task_id, TASK_STATUS_FAILED)
            self.logger.exception("[%s] Error: %s", task_id, exc)
