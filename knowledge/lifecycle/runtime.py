"""Production scheduler and worker entry points for the lifecycle control plane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
from typing import Any, Callable, Mapping

from knowledge.lifecycle.connectors import LocalDirectoryConnector, SourceConnector
from knowledge.lifecycle.factory import create_lifecycle_store
from knowledge.lifecycle.handlers import build_handlers
from knowledge.lifecycle.models import ReleaseState, RevisionState
from knowledge.lifecycle.production_storage import (
    AbsentCacheBackend,
    ControlPlaneBackend,
    MilvusChunkBackend,
    MilvusEntityBackend,
    MinioDocumentBackend,
    Neo4jDocumentBackend,
    ReleaseReferenceBackend,
)
from knowledge.lifecycle.publisher import (
    AtomicReleasePointer,
    DatabaseReleasePointer,
    ReleasePublisher,
    ReleaseValidationError,
)
from knowledge.lifecycle.release_validation import build_release_checks
from knowledge.lifecycle.storage import ACLSynchronizer, DeletionOrchestrator, Reconciler
from knowledge.lifecycle.store import LifecycleStore
from knowledge.lifecycle.sync import SyncEngine
from knowledge.lifecycle.worker import (
    LifecycleWorker,
    PermanentTaskError,
    TaskCancelledError,
    TaskContext,
)


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.05, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _runtime_stop_event() -> threading.Event:
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return stop


def _touch_runtime_heartbeat() -> None:
    path = os.getenv("LIFECYCLE_RUNTIME_HEARTBEAT_PATH", "").strip()
    if not path:
        return
    heartbeat = Path(path)
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.touch()


def connector_for_source(source: Mapping[str, Any]) -> SourceConnector:
    configuration = json.loads(str(source["configuration_json"]))
    connector_type = str(source["connector_type"])
    if connector_type == "local_directory":
        root = str(configuration.get("root") or "").strip()
        if not root:
            raise PermanentTaskError("local_directory source requires configuration.root")
        return LocalDirectoryConnector(
            root,
            acl_by_path=configuration.get("acl_by_path") or {},
        )
    raise PermanentTaskError(f"unsupported connector type: {connector_type}")


class LifecycleScheduler:
    def __init__(self, store: LifecycleStore, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    def run_once(self, *, limit: int = 100) -> list[str]:
        scheduled: list[str] = []
        now = self.clock()
        for source in self.store.list_due_sources(limit=limit):
            policy = json.loads(str(source["sync_policy_json"]))
            try:
                interval = max(5.0, float(policy.get("interval_seconds", 300)))
            except (TypeError, ValueError):
                interval = 300.0
            bucket = int(now // interval)
            event_key = f"scheduled:{source['source_id']}:{bucket}"
            task = self.store.enqueue_task(
                "source_sync",
                {
                    "source_id": source["source_id"],
                    "sync_idempotency_key": event_key,
                    "trigger_type": "scheduled",
                    "full": bool(policy.get("full_scan", False)),
                },
                idempotency_key=event_key,
                source_id=source["source_id"],
            )
            self.store.mark_source_scheduled(
                source["source_id"], next_sync_at=now + interval
            )
            scheduled.append(task["task_id"])
        reconcile_interval = max(
            60.0,
            _positive_float("LIFECYCLE_RECONCILE_INTERVAL_SECONDS", 900),
        )
        reconcile_bucket = int(now // reconcile_interval)
        reconcile_key = f"reconcile:{reconcile_bucket}"
        existing_reconcile = self.store.fetch_one(
            "SELECT task_id FROM lifecycle_tasks WHERE idempotency_key=?",
            (reconcile_key,),
        )
        reconcile = self.store.enqueue_task(
            "reconcile",
            {"repair": False},
            idempotency_key=reconcile_key,
        )
        if existing_reconcile is None:
            scheduled.append(reconcile["task_id"])
        return scheduled


def build_source_sync_handler(store: LifecycleStore, owner: str):
    def source_sync(context: TaskContext) -> None:
        if context.cancelled():
            return
        source_id = str(context.payload["source_id"])
        source = store.get_source(source_id)
        connector = connector_for_source(source)
        try:
            run = SyncEngine(store).run(
                source_id,
                connector,
                idempotency_key=str(context.payload["sync_idempotency_key"]),
                owner=owner,
                trigger_type=str(context.payload.get("trigger_type") or "scheduled"),
                full=bool(context.payload.get("full")),
            )
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            raise PermanentTaskError(str(exc)) from exc
        context.heartbeat({"sync_run_id": run["run_id"], "state": run["current_state"]})

    return source_sync


def build_parse_enrich_handler(store: LifecycleStore):
    def parse_enrich(context: TaskContext) -> None:
        if context.cancelled():
            return
        revision_id = str(context.payload["revision_id"])
        task = store.get_task(context.task_id)
        source = store.get_source(str(task["source_id"]))
        configuration = json.loads(str(source["configuration_json"]))
        root = Path(str(configuration.get("root") or "")).resolve()
        source_file = (root / str(context.payload["source_path"])).resolve()
        if not source_file.is_relative_to(root) or not source_file.is_file():
            raise PermanentTaskError("source file is outside connector root or missing")

        release_id = str(
            configuration.get("staging_release_id")
            or os.getenv("LIFECYCLE_STAGING_RELEASE_ID", "")
        ).strip()
        if not release_id:
            raise PermanentTaskError("source requires a staging_release_id")
        release = store.get_release(release_id)
        if release["current_state"] not in {
            ReleaseState.BUILDING.value,
            ReleaseState.STAGING.value,
        }:
            raise PermanentTaskError("parse target release is not building or staging")

        from knowledge.processor.import_process.config import ImportConfig
        from knowledge.processor.import_process.main_graph import run_import_graph

        import_config = ImportConfig.from_env()
        import_config.chunks_collection = str(release["chunk_collection"])
        import_config.entity_name_collection = str(release["entity_collection"])
        import_config.kg_graph_version = str(release["graph_version"])
        import_config.object_asset_namespace = str(release["object_asset_namespace"])
        staging_root = Path(
            os.getenv("LIFECYCLE_STAGING_ROOT", "knowledge/data/lifecycle-staging")
        ).resolve()
        task_dir = staging_root / release_id / revision_id
        task_dir.mkdir(parents=True, exist_ok=True)

        revision = store.get_revision(revision_id)
        document = store.fetch_one(
            "SELECT tenant_id,visibility,acl_json FROM documents WHERE document_id=?",
            (revision["document_id"],),
        )
        if document is None:
            raise PermanentTaskError("revision document is missing")
        if revision["processing_status"] == RevisionState.DISCOVERED.value:
            store.transition_revision(
                revision_id,
                RevisionState.PROCESSING,
                actor="worker",
                idempotency_key=f"parse-start:{context.task_id}",
            )
        context.heartbeat({"phase": "parse", "release_id": release_id})
        try:
            final_state = run_import_graph(
                import_file_path=str(source_file),
                file_dir=str(task_dir),
                task_id=context.task_id,
                logical_document_key=str(context.payload["document_id"]),
                previous_ir_path=str(context.checkpoint.get("ir_path") or ""),
                config=import_config,
                tenant_id=str(document["tenant_id"]),
                visibility=str(document["visibility"]),
                acl_readers=json.loads(str(document["acl_json"])),
                cancelled=context.cancelled,
            )
            if not isinstance(final_state, dict) or not final_state.get("revision_id"):
                raise RuntimeError("import graph returned no durable revision identity")
            ir_path = str(final_state.get("ir_path") or "")
            store.bind_revision_projection(
                revision_id,
                data_revision_id=str(final_state["revision_id"]),
                staging_release_id=release_id,
            )
            context.heartbeat(
                {
                    "phase": "staged",
                    "release_id": release_id,
                    "data_revision_id": final_state["revision_id"],
                    "ir_path": ir_path,
                }
            )
            store.transition_revision(
                revision_id,
                RevisionState.STAGED,
                actor="worker",
                idempotency_key=f"parse-staged:{context.task_id}",
            )
            store.attach_revision_to_release(release_id, revision_id)
        except InterruptedError as exc:
            current = store.get_revision(revision_id)
            if current["processing_status"] == RevisionState.PROCESSING.value:
                store.transition_revision(
                    revision_id,
                    RevisionState.FAILED,
                    actor="worker",
                    idempotency_key=f"parse-cancelled:{context.task_id}",
                )
            raise TaskCancelledError(str(exc)) from exc
        except Exception:
            current = store.get_revision(revision_id)
            if current["processing_status"] == RevisionState.PROCESSING.value:
                store.transition_revision(
                    revision_id,
                    RevisionState.FAILED,
                    actor="worker",
                    idempotency_key=f"parse-failed:{context.task_id}:{context.attempt}",
                )
            raise

    return parse_enrich


def build_runtime_handlers(store: LifecycleStore, owner: str):
    from knowledge.utils.milvus_utils import get_milvus_client
    from knowledge.utils.minio_util import get_minio_client
    from knowledge.utils.neo4j_util import get_neo4j_driver

    milvus = get_milvus_client()
    neo4j = get_neo4j_driver()
    minio = get_minio_client()
    bucket = os.getenv("MINIO_BUCKET_NAME", "").strip()
    if minio is None or not bucket:
        raise RuntimeError("production lifecycle worker requires a reachable MinIO bucket")

    chunks = MilvusChunkBackend(store, milvus)
    entities = MilvusEntityBackend(store, milvus, chunks)
    graph = Neo4jDocumentBackend(
        store,
        neo4j,
        chunks,
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )
    objects = MinioDocumentBackend(store, minio, bucket)
    search_cache = AbsentCacheBackend("search_cache")
    permission_cache = AbsentCacheBackend("permission_cache")
    release_references = ReleaseReferenceBackend(store)
    metadata = ControlPlaneBackend(store, "metadata")
    revision = ControlPlaneBackend(store, "revision")
    pointer_mode = os.getenv("LIFECYCLE_RELEASE_POINTER_MODE", "file").strip().lower()
    if pointer_mode == "database":
        pointer = DatabaseReleasePointer()
    elif pointer_mode == "file":
        pointer = AtomicReleasePointer(
            os.getenv(
                "LIFECYCLE_ACTIVE_RELEASE_PATH",
                "knowledge/data/active-release.json",
            )
        )
    else:
        raise RuntimeError(f"unsupported LIFECYCLE_RELEASE_POINTER_MODE: {pointer_mode}")
    publisher = ReleasePublisher(store, pointer)

    def validate_release(context: TaskContext) -> None:
        release_id = str(context.payload["release_id"])
        release = store.get_release(release_id)
        if release["current_state"] == ReleaseState.STAGING.value:
            store.transition_release(
                release_id,
                ReleaseState.VALIDATING,
                actor="validator",
                idempotency_key=f"validation-start:{context.task_id}",
            )
        try:
            results = publisher.validate(
                release_id,
                build_release_checks(
                    store,
                    milvus=milvus,
                    neo4j=neo4j,
                    minio=minio,
                    minio_bucket=bucket,
                    neo4j_database=os.getenv("NEO4J_DATABASE", "neo4j"),
                ),
            )
        except ReleaseValidationError as exc:
            raise PermanentTaskError(str(exc)) from exc
        context.heartbeat({"release_id": release_id, "validation": results})

    def activate_release(context: TaskContext) -> None:
        release = publisher.activate(str(context.payload["release_id"]))
        context.heartbeat(
            {"release_id": release["release_id"], "state": release["current_state"]}
        )

    def rollback_release(context: TaskContext) -> None:
        release = publisher.rollback(str(context.payload["release_id"]))
        context.heartbeat(
            {"release_id": release["release_id"], "state": release["current_state"]}
        )

    def reconcile(context: TaskContext) -> None:
        findings = Reconciler(store, (chunks, entities, graph)).run(
            repair=bool(context.payload.get("repair", False))
        )
        context.heartbeat(
            {
                "finding_count": len(findings),
                "repaired_count": sum(1 for finding in findings if finding.repaired),
            }
        )

    handlers = dict(
        build_handlers(
            store,
            acl=ACLSynchronizer(
                store,
                (permission_cache, search_cache, entities, graph, chunks, objects),
            ),
            deletion=DeletionOrchestrator(
                store,
                (
                    entities,
                    graph,
                    objects,
                    chunks,
                    search_cache,
                    permission_cache,
                    release_references,
                    metadata,
                    revision,
                ),
            ),
            parse_enrich_stage=build_parse_enrich_handler(store),
        )
    )
    handlers["source_sync"] = build_source_sync_handler(store, owner)
    handlers["release_validate"] = validate_release
    handlers["release_activate"] = activate_release
    handlers["release_rollback"] = rollback_release
    handlers["reconcile"] = reconcile
    return handlers


def run_scheduler(*, once: bool = False) -> None:
    store = create_lifecycle_store()
    scheduler = LifecycleScheduler(store)
    poll = _positive_float("LIFECYCLE_SCHEDULER_POLL_SECONDS", 5)
    stop = _runtime_stop_event()
    while not stop.is_set():
        with store.scheduler_leadership() as leader:
            if leader:
                scheduler.run_once()
        _touch_runtime_heartbeat()
        if once:
            return
        stop.wait(poll)


def run_migrate() -> None:
    create_lifecycle_store(migrate=True)


def run_worker(*, once: bool = False) -> None:
    store = create_lifecycle_store()
    owner = os.getenv("LIFECYCLE_WORKER_ID", "").strip() or (
        f"{socket.gethostname()}:{os.getpid()}"
    )
    lease = _positive_float("LIFECYCLE_WORKER_LEASE_SECONDS", 60)
    worker = LifecycleWorker(
        store, owner, build_runtime_handlers(store, owner), lease_seconds=lease
    )
    poll = _positive_float("LIFECYCLE_WORKER_POLL_SECONDS", 1)
    store.recover_expired_sync_runs()
    worker.recover()
    stop = _runtime_stop_event()
    while not stop.is_set():
        task_id = worker.run_once()
        _touch_runtime_heartbeat()
        if once:
            return
        if task_id is None:
            stop.wait(poll)


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge lifecycle runtime")
    parser.add_argument("mode", choices=("scheduler", "worker", "migrate"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.mode == "migrate":
        run_migrate()
    elif args.mode == "scheduler":
        run_scheduler(once=args.once)
    else:
        run_worker(once=args.once)


if __name__ == "__main__":
    main()
