"""Validated lifecycle state machines.

Transition metadata is kept beside the graph so implementation, tests and the
ADR share one source of truth.  Store operations additionally record actor,
idempotency key, transaction boundary, failure state and audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Mapping, TypeVar

from knowledge.lifecycle.models import (
    DeletionState,
    ReleaseState,
    RevisionState,
    SyncRunState,
    TaskState,
)


S = TypeVar("S", bound=Enum)


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class TransitionRule:
    actors: tuple[str, ...]
    precondition: str
    failure_state: str
    retryable: bool
    audit_event: str


@dataclass(frozen=True)
class StateMachine(Generic[S]):
    name: str
    transitions: Mapping[S, frozenset[S]]
    default_rule: TransitionRule

    def validate(self, current: S, target: S, actor: str) -> None:
        allowed = self.transitions.get(current, frozenset())
        if target not in allowed:
            raise InvalidTransition(
                f"illegal {self.name} transition: {current.value} -> {target.value}"
            )
        if actor not in self.default_rule.actors:
            raise InvalidTransition(f"actor {actor!r} cannot transition {self.name}")


SYNC_RUN_MACHINE = StateMachine(
    "sync_run",
    {
        SyncRunState.PENDING: frozenset({SyncRunState.RUNNING, SyncRunState.CANCELLED}),
        SyncRunState.RUNNING: frozenset(
            {
                SyncRunState.SUCCEEDED,
                SyncRunState.PARTIALLY_FAILED,
                SyncRunState.RETRYING,
                SyncRunState.FAILED,
                SyncRunState.CANCELLED,
                SyncRunState.DEAD_LETTERED,
            }
        ),
        SyncRunState.RETRYING: frozenset(
            {SyncRunState.RUNNING, SyncRunState.CANCELLED, SyncRunState.DEAD_LETTERED}
        ),
    },
    TransitionRule(
        actors=("scheduler", "worker", "operator", "reaper"),
        precondition="source lease and expected current state",
        failure_state="retrying|failed|dead_lettered",
        retryable=True,
        audit_event="sync_run.state_changed",
    ),
)

REVISION_MACHINE = StateMachine(
    "document_revision",
    {
        RevisionState.DISCOVERED: frozenset(
            {RevisionState.PROCESSING, RevisionState.STAGED, RevisionState.FAILED, RevisionState.DELETED}
        ),
        RevisionState.PROCESSING: frozenset(
            {RevisionState.STAGED, RevisionState.FAILED, RevisionState.DELETED}
        ),
        RevisionState.STAGED: frozenset(
            {RevisionState.VALIDATED, RevisionState.FAILED, RevisionState.DELETED}
        ),
        RevisionState.VALIDATED: frozenset(
            {RevisionState.ACTIVE, RevisionState.FAILED, RevisionState.DELETED}
        ),
        RevisionState.ACTIVE: frozenset({RevisionState.RETIRED, RevisionState.DELETED}),
        RevisionState.FAILED: frozenset({RevisionState.PROCESSING, RevisionState.DELETED}),
        RevisionState.RETIRED: frozenset({RevisionState.ACTIVE, RevisionState.DELETED}),
    },
    TransitionRule(
        actors=("sync", "worker", "publisher", "operator", "deletion"),
        precondition="revision exists and storage effects are durable",
        failure_state="failed",
        retryable=True,
        audit_event="revision.state_changed",
    ),
)

RELEASE_MACHINE = StateMachine(
    "index_release",
    {
        ReleaseState.BUILDING: frozenset({ReleaseState.STAGING, ReleaseState.FAILED}),
        ReleaseState.STAGING: frozenset({ReleaseState.VALIDATING, ReleaseState.FAILED}),
        ReleaseState.VALIDATING: frozenset({ReleaseState.READY, ReleaseState.FAILED}),
        ReleaseState.READY: frozenset({ReleaseState.ACTIVATING, ReleaseState.FAILED}),
        ReleaseState.ACTIVATING: frozenset({ReleaseState.ACTIVE, ReleaseState.FAILED}),
        ReleaseState.ACTIVE: frozenset({ReleaseState.ROLLING_BACK, ReleaseState.RETIRED}),
        ReleaseState.ROLLING_BACK: frozenset({ReleaseState.ROLLED_BACK, ReleaseState.FAILED}),
        ReleaseState.ROLLED_BACK: frozenset({ReleaseState.RETIRED}),
        ReleaseState.FAILED: frozenset({ReleaseState.BUILDING, ReleaseState.RETIRED}),
        ReleaseState.RETIRED: frozenset({ReleaseState.ACTIVE}),
    },
    TransitionRule(
        actors=("builder", "validator", "publisher", "rollback", "operator"),
        precondition="manifest valid; validation gate required before activation",
        failure_state="failed",
        retryable=True,
        audit_event="release.state_changed",
    ),
)

TASK_MACHINE = StateMachine(
    "lifecycle_task",
    {
        TaskState.PENDING: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
        TaskState.RUNNING: frozenset(
            {
                TaskState.SUCCEEDED,
                TaskState.RETRYING,
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.DEAD_LETTERED,
            }
        ),
        TaskState.RETRYING: frozenset(
            {TaskState.RUNNING, TaskState.CANCELLED, TaskState.DEAD_LETTERED}
        ),
        TaskState.DEAD_LETTERED: frozenset({TaskState.PENDING}),
    },
    TransitionRule(
        actors=("scheduler", "worker", "operator", "reaper"),
        precondition="lease owner matches for worker mutations",
        failure_state="retrying|failed|dead_lettered",
        retryable=True,
        audit_event="task.state_changed",
    ),
)

DELETION_MACHINE = StateMachine(
    "deletion",
    {
        DeletionState.DETECTED: frozenset({DeletionState.TOMBSTONED, DeletionState.FAILED}),
        DeletionState.TOMBSTONED: frozenset({DeletionState.PROPAGATING, DeletionState.FAILED}),
        DeletionState.PROPAGATING: frozenset({DeletionState.VERIFIED, DeletionState.FAILED}),
        DeletionState.VERIFIED: frozenset({DeletionState.COMPLETED, DeletionState.FAILED}),
        DeletionState.FAILED: frozenset({DeletionState.PROPAGATING}),
    },
    TransitionRule(
        actors=("sync", "deletion", "reconciler", "operator"),
        precondition="document tombstoned before external deletion",
        failure_state="failed",
        retryable=True,
        audit_event="deletion.state_changed",
    ),
)
