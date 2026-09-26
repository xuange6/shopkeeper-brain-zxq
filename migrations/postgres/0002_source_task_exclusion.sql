CREATE UNIQUE INDEX IF NOT EXISTS ux_task_source_running
ON lifecycle_tasks(source_id)
WHERE state = 'running' AND source_id IS NOT NULL;
