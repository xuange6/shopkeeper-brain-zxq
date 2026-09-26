"""Idempotent source synchronization and change classification."""

from __future__ import annotations

from typing import Any

from knowledge.lifecycle.connectors import SourceConnector
from knowledge.lifecycle.models import ChangeKind, FailureCategory, RevisionState, SyncRunState
from knowledge.lifecycle.store import LifecycleStore


DEFAULT_DELETION_TARGETS = (
    "milvus", "neo4j", "objects", "chunks", "search_cache",
    "permission_cache", "release_references", "metadata", "revision",
)


class SyncEngine:
    def __init__(self, store: LifecycleStore, *, parser_fingerprint: str = "document-ir:1.1.0"):
        self.store = store
        self.parser_fingerprint = parser_fingerprint

    def run(
        self,
        source_id: str,
        connector: SourceConnector,
        *,
        idempotency_key: str,
        owner: str,
        trigger_type: str = "manual",
        full: bool = False,
    ) -> dict[str, Any]:
        run = self.store.create_sync_run(
            source_id, idempotency_key=idempotency_key, trigger_type=trigger_type
        )
        if run["current_state"] in {
            SyncRunState.SUCCEEDED.value,
            SyncRunState.PARTIALLY_FAILED.value,
        }:
            return run
        self.store.acquire_sync_lease(run["run_id"], owner)
        try:
            return self._run_leased(run, source_id, connector, owner=owner, full=full)
        except Exception as exc:
            permanent = isinstance(exc, (FileNotFoundError, PermissionError, ValueError))
            self.store.record_metric(
                "connector_error_total", 1,
                labels={"source_id": source_id, "error": type(exc).__name__},
            )
            self.store.fail_sync_attempt(
                run["run_id"], owner,
                category=(
                    FailureCategory.PERMANENT
                    if permanent
                    else FailureCategory.TRANSIENT
                ),
                message=str(exc),
                retryable=not permanent,
            )
            raise

    def _run_leased(
        self,
        run: dict[str, Any],
        source_id: str,
        connector: SourceConnector,
        *,
        owner: str,
        full: bool,
    ) -> dict[str, Any]:
        source = self.store.get_source(source_id)
        items, next_cursor = connector.scan(source["sync_cursor"], full=full)
        current = self.store.list_source_documents(source_id)
        by_external = {
            row["source_item_id"]: row for row in current if not row["tombstoned"]
        }
        unseen_ids = set(by_external)
        counts = {
            "discovered": len(items), "created": 0, "updated": 0,
            "deleted": 0, "unchanged": 0,
        }

        for position, item in enumerate(items, start=1):
            existing = by_external.get(item.external_id)
            renamed_from: dict[str, Any] | None = None
            if existing is None:
                candidates = [
                    by_external[key] for key in unseen_ids
                    if by_external[key].get("content_hash") == item.content_hash
                ]
                if len(candidates) == 1:
                    renamed_from = candidates[0]
                    existing = renamed_from
                    unseen_ids.discard(existing["source_item_id"])
                    self.store.rename_document(
                        existing["document_id"], new_source_item_id=item.external_id,
                        new_path=item.path, run_id=run["run_id"],
                    )
            else:
                unseen_ids.discard(item.external_id)

            change = self._classify(existing, item, renamed=renamed_from is not None)
            if change == ChangeKind.UNCHANGED:
                counts["unchanged"] += 1
                self.store.heartbeat_sync(
                    run["run_id"], owner,
                    checkpoint={"position": position, "external_id": item.external_id},
                )
                continue

            previous_revision = existing.get("active_revision_id", "") if existing else ""
            revision, inserted = self.store.upsert_revision(
                source_id=source_id,
                tenant_id=source["tenant_id"],
                source_item_id=item.external_id,
                source_path=item.path,
                content_hash=item.content_hash,
                metadata_hash=item.metadata_hash,
                acl_hash=item.acl_hash,
                parser_fingerprint=self.parser_fingerprint,
                visibility=item.visibility,
                acl_readers=item.acl_readers,
                source_modified_at=item.modified_at,
                change_kind=change,
                run_id=run["run_id"],
                previous_revision_id=previous_revision,
                document_id_override=existing["document_id"] if existing else "",
            )
            counts["created" if change == ChangeKind.CREATED else "updated"] += 1
            if inserted:
                if change == ChangeKind.ACL_ONLY:
                    previous = (
                        self.store.get_revision(previous_revision)
                        if previous_revision
                        else {}
                    )
                    data_revision_id = str(previous.get("data_revision_id") or "")
                    data_release_id = str(previous.get("staging_release_id") or "")
                    if data_revision_id and data_release_id:
                        self.store.bind_revision_projection(
                            revision["revision_id"],
                            data_revision_id=data_revision_id,
                            staging_release_id=data_release_id,
                        )
                        self.store.transition_revision(
                            revision["revision_id"], RevisionState.STAGED, actor="sync",
                            idempotency_key=f"acl-stage:{run['run_id']}",
                        )
                        self.store.enqueue_task(
                            "acl_propagation",
                            {"document_id": revision["document_id"], "revision_id": revision["revision_id"]},
                            idempotency_key=f"acl:{revision['revision_id']}",
                            source_id=source_id, run_id=run["run_id"],
                        )
                    else:
                        # Legacy revisions may not yet have a durable projection
                        # binding. Rebuild them instead of claiming that an ACL
                        # update reached storage that cannot be identified.
                        self.store.enqueue_task(
                            "parse_enrich_stage",
                            {
                                "document_id": revision["document_id"],
                                "revision_id": revision["revision_id"],
                                "source_path": item.path,
                                "change_kind": change.value,
                            },
                            idempotency_key=f"parse:{revision['revision_id']}",
                            source_id=source_id, run_id=run["run_id"],
                        )
                else:
                    self.store.enqueue_task(
                        "parse_enrich_stage",
                        {
                            "document_id": revision["document_id"],
                            "revision_id": revision["revision_id"],
                            "source_path": item.path,
                            "change_kind": change.value,
                        },
                        idempotency_key=f"parse:{revision['revision_id']}",
                        source_id=source_id, run_id=run["run_id"],
                    )
            self.store.heartbeat_sync(
                run["run_id"], owner,
                checkpoint={"position": position, "external_id": item.external_id},
            )

        for external_id in sorted(unseen_ids):
            document = by_external[external_id]
            deletion = self.store.create_deletion(
                document["document_id"], document.get("active_revision_id"),
                run_id=run["run_id"], targets=DEFAULT_DELETION_TARGETS,
            )
            self.store.enqueue_task(
                "delete_propagate", {"deletion_id": deletion["deletion_id"]},
                idempotency_key=f"delete:{deletion['deletion_id']}",
                source_id=source_id, run_id=run["run_id"],
            )
            counts["deleted"] += 1

        return self.store.finish_sync(
            run["run_id"], owner, state=SyncRunState.SUCCEEDED,
            cursor_after=next_cursor, counts=counts,
        )

    @staticmethod
    def _classify(existing: dict[str, Any] | None, item: Any, *, renamed: bool) -> ChangeKind:
        if existing is None:
            return ChangeKind.CREATED
        if existing.get("content_hash") != item.content_hash:
            return ChangeKind.CONTENT
        if existing.get("acl_hash") != item.acl_hash:
            return ChangeKind.ACL_ONLY
        if renamed:
            return ChangeKind.RENAMED
        if existing.get("metadata_hash") != item.metadata_hash:
            return ChangeKind.METADATA
        return ChangeKind.UNCHANGED
