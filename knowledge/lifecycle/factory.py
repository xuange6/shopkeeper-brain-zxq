"""Select the lifecycle control-plane backend from environment."""

from __future__ import annotations

import os

from knowledge.core.paths import get_lifecycle_db_path
from knowledge.lifecycle.store import LifecycleStore


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_lifecycle_store(*, migrate: bool | None = None) -> LifecycleStore:
    database_url = os.getenv("LIFECYCLE_DATABASE_URL", "").strip()
    if database_url:
        from knowledge.lifecycle.postgres_store import PostgresLifecycleStore

        should_migrate = (
            _env_bool("LIFECYCLE_AUTO_MIGRATE", True)
            if migrate is None
            else migrate
        )
        return PostgresLifecycleStore(database_url, migrate=should_migrate)
    return LifecycleStore(get_lifecycle_db_path())
