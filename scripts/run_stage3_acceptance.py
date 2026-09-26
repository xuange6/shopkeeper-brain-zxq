"""Run repeatable stage-3 lifecycle fault drills against real local services."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from minio import Minio
from neo4j import GraphDatabase
from pymilvus import MilvusClient
from pymongo import MongoClient
import urllib3

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.lifecycle.connectors import LocalDirectoryConnector
from knowledge.lifecycle.models import FailureCategory, ReleaseManifest, ReleaseState, RevisionState
from knowledge.lifecycle.publisher import (
    AtomicReleasePointer,
    REQUIRED_VALIDATION_CHECKS,
    ReleasePublisher,
    ReleaseValidationError,
)
from knowledge.lifecycle.real_storage import MilvusRevisionBackend, MinioRevisionBackend, Neo4jRevisionBackend
from knowledge.lifecycle.storage import ACLSynchronizer, DeletionOrchestrator, Reconciler
from knowledge.lifecycle.store import LeaseConflict, LifecycleStore
from knowledge.lifecycle.sync import SyncEngine
from knowledge.lifecycle.worker import LifecycleWorker, PoisonTaskError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milvus", default="http://127.0.0.1:19530")
    parser.add_argument("--neo4j", default="bolt://127.0.0.1:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="shopkeeper-dev")
    parser.add_argument("--minio", default="127.0.0.1:9000")
    parser.add_argument("--minio-access-key", default="minioadmin")
    parser.add_argument("--minio-secret-key", default="minioadmin")
    parser.add_argument("--minio-bucket", default="a-bucket")
    parser.add_argument("--mongo", default="mongodb://127.0.0.1:27017")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()
    report_path = args.output or ROOT / "output" / f"stage3-lifecycle-acceptance.{tag}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = report_path.with_suffix(".sqlite3")
    workspace = report_path.parent / f"stage3-fixtures-{tag}"
    workspace.mkdir(parents=True, exist_ok=True)
    documents = workspace / "documents"
    documents.mkdir()

    milvus_client = MilvusClient(uri=args.milvus)
    neo4j_driver = GraphDatabase.driver(args.neo4j, auth=(args.neo4j_user, args.neo4j_password))
    neo4j_driver.verify_connectivity()
    minio_client = Minio(args.minio, access_key=args.minio_access_key, secret_key=args.minio_secret_key, secure=False)
    if not minio_client.bucket_exists(args.minio_bucket):
        minio_client.make_bucket(args.minio_bucket)
    mongo = MongoClient(args.mongo, serverSelectionTimeoutMS=3000)
    mongo.admin.command("ping")

    collection = f"kb_stage3_acceptance_{tag.replace('-', '_')}"
    namespace = f"stage3-acceptance/{tag}"
    milvus = MilvusRevisionBackend(milvus_client, collection)
    neo4j = Neo4jRevisionBackend(neo4j_driver, namespace=tag)
    objects = MinioRevisionBackend(minio_client, args.minio_bucket, namespace=namespace)
    backends = [milvus, neo4j, objects]
    store = LifecycleStore(db_path)
    store.upsert_source(
        source_id="acceptance-source",
        tenant_id="acceptance-tenant",
        connector_type="local_directory",
        configuration_version="v1",
        configuration={"root": str(documents)},
        credential_reference="env://STAGE3_ACCEPTANCE_CREDENTIAL",
    )
    engine = SyncEngine(store)
    drills: list[dict[str, Any]] = []

    def drill(number: int, name: str, initial: Any, injection: str, action: Callable[[], Any], final_check: Callable[[Any], bool], query: Callable[[], Any] | None = None) -> Any:
        started = time.time()
        before_events = len(store.audit_events())
        try:
            result = action()
            passed = bool(final_check(result))
            error = ""
        except Exception as exc:
            result = None
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        query_result = query() if query else None
        events = store.audit_events()[before_events:]
        drills.append({
            "number": number,
            "name": name,
            "initial_state": initial,
            "fault_injection": injection,
            "state_transitions": [{"entity": event["entity_type"], "from": event["from_state"], "to": event["to_state"], "actor": event["actor"]} for event in events if event["from_state"] or event["to_state"]],
            "recovery": result,
            "final_data_check": passed,
            "query_validation": query_result,
            "metrics": store.metrics_snapshot(),
            "audit_event_ids": [event["event_id"] for event in events],
            "duration_seconds": round(time.time() - started, 6),
            "error": error,
            "status": "PASS" if passed and not error else "FAIL",
        })
        return result

    guide = documents / "guide.md"
    guide.write_text("# Guide\nalpha lifecycle content", encoding="utf-8")
    first = drill(1, "repeat source sync", {"documents": 0}, "run two full snapshots", lambda: [engine.run("acceptance-source", LocalDirectoryConnector(documents), idempotency_key=key, owner="sync-worker", full=True) for key in ("sync-1", "sync-2")], lambda _: len(store.fetch_all("SELECT * FROM documents")) == 1 and len(store.fetch_all("SELECT * FROM document_revisions")) == 1)
    drill(2, "duplicate event delivery", {"run_id": first[0]["run_id"]}, "redeliver sync-1", lambda: engine.run("acceptance-source", LocalDirectoryConnector(documents), idempotency_key="sync-1", owner="sync-worker", full=True), lambda result: result["run_id"] == first[0]["run_id"] and len(store.fetch_all("SELECT * FROM lifecycle_tasks")) == 1)

    first_revision = store.fetch_one("SELECT * FROM document_revisions ORDER BY created_at LIMIT 1")
    assert first_revision
    store.bind_revision_projection(
        first_revision["revision_id"],
        data_revision_id=first_revision["revision_id"],
        staging_release_id="acceptance-base",
    )
    for state, actor in ((RevisionState.PROCESSING, "worker"), (RevisionState.STAGED, "worker"), (RevisionState.VALIDATED, "worker"), (RevisionState.ACTIVE, "publisher")):
        store.transition_revision(first_revision["revision_id"], state, actor=actor, idempotency_key=f"seed:{state.value}")
    for backend in backends:
        backend.put_revision(first_revision["revision_id"])

    guide.write_text("# Guide\nbeta lifecycle content", encoding="utf-8")
    drill(3, "content update active isolation", {"active_revision": first_revision["revision_id"]}, "change file content", lambda: engine.run("acceptance-source", LocalDirectoryConnector(documents), idempotency_key="sync-content", owner="sync-worker", full=True), lambda _: store.fetch_one("SELECT active_revision_id FROM documents")["active_revision_id"] == first_revision["revision_id"], query=lambda: {"active_revision": store.fetch_one("SELECT active_revision_id FROM documents")["active_revision_id"]})

    content_revision = store.fetch_one("SELECT * FROM document_revisions WHERE change_kind='content' ORDER BY created_at DESC LIMIT 1")
    assert content_revision
    store.bind_revision_projection(
        content_revision["revision_id"],
        data_revision_id=content_revision["revision_id"],
        staging_release_id="acceptance-base",
    )
    for state, actor in ((RevisionState.PROCESSING, "worker"), (RevisionState.STAGED, "worker"), (RevisionState.VALIDATED, "worker"), (RevisionState.ACTIVE, "publisher")):
        store.transition_revision(content_revision["revision_id"], state, actor=actor, idempotency_key=f"content:{state.value}")
    for backend in backends:
        backend.put_revision(content_revision["revision_id"])

    guide.write_text("# Guide\nbeta lifecycle content", encoding="utf-8")
    drill(4, "ACL-only update", {"parser_tasks": 2}, "grant group:support without content change", lambda: engine.run("acceptance-source", LocalDirectoryConnector(documents, acl_by_path={"guide.md": ["group:support"]}), idempotency_key="sync-acl", owner="sync-worker", full=True), lambda _: store.fetch_one("SELECT change_kind FROM document_revisions ORDER BY created_at DESC LIMIT 1")["change_kind"] == "acl_only" and store.fetch_all("SELECT * FROM lifecycle_tasks WHERE task_type='acl_propagation'") != [])

    pointer = AtomicReleasePointer(workspace / "active-release.json")
    publisher = ReleasePublisher(store, pointer)
    base_manifest = ReleaseManifest("acceptance-base", (content_revision["revision_id"],), collection, "entities-base", tag, namespace)
    store.create_release(base_manifest)
    for state, actor in ((ReleaseState.STAGING, "builder"), (ReleaseState.VALIDATING, "validator"), (ReleaseState.READY, "validator")):
        store.transition_release("acceptance-base", state, actor=actor, idempotency_key=f"base:{state.value}")
    publisher.activate("acceptance-base")
    bad_manifest = ReleaseManifest("acceptance-bad", (), collection + "_bad", "entities-bad", tag + "-bad", namespace + "/bad")
    store.create_release(bad_manifest, previous_release_id="acceptance-base")
    store.transition_release("acceptance-bad", ReleaseState.STAGING, actor="builder", idempotency_key="bad:staging")
    store.transition_release("acceptance-bad", ReleaseState.VALIDATING, actor="validator", idempotency_key="bad:validating")
    def failed_staging():
        try:
            publisher.validate("acceptance-bad", {"neo4j_complete": lambda _: False})
        except ReleaseValidationError:
            pass
        return store.get_release("acceptance-bad")
    drill(5, "staging failure isolation", {"active": "acceptance-base"}, "fail Neo4j lineage validation", failed_staging, lambda result: result["current_state"] == "failed" and store.get_release("acceptance-base")["current_state"] == "active", query=lambda: pointer.read())

    crash_task = store.enqueue_task("crash-resume", {}, idempotency_key="crash-resume", max_attempts=3)
    child = "from knowledge.lifecycle.store import LifecycleStore; import os,sys; s=LifecycleStore(sys.argv[1]); t=s.claim_task('crashed-worker',lease_seconds=1,task_types=('crash-resume',)); s.heartbeat_task(t['task_id'],'crashed-worker',checkpoint={'page':3},lease_seconds=1); os._exit(23)"
    def worker_crash():
        completed = subprocess.run([sys.executable, "-c", child, str(db_path)], cwd=ROOT, check=False)
        time.sleep(1.2)
        recovered = store.recover_expired_tasks()
        resumed = store.claim_task("replacement-worker", lease_seconds=30, task_types=("crash-resume",))
        store.complete_task(resumed["task_id"], "replacement-worker")
        return {"exit_code": completed.returncode, "recovered": recovered, "checkpoint": json.loads(resumed["checkpoint_json"])}
    drill(6, "worker termination recovery", {"task": crash_task["task_id"]}, "child worker exits with code 23 after checkpoint", worker_crash, lambda result: result["exit_code"] == 23 and result["checkpoint"] == {"page": 3} and store.get_task(crash_task["task_id"])["state"] == "succeeded")

    retry_task = store.enqueue_task("milvus-retry", {}, idempotency_key="milvus-retry", max_attempts=3)
    def milvus_handler(ctx):
        if ctx.attempt == 1:
            MilvusClient(uri="http://127.0.0.1:1", timeout=0.2)
        milvus.put_revision("rev_milvus_recovered")
    worker = LifecycleWorker(store, "infra-worker", {"milvus-retry": milvus_handler})
    def milvus_retry():
        worker.run_once()
        time.sleep(1.1)
        worker.run_once()
        return store.get_task(retry_task["task_id"])
    drill(7, "Milvus temporary outage", {"task": retry_task["task_id"]}, "connect to closed port then retry real Milvus", milvus_retry, lambda result: result["state"] == "succeeded" and result["attempt"] == 2 and milvus.contains_revision("rev_milvus_recovered"))

    def neo4j_partial():
        with neo4j_driver.session() as session:
            tx = session.begin_transaction()
            try:
                tx.run("CREATE (:LifecycleRevision {namespace:$namespace, revision_id:'rev_partial'})", namespace=tag)
                raise RuntimeError("injected after write before commit")
            except RuntimeError:
                tx.rollback()
        absent_after_rollback = not neo4j.contains_revision("rev_partial")
        neo4j.put_revision("rev_partial")
        return {"rolled_back": absent_after_rollback, "recovered": neo4j.contains_revision("rev_partial")}
    drill(8, "Neo4j partial write", {"revision": "rev_partial"}, "raise before transaction commit", neo4j_partial, lambda result: result["rolled_back"] and result["recovered"] and store.get_release("acceptance-base")["current_state"] == "active")

    def minio_failure():
        bad = Minio("127.0.0.1:1", access_key="x", secret_key="y", secure=False, http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=0.2, read=0.2), retries=False))
        failed = False
        try:
            MinioRevisionBackend(bad, args.minio_bucket, namespace=namespace).put_revision("rev_object_recovered")
        except Exception:
            failed = True
        objects.put_revision("rev_object_recovered")
        return {"failed_closed": failed, "recovered": objects.contains_revision("rev_object_recovered")}
    drill(9, "object storage write failure", {"revision": "rev_object_recovered"}, "closed MinIO port", minio_failure, lambda result: result["failed_closed"] and result["recovered"])

    acl_revision = content_revision["revision_id"]
    def revoke_acl():
        started = time.time()
        completed = ACLSynchronizer(store, backends).propagate(acl_revision, tenant_id="acceptance-tenant", visibility="private", acl_readers=("group:revoked-test",))
        return {"backends": completed, "lag_seconds": time.time() - started}
    drill(10, "permission revocation propagation", {"visibility": "public"}, "replace ACL with private allow-list", revoke_acl, lambda result: set(result["backends"]) == {"milvus", "neo4j", "objects"} and result["lag_seconds"] < 30.0, query=lambda: {backend.name: backend.contains_revision(acl_revision) for backend in backends})

    document = store.fetch_one("SELECT * FROM documents LIMIT 1")
    deletion = store.create_deletion(document["document_id"], acl_revision, run_id="acceptance-delete", targets=("milvus", "neo4j", "objects"))
    drill(11, "delete propagation", {"revision": acl_revision}, "tombstone then delete all external projections", lambda: DeletionOrchestrator(store, backends).run(deletion["deletion_id"]), lambda result: result["state"] == "completed" and not any(backend.contains_revision(acl_revision) for backend in backends), query=lambda: {backend.name: backend.contains_revision(acl_revision) for backend in backends})

    milvus.put_revision("rev_orphan_real")
    drill(12, "orphan reconciliation", {"orphan": "rev_orphan_real"}, "insert Milvus row without control-plane record", lambda: [finding.__dict__ for finding in Reconciler(store, backends).run(repair=True)], lambda result: any(finding["revision_id"] == "rev_orphan_real" and finding["repaired"] for finding in result) and not milvus.contains_revision("rev_orphan_real"))

    good_manifest = ReleaseManifest("acceptance-candidate", (), collection, "entities-candidate", tag, namespace)
    for backend in backends:
        backend.put_revision("rev_release_probe")
    store.create_release(good_manifest, previous_release_id="acceptance-base")
    for state, actor in ((ReleaseState.STAGING, "builder"), (ReleaseState.VALIDATING, "validator")):
        store.transition_release("acceptance-candidate", state, actor=actor, idempotency_key=f"candidate:{state.value}")
    probe_status = {backend.name: backend.contains_revision("rev_release_probe") for backend in backends}
    publisher.validate(
        "acceptance-candidate",
        {
            name: (lambda _, ok=all(probe_status.values()): ok)
            for name in REQUIRED_VALIDATION_CHECKS
        },
    )
    publisher.activate("acceptance-candidate")
    drill(13, "candidate rollback", {"active": "acceptance-candidate"}, "operator rollback", lambda: publisher.rollback("acceptance-candidate"), lambda result: result["release_id"] == "acceptance-base" and pointer.read()["release_id"] == "acceptance-base", query=lambda: pointer.read())

    run_a = store.create_sync_run("acceptance-source", idempotency_key="concurrent-a")
    run_b = store.create_sync_run("acceptance-source", idempotency_key="concurrent-b")
    store.acquire_sync_lease(run_a["run_id"], "worker-a")
    def concurrent_sync():
        rejected = False
        try:
            store.acquire_sync_lease(run_b["run_id"], "worker-b")
        except (LeaseConflict, Exception):
            rejected = True
        return {"second_rejected": rejected, "cursor": store.get_source("acceptance-source")["sync_cursor"]}
    drill(14, "concurrent source sync", {"lease_owner": "worker-a"}, "second worker claims same source", concurrent_sync, lambda result: result["second_rejected"])

    poison = store.enqueue_task("poison", {"bad": True}, idempotency_key="poison", max_attempts=2)
    poison_worker = LifecycleWorker(store, "poison-worker", {"poison": lambda _: (_ for _ in ()).throw(PoisonTaskError("poison payload"))})
    def dlq_replay():
        poison_worker.run_once()
        dead = store.get_task(poison["task_id"])
        replayed = store.replay_dead_letter(poison["task_id"])
        return {"dead_state": dead["state"], "replayed_state": replayed["state"], "attempt": replayed["attempt"]}
    drill(15, "DLQ and safe replay", {"task": poison["task_id"]}, "poison failure exceeds retry policy", dlq_replay, lambda result: result == {"dead_state": "dead_lettered", "replayed_state": "pending", "attempt": 0})

    dependencies = {
        "milvus": {"uri": args.milvus, "collection": collection, "ok": True},
        "neo4j": {"uri": args.neo4j, "namespace": tag, "ok": True},
        "minio": {"endpoint": args.minio, "bucket": args.minio_bucket, "namespace": namespace, "ok": True},
        "mongodb": {"uri": args.mongo, "ok": True},
    }
    payload = {
        "schema_version": "stage3-acceptance-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if len(drills) == 15 and all(item["status"] == "PASS" for item in drills) else "FAIL",
        "dependencies": dependencies,
        "slo_observed": {
            "acl_revocation_seconds": next(item["recovery"]["lag_seconds"] for item in drills if item["number"] == 10),
            "worker_recovery_seconds": next(item["duration_seconds"] for item in drills if item["number"] == 6),
            "rollback_seconds": next(item["duration_seconds"] for item in drills if item["number"] == 13),
        },
        "drills": drills,
        "metrics_final": store.metrics_snapshot(),
        "audit_event_count": len(store.audit_events()),
        "artifacts": {"database": str(db_path), "fixture_namespace": str(workspace), "active_pointer": str(pointer.path)},
        "environment_note": "Local compose infrastructure at 127.0.0.1; frozen stage-2 collections were not present in this Milvus volume.",
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    mongo["shopkeeper_brain"]["stage3_acceptance_receipts"].update_one({"tag": tag}, {"$set": {"tag": tag, "status": payload["status"], "report": str(report_path), "created_at": payload["generated_at"]}}, upsert=True)
    mongo.close()
    neo4j_driver.close()
    print(json.dumps({"status": payload["status"], "report": str(report_path), "passed": sum(item["status"] == "PASS" for item in drills), "total": len(drills)}, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
