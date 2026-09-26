"""Lazy Redis connection used for cross-pod task state and SSE streams."""

from __future__ import annotations

from functools import lru_cache
import os


@lru_cache(maxsize=1)
def get_runtime_redis():
    url = (
        os.getenv("TASK_STATE_REDIS_URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
    )
    if not url:
        return None
    try:
        import redis
    except ModuleNotFoundError as exc:  # pragma: no cover - deployment error
        raise RuntimeError("Redis runtime requires the redis package") from exc
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )


def reset_runtime_redis() -> None:
    get_runtime_redis.cache_clear()
