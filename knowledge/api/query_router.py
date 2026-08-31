"""查询接口路由。"""

from __future__ import annotations

import os

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from knowledge.core.deps import get_query_service
from knowledge.core.paths import get_front_page_dir
from knowledge.processor.query_process.base import setup_logging
from knowledge.schema.query_schema import (
    HistoryResponse,
    QueryRequest,
    QueryResponse,
    StreamSubmitResponse,
)
from knowledge.utils.sse_util import sse_generator
from knowledge.utils.task_utils import get_done_task_list


def register_query_router(app: FastAPI) -> None:
    @app.get("/chat.html")
    async def chat_page():
        return FileResponse(os.path.join(get_front_page_dir(), "chat.html"))

    @app.post("/query", response_model=QueryResponse | StreamSubmitResponse)
    async def query(
        request: QueryRequest,
        background_tasks: BackgroundTasks,
        service=Depends(get_query_service),
    ):
        session_id = request.session_id or service.generate_session_id()
        task_id = service.generate_task_id()
        service.submit_query(task_id, request.is_stream)

        if request.is_stream:
            background_tasks.add_task(
                service.run_query_graph,
                task_id,
                session_id,
                request.query,
                True,
                request.item_names,
            )
            return StreamSubmitResponse(
                message="Query submitted",
                session_id=session_id,
                task_id=task_id,
            )

        final_state = service.run_query_graph(
            task_id,
            session_id,
            request.query,
            False,
            request.item_names,
        )
        error = service.get_error(task_id)
        if error:
            return QueryResponse(
                message="处理失败",
                session_id=session_id,
                answer="",
                done_list=get_done_task_list(task_id),
                error=error,
                diagnostics=service.get_diagnostics(task_id),
            )

        return QueryResponse(
            message="处理完成",
            session_id=session_id,
            answer=service.get_answer(task_id),
            done_list=get_done_task_list(task_id),
            error="",
            image_urls=service.get_image_urls(task_id),
            sources=service.get_sources(task_id),
            diagnostics=service.get_diagnostics(task_id),
        )

    @app.get("/stream/{task_id}")
    async def stream(task_id: str, request: Request):
        return StreamingResponse(
            sse_generator(task_id, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/history/{session_id}", response_model=HistoryResponse)
    async def get_history(
        session_id: str,
        limit: int = Query(50, ge=1, le=200),
        service=Depends(get_query_service),
    ):
        try:
            items = service.get_history(session_id, limit)
            return {"session_id": session_id, "items": items}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"history error: {exc}") from exc

    @app.delete("/history/{session_id}")
    async def clear_chat_history(
        session_id: str,
        service=Depends(get_query_service),
    ):
        count = service.clear_history(session_id)
        return {"message": "History cleared", "deleted_count": count}


def create_app() -> FastAPI:
    """Compatibility wrapper for the former query-only application factory."""

    from knowledge.app import create_app as create_unified_app

    return create_unified_app()


if __name__ == "__main__":
    setup_logging()
    from knowledge.main import main

    main()
