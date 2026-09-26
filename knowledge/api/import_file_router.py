"""File import API router."""

import os

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile

from knowledge.core.deps import get_file_import_service, get_task_service
from knowledge.schema.task_schema import TaskStatusResponse
from knowledge.schema.upload_schema import UploadResponse
from knowledge.service.file_import_service import (
    FileImportService,
    UploadTooLargeError,
    UploadValidationError,
)
from knowledge.service.task_service import TaskService
from knowledge.utils.task_utils import TASK_STATUS_NOT_FOUND


def _regist_router(app: FastAPI):
    @app.post("/upload", response_model=UploadResponse)
    def upload_file_endpoint(
            background_tasks: BackgroundTasks,
            file: UploadFile = File(...),
            document_key: str | None = Form(None),
            service: FileImportService = Depends(get_file_import_service)
    ) -> UploadResponse:
        if os.getenv("DISTRIBUTED_RUNTIME", "").strip().lower() in {
            "1", "true", "yes", "on"
        }:
            raise HTTPException(
                status_code=503,
                detail=(
                    "direct background upload is disabled in distributed mode; "
                    "use a durable lifecycle Source and sync task"
                ),
            )
        # 1. Synchronous processing: save file + upload MinIO.
        try:
            task_id, file_dir, import_file_path = service.process_file_upload(file, document_key)
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UploadValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 2. Background processing: run the LangGraph import pipeline.
        background_tasks.add_task(
            service.run_upload_file_task, task_id, file_dir, import_file_path, document_key
        )

        return UploadResponse(message="文件已进入知识库处理队列", task_id=task_id)

    @app.get("/status/{task_id}", response_model=TaskStatusResponse)
    async def status_endpoint(
            task_id: str,
            task_service: TaskService = Depends(get_task_service)
    ) -> TaskStatusResponse:
        task_info = task_service.get_task_info(task_id)
        if task_info.get("status") == TASK_STATUS_NOT_FOUND:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskStatusResponse(**task_info)


def register_import_router(app: FastAPI) -> None:
    _regist_router(app)


def create_app() -> FastAPI:
    """Compatibility wrapper for the former application factory."""

    from knowledge.app import create_app as create_unified_app

    return create_unified_app()


def main() -> None:
    from knowledge.main import main as run_application

    run_application()


if __name__ == "__main__":
    main()
