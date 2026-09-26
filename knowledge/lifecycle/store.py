"""SQLite-backed lifecycle repository.

SQLite is the control-plane database for the current single-site deployment.
The API deliberately uses short ``BEGIN IMMEDIATE`` transactions for leases,
cursor advancement, revision activation, and release swaps.  The schema and
repository can later be moved to PostgreSQL without changing connector or
worker contracts.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from knowledge.lifecycle.models import (
    ChangeKind,
    DeletionState,
    FailureCategory,
    ReleaseManifest,
    ReleaseState,
    RevisionState,
    SourceStatus,
    SyncRunState,
    TaskState,
    canonical_hash,
    canonical_json,
    stable_id,
)
from knowledge.lifecycle.state_machines import (
    DELETION_MACHINE,
    RELEASE_MACHINE,
    REVISION_MACHINE,
    SYNC_RUN_MACHINE,
    TASK_MACHINE,
)


MIGRATION_ROOT = Path(__file__).resolve().parents[2] / "migrations"
MIGRATIONS = (
    MIGRATION_ROOT / "0001_stage3_lifecycle.sql",
    *sorted((MIGRATION_ROOT / "sqlite").glob("*.sql")),
)
TERMINAL_TASK_STATES = {
    TaskState.SUCCEEDED.value,
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
    TaskState.DEAD_LETTERED.value,
}
SECRET_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "access_key",
    "secret_key",
    "authorization",
    "cookie",
    "private_key",
}


def _is_secret_key(value: Any) -> bool:
    key = str(value).strip().lower().replace("-", "_")
    if key.endswith(("_reference", "_ref")):
        return False
    return key in SECRET_KEYS or any(
        key.endswith(f"_{secret}") for secret in SECRET_KEYS
    )


def _contains_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _is_secret_key(key) or _contains_secret(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


class LeaseConflict(RuntimeError):
    pass


class LifecycleStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for migration in MIGRATIONS:
                if migration.stem in applied:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.stem, self.clock()),
                )
        finally:
            connection.close()

    @contextmanager
    def scheduler_leadership(self) -> Iterator[bool]:
        """Yield leadership for one scheduler pass.

        SQLite is development-only and therefore has a single scheduler. The
        PostgreSQL implementation overrides this with a session advisory lock.
        """

        yield True

    def fetch_one(self, sql: str, values: Sequence[Any] = ()) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(sql, values).fetchone()
        finally:
            connection.close()
        return dict(row) if row else None

    def fetch_all(self, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(sql, values).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ sources
    def upsert_source(
        self,
        *,
        source_id: str,
        tenant_id: str,
        connector_type: str,
        configuration_version: str,
        configuration: Mapping[str, Any],
        credential_reference: str = "",
        sync_policy: Mapping[str, Any] | None = None,
        permission_sync_policy: Mapping[str, Any] | None = None,
        status: SourceStatus = SourceStatus.ACTIVE,
    ) -> dict[str, Any]:
        if any(
            _contains_secret(value)
            for value in (
                configuration,
                sync_policy or {},
                permission_sync_policy or {},
            )
        ):
            raise ValueError("source configuration must reference credentials, not contain secrets")
        if credential_reference and not credential_reference.startswith(
            ("secret://", "vault://", "env://", "keyring://")
        ):
            raise ValueError("credential_reference must use an approved secret reference scheme")
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO sources(
                    source_id, tenant_id, connector_type, credential_reference,
                    configuration_version, configuration_json, sync_policy_json,
                    permission_sync_policy_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    connector_type=excluded.connector_type,
                    credential_reference=excluded.credential_reference,
                    configuration_version=excluded.configuration_version,
                    configuration_json=excluded.configuration_json,
                    sync_policy_json=excluded.sync_policy_json,
                    permission_sync_policy_json=excluded.permission_sync_policy_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    tenant_id,
                    connector_type,
                    credential_reference,
                    configuration_version,
                    canonical_json(dict(configuration)),
                    canonical_json(dict(sync_policy or {})),
                    canonical_json(dict(permission_sync_policy or {})),
                    status.value,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="source.upserted",
                entity_type="source",
                entity_id=source_id,
                actor="operator",
                idempotency_key=f"source:{configuration_version}",
                source_id=source_id,
                tenant_id=tenant_id,
            )
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM sources WHERE source_id=?", (source_id,))
        if not row:
            raise KeyError(f"unknown source: {source_id}")
        return row

    def list_due_sources(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.fetch_all(
            """SELECT * FROM sources WHERE status='active'
               AND (next_sync_at IS NULL OR next_sync_at<=?)
               ORDER BY COALESCE(next_sync_at,0), source_id LIMIT ?""",
            (self.clock(), max(1, int(limit))),
        )

    def mark_source_scheduled(self, source_id: str, *, next_sync_at: float) -> None:
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE sources SET next_sync_at=?,updated_at=? WHERE source_id=? AND status='active'",
                (next_sync_at, self.clock(), source_id),
            ).rowcount
            if updated != 1:
                raise KeyError(f"active source not found: {source_id}")

    # --------------------------------------------------------------- sync runs
    def create_sync_run(
        self,
        source_id: str,
        *,
        idempotency_key: str,
        trigger_type: str = "manual",
    ) -> dict[str, Any]:
        now = self.clock()
        source = self.get_source(source_id)
        run_id = stable_id("run", source_id, idempotency_key)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sync_runs(
                    run_id, source_id, idempotency_key, trigger_type,
                    cursor_before, current_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_id,
                    idempotency_key,
                    trigger_type,
                    source["sync_cursor"],
                    SyncRunState.PENDING.value,
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="sync_run.created",
                entity_type="sync_run",
                entity_id=run_id,
                actor="scheduler",
                idempotency_key=f"create:{idempotency_key}",
                run_id=run_id,
                source_id=source_id,
                tenant_id=source["tenant_id"],
                to_state=SyncRunState.PENDING.value,
            )
        return self.get_sync_run(run_id)

    def get_sync_run(self, run_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM sync_runs WHERE run_id=?", (run_id,))
        if not row:
            raise KeyError(f"unknown sync run: {run_id}")
        return row

    def acquire_sync_lease(self, run_id: str, owner: str, *, lease_seconds: float = 60) -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown sync run: {run_id}")
            current = SyncRunState(row["current_state"])
            target = SyncRunState.RUNNING
            if current == target and row["lease_owner"] == owner and (row["lease_expires_at"] or 0) > now:
                return dict(row)
            SYNC_RUN_MACHINE.validate(current, target, "worker")
            conflict = connection.execute(
                """SELECT run_id FROM sync_runs WHERE source_id=? AND run_id<>?
                   AND current_state='running' AND lease_expires_at>?""",
                (row["source_id"], run_id, now),
            ).fetchone()
            if conflict:
                raise LeaseConflict(f"source already leased by run {conflict['run_id']}")
            connection.execute(
                """UPDATE sync_runs SET current_state=?, lease_owner=?, lease_expires_at=?,
                   heartbeat_at=?, started_at=COALESCE(started_at, ?) WHERE run_id=?""",
                (target.value, owner, now + lease_seconds, now, now, run_id),
            )
            self._state_audit(connection, "sync_run", run_id, "worker", current.value, target.value, f"lease:{owner}:{int(now)}", run_id=run_id, source_id=row["source_id"])
        return self.get_sync_run(run_id)

    def heartbeat_sync(self, run_id: str, owner: str, *, lease_seconds: float = 60, checkpoint: Mapping[str, Any] | None = None) -> None:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """UPDATE sync_runs SET heartbeat_at=?, lease_expires_at=?,
                   checkpoint_json=COALESCE(?, checkpoint_json)
                   WHERE run_id=? AND current_state='running' AND lease_owner=?""",
                (now, now + lease_seconds, canonical_json(dict(checkpoint)) if checkpoint is not None else None, run_id, owner),
            ).rowcount
            if updated != 1:
                raise LeaseConflict("sync heartbeat rejected: lease is absent or owned by another worker")

    def finish_sync(
        self,
        run_id: str,
        owner: str,
        *,
        state: SyncRunState,
        cursor_after: str,
        counts: Mapping[str, int],
        error_category: FailureCategory | None = None,
        error_message: str = "",
    ) -> dict[str, Any]:
        if state not in {SyncRunState.SUCCEEDED, SyncRunState.PARTIALLY_FAILED, SyncRunState.FAILED, SyncRunState.DEAD_LETTERED, SyncRunState.CANCELLED}:
            raise ValueError("finish_sync requires a terminal state")
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["lease_owner"] != owner:
                raise LeaseConflict("sync finish rejected for non-owner")
            current = SyncRunState(row["current_state"])
            SYNC_RUN_MACHINE.validate(current, state, "worker")
            connection.execute(
                """UPDATE sync_runs SET current_state=?, cursor_after=?, discovered_count=?,
                   created_count=?, updated_count=?, deleted_count=?, unchanged_count=?,
                   error_category=?, error_message=?, finished_at=?, lease_owner=NULL,
                   lease_expires_at=NULL WHERE run_id=?""",
                (
                    state.value,
                    cursor_after,
                    int(counts.get("discovered", 0)),
                    int(counts.get("created", 0)),
                    int(counts.get("updated", 0)),
                    int(counts.get("deleted", 0)),
                    int(counts.get("unchanged", 0)),
                    error_category.value if error_category else None,
                    error_message[:2000],
                    now,
                    run_id,
                ),
            )
            # Cursor advancement shares the same commit as the successful run.
            if state in {SyncRunState.SUCCEEDED, SyncRunState.PARTIALLY_FAILED}:
                connection.execute(
                    "UPDATE sources SET sync_cursor=?, last_successful_sync=?, updated_at=? WHERE source_id=?",
                    (cursor_after, now, now, row["source_id"]),
                )
            self._state_audit(connection, "sync_run", run_id, "worker", current.value, state.value, f"finish:{state.value}", run_id=run_id, source_id=row["source_id"], error_category=error_category.value if error_category else "")
        return self.get_sync_run(run_id)

    def fail_sync_attempt(
        self,
        run_id: str,
        owner: str,
        *,
        category: FailureCategory,
        message: str,
        retryable: bool = True,
        max_retries: int = 5,
    ) -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["lease_owner"] != owner:
                raise LeaseConflict("sync failure rejected for non-owner")
            current = SyncRunState(row["current_state"])
            retry_count = int(row["retry_count"]) + 1
            target = (
                SyncRunState.RETRYING
                if retryable and retry_count < max_retries
                else SyncRunState.DEAD_LETTERED
                if retryable
                else SyncRunState.FAILED
            )
            SYNC_RUN_MACHINE.validate(current, target, "worker")
            connection.execute(
                """UPDATE sync_runs SET current_state=?,retry_count=?,lease_owner=NULL,
                   lease_expires_at=NULL,error_category=?,error_message=?,finished_at=?
                   WHERE run_id=?""",
                (
                    target.value,
                    retry_count,
                    category.value,
                    message[:2000],
                    now if target in {SyncRunState.FAILED, SyncRunState.DEAD_LETTERED} else None,
                    run_id,
                ),
            )
            self._state_audit(
                connection,
                "sync_run",
                run_id,
                "worker",
                current.value,
                target.value,
                f"failure:{retry_count}:{target.value}",
                run_id=run_id,
                source_id=row["source_id"],
                error_category=category.value,
            )
        return self.get_sync_run(run_id)

    def recover_expired_sync_runs(self, *, max_retries: int = 5) -> list[str]:
        now = self.clock()
        recovered: list[str] = []
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM sync_runs WHERE current_state='running' AND lease_expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                retry_count = int(row["retry_count"]) + 1
                target = (
                    SyncRunState.RETRYING
                    if retry_count < max_retries
                    else SyncRunState.DEAD_LETTERED
                )
                SYNC_RUN_MACHINE.validate(SyncRunState.RUNNING, target, "reaper")
                connection.execute(
                    """UPDATE sync_runs SET current_state=?,retry_count=?,lease_owner=NULL,
                       lease_expires_at=NULL,error_category='transient',
                       error_message='sync lease expired',finished_at=? WHERE run_id=?""",
                    (
                        target.value,
                        retry_count,
                        now if target == SyncRunState.DEAD_LETTERED else None,
                        row["run_id"],
                    ),
                )
                self._state_audit(
                    connection,
                    "sync_run",
                    row["run_id"],
                    "reaper",
                    SyncRunState.RUNNING.value,
                    target.value,
                    f"lease-expired:{retry_count}",
                    run_id=row["run_id"],
                    source_id=row["source_id"],
                    error_category=FailureCategory.TRANSIENT.value,
                )
                recovered.append(row["run_id"])
        return recovered

    # --------------------------------------------------------- documents/revisions
    def list_source_documents(self, source_id: str) -> list[dict[str, Any]]:
        return self.fetch_all(
            """SELECT d.*, r.content_hash, r.metadata_hash, r.acl_hash,
                      r.parser_fingerprint, r.processing_status
               FROM documents d LEFT JOIN document_revisions r ON r.revision_id=COALESCE(
                   d.active_revision_id,
                   (SELECT revision_id FROM document_revisions latest
                    WHERE latest.document_id=d.document_id
                    ORDER BY latest.created_at DESC, latest.revision_id DESC LIMIT 1)
               ) WHERE d.source_id=?""",
            (source_id,),
        )

    def upsert_revision(
        self,
        *,
        source_id: str,
        tenant_id: str,
        source_item_id: str,
        source_path: str,
        content_hash: str,
        metadata_hash: str,
        acl_hash: str,
        parser_fingerprint: str,
        visibility: str,
        acl_readers: Sequence[str],
        source_modified_at: float | None,
        change_kind: ChangeKind,
        run_id: str,
        previous_revision_id: str = "",
        document_id_override: str = "",
    ) -> tuple[dict[str, Any], bool]:
        now = self.clock()
        document_id = document_id_override or stable_id("doc", source_id, source_item_id, length=24)
        revision_id = stable_id(
            "rev", document_id, content_hash, metadata_hash, acl_hash, parser_fingerprint
        )
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO documents(
                    document_id, source_id, tenant_id, source_item_id, source_path,
                    visibility, acl_json, tombstoned, first_discovered_at, last_discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET source_path=excluded.source_path,
                    visibility=excluded.visibility, acl_json=excluded.acl_json,
                    tombstoned=0, last_discovered_at=excluded.last_discovered_at""",
                (document_id, source_id, tenant_id, source_item_id, source_path, visibility, canonical_json(sorted(set(acl_readers))), now, now),
            )
            inserted = connection.execute(
                """INSERT OR IGNORE INTO document_revisions(
                    revision_id, document_id, content_hash, metadata_hash, acl_hash,
                    parser_fingerprint, source_modified_at, processing_status,
                    change_kind, lineage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    document_id,
                    content_hash,
                    metadata_hash,
                    acl_hash,
                    parser_fingerprint,
                    source_modified_at,
                    RevisionState.DISCOVERED.value,
                    change_kind.value,
                    canonical_json({"run_id": run_id, "previous_revision_id": previous_revision_id}),
                    now,
                ),
            ).rowcount == 1
            self._audit(
                connection,
                event_type="revision.discovered" if inserted else "revision.redelivered",
                entity_type="document_revision",
                entity_id=revision_id,
                actor="sync",
                idempotency_key=f"discover:{run_id}",
                run_id=run_id,
                source_id=source_id,
                tenant_id=tenant_id,
                document_id=document_id,
                revision_id=revision_id,
                to_state=RevisionState.DISCOVERED.value,
                details={"change_kind": change_kind.value},
            )
        return self.get_revision(revision_id), inserted

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM document_revisions WHERE revision_id=?", (revision_id,))
        if not row:
            raise KeyError(revision_id)
        return row

    def bind_revision_projection(
        self, revision_id: str, *, data_revision_id: str, staging_release_id: str
    ) -> dict[str, Any]:
        if not data_revision_id or not staging_release_id:
            raise ValueError("data revision and staging release are required")
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """UPDATE document_revisions SET data_revision_id=?,staging_release_id=?
                   WHERE revision_id=?""",
                (data_revision_id, staging_release_id, revision_id),
            ).rowcount
            if updated != 1:
                raise KeyError(revision_id)
            row = connection.execute(
                "SELECT document_id FROM document_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            self._audit(
                connection,
                event_type="revision.projection_bound",
                entity_type="document_revision",
                entity_id=revision_id,
                actor="worker",
                idempotency_key=f"projection:{data_revision_id}",
                document_id=row["document_id"],
                revision_id=revision_id,
                release_id=staging_release_id,
            )
        return self.get_revision(revision_id)

    def transition_revision(self, revision_id: str, target: RevisionState, *, actor: str, idempotency_key: str) -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT r.*, d.source_id, d.tenant_id FROM document_revisions r
                   JOIN documents d ON d.document_id=r.document_id WHERE revision_id=?""",
                (revision_id,),
            ).fetchone()
            if not row:
                raise KeyError(revision_id)
            current = RevisionState(row["processing_status"])
            if current == target:
                return dict(row)
            REVISION_MACHINE.validate(current, target, actor)
            activated = now if target == RevisionState.ACTIVE else row["activated_at"]
            retired = now if target == RevisionState.RETIRED else row["retired_at"]
            connection.execute(
                "UPDATE document_revisions SET processing_status=?, activated_at=?, retired_at=? WHERE revision_id=?",
                (target.value, activated, retired, revision_id),
            )
            if target == RevisionState.ACTIVE:
                old = connection.execute("SELECT active_revision_id FROM documents WHERE document_id=?", (row["document_id"],)).fetchone()
                old_id = old["active_revision_id"] if old else None
                if old_id and old_id != revision_id:
                    connection.execute(
                        "UPDATE document_revisions SET processing_status='retired', retired_at=? WHERE revision_id=? AND processing_status='active'",
                        (now, old_id),
                    )
                connection.execute("UPDATE documents SET active_revision_id=? WHERE document_id=?", (revision_id, row["document_id"]))
            self._state_audit(connection, "document_revision", revision_id, actor, current.value, target.value, idempotency_key, source_id=row["source_id"], tenant_id=row["tenant_id"], document_id=row["document_id"], revision_id=revision_id)
        return self.get_revision(revision_id)

    def rename_document(self, document_id: str, *, new_source_item_id: str, new_path: str, run_id: str) -> None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if not row:
                raise KeyError(document_id)
            connection.execute("UPDATE documents SET source_item_id=?, source_path=?, last_discovered_at=? WHERE document_id=?", (new_source_item_id, new_path, self.clock(), document_id))
            self._audit(connection, event_type="document.renamed", entity_type="document", entity_id=document_id, actor="sync", idempotency_key=f"rename:{run_id}:{new_source_item_id}", run_id=run_id, source_id=row["source_id"], tenant_id=row["tenant_id"], document_id=document_id, details={"from": row["source_path"], "to": new_path})

    # --------------------------------------------------------------------- tasks
    def enqueue_task(
        self,
        task_type: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        source_id: str | None = None,
        run_id: str | None = None,
        max_attempts: int = 5,
        available_at: float | None = None,
    ) -> dict[str, Any]:
        now = self.clock()
        task_id = stable_id("task", task_type, idempotency_key)
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO lifecycle_tasks(
                    task_id, source_id, run_id, task_type, idempotency_key,
                    payload_json, state, max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, source_id, run_id, task_type, idempotency_key, canonical_json(dict(payload)), TaskState.PENDING.value, max_attempts, now if available_at is None else available_at, now, now),
            )
            self._audit(connection, event_type="task.enqueued", entity_type="lifecycle_task", entity_id=task_id, actor="scheduler", idempotency_key=f"enqueue:{idempotency_key}", run_id=run_id or "", source_id=source_id or "", to_state=TaskState.PENDING.value)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,))
        if not row:
            raise KeyError(task_id)
        return row

    def claim_task(self, owner: str, *, lease_seconds: float = 60, task_types: Sequence[str] = ()) -> dict[str, Any] | None:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            params: list[Any] = [now]
            type_clause = ""
            if task_types:
                type_clause = f" AND task_type IN ({','.join('?' for _ in task_types)})"
                params.extend(task_types)
            # One running task per source; source-less maintenance tasks remain claimable.
            row = connection.execute(
                f"""SELECT * FROM lifecycle_tasks t WHERE state IN ('pending','retrying')
                    AND available_at<=? {type_clause}
                    AND (source_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM lifecycle_tasks active
                        WHERE active.source_id=t.source_id AND active.state='running'
                          AND active.lease_expires_at>?
                    )) ORDER BY created_at LIMIT 1""",
                (*params, now),
            ).fetchone()
            if not row:
                return None
            current = TaskState(row["state"])
            TASK_MACHINE.validate(current, TaskState.RUNNING, "worker")
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """UPDATE lifecycle_tasks SET state='running', attempt=?, lease_owner=?,
                   lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE task_id=?""",
                (attempt, owner, now + lease_seconds, now, now, row["task_id"]),
            )
            self._state_audit(connection, "lifecycle_task", row["task_id"], "worker", current.value, TaskState.RUNNING.value, f"claim:{attempt}", run_id=row["run_id"] or "", source_id=row["source_id"] or "", task_attempt=attempt)
        return self.get_task(row["task_id"])

    def heartbeat_task(self, task_id: str, owner: str, *, checkpoint: Mapping[str, Any] | None = None, lease_seconds: float = 60) -> None:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                """UPDATE lifecycle_tasks SET heartbeat_at=?, lease_expires_at=?,
                   checkpoint_json=COALESCE(?,checkpoint_json), updated_at=?
                   WHERE task_id=? AND state='running' AND lease_owner=?""",
                (now, now + lease_seconds, canonical_json(dict(checkpoint)) if checkpoint is not None else None, now, task_id, owner),
            ).rowcount
            if updated != 1:
                raise LeaseConflict("task heartbeat rejected: lease is absent or owned by another worker")

    def complete_task(self, task_id: str, owner: str) -> dict[str, Any]:
        return self._finish_task(task_id, owner, TaskState.SUCCEEDED)

    def cancel_running_task(self, task_id: str, owner: str) -> dict[str, Any]:
        return self._finish_task(
            task_id,
            owner,
            TaskState.CANCELLED,
            message="cancellation requested",
        )

    def fail_task(
        self,
        task_id: str,
        owner: str,
        category: FailureCategory,
        message: str,
        *,
        base_delay: float = 1.0,
        max_delay: float = 300.0,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        attempt = int(task["attempt"])
        if category == FailureCategory.TRANSIENT and attempt < int(task["max_attempts"]):
            delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
            return self._finish_task(task_id, owner, TaskState.RETRYING, category=category, message=message, available_at=self.clock() + delay)
        target = TaskState.DEAD_LETTERED if category in {FailureCategory.TRANSIENT, FailureCategory.POISON} else TaskState.FAILED
        return self._finish_task(task_id, owner, target, category=category, message=message)

    def _finish_task(self, task_id: str, owner: str, target: TaskState, *, category: FailureCategory | None = None, message: str = "", available_at: float | None = None) -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["lease_owner"] != owner:
                raise LeaseConflict("task mutation rejected for non-owner")
            current = TaskState(row["state"])
            TASK_MACHINE.validate(current, target, "worker")
            finished = now if target.value in TERMINAL_TASK_STATES else None
            connection.execute(
                """UPDATE lifecycle_tasks SET state=?, error_category=?, error_message=?,
                   available_at=COALESCE(?,available_at), lease_owner=NULL,
                   lease_expires_at=NULL, updated_at=?, finished_at=? WHERE task_id=?""",
                (target.value, category.value if category else None, message[:2000], available_at, now, finished, task_id),
            )
            self._state_audit(connection, "lifecycle_task", task_id, "worker", current.value, target.value, f"finish:{row['attempt']}:{target.value}", run_id=row["run_id"] or "", source_id=row["source_id"] or "", task_attempt=int(row["attempt"]), error_category=category.value if category else "")
        return self.get_task(task_id)

    def recover_expired_tasks(self) -> list[str]:
        now = self.clock()
        recovered: list[str] = []
        with self.transaction(immediate=True) as connection:
            rows = connection.execute("SELECT * FROM lifecycle_tasks WHERE state='running' AND lease_expires_at<=?", (now,)).fetchall()
            for row in rows:
                current = TaskState.RUNNING
                target = TaskState.RETRYING if int(row["attempt"]) < int(row["max_attempts"]) else TaskState.DEAD_LETTERED
                TASK_MACHINE.validate(current, target, "reaper")
                connection.execute("UPDATE lifecycle_tasks SET state=?, lease_owner=NULL, lease_expires_at=NULL, available_at=?, updated_at=?, error_category='transient', error_message='worker lease expired' WHERE task_id=?", (target.value, now, now, row["task_id"]))
                self._state_audit(connection, "lifecycle_task", row["task_id"], "reaper", current.value, target.value, f"lease-expired:{row['attempt']}", run_id=row["run_id"] or "", source_id=row["source_id"] or "", task_attempt=int(row["attempt"]), error_category=FailureCategory.TRANSIENT.value)
                recovered.append(row["task_id"])
        return recovered

    def replay_dead_letter(self, task_id: str, *, actor: str = "operator") -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            TASK_MACHINE.validate(current, TaskState.PENDING, actor)
            connection.execute("UPDATE lifecycle_tasks SET state='pending', attempt=0, available_at=?, error_category=NULL, error_message='', finished_at=NULL, updated_at=? WHERE task_id=?", (now, now, task_id))
            self._state_audit(connection, "lifecycle_task", task_id, actor, current.value, TaskState.PENDING.value, f"replay:{int(now)}", run_id=row["run_id"] or "", source_id=row["source_id"] or "")
        return self.get_task(task_id)

    def request_task_cancellation(self, task_id: str, *, actor: str = "operator") -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM lifecycle_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            if current.value in TERMINAL_TASK_STATES:
                return dict(row)
            now = self.clock()
            if current in {TaskState.PENDING, TaskState.RETRYING}:
                TASK_MACHINE.validate(current, TaskState.CANCELLED, actor)
                connection.execute(
                    """UPDATE lifecycle_tasks SET state='cancelled',
                       cancellation_requested=1,finished_at=?,updated_at=? WHERE task_id=?""",
                    (now, now, task_id),
                )
                self._state_audit(
                    connection,
                    "lifecycle_task",
                    task_id,
                    actor,
                    current.value,
                    TaskState.CANCELLED.value,
                    f"cancel:{int(now)}",
                    run_id=row["run_id"] or "",
                    source_id=row["source_id"] or "",
                    task_attempt=int(row["attempt"]),
                )
            else:
                connection.execute(
                    "UPDATE lifecycle_tasks SET cancellation_requested=1,updated_at=? WHERE task_id=?",
                    (now, task_id),
                )
                self._audit(connection, event_type="task.cancellation_requested", entity_type="lifecycle_task", entity_id=task_id, actor=actor, idempotency_key=f"cancel:{int(now)}", run_id=row["run_id"] or "", source_id=row["source_id"] or "", task_attempt=int(row["attempt"]))
        return self.get_task(task_id)

    # ------------------------------------------------------------------ releases
    def create_release(self, manifest: ReleaseManifest, *, previous_release_id: str | None = None) -> dict[str, Any]:
        now = self.clock()
        payload = manifest.canonical_dict()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM index_releases WHERE release_id=? OR manifest_hash=?",
                (manifest.release_id, manifest.manifest_hash),
            ).fetchone()
            if existing and (
                existing["release_id"] != manifest.release_id
                or existing["manifest_hash"] != manifest.manifest_hash
            ):
                raise ValueError("release id or manifest hash already belongs to another release")
            connection.execute(
                """INSERT OR IGNORE INTO index_releases(
                    release_id, source_revisions_json, chunk_collection, entity_collection,
                    graph_version, object_asset_namespace, schema_version,
                    evaluation_report, manifest_json, manifest_hash, current_state,
                    previous_release_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (manifest.release_id, canonical_json(manifest.source_revisions), manifest.chunk_collection, manifest.entity_collection, manifest.graph_version, manifest.object_asset_namespace, manifest.schema_version, manifest.evaluation_report, canonical_json(payload), manifest.manifest_hash, ReleaseState.BUILDING.value, previous_release_id, now, now),
            )
            self._audit(connection, event_type="release.created", entity_type="index_release", entity_id=manifest.release_id, actor="builder", idempotency_key=f"create:{manifest.manifest_hash}", release_id=manifest.release_id, to_state=ReleaseState.BUILDING.value)
        return self.get_release(manifest.release_id)

    def get_release(self, release_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM index_releases WHERE release_id=?", (release_id,))
        if not row:
            raise KeyError(release_id)
        return row

    def attach_revision_to_release(
        self, release_id: str, revision_id: str
    ) -> dict[str, Any]:
        """Add a staged revision while a release manifest is still being built."""

        now = self.clock()
        with self.transaction(immediate=True) as connection:
            release = connection.execute(
                "SELECT * FROM index_releases WHERE release_id=?", (release_id,)
            ).fetchone()
            if not release:
                raise KeyError(release_id)
            if release["current_state"] not in {
                ReleaseState.BUILDING.value,
                ReleaseState.STAGING.value,
            }:
                raise ValueError("release manifest is immutable after validation starts")
            revision = connection.execute(
                "SELECT * FROM document_revisions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if not revision or revision["processing_status"] not in {
                RevisionState.STAGED.value,
                RevisionState.VALIDATED.value,
                RevisionState.ACTIVE.value,
            }:
                raise ValueError("only staged or validated revisions can join a release")
            revision_ids = list(json.loads(str(release["source_revisions_json"])))
            if revision_id in revision_ids:
                return dict(release)
            revision_ids.append(revision_id)
            revision_ids.sort()
            manifest = json.loads(str(release["manifest_json"]))
            manifest["source_revisions"] = revision_ids
            manifest_json = canonical_json(manifest)
            manifest_hash = canonical_hash(manifest)
            connection.execute(
                """UPDATE index_releases SET source_revisions_json=?,manifest_json=?,
                   manifest_hash=?,updated_at=? WHERE release_id=?""",
                (
                    canonical_json(revision_ids),
                    manifest_json,
                    manifest_hash,
                    now,
                    release_id,
                ),
            )
            self._audit(
                connection,
                event_type="release.revision_attached",
                entity_type="index_release",
                entity_id=release_id,
                actor="worker",
                idempotency_key=f"attach:{revision_id}",
                revision_id=revision_id,
                release_id=release_id,
            )
        return self.get_release(release_id)

    def get_active_release(self) -> dict[str, Any] | None:
        return self.fetch_one(
            "SELECT * FROM index_releases WHERE current_state='active'"
        )

    def transition_release(self, release_id: str, target: ReleaseState, *, actor: str, idempotency_key: str) -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM index_releases WHERE release_id=?", (release_id,)).fetchone()
            if not row:
                raise KeyError(release_id)
            current = ReleaseState(row["current_state"])
            if current == target:
                return dict(row)
            RELEASE_MACHINE.validate(current, target, actor)
            activated_at = now if target == ReleaseState.ACTIVE else row["activated_at"]
            rolled_back_at = now if target == ReleaseState.ROLLED_BACK else row["rolled_back_at"]
            if target == ReleaseState.ACTIVE:
                existing = connection.execute("SELECT release_id FROM index_releases WHERE current_state='active' AND release_id<>?", (release_id,)).fetchone()
                if existing:
                    connection.execute("UPDATE index_releases SET current_state='retired', updated_at=? WHERE release_id=?", (now, existing["release_id"]))
                revision_ids = json.loads(str(row["source_revisions_json"]))
                for revision_id in revision_ids:
                    revision = connection.execute(
                        "SELECT * FROM document_revisions WHERE revision_id=?",
                        (revision_id,),
                    ).fetchone()
                    if not revision or revision["processing_status"] not in {
                        RevisionState.VALIDATED.value,
                        RevisionState.ACTIVE.value,
                    }:
                        raise ValueError(
                            f"release revision is not validated: {revision_id}"
                        )
                    previous = connection.execute(
                        "SELECT active_revision_id FROM documents WHERE document_id=?",
                        (revision["document_id"],),
                    ).fetchone()
                    previous_id = previous["active_revision_id"] if previous else None
                    if previous_id and previous_id != revision_id:
                        connection.execute(
                            """UPDATE document_revisions
                               SET processing_status='retired',retired_at=?
                               WHERE revision_id=? AND processing_status='active'""",
                            (now, previous_id),
                        )
                    connection.execute(
                        """UPDATE document_revisions SET processing_status='active',activated_at=?
                           WHERE revision_id=?""",
                        (now, revision_id),
                    )
                    connection.execute(
                        "UPDATE documents SET active_revision_id=? WHERE document_id=?",
                        (revision_id, revision["document_id"]),
                    )
            connection.execute("UPDATE index_releases SET current_state=?, activated_at=?, rolled_back_at=?, updated_at=? WHERE release_id=?", (target.value, activated_at, rolled_back_at, now, release_id))
            self._state_audit(connection, "index_release", release_id, actor, current.value, target.value, idempotency_key, release_id=release_id)
        return self.get_release(release_id)

    def rollback_release(self, release_id: str, *, idempotency_key: str) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM index_releases WHERE release_id=?", (release_id,)).fetchone()
            if not row:
                raise KeyError(release_id)
            previous_id = row["previous_release_id"]
            if not previous_id:
                raise ValueError("release has no previous release")
            current = ReleaseState(row["current_state"])
            RELEASE_MACHINE.validate(current, ReleaseState.ROLLING_BACK, "rollback")
            now = self.clock()
            connection.execute("UPDATE index_releases SET current_state='rolling_back', updated_at=? WHERE release_id=?", (now, release_id))
            previous = connection.execute("SELECT * FROM index_releases WHERE release_id=?", (previous_id,)).fetchone()
            if not previous or ReleaseState(previous["current_state"]) != ReleaseState.RETIRED:
                raise ValueError("previous release is not rollback-compatible")
            RELEASE_MACHINE.validate(ReleaseState.RETIRED, ReleaseState.ACTIVE, "rollback")
            connection.execute("UPDATE index_releases SET current_state='rolled_back', rolled_back_at=?, updated_at=? WHERE release_id=?", (now, now, release_id))
            connection.execute("UPDATE index_releases SET current_state='active', activated_at=?, updated_at=? WHERE release_id=?", (now, now, previous_id))
            self._state_audit(connection, "index_release", release_id, "rollback", current.value, ReleaseState.ROLLED_BACK.value, idempotency_key, release_id=release_id, details={"restored_release_id": previous_id})
        return self.get_release(previous_id)

    def sync_identity_mappings(self, source_id: str, mappings: Sequence[Mapping[str, Any]], *, idempotency_key: str) -> dict[str, int]:
        """Replace one source's IdP snapshot without persisting raw external IDs."""

        now = self.clock()
        seen: set[str] = set()
        with self.transaction(immediate=True) as connection:
            source = connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not source:
                raise KeyError(source_id)
            for mapping in mappings:
                external_hash = str(mapping["external_principal_hash"])
                seen.add(external_hash)
                mapping_id = stable_id("principal", source_id, external_hash)
                connection.execute(
                    """INSERT INTO identity_mappings(mapping_id,source_id,external_principal_hash,
                       internal_principal,principal_type,active,synced_at)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,external_principal_hash)
                       DO UPDATE SET internal_principal=excluded.internal_principal,
                       principal_type=excluded.principal_type,active=excluded.active,
                       synced_at=excluded.synced_at""",
                    (mapping_id, source_id, external_hash, mapping["internal_principal"], mapping.get("principal_type", "subject"), 1 if mapping.get("active", True) else 0, now),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                connection.execute(
                    f"UPDATE identity_mappings SET active=0,synced_at=? WHERE source_id=? AND external_principal_hash NOT IN ({placeholders})",
                    (now, source_id, *sorted(seen)),
                )
            else:
                connection.execute("UPDATE identity_mappings SET active=0,synced_at=? WHERE source_id=?", (now, source_id))
            self._audit(connection, event_type="acl.identity_snapshot_synced", entity_type="source", entity_id=source_id, actor="sync", idempotency_key=idempotency_key, source_id=source_id, tenant_id=source["tenant_id"], details={"principal_count": len(seen)})
            active = connection.execute("SELECT COUNT(*) FROM identity_mappings WHERE source_id=? AND active=1", (source_id,)).fetchone()[0]
            revoked = connection.execute("SELECT COUNT(*) FROM identity_mappings WHERE source_id=? AND active=0", (source_id,)).fetchone()[0]
        return {"active": int(active), "revoked": int(revoked)}

    def record_reconciliation(self, *, backend: str, finding: str, identity: str, action: str, repaired: bool) -> None:
        with self.transaction(immediate=True) as connection:
            self._audit(connection, event_type="reconciliation.orphan_detected", entity_type="reconciliation", entity_id=stable_id("finding", backend, finding, identity), actor="reconciler", idempotency_key=f"{action}:{int(self.clock())}", details={"backend": backend, "finding": finding, "identity": identity, "action": action, "repaired": repaired})

    # ---------------------------------------------------------------- deletions
    def create_deletion(self, document_id: str, revision_id: str | None, *, run_id: str, targets: Sequence[str]) -> dict[str, Any]:
        deletion_id = stable_id("del", document_id, revision_id or "all")
        key = f"delete:{document_id}:{revision_id or 'all'}"
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            document = connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
            if not document:
                raise KeyError(document_id)
            connection.execute("INSERT OR IGNORE INTO deletion_jobs(deletion_id,document_id,revision_id,idempotency_key,state,targets_json,detected_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (deletion_id, document_id, revision_id, key, DeletionState.DETECTED.value, canonical_json(list(targets)), now, now))
            self._audit(connection, event_type="deletion.detected", entity_type="deletion", entity_id=deletion_id, actor="sync", idempotency_key=f"detect:{run_id}", run_id=run_id, source_id=document["source_id"], tenant_id=document["tenant_id"], document_id=document_id, revision_id=revision_id or "", to_state=DeletionState.DETECTED.value)
        return self.get_deletion(deletion_id)

    def get_deletion(self, deletion_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM deletion_jobs WHERE deletion_id=?", (deletion_id,))
        if not row:
            raise KeyError(deletion_id)
        return row

    def transition_deletion(self, deletion_id: str, target: DeletionState, *, actor: str, idempotency_key: str, completed_targets: Sequence[str] | None = None, verification: Mapping[str, Any] | None = None, error_message: str = "") -> dict[str, Any]:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT dj.*, d.source_id, d.tenant_id FROM deletion_jobs dj JOIN documents d ON d.document_id=dj.document_id WHERE deletion_id=?", (deletion_id,)).fetchone()
            if not row:
                raise KeyError(deletion_id)
            current = DeletionState(row["state"])
            if current == target:
                return dict(row)
            DELETION_MACHINE.validate(current, target, actor)
            connection.execute("UPDATE deletion_jobs SET state=?, completed_targets_json=COALESCE(?,completed_targets_json), verification_json=COALESCE(?,verification_json), error_message=?, completed_at=?, updated_at=? WHERE deletion_id=?", (target.value, canonical_json(list(completed_targets)) if completed_targets is not None else None, canonical_json(dict(verification)) if verification is not None else None, error_message[:2000], now if target == DeletionState.COMPLETED else None, now, deletion_id))
            if target == DeletionState.TOMBSTONED:
                connection.execute("UPDATE documents SET tombstoned=1 WHERE document_id=?", (row["document_id"],))
            if target == DeletionState.COMPLETED and row["revision_id"]:
                connection.execute("UPDATE document_revisions SET processing_status='deleted' WHERE revision_id=?", (row["revision_id"],))
            self._state_audit(connection, "deletion", deletion_id, actor, current.value, target.value, idempotency_key, source_id=row["source_id"], tenant_id=row["tenant_id"], document_id=row["document_id"], revision_id=row["revision_id"] or "")
        return self.get_deletion(deletion_id)

    # ------------------------------------------------------------- observability
    def metrics_snapshot(self) -> dict[str, float]:
        now = self.clock()
        metrics: dict[str, float] = {}
        connection = self._connect()
        try:
            metrics["source_sync_total"] = float(connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0])
            metrics["source_sync_failure_total"] = float(connection.execute("SELECT COUNT(*) FROM sync_runs WHERE current_state IN ('failed','dead_lettered','partially_failed')").fetchone()[0])
            metrics["task_queue_depth"] = float(connection.execute("SELECT COUNT(*) FROM lifecycle_tasks WHERE state IN ('pending','retrying')").fetchone()[0])
            oldest = connection.execute("SELECT MIN(created_at) FROM lifecycle_tasks WHERE state IN ('pending','retrying')").fetchone()[0]
            metrics["oldest_task_age"] = max(0.0, now - oldest) if oldest is not None else 0.0
            metrics["retry_total"] = float(connection.execute("SELECT COALESCE(SUM(CASE WHEN attempt>1 THEN attempt-1 ELSE 0 END),0) FROM lifecycle_tasks").fetchone()[0])
            metrics["dead_letter_total"] = float(connection.execute("SELECT COUNT(*) FROM lifecycle_tasks WHERE state='dead_lettered'").fetchone()[0])
            metrics["documents_discovered"] = float(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            metrics["documents_updated"] = float(connection.execute("SELECT COUNT(*) FROM document_revisions WHERE change_kind IN ('content','metadata','acl_only','renamed')").fetchone()[0])
            metrics["documents_deleted"] = float(connection.execute("SELECT COUNT(*) FROM documents WHERE tombstoned=1").fetchone()[0])
            metrics["staging_validation_failure_total"] = float(connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='index_release.state_changed' AND to_state='failed'").fetchone()[0])
            metrics["release_activation_total"] = float(connection.execute("SELECT COUNT(*) FROM audit_events WHERE entity_type='index_release' AND to_state='active'").fetchone()[0])
            metrics["release_rollback_total"] = float(connection.execute("SELECT COUNT(*) FROM audit_events WHERE entity_type='index_release' AND to_state='rolled_back'").fetchone()[0])
            metrics["orphan_record_count"] = float(connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='reconciliation.orphan_detected'").fetchone()[0])
            active = connection.execute("SELECT COUNT(*) FROM index_releases WHERE current_state='active'").fetchone()[0]
            metrics["active_release_health"] = 1.0 if active == 1 else 0.0
            duration = connection.execute(
                """SELECT AVG(finished_at-started_at) FROM sync_runs
                   WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"""
            ).fetchone()[0]
            metrics["source_sync_duration"] = float(duration or 0.0)
            lag = connection.execute(
                """SELECT MAX(?-last_successful_sync) FROM sources
                   WHERE status='active' AND last_successful_sync IS NOT NULL""",
                (now,),
            ).fetchone()[0]
            metrics["source_sync_lag"] = max(0.0, float(lag or 0.0))
            revision_duration = connection.execute(
                """SELECT AVG(COALESCE(activated_at,retired_at)-created_at)
                   FROM document_revisions
                   WHERE activated_at IS NOT NULL OR retired_at IS NOT NULL"""
            ).fetchone()[0]
            metrics["revision_processing_duration"] = float(revision_duration or 0.0)
            for name in (
                "acl_sync_lag",
                "deletion_propagation_lag",
                "connector_rate_limit_total",
                "connector_error_total",
            ):
                aggregate = "SUM" if name.endswith("_total") else "MAX"
                value = connection.execute(
                    f"SELECT {aggregate}(value) FROM lifecycle_metric_events WHERE metric_name=?",
                    (name,),
                ).fetchone()[0]
                metrics[name] = float(value or 0.0)
        finally:
            connection.close()
        return metrics

    def record_metric(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO lifecycle_metric_events(metric_name,value,labels_json,created_at)
                   VALUES(?,?,?,?)""",
                (name, float(value), canonical_json(dict(labels or {})), self.clock()),
            )

    def audit_events(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        if entity_type:
            return self.fetch_all("SELECT * FROM audit_events WHERE entity_type=? ORDER BY event_id", (entity_type,))
        return self.fetch_all("SELECT * FROM audit_events ORDER BY event_id")

    # ---------------------------------------------------------------- internals
    def _state_audit(self, connection: sqlite3.Connection, entity_type: str, entity_id: str, actor: str, from_state: str, to_state: str, idempotency_key: str, **context: Any) -> None:
        self._audit(connection, event_type=f"{entity_type}.state_changed", entity_type=entity_type, entity_id=entity_id, actor=actor, idempotency_key=idempotency_key, from_state=from_state, to_state=to_state, **context)

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        idempotency_key: str,
        trace_id: str = "",
        run_id: str = "",
        source_id: str = "",
        tenant_id: str = "",
        document_id: str = "",
        revision_id: str = "",
        release_id: str = "",
        task_attempt: int = 0,
        from_state: str = "",
        to_state: str = "",
        error_category: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12] if tenant_id else ""
        connection.execute(
            """INSERT OR IGNORE INTO audit_events(
                event_type,entity_type,entity_id,actor,idempotency_key,trace_id,
                run_id,source_id,tenant_hash,document_id,revision_id,release_id,
                task_attempt,from_state,to_state,error_category,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_type, entity_type, entity_id, actor, idempotency_key, trace_id, run_id, source_id, tenant_hash, document_id, revision_id, release_id, task_attempt, from_state, to_state, error_category, canonical_json(dict(details or {})), self.clock()),
        )
