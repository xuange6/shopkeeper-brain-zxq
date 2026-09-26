"""Exercise Stage 3 migration and concurrent claims against real PostgreSQL."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from knowledge.lifecycle.models import SourceStatus
from knowledge.lifecycle.postgres_store import PostgresLifecycleStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dsn = os.getenv(
        "LIFECYCLE_DATABASE_URL",
        "postgresql://shopkeeper:shopkeeper-dev@localhost:5432/shopkeeper",
    )
    store = PostgresLifecycleStore(dsn)
    prefix = f"pg-accept-{uuid4().hex[:10]}"
    task_type = f"acceptance-{prefix}"
    source_ids = [f"{prefix}-a", f"{prefix}-b"]
    for source_id in source_ids:
        store.upsert_source(
            source_id=source_id,
            tenant_id="acceptance",
            connector_type="local_directory",
            configuration_version="v1",
            configuration={"root": "fixtures"},
            credential_reference="env://ACCEPTANCE_SOURCE_TOKEN",
            sync_policy={"interval_seconds": 300},
            status=SourceStatus.ACTIVE,
        )

    tasks = [
        store.enqueue_task(
            task_type,
            {"sequence": sequence},
            idempotency_key=f"{prefix}:{sequence}",
            source_id=source_id,
        )
        for sequence, source_id in enumerate(
            (source_ids[0], source_ids[0], source_ids[1]), start=1
        )
    ]

    barrier = threading.Barrier(2)

    def claim(owner: str):
        local = PostgresLifecycleStore(dsn)
        barrier.wait()
        return local.claim_task(owner, task_types=(task_type,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, (f"{prefix}-worker-1", f"{prefix}-worker-2")))

    active = [row for row in claimed if row is not None]
    claimed_sources = [str(row["source_id"]) for row in active]
    if len(claimed_sources) != len(set(claimed_sources)):
        raise AssertionError("two workers claimed tasks for the same source")
    for row in active:
        store.complete_task(row["task_id"], row["lease_owner"])

    first_scheduler = PostgresLifecycleStore(dsn)
    second_scheduler = PostgresLifecycleStore(dsn)
    with first_scheduler.scheduler_leadership() as first_leader:
        with second_scheduler.scheduler_leadership() as second_leader:
            if not first_leader or second_leader:
                raise AssertionError("PostgreSQL scheduler advisory lock is not exclusive")
    with second_scheduler.scheduler_leadership() as failover_leader:
        if not failover_leader:
            raise AssertionError("scheduler leadership did not fail over after release")

    result = {
        "status": "passed",
        "database": "postgresql",
        "schema_versions": [
            row["version"]
            for row in store.fetch_all(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ],
        "created_tasks": len(tasks),
        "concurrent_claims": len(active),
        "claimed_sources": claimed_sources,
        "same_source_exclusion": True,
        "scheduler_single_leader": True,
        "scheduler_failover": True,
        "metrics_available": "task_queue_depth" in store.metrics_snapshot(),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
