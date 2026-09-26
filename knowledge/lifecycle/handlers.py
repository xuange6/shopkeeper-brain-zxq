"""Standard durable-task handlers for lifecycle side effects."""

from __future__ import annotations

import json
from typing import Callable, Mapping

from knowledge.lifecycle.models import RevisionState
from knowledge.lifecycle.storage import ACLSynchronizer, DeletionOrchestrator
from knowledge.lifecycle.store import LifecycleStore
from knowledge.lifecycle.worker import PermanentTaskError, TaskContext


def build_handlers(
    store: LifecycleStore,
    *,
    acl: ACLSynchronizer,
    deletion: DeletionOrchestrator,
    parse_enrich_stage: Callable[[TaskContext], None],
) -> Mapping[str, Callable[[TaskContext], None]]:
    """Compose production handlers while keeping the parser implementation injectable."""

    def acl_propagation(context: TaskContext) -> None:
        revision_id = str(context.payload["revision_id"])
        row = store.fetch_one(
            """SELECT r.processing_status,d.tenant_id,d.visibility,d.acl_json
               FROM document_revisions r JOIN documents d ON d.document_id=r.document_id
               WHERE r.revision_id=?""",
            (revision_id,),
        )
        if not row:
            raise PermanentTaskError(f"unknown revision: {revision_id}")
        completed = acl.propagate(
            revision_id,
            tenant_id=row["tenant_id"],
            visibility=row["visibility"],
            acl_readers=json.loads(row["acl_json"]),
        )
        context.heartbeat({"completed_backends": completed})
        if row["processing_status"] == RevisionState.STAGED.value:
            store.transition_revision(
                revision_id,
                RevisionState.VALIDATED,
                actor="worker",
                idempotency_key=f"acl-validated:{context.task_id}",
            )

    def delete_propagate(context: TaskContext) -> None:
        deletion_id = str(context.payload["deletion_id"])
        result = deletion.run(deletion_id)
        context.heartbeat({"deletion_state": result["state"]})
        if result["state"] != "completed":
            raise RuntimeError(result.get("error_message") or "deletion propagation incomplete")

    return {
        "parse_enrich_stage": parse_enrich_stage,
        "acl_propagation": acl_propagation,
        "delete_propagate": delete_propagate,
    }
