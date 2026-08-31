"""Configuration for the unified FastAPI application."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Tuple

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dotenv is optional at import time
    load_dotenv = None


DEFAULT_CORS_ORIGINS: Tuple[str, ...] = (
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://localhost:8001",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8001",
)


def _load_project_env() -> None:
    if load_dotenv is None:
        return
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _deduplicate(values: Iterable[str]) -> Tuple[str, ...]:
    result = []
    seen = set()
    for value in values:
        normalized = str(value).strip().rstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _parse_cors_origins(raw_value: str | None) -> Tuple[str, ...]:
    if not raw_value or not raw_value.strip():
        return DEFAULT_CORS_ORIGINS

    value = raw_value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            origins = _deduplicate(str(item) for item in parsed)
            return origins or DEFAULT_CORS_ORIGINS

    origins = _deduplicate(value.split(","))
    return origins or DEFAULT_CORS_ORIGINS


@dataclass(frozen=True)
class AppConfig:
    """Process-level settings used by the web application."""

    name: str = field(default_factory=lambda: os.getenv("APP_NAME", "Shopkeeper Brain"))
    description: str = field(
        default_factory=lambda: os.getenv(
            "APP_DESCRIPTION",
            "多路检索、知识图谱与文档导入一体化知识库服务",
        )
    )
    version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "2.0.0"))
    environment: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    host: str = field(default_factory=lambda: os.getenv("APP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("APP_PORT", 8000))
    reload: bool = field(default_factory=lambda: _env_bool("APP_RELOAD", False))
    cors_origins: Tuple[str, ...] = field(
        default_factory=lambda: _parse_cors_origins(os.getenv("APP_CORS_ORIGINS"))
    )
    cors_allow_credentials: bool = field(
        default_factory=lambda: _env_bool("APP_CORS_ALLOW_CREDENTIALS", True)
    )


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    _load_project_env()
    return AppConfig()


def reset_app_config() -> None:
    """Clear cached application settings, primarily for tests."""

    get_app_config.cache_clear()
