"""Base node for query workflow nodes."""

from abc import ABC, abstractmethod
from typing import Optional, TypeVar
import logging
import time

from knowledge.processor.query_process.config import QueryConfig, get_config
from knowledge.processor.query_process.exceptions import QueryProcessError
from knowledge.utils.task_utils import add_done_task, add_failed_task, add_running_task


T = TypeVar("T")


class BaseNode(ABC):
    """Base class for query workflow nodes."""

    name: str = "base_node"

    def __init__(self, config: Optional[QueryConfig] = None):
        self.config = config or get_config()
        self.logger = logging.getLogger(f"query.{self.name}")

    def __call__(self, state: T) -> T:
        task_id = self._get_task_id(state)
        if task_id:
            add_running_task(task_id, self.name)
            self._push_progress(task_id)

        start_time = time.perf_counter()
        self.logger.info("--- %s start ---", self.name)

        try:
            result = self.process(state)
            self._record_stage_status(result, "ok")
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(result, elapsed_seconds)
            self.logger.info("--- %s done, %.2fs ---", self.name, elapsed_seconds)
            if task_id:
                add_done_task(task_id, self.name)
                self._push_progress(task_id)
            return result
        except QueryProcessError as exc:
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(state, elapsed_seconds)
            self.logger.error("--- %s failed, %.2fs: %s ---", self.name, elapsed_seconds, exc)
            if task_id:
                add_failed_task(task_id, self.name, str(exc))
                self._push_progress(task_id)
            raise
        except Exception as exc:
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(state, elapsed_seconds)
            self.logger.error("%s failed, %.2fs: %s", self.name, elapsed_seconds, exc)
            if task_id:
                add_failed_task(task_id, self.name, str(exc))
                self._push_progress(task_id)
            raise QueryProcessError(str(exc), node_name=self.name, cause=exc)

    def _record_timing(self, state: T, elapsed_seconds: float) -> None:
        if not isinstance(state, dict):
            return

        timings = state.get("node_timings")
        if not isinstance(timings, dict):
            timings = {}
            state["node_timings"] = timings

        timings[self.name] = round(elapsed_seconds, 3)

    def _record_stage_status(self, state: T, status: str, reason: str = "") -> None:
        if not isinstance(state, dict) or self.name not in {
            "search_embedding",
            "search_embedding_hyde",
            "query_kg",
            "web_search_mcp",
            "rrf",
            "rerank_node",
        }:
            return
        statuses = state.get("retrieval_status")
        if not isinstance(statuses, dict):
            statuses = {}
            state["retrieval_status"] = statuses
        if self.name not in statuses:
            statuses[self.name] = {"status": status, "reason": reason}

    @abstractmethod
    def process(self, state: T) -> T:
        pass

    def log_step(self, step_name: str, message: str = "") -> None:
        log_message = f"[{step_name}]"
        if message:
            log_message += f" {message}"
        self.logger.info(log_message)

    @staticmethod
    def _get_task_id(state: T) -> str:
        if not isinstance(state, dict):
            return ""
        return state.get("task_id") or state.get("session_id", "")

    @staticmethod
    def _push_progress(task_id: str) -> None:
        try:
            from knowledge.utils.sse_util import SSEEvent, push_sse_event
            from knowledge.utils.task_utils import (
                get_done_task_list,
                get_running_task_list,
                get_task_status,
            )

            push_sse_event(
                task_id,
                SSEEvent.PROGRESS,
                {
                    "status": get_task_status(task_id),
                    "done_list": get_done_task_list(task_id),
                    "running_list": get_running_task_list(task_id),
                },
            )
        except Exception:
            return


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
