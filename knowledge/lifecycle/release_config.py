"""Read validated active-release pointer values for query/import configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re


SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.-]+$")
RELEASE_ENVIRONMENT_KEYS = (
    "CHUNKS_COLLECTION",
    "ENTITY_NAME_COLLECTION",
    "KG_GRAPH_VERSION",
    "OBJECT_ASSET_NAMESPACE",
)


def active_release_value(key: str, fallback: str) -> str:
    configured = os.getenv("LIFECYCLE_ACTIVE_RELEASE_PATH", "").strip()
    path = Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "active-release.json"
    if not path.is_file():
        return fallback
    try:
        value = str(json.loads(path.read_text(encoding="utf-8")).get(key) or "")
    except (OSError, ValueError, json.JSONDecodeError):
        return fallback
    return value if SAFE_VALUE.fullmatch(value) else fallback


def apply_active_release_environment() -> dict[str, str]:
    """Load the validated atomic pointer into process configuration.

    Query configuration remains a stage-2 compatibility surface.  Applying the
    pointer at service composition keeps that surface byte-for-byte stable while
    still making a release switch effective before the lazy query graph import.
    """

    applied: dict[str, str] = {}
    for key in RELEASE_ENVIRONMENT_KEYS:
        value = active_release_value(key, "")
        if value:
            os.environ[key] = value
            applied[key] = value
    return applied
