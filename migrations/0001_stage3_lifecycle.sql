PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    credential_reference TEXT NOT NULL DEFAULT '',
    configuration_version TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    sync_cursor TEXT NOT NULL DEFAULT '',
    sync_policy_json TEXT NOT NULL,
    permission_sync_policy_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','error','retired')),
    last_successful_sync REAL,
    next_sync_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    tenant_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    active_revision_id TEXT,
    visibility TEXT NOT NULL,
    acl_json TEXT NOT NULL,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    first_discovered_at REAL NOT NULL,
    last_discovered_at REAL NOT NULL,
    UNIQUE(source_id, source_item_id)
);

CREATE TABLE IF NOT EXISTS document_revisions (
    revision_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    content_hash TEXT NOT NULL,
    metadata_hash TEXT NOT NULL,
    acl_hash TEXT NOT NULL,
    parser_fingerprint TEXT NOT NULL,
    source_modified_at REAL,
    processing_status TEXT NOT NULL CHECK(processing_status IN (
        'discovered','processing','staged','validated','active','failed','retired','deleted'
    )),
    change_kind TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    activated_at REAL,
    retired_at REAL,
    UNIQUE(document_id, content_hash, metadata_hash, acl_hash, parser_fingerprint)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    idempotency_key TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    cursor_before TEXT NOT NULL DEFAULT '',
    cursor_after TEXT NOT NULL DEFAULT '',
    discovered_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    current_state TEXT NOT NULL CHECK(current_state IN (
        'pending','running','retrying','succeeded','partially_failed','failed','cancelled','dead_lettered'
    )),
    retry_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at REAL,
    heartbeat_at REAL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_category TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    started_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    UNIQUE(source_id, idempotency_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_source_active
ON sync_runs(source_id)
WHERE current_state IN ('running','retrying');

CREATE TABLE IF NOT EXISTS lifecycle_tasks (
    task_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(source_id),
    run_id TEXT REFERENCES sync_runs(run_id),
    task_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','running','retrying','succeeded','failed','cancelled','dead_lettered'
    )),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    heartbeat_at REAL,
    available_at REAL NOT NULL,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_category TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS ix_tasks_claim
ON lifecycle_tasks(state, available_at, created_at);

CREATE TABLE IF NOT EXISTS index_releases (
    release_id TEXT PRIMARY KEY,
    source_revisions_json TEXT NOT NULL,
    chunk_collection TEXT NOT NULL,
    entity_collection TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    object_asset_namespace TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    evaluation_report TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    current_state TEXT NOT NULL CHECK(current_state IN (
        'building','staging','validating','ready','activating','active','failed',
        'rolling_back','rolled_back','retired'
    )),
    previous_release_id TEXT REFERENCES index_releases(release_id),
    activated_at REAL,
    rolled_back_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_release
ON index_releases((1)) WHERE current_state = 'active';

CREATE TABLE IF NOT EXISTS identity_mappings (
    mapping_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_principal_hash TEXT NOT NULL,
    internal_principal TEXT NOT NULL,
    principal_type TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at REAL NOT NULL,
    UNIQUE(source_id, external_principal_hash)
);

CREATE TABLE IF NOT EXISTS deletion_jobs (
    deletion_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    revision_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'detected','tombstoned','propagating','verified','completed','failed'
    )),
    targets_json TEXT NOT NULL,
    completed_targets_json TEXT NOT NULL DEFAULT '[]',
    verification_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    detected_at REAL NOT NULL,
    completed_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    tenant_hash TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    revision_id TEXT NOT NULL DEFAULT '',
    release_id TEXT NOT NULL DEFAULT '',
    task_attempt INTEGER NOT NULL DEFAULT 0,
    from_state TEXT NOT NULL DEFAULT '',
    to_state TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(entity_type, entity_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_audit_entity
ON audit_events(entity_type, entity_id, created_at);
