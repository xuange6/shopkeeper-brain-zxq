"""Validated atomic release activation and rollback pointer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Mapping

from knowledge.lifecycle.models import ReleaseState, RevisionState
from knowledge.lifecycle.store import LifecycleStore


REQUIRED_VALIDATION_CHECKS = frozenset(
    {
        "document_count",
        "chunk_count",
        "acl",
        "assets",
        "kg_lineage",
        "evaluation_gate",
        "manifest",
    }
)


class ReleaseValidationError(ValueError):
    pass


class AtomicReleasePointer:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict:
        if not self.path.is_file():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def switch(self, release: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def value(db_key: str, pointer_key: str) -> object:
            return release.get(db_key, release.get(pointer_key, ""))

        payload = {
            "release_id": release["release_id"],
            "manifest_hash": release["manifest_hash"],
            "CHUNKS_COLLECTION": value("chunk_collection", "CHUNKS_COLLECTION"),
            "ENTITY_NAME_COLLECTION": value("entity_collection", "ENTITY_NAME_COLLECTION"),
            "KG_GRAPH_VERSION": value("graph_version", "KG_GRAPH_VERSION"),
            "OBJECT_ASSET_NAMESPACE": value("object_asset_namespace", "OBJECT_ASSET_NAMESPACE"),
        }
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            temporary = Path(handle.name)
        try:
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class DatabaseReleasePointer:
    """Distributed pointer mode where PostgreSQL is the sole source of truth."""

    def read(self) -> dict:
        return {}

    def switch(self, release: Mapping[str, object]) -> None:
        # The following LifecycleStore transition is the atomic pointer switch.
        # This adapter intentionally performs no node-local filesystem write.
        return None


class ReleasePublisher:
    def __init__(
        self,
        store: LifecycleStore,
        pointer: AtomicReleasePointer | DatabaseReleasePointer,
    ):
        self.store = store
        self.pointer = pointer

    def validate(self, release_id: str, checks: Mapping[str, Callable[[dict], bool]]) -> dict[str, bool]:
        release = self.store.get_release(release_id)
        if release["current_state"] != ReleaseState.VALIDATING.value:
            raise ReleaseValidationError("release must be validating")
        missing = sorted(REQUIRED_VALIDATION_CHECKS - set(checks))
        results: dict[str, bool] = {name: False for name in missing}
        for name, check in checks.items():
            try:
                results[name] = bool(check(release))
            except Exception:
                results[name] = False
        if missing or not all(results.values()):
            self.store.transition_release(release_id, ReleaseState.FAILED, actor="validator", idempotency_key="validation-failed")
            raise ReleaseValidationError(f"release validation failed: {results}")
        for revision_id in json.loads(str(release["source_revisions_json"])):
            revision = self.store.get_revision(str(revision_id))
            if revision["processing_status"] == RevisionState.STAGED.value:
                self.store.transition_revision(
                    str(revision_id),
                    RevisionState.VALIDATED,
                    actor="worker",
                    idempotency_key=f"release-validated:{release_id}",
                )
            elif revision["processing_status"] not in {
                RevisionState.VALIDATED.value,
                RevisionState.ACTIVE.value,
            }:
                self.store.transition_release(
                    release_id,
                    ReleaseState.FAILED,
                    actor="validator",
                    idempotency_key="revision-validation-failed",
                )
                raise ReleaseValidationError(
                    f"revision {revision_id} is not eligible for release"
                )
        self.store.transition_release(
            release_id,
            ReleaseState.READY,
            actor="validator",
            idempotency_key="validation-passed",
        )
        return results

    def activate(self, release_id: str) -> dict:
        release = self.store.get_release(release_id)
        if release["current_state"] != ReleaseState.READY.value:
            raise ReleaseValidationError("only a ready release can activate")
        self.store.transition_release(release_id, ReleaseState.ACTIVATING, actor="publisher", idempotency_key="activation-start")
        before = self.pointer.read()
        try:
            self.pointer.switch(release)
            return self.store.transition_release(release_id, ReleaseState.ACTIVE, actor="publisher", idempotency_key="activation-complete")
        except Exception:
            if before:
                self.pointer.switch(before)
            self.store.transition_release(release_id, ReleaseState.FAILED, actor="publisher", idempotency_key="activation-failed")
            raise

    def rollback(self, release_id: str) -> dict:
        release = self.store.get_release(release_id)
        previous_id = release.get("previous_release_id")
        if not previous_id:
            raise ReleaseValidationError("release has no previous release")
        previous = self.store.get_release(previous_id)
        # Pointer restoration happens before the control-plane transaction; if
        # it fails the active DB state is unchanged.
        self.pointer.switch(previous)
        try:
            return self.store.rollback_release(release_id, idempotency_key="rollback-complete")
        except Exception:
            self.pointer.switch(release)
            raise
