"""PostgreSQL lifecycle repository for multi-process production deployments."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterator, Sequence

from knowledge.lifecycle.models import SyncRunState, TaskState
from knowledge.lifecycle.state_machines import SYNC_RUN_MACHINE, TASK_MACHINE
from knowledge.lifecycle.store import LeaseConflict, LifecycleStore


POSTGRES_MIGRATIONS = tuple(
    sorted(
        (Path(__file__).resolve().parents[2] / "migrations" / "postgres").glob("*.sql")
    )
)


class CompatRow(dict[str, Any]):
    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class CompatCursor:
    def __init__(self, cursor: Any):
        self.cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(self.cursor.rowcount)

    def _convert(self, row: Sequence[Any] | None) -> CompatRow | None:
        if row is None:
            return None
        columns = [column.name for column in self.cursor.description or ()]
        return CompatRow(zip(columns, row))

    def fetchone(self) -> CompatRow | None:
        return self._convert(self.cursor.fetchone())

    def fetchall(self) -> list[CompatRow]:
        return [self._convert(row) for row in self.cursor.fetchall()]  # type: ignore[list-item]


def _postgres_sql(sql: str) -> str:
    statement = sql.strip().rstrip(";")
    ignore = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", statement))
    if ignore:
        statement = re.sub(
            r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", statement
        )
        statement += " ON CONFLICT DO NOTHING"
    return statement.replace("?", "%s")


class CompatConnection:
    def __init__(self, connection: Any):
        self.connection = connection

    def execute(self, sql: str, values: Sequence[Any] = ()) -> CompatCursor:
        return CompatCursor(self.connection.execute(_postgres_sql(sql), tuple(values)))

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class PostgresLifecycleStore(LifecycleStore):
    """LifecycleStore API backed by PostgreSQL row locks and SKIP LOCKED."""

    SCHEDULER_ADVISORY_LOCK = 7419032602

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], float] = time.time,
        migrate: bool = True,
    ):
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgreSQL DSN must use postgresql:// or postgres://")
        self.dsn = dsn
        self.clock = clock
        if migrate:
            self.migrate()

    def _raw_connect(self) -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "PostgreSQL lifecycle mode requires psycopg[binary]"
            ) from exc
        return psycopg.connect(self.dsn)

    def _connect(self) -> CompatConnection:
        return CompatConnection(self._raw_connect())

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[CompatConnection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        raw = self._raw_connect()
        try:
            # Serialize process startup so two fresh workers cannot both apply
            # the same migration and race on schema_migrations.
            raw.execute("SELECT pg_advisory_xact_lock(%s)", (7419032601,))
            raw.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at DOUBLE PRECISION NOT NULL)"
            )
            applied = {row[0] for row in raw.execute("SELECT version FROM schema_migrations")}
            for migration in POSTGRES_MIGRATIONS:
                if migration.stem in applied:
                    continue
                raw.execute(migration.read_text(encoding="utf-8"))
                raw.execute(
                    "INSERT INTO schema_migrations(version,applied_at) VALUES (%s,%s)",
                    (migration.stem, self.clock()),
                )
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

    @contextmanager
    def scheduler_leadership(self) -> Iterator[bool]:
        """Hold a PostgreSQL advisory lock for exactly one scheduling pass."""

        raw = self._raw_connect()
        acquired = False
        try:
            row = raw.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (self.SCHEDULER_ADVISORY_LOCK,),
            ).fetchone()
            acquired = bool(row and row[0])
            yield acquired
        finally:
            if acquired:
                try:
                    raw.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (self.SCHEDULER_ADVISORY_LOCK,),
                    )
                except Exception:
                    pass
            raw.close()

    def acquire_sync_lease(
        self, run_id: str, owner: str, *, lease_seconds: float = 60
    ) -> dict[str, Any]:
        now = self.clock()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE run_id=? FOR UPDATE", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown sync run: {run_id}")
            current = SyncRunState(row["current_state"])
            if (
                current == SyncRunState.RUNNING
                and row["lease_owner"] == owner
                and (row["lease_expires_at"] or 0) > now
            ):
                return dict(row)
            SYNC_RUN_MACHINE.validate(current, SyncRunState.RUNNING, "worker")
            conflict = connection.execute(
                """SELECT run_id FROM sync_runs WHERE source_id=? AND run_id<>?
                   AND current_state IN ('running','retrying') FOR UPDATE""",
                (row["source_id"], run_id),
            ).fetchone()
            if conflict:
                raise LeaseConflict(f"source already leased by run {conflict['run_id']}")
            connection.execute(
                """UPDATE sync_runs SET current_state='running',lease_owner=?,
                   lease_expires_at=?,heartbeat_at=?,started_at=COALESCE(started_at,?)
                   WHERE run_id=?""",
                (owner, now + lease_seconds, now, now, run_id),
            )
            self._state_audit(
                connection, "sync_run", run_id, "worker", current.value,
                SyncRunState.RUNNING.value, f"lease:{owner}:{int(now)}",
                run_id=run_id, source_id=row["source_id"],
            )
        return self.get_sync_run(run_id)

    def claim_task(
        self,
        owner: str,
        *,
        lease_seconds: float = 60,
        task_types: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        now = self.clock()
        with self.transaction() as connection:
            params: list[Any] = [now]
            type_clause = ""
            if task_types:
                type_clause = f" AND task_type IN ({','.join('?' for _ in task_types)})"
                params.extend(task_types)
            row = connection.execute(
                f"""SELECT t.* FROM lifecycle_tasks t
                    JOIN sources s ON s.source_id=t.source_id
                    WHERE t.state IN ('pending','retrying') AND t.available_at<=?
                    {type_clause}
                    AND NOT EXISTS (
                        SELECT 1 FROM lifecycle_tasks active
                        WHERE active.source_id=t.source_id AND active.state='running'
                          AND active.lease_expires_at>?
                    )
                    ORDER BY t.created_at LIMIT 1
                    FOR UPDATE OF t,s SKIP LOCKED""",
                (*params, now),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"""SELECT * FROM lifecycle_tasks t
                        WHERE t.source_id IS NULL
                          AND t.state IN ('pending','retrying') AND t.available_at<=?
                          {type_clause}
                        ORDER BY t.created_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
                    tuple(params),
                ).fetchone()
            if not row:
                return None
            current = TaskState(row["state"])
            TASK_MACHINE.validate(current, TaskState.RUNNING, "worker")
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """UPDATE lifecycle_tasks SET state='running',attempt=?,lease_owner=?,
                   lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE task_id=?""",
                (attempt, owner, now + lease_seconds, now, now, row["task_id"]),
            )
            self._state_audit(
                connection, "lifecycle_task", row["task_id"], "worker",
                current.value, TaskState.RUNNING.value, f"claim:{attempt}",
                run_id=row["run_id"] or "", source_id=row["source_id"] or "",
                task_attempt=attempt,
            )
        return self.get_task(row["task_id"])
