"""Deterministic identity helpers for the document IR."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


_SPACE_RE = re.compile(r"\s+")


def normalized_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def document_id(sha256: str) -> str:
    return f"doc_{sha256[:24]}"


def logical_document_id(key: str | None, source_sha256: str) -> str:
    """An explicit source key groups revisions; absent one, never guess from filename."""

    return stable_id("doc", key.strip(), length=24) if key and key.strip() else document_id(source_sha256)


def revision_id(*, source_sha256: str, parser_name: str, parser_version: str, blocks: Any) -> str:
    return stable_id(
        "rev",
        source_sha256,
        parser_name,
        parser_version,
        blocks,
        length=32,
    )
