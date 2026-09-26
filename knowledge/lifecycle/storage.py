"""Storage propagation contracts, deletion orchestration, and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Mapping, Protocol, Sequence

from knowledge.lifecycle.models import DeletionState
from knowledge.lifecycle.store import LifecycleStore


class StorageBackend(Protocol):
    name: str

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None: ...
    def delete_revision(self, revision_id: str) -> None: ...
    def contains_revision(self, revision_id: str) -> bool: ...
    def revision_ids(self) -> set[str]: ...


@dataclass(frozen=True)
class ReconciliationFinding:
    backend: str
    kind: str
    revision_id: str
    action: str
    repaired: bool


class ACLSynchronizer:
    def __init__(self, store: LifecycleStore, backends: Iterable[StorageBackend]):
        self.store = store
        self.backends = tuple(backends)

    def propagate(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> list[str]:
        revision = self.store.get_revision(revision_id)
        completed: list[str] = []
        # Fail closed: cache backends should be ordered first by callers.  Any
        # failure aborts activation of the ACL-only revision.
        for backend in self.backends:
            backend.update_acl(revision_id, tenant_id=tenant_id, visibility=visibility, acl_readers=acl_readers)
            completed.append(backend.name)
        self.store.record_metric(
            "acl_sync_lag",
            max(0.0, self.store.clock() - float(revision["created_at"])),
            labels={"revision_id": revision_id},
        )
        return completed


class DeletionOrchestrator:
    def __init__(self, store: LifecycleStore, backends: Iterable[StorageBackend]):
        self.store = store
        self.backends = {backend.name: backend for backend in backends}

    def run(self, deletion_id: str) -> dict:
        job = self.store.get_deletion(deletion_id)
        state = DeletionState(job["state"])
        if state == DeletionState.DETECTED:
            job = self.store.transition_deletion(deletion_id, DeletionState.TOMBSTONED, actor="deletion", idempotency_key="tombstone")
            state = DeletionState(job["state"])
        if state in {DeletionState.TOMBSTONED, DeletionState.FAILED}:
            job = self.store.transition_deletion(deletion_id, DeletionState.PROPAGATING, actor="deletion", idempotency_key=f"propagate:{state.value}")
        targets = json.loads(job["targets_json"])
        completed = set(json.loads(job["completed_targets_json"]))
        control_revision_id = str(job["revision_id"] or "")
        revision_id = control_revision_id
        missing = sorted(set(targets) - set(self.backends))
        if missing:
            return self.store.transition_deletion(
                deletion_id,
                DeletionState.FAILED,
                actor="deletion",
                idempotency_key=f"missing-targets:{','.join(missing)}",
                completed_targets=sorted(completed),
                error_message=f"deletion backends are not registered: {missing}",
            )
        try:
            for target in targets:
                backend = self.backends.get(target)
                if target not in completed and revision_id:
                    backend.delete_revision(revision_id)
                completed.add(target)
        except Exception as exc:
            return self.store.transition_deletion(deletion_id, DeletionState.FAILED, actor="deletion", idempotency_key=f"failed:{target}:{len(completed)}", completed_targets=sorted(completed), error_message=str(exc))
        verification = {
            name: (not backend.contains_revision(revision_id))
            for name, backend in self.backends.items()
            if revision_id
        }
        if not all(verification.values()):
            return self.store.transition_deletion(deletion_id, DeletionState.FAILED, actor="deletion", idempotency_key="verification-failed", completed_targets=sorted(completed), verification=verification, error_message="deletion verification failed")
        self.store.transition_deletion(deletion_id, DeletionState.VERIFIED, actor="deletion", idempotency_key="verified", completed_targets=sorted(completed), verification=verification)
        completed_job = self.store.transition_deletion(deletion_id, DeletionState.COMPLETED, actor="deletion", idempotency_key="completed", completed_targets=sorted(completed), verification=verification)
        self.store.record_metric(
            "deletion_propagation_lag",
            max(0.0, self.store.clock() - float(job["detected_at"])),
            labels={"deletion_id": deletion_id},
        )
        return completed_job


class Reconciler:
    def __init__(self, store: LifecycleStore, backends: Iterable[StorageBackend]):
        self.store = store
        self.backends = tuple(backends)

    def run(self, *, repair: bool = False) -> list[ReconciliationFinding]:
        rows = self.store.fetch_all(
            """SELECT r.revision_id, r.processing_status, d.tombstoned
               FROM document_revisions r JOIN documents d ON d.document_id=r.document_id"""
        )
        expected = {
            row["revision_id"]
            for row in rows
            if not row["tombstoned"] and row["processing_status"] in {"staged", "validated", "active"}
        }
        deleted = {row["revision_id"] for row in rows if row["tombstoned"] or row["processing_status"] == "deleted"}
        findings: list[ReconciliationFinding] = []
        for backend in self.backends:
            actual = backend.revision_ids()
            for revision_id in sorted(expected - actual):
                finding = ReconciliationFinding(backend.name, "database_record_missing_in_storage", revision_id, "quarantine_release", False)
                findings.append(finding)
                self.store.record_reconciliation(backend=backend.name, finding=finding.kind, identity=revision_id, action=finding.action, repaired=False)
            for revision_id in sorted(actual - expected):
                should_delete = revision_id in deleted or revision_id not in {row["revision_id"] for row in rows}
                repaired = False
                action = "isolate"
                if repair and should_delete:
                    backend.delete_revision(revision_id)
                    repaired = not backend.contains_revision(revision_id)
                    action = "delete_orphan"
                finding = ReconciliationFinding(backend.name, "storage_orphan", revision_id, action, repaired)
                findings.append(finding)
                self.store.record_reconciliation(backend=backend.name, finding=finding.kind, identity=revision_id, action=action, repaired=repaired)
        return findings


class MemoryStorageBackend:
    """Deterministic backend used by unit/fault tests, not production wiring."""

    def __init__(self, name: str, revisions: Iterable[str] = ()):
        self.name = name
        self.rows = {revision: {"acl": ()} for revision in revisions}
        self.fail_next = False

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError(f"injected {self.name} failure")

    def update_acl(self, revision_id: str, *, tenant_id: str, visibility: str, acl_readers: Sequence[str]) -> None:
        self._maybe_fail()
        if revision_id not in self.rows:
            raise KeyError(revision_id)
        self.rows[revision_id]["acl"] = (tenant_id, visibility, tuple(sorted(set(acl_readers))))

    def delete_revision(self, revision_id: str) -> None:
        self._maybe_fail()
        self.rows.pop(revision_id, None)

    def contains_revision(self, revision_id: str) -> bool:
        return revision_id in self.rows

    def revision_ids(self) -> set[str]:
        return set(self.rows)
