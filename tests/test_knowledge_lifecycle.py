from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge.lifecycle.connectors import (
    ExternalPrincipal,
    LocalDirectoryConnector,
    StaticIdentityConnector,
)
from knowledge.lifecycle.identity import sync_identities
from knowledge.lifecycle.models import (
    DeletionState,
    FailureCategory,
    ReleaseManifest,
    ReleaseState,
    RevisionState,
    SyncRunState,
)
from knowledge.lifecycle.observability import prometheus_text
from knowledge.lifecycle.monitor import HealthWindow, ReleaseGuard
from knowledge.lifecycle.publisher import (
    AtomicReleasePointer,
    DatabaseReleasePointer,
    REQUIRED_VALIDATION_CHECKS,
    ReleasePublisher,
    ReleaseValidationError,
)
from knowledge.lifecycle.release_config import active_release_value, apply_active_release_environment
from knowledge.lifecycle.runtime import LifecycleScheduler
from knowledge.lifecycle.state_machines import InvalidTransition
from knowledge.lifecycle.storage import DeletionOrchestrator, MemoryStorageBackend, Reconciler
from knowledge.lifecycle.store import LeaseConflict, LifecycleStore
from knowledge.lifecycle.sync import SyncEngine
from knowledge.lifecycle.worker import LifecycleWorker, PoisonTaskError
from knowledge.service.query_service import QueryService


class Clock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.store = LifecycleStore(self.root / "lifecycle.sqlite3", clock=self.clock)
        self.store.upsert_source(
            source_id="source-a",
            tenant_id="tenant-a",
            connector_type="local_directory",
            configuration_version="v1",
            configuration={"root": "fixture"},
            credential_reference="vault://source-a",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, content: str) -> Path:
        docs = self.root / "docs"
        docs.mkdir(exist_ok=True)
        path = docs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def sync(self, key: str, *, acl=None):
        return SyncEngine(self.store).run(
            "source-a",
            LocalDirectoryConnector(self.root / "docs", acl_by_path=acl),
            idempotency_key=key,
            owner="worker-a",
            full=True,
        )

    def test_source_rejects_embedded_credentials(self):
        with self.assertRaisesRegex(ValueError, "reference credentials"):
            self.store.upsert_source(
                source_id="bad",
                tenant_id="t",
                connector_type="x",
                configuration_version="v1",
                configuration={"password": "secret"},
            )
        with self.assertRaisesRegex(ValueError, "reference credentials"):
            self.store.upsert_source(
                source_id="nested-bad",
                tenant_id="t",
                connector_type="x",
                configuration_version="v1",
                configuration={"options": [{"client_secret": "secret"}]},
            )
        with self.assertRaisesRegex(ValueError, "approved secret reference"):
            self.store.upsert_source(
                source_id="bad-reference",
                tenant_id="t",
                connector_type="x",
                configuration_version="v1",
                configuration={},
                credential_reference="plaintext-secret",
            )

    def test_database_release_pointer_has_no_node_local_state(self):
        pointer = DatabaseReleasePointer()
        pointer.switch({"release_id": "release-a"})
        self.assertEqual(pointer.read(), {})

    def test_sqlite_scheduler_leadership_is_available_for_local_runtime(self):
        with self.store.scheduler_leadership() as leader:
            self.assertTrue(leader)

    def test_duplicate_sync_and_duplicate_event_create_one_revision_and_task(self):
        self.write("guide.md", "alpha")
        first = self.sync("event-1")
        second = self.sync("event-1")
        third = self.sync("event-2")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(third["unchanged_count"], 1)
        self.assertEqual(len(self.store.fetch_all("SELECT * FROM documents")), 1)
        self.assertEqual(len(self.store.fetch_all("SELECT * FROM document_revisions")), 1)
        self.assertEqual(len(self.store.fetch_all("SELECT * FROM lifecycle_tasks")), 1)

    def test_content_update_creates_revision_and_only_explicit_activation_changes_active(self):
        self.write("guide.md", "alpha")
        self.sync("event-1")
        first = self.store.fetch_one("SELECT * FROM document_revisions")
        assert first
        for state in (RevisionState.PROCESSING, RevisionState.STAGED, RevisionState.VALIDATED, RevisionState.ACTIVE):
            self.store.transition_revision(first["revision_id"], state, actor="worker" if state != RevisionState.ACTIVE else "publisher", idempotency_key=f"first:{state.value}")
        self.write("guide.md", "beta")
        self.clock.advance(1)
        self.sync("event-2")
        revisions = self.store.fetch_all("SELECT * FROM document_revisions ORDER BY created_at")
        self.assertEqual(len(revisions), 2)
        document = self.store.fetch_one("SELECT * FROM documents")
        self.assertEqual(document["active_revision_id"], first["revision_id"])
        self.assertEqual(revisions[-1]["processing_status"], "discovered")

    def test_acl_only_change_skips_parse_and_enqueues_acl_propagation(self):
        self.write("guide.md", "alpha")
        self.sync("event-1")
        revision = self.store.fetch_one("SELECT * FROM document_revisions")
        assert revision
        self.store.bind_revision_projection(
            revision["revision_id"],
            data_revision_id="data-revision-a",
            staging_release_id="release-a",
        )
        for state in (RevisionState.PROCESSING, RevisionState.STAGED, RevisionState.VALIDATED, RevisionState.ACTIVE):
            self.store.transition_revision(revision["revision_id"], state, actor="worker" if state != RevisionState.ACTIVE else "publisher", idempotency_key=f"activate:{state.value}")
        self.clock.advance(1)
        self.sync("event-2", acl={"guide.md": ["group:support"]})
        latest = self.store.fetch_one("SELECT * FROM document_revisions ORDER BY created_at DESC LIMIT 1")
        self.assertEqual(latest["change_kind"], "acl_only")
        self.assertEqual(latest["processing_status"], "staged")
        tasks = self.store.fetch_all("SELECT task_type FROM lifecycle_tasks ORDER BY created_at")
        self.assertEqual([row["task_type"] for row in tasks], ["parse_enrich_stage", "acl_propagation"])

    def test_rename_preserves_document_identity(self):
        old = self.write("old.md", "same")
        self.sync("event-1")
        document = self.store.fetch_one("SELECT * FROM documents")
        old.rename(old.with_name("new.md"))
        self.clock.advance(1)
        self.sync("event-2")
        renamed = self.store.fetch_one("SELECT * FROM documents")
        self.assertEqual(renamed["document_id"], document["document_id"])
        self.assertEqual(renamed["source_path"], "new.md")

    def test_same_source_concurrent_lease_is_rejected(self):
        run1 = self.store.create_sync_run("source-a", idempotency_key="one")
        run2 = self.store.create_sync_run("source-a", idempotency_key="two")
        self.store.acquire_sync_lease(run1["run_id"], "worker-a")
        with self.assertRaises((LeaseConflict, Exception)):
            self.store.acquire_sync_lease(run2["run_id"], "worker-b")

    def test_cursor_advances_only_on_success(self):
        run = self.store.create_sync_run("source-a", idempotency_key="cursor")
        self.store.acquire_sync_lease(run["run_id"], "worker-a")
        self.store.finish_sync(run["run_id"], "worker-a", state=SyncRunState.FAILED, cursor_after="unsafe", counts={})
        self.assertEqual(self.store.get_source("source-a")["sync_cursor"], "")

    def test_worker_lease_recovery_checkpoint_retry_dlq_and_replay(self):
        task = self.store.enqueue_task("step", {"x": 1}, idempotency_key="step-1", source_id="source-a", max_attempts=2)
        claimed = self.store.claim_task("dead-worker", lease_seconds=5)
        self.store.heartbeat_task(task["task_id"], "dead-worker", checkpoint={"offset": 7}, lease_seconds=5)
        self.clock.advance(6)
        self.assertEqual(self.store.recover_expired_tasks(), [task["task_id"]])
        resumed = self.store.claim_task("new-worker", lease_seconds=5)
        self.assertEqual(json.loads(resumed["checkpoint_json"]), {"offset": 7})
        dead = self.store.fail_task(task["task_id"], "new-worker", FailureCategory.TRANSIENT, "milvus unavailable", base_delay=0)
        self.assertEqual(dead["state"], "dead_lettered")
        replayed = self.store.replay_dead_letter(task["task_id"])
        self.assertEqual(replayed["state"], "pending")
        self.assertEqual(replayed["attempt"], 0)

    def test_worker_classifies_poison_failure(self):
        task = self.store.enqueue_task("poison", {}, idempotency_key="poison", max_attempts=4)
        worker = LifecycleWorker(self.store, "worker-a", {"poison": lambda ctx: (_ for _ in ()).throw(PoisonTaskError("bad payload"))})
        worker.run_once()
        self.assertEqual(self.store.get_task(task["task_id"])["state"], "dead_lettered")

    def test_worker_honors_cancellation_before_side_effect(self):
        task = self.store.enqueue_task("cancel-me", {}, idempotency_key="cancel-me")
        self.store.request_task_cancellation(task["task_id"])
        called = []
        worker = LifecycleWorker(
            self.store, "worker-a", {"cancel-me": lambda context: called.append(True)}
        )
        worker.run_once()
        self.assertEqual(called, [])
        self.assertEqual(self.store.get_task(task["task_id"])["state"], "cancelled")

    def test_sync_exception_releases_lease_and_enters_retry(self):
        class BrokenConnector:
            def scan(self, cursor, *, full=False):
                raise ConnectionError("upstream unavailable")

        with self.assertRaises(ConnectionError):
            SyncEngine(self.store).run(
                "source-a",
                BrokenConnector(),
                idempotency_key="broken",
                owner="worker-a",
            )
        run = self.store.fetch_one(
            "SELECT * FROM sync_runs WHERE idempotency_key='broken'"
        )
        self.assertEqual(run["current_state"], "retrying")
        self.assertIsNone(run["lease_owner"])
        self.assertEqual(self.store.metrics_snapshot()["connector_error_total"], 1.0)

    def test_scheduler_enqueues_each_interval_once(self):
        scheduler = LifecycleScheduler(self.store, clock=self.clock)
        first = scheduler.run_once()
        second = scheduler.run_once()
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])

    def test_release_validation_activation_failure_isolation_and_rollback(self):
        pointer = AtomicReleasePointer(self.root / "active-release.json")
        publisher = ReleasePublisher(self.store, pointer)
        base = ReleaseManifest("base", (), "chunks-base", "entities-base", "graph-base", "assets/base")
        candidate = ReleaseManifest("candidate", (), "chunks-candidate", "entities-candidate", "graph-candidate", "assets/candidate")
        self.store.create_release(base)
        for state, actor in ((ReleaseState.STAGING, "builder"), (ReleaseState.VALIDATING, "validator"), (ReleaseState.READY, "validator")):
            self.store.transition_release("base", state, actor=actor, idempotency_key=f"base:{state.value}")
        publisher.activate("base")
        self.store.create_release(candidate, previous_release_id="base")
        self.store.transition_release("candidate", ReleaseState.STAGING, actor="builder", idempotency_key="candidate:staging")
        self.store.transition_release("candidate", ReleaseState.VALIDATING, actor="validator", idempotency_key="candidate:validating")
        with self.assertRaises(ReleaseValidationError):
            publisher.validate("candidate", {"chunks": lambda release: False})
        self.assertEqual(self.store.get_release("base")["current_state"], "active")
        self.store.transition_release("candidate", ReleaseState.BUILDING, actor="operator", idempotency_key="retry")
        self.store.transition_release("candidate", ReleaseState.STAGING, actor="builder", idempotency_key="restage")
        self.store.transition_release("candidate", ReleaseState.VALIDATING, actor="validator", idempotency_key="revalidate")
        publisher.validate(
            "candidate",
            {name: (lambda release: True) for name in REQUIRED_VALIDATION_CHECKS},
        )
        publisher.activate("candidate")
        self.assertEqual(pointer.read()["release_id"], "candidate")
        restored = publisher.rollback("candidate")
        self.assertEqual(restored["release_id"], "base")
        self.assertEqual(pointer.read()["release_id"], "base")

    def test_building_release_collects_staged_revisions_and_then_becomes_immutable(self):
        self.write("guide.md", "alpha")
        self.sync("event-1")
        revision = self.store.fetch_one("SELECT * FROM document_revisions")
        manifest = ReleaseManifest(
            "build-release", (), "chunks-build", "entities-build", "graph-build", "assets/build"
        )
        self.store.create_release(manifest)
        for state in (RevisionState.PROCESSING, RevisionState.STAGED):
            self.store.transition_revision(
                revision["revision_id"], state, actor="worker",
                idempotency_key=f"build:{state.value}",
            )
        self.store.bind_revision_projection(
            revision["revision_id"],
            data_revision_id="data-build",
            staging_release_id="build-release",
        )
        release = self.store.attach_revision_to_release(
            "build-release", revision["revision_id"]
        )
        self.assertEqual(json.loads(release["source_revisions_json"]), [revision["revision_id"]])
        self.store.transition_release(
            "build-release", ReleaseState.STAGING, actor="builder", idempotency_key="stage"
        )
        self.store.transition_release(
            "build-release", ReleaseState.VALIDATING, actor="validator", idempotency_key="validate"
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.attach_revision_to_release("build-release", revision["revision_id"] + "x")

    def test_query_service_reads_active_release_from_control_plane_per_request(self):
        manifest = ReleaseManifest(
            "query-release", (), "chunks-live", "entities-live", "graph-live", "assets/live"
        )
        self.store.create_release(manifest)
        for state, actor in (
            (ReleaseState.STAGING, "builder"),
            (ReleaseState.VALIDATING, "validator"),
            (ReleaseState.READY, "validator"),
            (ReleaseState.ACTIVATING, "publisher"),
            (ReleaseState.ACTIVE, "publisher"),
        ):
            self.store.transition_release(
                "query-release", state, actor=actor,
                idempotency_key=f"query:{state.value}",
            )
        config = QueryService(self.store)._query_config()
        self.assertEqual(config.chunks_collection, "chunks-live")
        self.assertEqual(config.entity_name_collection, "entities-live")
        self.assertEqual(config.kg_graph_version, "graph-live")

    def test_illegal_transition_rejected_and_audited_transitions_have_actor(self):
        manifest = ReleaseManifest("bad", (), "c", "e", "g", "a")
        self.store.create_release(manifest)
        with self.assertRaises(InvalidTransition):
            self.store.transition_release("bad", ReleaseState.ACTIVE, actor="publisher", idempotency_key="skip")
        events = self.store.audit_events("index_release")
        self.assertTrue(all(event["actor"] for event in events))

    def test_identity_snapshot_hashes_external_ids_and_revokes_missing(self):
        result = sync_identities(self.store, "source-a", StaticIdentityConnector([ExternalPrincipal("alice@example.test", "subject:alice")]), idempotency_key="idp-1")
        self.assertEqual(result, {"active": 1, "revoked": 0})
        row = self.store.fetch_one("SELECT * FROM identity_mappings")
        self.assertNotIn("alice@example.test", json.dumps(row))
        result = sync_identities(self.store, "source-a", StaticIdentityConnector([]), idempotency_key="idp-2")
        self.assertEqual(result, {"active": 0, "revoked": 1})

    def test_delete_resumes_after_partial_failure_and_verifies_all_backends(self):
        self.write("guide.md", "alpha")
        self.sync("event-1")
        revision = self.store.fetch_one("SELECT * FROM document_revisions")
        document = self.store.fetch_one("SELECT * FROM documents")
        job = self.store.create_deletion(document["document_id"], revision["revision_id"], run_id="manual", targets=("milvus", "neo4j"))
        milvus = MemoryStorageBackend("milvus", [revision["revision_id"]])
        neo4j = MemoryStorageBackend("neo4j", [revision["revision_id"]])
        neo4j.fail_next = True
        orchestrator = DeletionOrchestrator(self.store, [milvus, neo4j])
        failed = orchestrator.run(job["deletion_id"])
        self.assertEqual(failed["state"], "failed")
        completed = orchestrator.run(job["deletion_id"])
        self.assertEqual(completed["state"], "completed")
        self.assertFalse(milvus.contains_revision(revision["revision_id"]))
        self.assertFalse(neo4j.contains_revision(revision["revision_id"]))

    def test_delete_fails_closed_when_a_backend_is_not_registered(self):
        self.write("guide.md", "alpha")
        self.sync("event-1")
        revision = self.store.fetch_one("SELECT * FROM document_revisions")
        document = self.store.fetch_one("SELECT * FROM documents")
        job = self.store.create_deletion(
            document["document_id"],
            revision["revision_id"],
            run_id="manual",
            targets=("chunks", "unregistered"),
        )
        result = DeletionOrchestrator(
            self.store, [MemoryStorageBackend("chunks", [revision["revision_id"]])]
        ).run(job["deletion_id"])
        self.assertEqual(result["state"], "failed")
        self.assertIn("unregistered", result["error_message"])

    def test_reconciliation_detects_and_repairs_orphan(self):
        backend = MemoryStorageBackend("milvus", ["rev_orphan"])
        findings = Reconciler(self.store, [backend]).run(repair=True)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].repaired)
        self.assertFalse(backend.contains_revision("rev_orphan"))

    def test_source_delete_creates_durable_propagation_task(self):
        path = self.write("delete-me.md", "alpha")
        self.sync("delete-seed")
        path.unlink()
        self.clock.advance(1)
        result = self.sync("delete-detected")
        self.assertEqual(result["deleted_count"], 1)
        task = self.store.fetch_one("SELECT * FROM lifecycle_tasks WHERE task_type='delete_propagate'")
        self.assertIsNotNone(task)

    def test_metrics_surface_required_names(self):
        text = prometheus_text(self.store)
        for name in (
            "source_sync_total",
            "task_queue_depth",
            "dead_letter_total",
            "acl_sync_lag",
            "deletion_propagation_lag",
            "orphan_record_count",
            "active_release_health",
        ):
            self.assertIn(name, text)

    def test_release_guard_requires_multiple_consecutive_signals(self):
        guard = ReleaseGuard()
        one_signal = HealthWindow(100, 1.0, 0.0, 0.8, 0.1, 1000, 0.001)
        decision = guard.evaluate([one_signal, one_signal], now=1000)
        self.assertEqual(decision.action, "pause")
        unhealthy = HealthWindow(100, 0.8, 0.2, 0.8, 0.5, 12000, 0.1, 3)
        decision = guard.evaluate([unhealthy, unhealthy], now=1000)
        self.assertEqual(decision.action, "rollback")
        self.assertGreaterEqual(len(decision.signals), 2)
        self.assertEqual(guard.evaluate([unhealthy, unhealthy], now=1001, manual_hold=True).action, "manual_hold")

    def test_active_release_pointer_overrides_environment_safely(self):
        pointer = self.root / "active.json"
        pointer.write_text(json.dumps({"CHUNKS_COLLECTION": "candidate_chunks"}), encoding="utf-8")
        with patch.dict("os.environ", {"LIFECYCLE_ACTIVE_RELEASE_PATH": str(pointer)}):
            self.assertEqual(active_release_value("CHUNKS_COLLECTION", "fallback"), "candidate_chunks")
        pointer.write_text(json.dumps({"CHUNKS_COLLECTION": "unsafe value"}), encoding="utf-8")
        with patch.dict("os.environ", {"LIFECYCLE_ACTIVE_RELEASE_PATH": str(pointer)}):
            self.assertEqual(active_release_value("CHUNKS_COLLECTION", "fallback"), "fallback")

    def test_active_release_environment_is_validated_and_applied(self):
        pointer = self.root / "active-release-env.json"
        pointer.write_text(
            json.dumps(
                {
                    "CHUNKS_COLLECTION": "candidate_chunks",
                    "ENTITY_NAME_COLLECTION": "candidate_entities",
                    "KG_GRAPH_VERSION": "candidate-graph",
                    "OBJECT_ASSET_NAMESPACE": "candidate-assets",
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(
            "os.environ",
            {"LIFECYCLE_ACTIVE_RELEASE_PATH": str(pointer)},
            clear=False,
        ):
            applied = apply_active_release_environment()
            self.assertEqual(applied["CHUNKS_COLLECTION"], "candidate_chunks")
            self.assertEqual(os.environ["KG_GRAPH_VERSION"], "candidate-graph")


if __name__ == "__main__":
    unittest.main()
