"""Health and non-sensitive system capability endpoints."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Depends, FastAPI

from knowledge.core.app_config import AppConfig, get_app_config


router = APIRouter(tags=["system"])


def _is_configured(*names: str) -> bool:
    return all(bool(os.getenv(name, "").strip()) for name in names)


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
        ],
        "integrations": get_integration_status(),
    }


def register_system_router(app: FastAPI) -> None:
    app.include_router(router)
