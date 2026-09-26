"""Validate Redis-backed task state and SSE visibility across processes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from knowledge.utils.redis_runtime import get_runtime_redis


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not os.getenv("TASK_STATE_REDIS_URL", "").strip():
        raise RuntimeError("TASK_STATE_REDIS_URL is required")
    client = get_runtime_redis()
    if client is None or not client.ping():
        raise RuntimeError("Redis is not reachable")

    task_id = f"distributed-accept-{uuid4().hex}"
    writer = """
import sys
from knowledge.utils.task_utils import create_task, add_running_task, add_done_task, set_task_result, update_task_status, TASK_STATUS_COMPLETED
from knowledge.utils.sse_util import create_sse_queue, push_sse_event, SSEEvent
task_id = sys.argv[1]
create_task(task_id)
add_running_task(task_id, 'search_embedding')
add_done_task(task_id, 'search_embedding')
set_task_result(task_id, 'answer', {'text': 'ok'})
update_task_status(task_id, TASK_STATUS_COMPLETED)
create_sse_queue(task_id)
push_sse_event(task_id, SSEEvent.FINAL, {'answer': 'ok'})
"""
    reader = """
import json, sys
from knowledge.utils.task_utils import get_task_info
from knowledge.utils.redis_runtime import get_runtime_redis
task_id = sys.argv[1]
info = get_task_info(task_id)
events = get_runtime_redis().xrange(f'shopkeeper:sse:{task_id}')
assert info['status'] == 'completed'
assert info['answer'] == {'text': 'ok'}
assert len(info['done_list']) == 1
assert any(fields.get('event') == 'final' for _, fields in events)
print(json.dumps({'status': info['status'], 'event_count': len(events)}))
"""
    environment = dict(os.environ)
    try:
        subprocess.run(
            [sys.executable, "-c", writer, task_id],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        observed = subprocess.run(
            [sys.executable, "-c", reader, task_id],
            cwd=PROJECT_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        child_result = json.loads(observed.stdout)
        result = {
            "status": "passed",
            "backend": "redis",
            "cross_process_task_state": True,
            "cross_process_sse_stream": True,
            "observed_events": child_result["event_count"],
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        client.delete(
            f"shopkeeper:task:{task_id}",
            f"shopkeeper:task:{task_id}:running",
            f"shopkeeper:task:{task_id}:done",
            f"shopkeeper:task:{task_id}:failed",
            f"shopkeeper:task:{task_id}:result",
            f"shopkeeper:sse:{task_id}",
        )


if __name__ == "__main__":
    main()
