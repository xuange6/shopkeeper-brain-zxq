"""Enterprise knowledge lifecycle primitives.

The package is intentionally independent from FastAPI and the query graph.  It
owns durable lifecycle state and can therefore be used by API processes,
workers, reconciliation commands, and acceptance tests without importing model
runtimes.
"""

from knowledge.lifecycle.models import (
    ChangeKind,
    DeletionState,
    FailureCategory,
    ReleaseState,
    RevisionState,
    SyncRunState,
    TaskState,
)
from knowledge.lifecycle.store import LifecycleStore

__all__ = [
    "ChangeKind",
    "DeletionState",
    "FailureCategory",
    "LifecycleStore",
    "ReleaseState",
    "RevisionState",
    "SyncRunState",
    "TaskState",
]
