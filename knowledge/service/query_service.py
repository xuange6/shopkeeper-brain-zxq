"""查询业务服务。"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from knowledge.utils.sse_util import SSEEvent, create_sse_queue, push_sse_event
from knowledge.utils.query_result_utils import (
    build_query_diagnostics,
    build_source_references,
)
from knowledge.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    create_task,
    get_done_task_list,
    get_running_task_list,
    get_task_error,
    get_task_result,
    get_task_status,
    set_task_result,
    update_task_status,
)


class QueryService:
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def generate_session_id(self) -> str:
        return uuid4().hex

    def generate_task_id(self) -> str:
        return uuid4().hex

    def submit_query(self, task_id: str, is_stream: bool) -> None:
        """提交查询任务：记录任务状态；流式模式提前创建 SSE 队列。"""

        create_task(task_id, status=TASK_STATUS_PROCESSING)
        if is_stream:
            create_sse_queue(task_id)

    def run_query_graph(
        self,
        task_id: str,
        session_id: str,
        user_query: str,
        is_stream: bool,
        item_names: List[str] | None = None,
    ) -> Dict[str, Any]:
        """执行 LangGraph 查询流程。"""

        started_at = time.perf_counter()
        try:
            from knowledge.processor.query_process.main_graph import run_query

            final_state = run_query(
                query=user_query,
                session_id=session_id,
                item_names=item_names or [],
                is_stream=is_stream,
                task_id=task_id,
            )
            answer = str(final_state.get("answer", "") or "")
            image_urls = list(final_state.get("image_urls") or [])
            sources = list(final_state.get("sources") or []) or self._build_sources(
                final_state.get("reranked_docs") or []
            )
            diagnostics = self._build_diagnostics(
                task_id,
                final_state,
                elapsed=time.perf_counter() - started_at,
            )
            set_task_result(task_id, "answer", answer)
            set_task_result(task_id, "image_urls", image_urls)
            set_task_result(task_id, "sources", sources)
            set_task_result(task_id, "diagnostics", diagnostics)
            update_task_status(task_id, TASK_STATUS_COMPLETED)
            self._push_progress(task_id)
            if is_stream:
                push_sse_event(
                    task_id,
                    SSEEvent.FINAL,
                    {
                        "answer": answer,
                        "image_urls": image_urls,
                        "sources": sources,
                        "diagnostics": diagnostics,
                    },
                )
            return final_state
        except Exception as exc:
            error_text = str(exc)
            self.logger.error("查询流程执行失败: %s", error_text, exc_info=True)
            set_task_result(task_id, "error", error_text)
            set_task_result(
                task_id,
                "diagnostics",
                {
                    "trace_id": task_id,
                    "retrieval_counts": {},
                    "node_timings": {},
                    "total_time": round(time.perf_counter() - started_at, 3),
                },
            )
            update_task_status(task_id, TASK_STATUS_FAILED)
            self._push_progress(task_id)
            if is_stream:
                push_sse_event(task_id, SSEEvent.ERROR, {"error": error_text})
            return {}

    def get_answer(self, task_id: str) -> str:
        return str(get_task_result(task_id, "answer", "") or "")

    def get_error(self, task_id: str) -> str:
        return str(get_task_result(task_id, "error", "") or get_task_error(task_id) or "")

    def get_sources(self, task_id: str) -> List[Dict[str, Any]]:
        return list(get_task_result(task_id, "sources", []) or [])

    def get_image_urls(self, task_id: str) -> List[str]:
        return list(get_task_result(task_id, "image_urls", []) or [])

    def get_diagnostics(self, task_id: str) -> Dict[str, Any]:
        return dict(get_task_result(task_id, "diagnostics", {}) or {})

    def get_history(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            from knowledge.utils.mongo_history_utils import get_recent_messages

            records = get_recent_messages(session_id, limit=limit)
            return [self._format_history_record(record) for record in records]
        except Exception as exc:
            self.logger.warning("会话历史不可用，降级为空历史: %s", exc)
            return []

    def clear_history(self, session_id: str) -> int:
        try:
            from knowledge.utils.mongo_history_utils import clear_history

            return clear_history(session_id)
        except Exception as exc:
            self.logger.warning("清理会话历史失败，按无历史处理: %s", exc)
            return 0

    def _push_progress(self, task_id: str) -> None:
        push_sse_event(
            task_id,
            SSEEvent.PROGRESS,
            {
                "status": get_task_status(task_id),
                "done_list": get_done_task_list(task_id),
                "running_list": get_running_task_list(task_id),
            },
        )

    @staticmethod
    def _build_sources(raw_docs: Any) -> List[Dict[str, Any]]:
        return build_source_references(raw_docs)

    @staticmethod
    def _build_diagnostics(
        task_id: str,
        state: Dict[str, Any],
        elapsed: float,
    ) -> Dict[str, Any]:
        return build_query_diagnostics(task_id, state, elapsed)

    @staticmethod
    def _format_history_record(record: Dict[str, Any]) -> Dict[str, Any]:
        created_at = record.get("created_at") or record.get("updated_at")
        ts = None
        if isinstance(created_at, datetime):
            ts = created_at.timestamp()

        return {
            "_id": str(record.get("_id", "")),
            "session_id": record.get("session_id", ""),
            "role": record.get("role", ""),
            "text": record.get("text", ""),
            "rewritten_query": record.get("rewritten_query", ""),
            "item_names": record.get("item_names", []),
            "image_urls": record.get("image_urls", []),
            "sources": record.get("sources", []),
            "diagnostics": record.get("diagnostics", {}),
            "ts": ts,
        }
