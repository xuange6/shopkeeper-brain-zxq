"""Read-only runtime checks required before an end-to-end RAG evaluation."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


if load_dotenv:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def check_service_runtime(timeout_seconds: float = 2.0) -> Dict[str, Any]:
    """Check configured dependencies without running queries or exposing secrets."""

    checks: list[Dict[str, str]] = []
    _check_endpoint(checks, "milvus", os.getenv("MILVUS_URL", ""), 19530, timeout_seconds)
    _check_endpoint(checks, "neo4j", os.getenv("NEO4J_URI", ""), 7687, timeout_seconds)
    _check_endpoint(checks, "mongodb", os.getenv("MONGO_URL", ""), 27017, timeout_seconds)
    _check_endpoint(
        checks,
        "web_mcp",
        os.getenv("MCP_DASHSCOPE_BASE_URL", ""),
        443,
        timeout_seconds,
    )
    _check_endpoint(
        checks,
        "llm_api",
        os.getenv("OPENAI_API_BASE", ""),
        443,
        timeout_seconds,
    )
    _check_model_path(checks, "embedding_model", os.getenv("BGE_M3_PATH", ""))
    _check_model_path(
        checks,
        "reranker_model",
        os.getenv("BGE_RERANKER_LARGE", ""),
    )

    failures = [
        f"{item['name']}: {item['reason']}"
        for item in checks
        if item["status"] != "ok"
    ]
    return {"passed": not failures, "checks": checks, "failures": failures}


def _check_endpoint(
    checks: list[Dict[str, str]],
    name: str,
    raw_url: str,
    default_port: int,
    timeout_seconds: float,
) -> None:
    if not raw_url.strip():
        checks.append({"name": name, "status": "error", "reason": "not configured"})
        return

    parsed = urlparse(raw_url if "://" in raw_url else f"tcp://{raw_url}")
    host = parsed.hostname
    port = parsed.port or default_port
    if not host:
        checks.append({"name": name, "status": "error", "reason": "invalid endpoint"})
        return
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            pass
        checks.append(
            {"name": name, "status": "ok", "reason": "configured endpoint reachable"}
        )
    except OSError as exc:
        checks.append(
            {
                "name": name,
                "status": "error",
                "reason": f"configured endpoint unreachable ({type(exc).__name__})",
            }
        )


def _check_model_path(
    checks: list[Dict[str, str]],
    name: str,
    configured_value: str,
) -> None:
    value = configured_value.strip()
    if not value:
        checks.append({"name": name, "status": "error", "reason": "not configured"})
        return

    looks_local = Path(value).is_absolute() or value.startswith((".", "~"))
    if looks_local and not Path(value).expanduser().is_dir():
        checks.append(
            {"name": name, "status": "error", "reason": "configured path does not exist"}
        )
        return
    checks.append(
        {
            "name": name,
            "status": "ok",
            "reason": "local path exists" if looks_local else "remote model id configured",
        }
    )
