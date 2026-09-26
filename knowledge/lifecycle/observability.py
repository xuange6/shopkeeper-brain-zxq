"""Prometheus text export and structured, redacted lifecycle log context."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from knowledge.lifecycle.store import LifecycleStore


def prometheus_text(store: LifecycleStore) -> str:
    lines = []
    for name, value in sorted(store.metrics_snapshot().items()):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value:.6f}")
    return "\n".join(lines) + "\n"


def log_context(**values: Any) -> dict[str, Any]:
    allowed = {
        "trace_id",
        "run_id",
        "source_id",
        "document_id",
        "revision_id",
        "release_id",
        "task_attempt",
        "from_state",
        "to_state",
        "error_category",
    }
    result = {key: values[key] for key in allowed if key in values and values[key] not in (None, "")}
    tenant = str(values.get("tenant_id") or "")
    if tenant:
        result["tenant_hash"] = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:12]
    return result
