"""Task status service."""

from __future__ import annotations

from knowledge.utils.task_utils import get_task_info, update_task_status


class TaskService:
    def get_task_info(self, task_id: str):
        return get_task_info(task_id)

    def update_task_status(self, task_id: str, status: str) -> None:
        update_task_status(task_id, status)
