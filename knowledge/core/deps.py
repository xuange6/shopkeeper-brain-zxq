"""FastAPI dependency injection."""

from functools import lru_cache

from knowledge.core.paths import get_local_base_dir
from knowledge.service.file_import_service import FileImportService
from knowledge.service.task_service import TaskService


@lru_cache
def get_task_service() -> TaskService:
    return TaskService()


@lru_cache
def get_file_import_service() -> FileImportService:
    return FileImportService(base_dir=get_local_base_dir(), task_service=get_task_service())


@lru_cache
def get_query_service():
    from knowledge.service.query_service import QueryService

    return QueryService()
