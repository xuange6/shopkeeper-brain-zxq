"""Small durable worker with leases, checkpoints, retries and DLQ."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from threading import Lock
from typing import Any, Callable, Mapping

from knowledge.lifecycle.models import FailureCategory
from knowledge.lifecycle.store import LifecycleStore


class PermanentTaskError(RuntimeError):
    pass


class PoisonTaskError(RuntimeError):
    pass


class TaskCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskContext:
    task_id: str
    attempt: int
    payload: Mapping[str, Any]
    checkpoint: Mapping[str, Any]
    heartbeat: Callable[[Mapping[str, Any]], None]
    cancelled: Callable[[], bool]


class LifecycleWorker:
    def __init__(self, store: LifecycleStore, owner: str, handlers: Mapping[str, Callable[[TaskContext], None]], *, lease_seconds: float = 60):
        self.store = store
        self.owner = owner
        self.handlers = dict(handlers)
        self.lease_seconds = lease_seconds
        self.logger = logging.getLogger("knowledge.lifecycle.worker")

    def run_once(self) -> str | None:
        task = self.store.claim_task(self.owner, lease_seconds=self.lease_seconds, task_types=tuple(self.handlers))
        if not task:
            return None
        task_id = task["task_id"]
        handler = self.handlers[task["task_type"]]

        def heartbeat(checkpoint: Mapping[str, Any]) -> None:
            self.store.heartbeat_task(task_id, self.owner, checkpoint=checkpoint, lease_seconds=self.lease_seconds)

        def cancelled() -> bool:
            return bool(self.store.get_task(task_id)["cancellation_requested"])

        context = TaskContext(
            task_id=task_id,
            attempt=int(task["attempt"]),
            payload=json.loads(task["payload_json"]),
            checkpoint=json.loads(task["checkpoint_json"]),
            heartbeat=heartbeat,
            cancelled=cancelled,
        )
        if cancelled():
            self.store.cancel_running_task(task_id, self.owner)
            return task_id
        try:
            handler(context)
        except TaskCancelledError:
            self.store.cancel_running_task(task_id, self.owner)
        except PoisonTaskError as exc:
            self.store.fail_task(task_id, self.owner, FailureCategory.POISON, str(exc))
        except PermanentTaskError as exc:
            self.store.fail_task(task_id, self.owner, FailureCategory.PERMANENT, str(exc))
        except Exception as exc:
            self.store.fail_task(task_id, self.owner, FailureCategory.TRANSIENT, str(exc))
        else:
            self.store.complete_task(task_id, self.owner)
        return task_id

    def recover(self) -> list[str]:
        return self.store.recover_expired_tasks()

    def run_until_idle(self, *, max_tasks: int = 100) -> list[str]:
        completed: list[str] = []
        for _ in range(max_tasks):
            task_id = self.run_once()
            if task_id is None:
                break
            completed.append(task_id)
        return completed


class RateLimiter:
    """Thread-safe token bucket for connector/external-service limits."""

    def __init__(self, rate_per_second: float, *, burst: int = 1, clock: Callable[[], float] = time.monotonic):
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate and burst must be positive")
        self.rate = rate_per_second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.updated_at = clock()
        self._lock = Lock()

    def acquire(self) -> float:
        """Consume a token and return required wait seconds (zero when allowed)."""

        with self._lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated_at) * self.rate)
            self.updated_at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            self.updated_at = now + wait
            return wait
