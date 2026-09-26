"""SSE 流式输出工具。

这里不负责生成答案，只负责按照 task_id 维护一条消息队列，
让 FastAPI 的 StreamingResponse 可以把队列里的消息持续推给前端。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
from threading import RLock
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, Optional

from knowledge.utils.redis_runtime import get_runtime_redis

if TYPE_CHECKING:
    from fastapi import Request


class SSEEvent:
    READY = "ready"  # 连接建立
    PROGRESS = "progress"  # 节点处理进度
    DELTA = "delta"  # LLM 流式输出增量
    FINAL = "final"  # 最终完整答案
    ERROR = "error"  # 异常信息


# 全局 SSE 队列：一个 task_id 对应一次查询任务的消息通道。
_task_stream: Dict[str, queue.Queue] = {}
_stream_lock = RLock()


def _redis_stream_key(task_id: str) -> str:
    return f"shopkeeper:sse:{task_id}"


def _redis_stream_ttl() -> int:
    try:
        return max(300, int(os.getenv("TASK_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400


def get_sse_queue(task_id: str) -> Optional[queue.Queue]:
    """获取指定任务的 SSE 队列。"""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        return queue.Queue() if redis_client.exists(_redis_stream_key(task_id)) else None
    with _stream_lock:
        return _task_stream.get(task_id)


def create_sse_queue(task_id: str) -> queue.Queue:
    """创建并注册一个新的 SSE 队列。"""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        key = _redis_stream_key(task_id)
        redis_client.delete(key)
        redis_client.xadd(key, {"event": "_created", "data": "{}"})
        redis_client.expire(key, _redis_stream_ttl())
        return queue.Queue()
    stream_queue: queue.Queue = queue.Queue()
    with _stream_lock:
        _task_stream[task_id] = stream_queue
    return stream_queue


def remove_sse_queue(task_id: str) -> None:
    """移除指定任务的 SSE 队列，避免任务结束后占用内存。"""

    if get_runtime_redis() is not None:
        return
    with _stream_lock:
        _task_stream.pop(task_id, None)


def _sse_pack(event: str, data: Dict[str, Any]) -> str:
    """打包成浏览器 EventSource 能识别的 SSE 文本格式。"""

    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def push_sse_event(task_id: str, event: str, data: Dict[str, Any]) -> None:
    """把一条事件消息推入指定 task_id 的 SSE 队列。"""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        key = _redis_stream_key(task_id)
        redis_client.xadd(
            key,
            {"event": event, "data": json.dumps(data, ensure_ascii=False)},
            maxlen=2000,
            approximate=True,
        )
        redis_client.expire(key, _redis_stream_ttl())
        return
    stream_queue = get_sse_queue(task_id)
    if stream_queue is None:
        return
    stream_queue.put({"event": event, "data": data})


async def sse_generator(task_id: str, request: "Request") -> AsyncGenerator[str, None]:
    """SSE 生成器，用于 FastAPI StreamingResponse 持续返回消息。"""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        key = _redis_stream_key(task_id)
        if not redis_client.exists(key):
            yield _sse_pack(SSEEvent.ERROR, {"error": "SSE task not found"})
            return
        yield _sse_pack(SSEEvent.READY, {})
        last_id = "0-0"
        try:
            while True:
                if await request.is_disconnected():
                    break
                messages = await asyncio.to_thread(
                    redis_client.xread,
                    {key: last_id},
                    count=100,
                    block=1000,
                )
                if not messages:
                    yield ": keep-alive\n\n"
                    continue
                for _, entries in messages:
                    for entry_id, fields in entries:
                        last_id = entry_id
                        event = str(fields.get("event", ""))
                        if event == "_created":
                            continue
                        try:
                            data = json.loads(str(fields.get("data", "{}")))
                        except json.JSONDecodeError:
                            data = {"error": "invalid stream event"}
                            event = SSEEvent.ERROR
                        yield _sse_pack(event, data)
                        if event in {SSEEvent.FINAL, SSEEvent.ERROR}:
                            return
        except (ConnectionResetError, BrokenPipeError):
            return
        except asyncio.CancelledError:
            raise
        return

    stream_queue = get_sse_queue(task_id)
    if stream_queue is None:
        yield _sse_pack(SSEEvent.ERROR, {"error": "SSE task not found"})
        return

    loop = asyncio.get_running_loop()
    try:
        yield _sse_pack(SSEEvent.READY, {})
        last_keepalive = time.monotonic()

        while True:
            if await request.is_disconnected():
                break

            try:
                # queue.Queue 是同步阻塞队列，放到线程池里等待，避免卡住 async 事件循环。
                msg = await loop.run_in_executor(None, stream_queue.get, True, 1.0)
            except queue.Empty:
                if time.monotonic() - last_keepalive >= 15:
                    yield ": keep-alive\n\n"
                    last_keepalive = time.monotonic()
                continue

            event = msg.get("event", "")
            data = msg.get("data", {})
            yield _sse_pack(event, data)

            if event in {SSEEvent.FINAL, SSEEvent.ERROR}:
                break
    except (ConnectionResetError, BrokenPipeError):
        return
    except asyncio.CancelledError:
        raise
    finally:
        remove_sse_queue(task_id)
