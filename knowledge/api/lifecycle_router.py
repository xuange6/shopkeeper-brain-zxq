"""Authenticated operator API for the knowledge lifecycle control plane."""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Mapping

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from knowledge.core.deps import get_lifecycle_store
from knowledge.lifecycle.models import ReleaseManifest, ReleaseState, SourceStatus
from knowledge.lifecycle.store import LifecycleStore


router = APIRouter(prefix="/api/lifecycle/admin", tags=["knowledge-lifecycle"])


def require_lifecycle_admin(
    supplied: str | None = Header(default=None, alias="X-Lifecycle-Admin-Token"),
) -> None:
    expected = os.getenv("LIFECYCLE_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lifecycle administration is not configured",
        )
    if supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid lifecycle administrator credential",
        )


Admin = Depends(require_lifecycle_admin)


class SourceUpsertRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    connector_type: str = Field(min_length=1, max_length=64)
    configuration_version: str = Field(min_length=1, max_length=128)
    configuration: dict[str, Any]
    credential_reference: str = ""
    sync_policy: dict[str, Any] = Field(default_factory=dict)
    permission_sync_policy: dict[str, Any] = Field(default_factory=dict)
    status: SourceStatus = SourceStatus.ACTIVE


class SyncRequest(BaseModel):
    full: bool = False
    idempotency_key: str | None = Field(default=None, max_length=256)


class ReleaseCreateRequest(BaseModel):
    release_id: str = Field(min_length=1, max_length=128)
    source_revisions: list[str]
    chunk_collection: str = Field(min_length=1, max_length=255)
    entity_collection: str = Field(min_length=1, max_length=255)
    graph_version: str = Field(min_length=1, max_length=255)
    object_asset_namespace: str = Field(min_length=1, max_length=512)
    schema_version: str = Field(default="knowledge-release-v1", min_length=1)
    evaluation_report: str = ""
    previous_release_id: str | None = None


def _decoded(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "configuration_json",
        "sync_policy_json",
        "permission_sync_policy_json",
        "payload_json",
        "checkpoint_json",
        "source_revisions_json",
        "manifest_json",
    ):
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key.removesuffix("_json")] = json.loads(value)
            except json.JSONDecodeError:
                pass
        result.pop(key, None)
    # Secret references are operational metadata, but should never be echoed.
    result.pop("credential_reference", None)
    return result


@router.get("/sources", dependencies=[Admin])
def list_sources(store: LifecycleStore = Depends(get_lifecycle_store)) -> list[dict[str, Any]]:
    return [_decoded(row) for row in store.fetch_all("SELECT * FROM sources ORDER BY source_id")]


@router.put("/sources/{source_id}", dependencies=[Admin])
def upsert_source(
    source_id: str,
    body: SourceUpsertRequest,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    try:
        row = store.upsert_source(source_id=source_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _decoded(row)


@router.post("/sources/{source_id}/sync", dependencies=[Admin], status_code=202)
def request_sync(
    source_id: str,
    body: SyncRequest,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    try:
        store.get_source(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    key = body.idempotency_key or f"manual:{source_id}:{int(store.clock())}"
    task = store.enqueue_task(
        "source_sync",
        {
            "source_id": source_id,
            "sync_idempotency_key": key,
            "trigger_type": "manual",
            "full": body.full,
        },
        idempotency_key=f"source-sync:{key}",
        source_id=source_id,
    )
    return _decoded(task)


@router.get("/tasks", dependencies=[Admin])
def list_tasks(
    limit: int = 100,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> list[dict[str, Any]]:
    safe_limit = min(500, max(1, limit))
    return [
        _decoded(row)
        for row in store.fetch_all(
            "SELECT * FROM lifecycle_tasks ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        )
    ]


@router.get("/tasks/{task_id}", dependencies=[Admin])
def get_task(task_id: str, store: LifecycleStore = Depends(get_lifecycle_store)) -> dict[str, Any]:
    try:
        return _decoded(store.get_task(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel", dependencies=[Admin], status_code=202)
def cancel_task(task_id: str, store: LifecycleStore = Depends(get_lifecycle_store)) -> dict[str, Any]:
    try:
        return _decoded(store.request_task_cancellation(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/replay", dependencies=[Admin], status_code=202)
def replay_task(task_id: str, store: LifecycleStore = Depends(get_lifecycle_store)) -> dict[str, Any]:
    try:
        return _decoded(store.replay_dead_letter(task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/releases", dependencies=[Admin])
def list_releases(store: LifecycleStore = Depends(get_lifecycle_store)) -> list[dict[str, Any]]:
    return [
        _decoded(row)
        for row in store.fetch_all("SELECT * FROM index_releases ORDER BY created_at DESC")
    ]


@router.get("/releases/active", dependencies=[Admin])
def active_release(store: LifecycleStore = Depends(get_lifecycle_store)) -> dict[str, Any]:
    row = store.get_active_release()
    if row is None:
        raise HTTPException(status_code=404, detail="no active release")
    return _decoded(row)


@router.post("/releases", dependencies=[Admin], status_code=201)
def create_release(
    body: ReleaseCreateRequest,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    manifest = ReleaseManifest(
        release_id=body.release_id,
        source_revisions=tuple(body.source_revisions),
        chunk_collection=body.chunk_collection,
        entity_collection=body.entity_collection,
        graph_version=body.graph_version,
        object_asset_namespace=body.object_asset_namespace,
        schema_version=body.schema_version,
        evaluation_report=body.evaluation_report,
    )
    try:
        release = store.create_release(
            manifest, previous_release_id=body.previous_release_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decoded(release)


@router.post("/releases/{release_id}/stage", dependencies=[Admin])
def stage_release(
    release_id: str,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    try:
        return _decoded(
            store.transition_release(
                release_id,
                ReleaseState.STAGING,
                actor="builder",
                idempotency_key="operator-stage",
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _enqueue_release_action(
    store: LifecycleStore, release_id: str, task_type: str
) -> dict[str, Any]:
    try:
        release = store.get_release(release_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    task = store.enqueue_task(
        task_type,
        {"release_id": release_id},
        idempotency_key=f"{task_type}:{release_id}:{release['updated_at']}",
    )
    return _decoded(task)


@router.post("/releases/{release_id}/validate", dependencies=[Admin], status_code=202)
def validate_release(
    release_id: str,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    release = store.get_release(release_id)
    if release["current_state"] != ReleaseState.STAGING.value:
        raise HTTPException(status_code=409, detail="release must be staging")
    return _enqueue_release_action(store, release_id, "release_validate")


@router.post("/releases/{release_id}/activate", dependencies=[Admin], status_code=202)
def activate_release(
    release_id: str,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    release = store.get_release(release_id)
    if release["current_state"] != ReleaseState.READY.value:
        raise HTTPException(status_code=409, detail="release must be ready")
    return _enqueue_release_action(store, release_id, "release_activate")


@router.post("/releases/{release_id}/rollback", dependencies=[Admin], status_code=202)
def rollback_release(
    release_id: str,
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict[str, Any]:
    release = store.get_release(release_id)
    if release["current_state"] != ReleaseState.ACTIVE.value:
        raise HTTPException(status_code=409, detail="release must be active")
    return _enqueue_release_action(store, release_id, "release_rollback")


def register_lifecycle_router(app: Any) -> None:
    app.include_router(router)
