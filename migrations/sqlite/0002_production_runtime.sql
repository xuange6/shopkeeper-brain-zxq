ALTER TABLE document_revisions ADD COLUMN data_revision_id TEXT NOT NULL DEFAULT '';
ALTER TABLE document_revisions ADD COLUMN staging_release_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS lifecycle_metric_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    labels_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_lifecycle_metric_events
ON lifecycle_metric_events(metric_name, created_at);
