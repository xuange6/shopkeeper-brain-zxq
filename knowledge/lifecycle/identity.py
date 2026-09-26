"""External identity snapshot synchronization."""

from __future__ import annotations

from knowledge.lifecycle.connectors import IdentityConnector
from knowledge.lifecycle.store import LifecycleStore


def sync_identities(store: LifecycleStore, source_id: str, connector: IdentityConnector, *, idempotency_key: str) -> dict[str, int]:
    mappings = [
        {
            "external_principal_hash": principal.external_hash,
            "internal_principal": principal.internal_principal,
            "principal_type": principal.principal_type,
            "active": principal.active,
        }
        for principal in connector.principals()
    ]
    return store.sync_identity_mappings(source_id, mappings, idempotency_key=idempotency_key)
