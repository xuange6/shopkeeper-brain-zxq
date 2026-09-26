"""Health and non-sensitive system capability endpoints."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse

from knowledge.core.app_config import AppConfig, get_app_config
from knowledge.core.deps import get_lifecycle_store
from knowledge.lifecycle.observability import prometheus_text
from knowledge.lifecycle.store import LifecycleStore
from knowledge.utils.redis_runtime import get_runtime_redis


router = APIRouter(tags=["system"])


def _is_configured(*names: str) -> bool:
    return all(bool(os.getenv(name, "").strip()) for name in names)


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _mineru_available() -> bool:
    executable_name = "mineru.exe" if os.name == "nt" else "mineru"
    sibling_executable = Path(sys.executable).resolve().parent / executable_name
    return bool(shutil.which("mineru") or sibling_executable.exists())


def get_integration_status() -> Dict[str, bool]:
    """Return configuration flags only; never return values or credentials."""

    return {
        "llm": _is_configured("OPENAI_API_KEY"),
        "embedding": bool(
            os.getenv("BGE_M3_PATH", "").strip()
            or os.getenv("BGE_M3", "").strip()
            or os.getenv("EMBEDDING_MODEL", "").strip()
        ),
        "milvus": _is_configured("MILVUS_URL", "CHUNKS_COLLECTION"),
        "neo4j": _is_configured("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"),
        "mongodb": _is_configured("MONGO_URL"),
        "minio": _is_configured(
            "MINIO_ENDPOINT",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
            "MINIO_BUCKET_NAME",
        ),
        "web_search": _is_configured("MCP_DASHSCOPE_BASE_URL", "OPENAI_API_KEY"),
        "mineru": _mineru_available(),
    }


@router.get("/health", summary="Service liveness")
async def health(config: AppConfig = Depends(get_app_config)) -> dict:
    return {
        "status": "ok",
        "service": config.name,
        "version": config.version,
    }


@router.get("/ready", summary="Service readiness")
async def ready(
    config: AppConfig = Depends(get_app_config),
    store: LifecycleStore = Depends(get_lifecycle_store),
) -> dict:
    failures: list[str] = []
    if config.environment.lower() == "production":
        if not os.getenv("LIFECYCLE_DATABASE_URL", "").strip():
            failures.append("postgres_control_plane_not_configured")
        required_integrations = {
            "llm": _is_configured("OPENAI_API_KEY"),
            "milvus": _is_configured("MILVUS_URL"),
            "neo4j": _is_configured("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"),
            "mongodb": _is_configured("MONGO_URL"),
            "minio": _is_configured(
                "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET_NAME"
            ),
        }
        failures.extend(
            f"{name}_not_configured"
            for name, configured in required_integrations.items()
            if not configured
        )
    try:
        row = store.fetch_one("SELECT 1 AS ok")
        if not row or int(row.get("ok", 0)) != 1:
            failures.append("control_plane_unhealthy")
    except Exception:
        failures.append("control_plane_unreachable")
    if _enabled("DISTRIBUTED_RUNTIME"):
        try:
            redis_client = get_runtime_redis()
            if redis_client is None or not redis_client.ping():
                failures.append("redis_runtime_unhealthy")
        except Exception:
            failures.append("redis_runtime_unreachable")
    if failures:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "failures": failures},
        )
    return {"status": "ready", "service": config.name, "version": config.version}


@router.get("/api/system", summary="Public capability summary")
async def system_info(config: AppConfig = Depends(get_app_config)) -> dict:
    return {
        "status": "ok",
        "service": config.name,
        "version": config.version,
        "capabilities": [
            "document_import",
            "hybrid_vector_retrieval",
            "hyde_retrieval",
            "knowledge_graph_retrieval",
            "web_search",
            "reranking",
            "streaming_answer",
            "conversation_history",
            "knowledge_lifecycle",
        ],
        "integrations": get_integration_status(),
    }


@router.get("/metrics", response_class=PlainTextResponse, summary="Lifecycle metrics")
async def metrics(store: LifecycleStore = Depends(get_lifecycle_store)) -> str:
    return prometheus_text(store)


def register_system_router(app: FastAPI) -> None:
    app.include_router(router)
