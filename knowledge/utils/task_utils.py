"""
导入任务进度追踪工具。

这个模块只负责记录任务状态，不处理具体导入逻辑。FastAPI 上传接口、
后台导入任务、BaseNode 和状态查询接口都会围绕这里读写任务进度。
"""

from __future__ import annotations

from collections import defaultdict
import os
import json
from threading import RLock
import time
from typing import Dict, List, Optional

from knowledge.utils.redis_runtime import get_runtime_redis


TASK_STATUS_UPLOADED = "uploaded"
TASK_STATUS_PROCESSING = "processing"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_NOT_FOUND = "not_found"


_tasks_running_list: Dict[str, List[str]] = defaultdict(list)
_tasks_done_list: Dict[str, List[str]] = defaultdict(list)
_tasks_failed_list: Dict[str, List[str]] = defaultdict(list)
_tasks_status: Dict[str, str] = {}
_tasks_error: Dict[str, str] = {}
_tasks_result: Dict[str, Dict[str, object]] = defaultdict(dict)
_tasks_created_at: Dict[str, float] = {}
_tasks_updated_at: Dict[str, float] = {}
_lock = RLock()


def _redis_keys(task_id: str) -> tuple[str, str, str, str, str]:
    prefix = f"shopkeeper:task:{task_id}"
    return (
        prefix,
        f"{prefix}:running",
        f"{prefix}:done",
        f"{prefix}:failed",
        f"{prefix}:result",
    )


def _redis_expire(client, keys: tuple[str, ...]) -> None:
    ttl = _task_ttl_seconds()
    pipeline = client.pipeline(transaction=True)
    for key in keys:
        pipeline.expire(key, ttl)
    pipeline.execute()


def _redis_ensure_task(client, task_id: str) -> tuple[str, ...]:
    keys = _redis_keys(task_id)
    now = time.time()
    client.hsetnx(keys[0], "status", TASK_STATUS_UPLOADED)
    client.hsetnx(keys[0], "created_at", now)
    client.hset(keys[0], "updated_at", now)
    _redis_expire(client, keys)
    return keys


_NODE_NAME_TO_CN: Dict[str, str] = {
    "upload_file": "上传文件",
    "entry": "检查文件",
    "entry_node": "检查文件",
    "pdf_to_md_node": "PDF转Markdown",
    "md_img_node": "Markdown图片处理",
    "document_split": "文档切分",
    "document_split_node": "文档切分",
    "item_name_recognition": "主体名称识别",
    "item_name_recognition_node": "主体名称识别",
    "bge_embedding": "向量生成",
    "bge_embedding_node": "向量生成",
    "beg_embedding_chunks_node": "向量生成",
    "import_milvus": "导入向量数据库",
    "import_milvus_node": "导入向量数据库",
    "knowledge_graph": "导入知识图谱",
    "knowledge_graph_node": "导入知识图谱",
    "item_name_confirm": "商品名确认",
    "item_name_confirm_node": "商品名确认",
    "search_embedding": "向量检索",
    "search_embedding_node": "向量检索",
    "search_embedding_hyde": "HyDE 向量检索",
    "search_embedding_hyde_node": "HyDE 向量检索",
    "query_kg": "知识图谱检索",
    "query_kg_node": "知识图谱检索",
    "web_search_mcp": "联网检索",
    "web_search_mcp_node": "联网检索",
    "rrf": "RRF 融合",
    "rrf_node": "RRF 融合",
    "rerank": "Rerank 重排序",
    "rerank_node": "Rerank 重排序",
    "answer_output": "答案生成",
    "answer_output_node": "答案生成",
    "__end__": "处理完成",
}


def _ensure_task(task_id: str) -> None:
    if task_id not in _tasks_status:
        _tasks_status[task_id] = TASK_STATUS_UPLOADED
        now = time.time()
        _tasks_created_at[task_id] = now
        _tasks_updated_at[task_id] = now


def _touch_task(task_id: str) -> None:
    _tasks_updated_at[task_id] = time.time()


def _task_ttl_seconds() -> int:
    try:
        return max(300, int(os.getenv("TASK_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400


def _cleanup_expired_locked() -> None:
    cutoff = time.time() - _task_ttl_seconds()
    expired = [
        task_id
        for task_id, updated_at in _tasks_updated_at.items()
        if updated_at < cutoff
        and _tasks_status.get(task_id) in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED}
    ]
    for task_id in expired:
        _clear_task_locked(task_id)


def _clear_task_locked(task_id: str) -> None:
    _tasks_status.pop(task_id, None)
    _tasks_error.pop(task_id, None)
    _tasks_result.pop(task_id, None)
    _tasks_running_list.pop(task_id, None)
    _tasks_done_list.pop(task_id, None)
    _tasks_failed_list.pop(task_id, None)
    _tasks_created_at.pop(task_id, None)
    _tasks_updated_at.pop(task_id, None)


def _to_cn(node_name: str) -> str:
    return _NODE_NAME_TO_CN.get(node_name, node_name)


def _append_unique(nodes: List[str], node_name: str) -> None:
    if node_name not in nodes:
        nodes.append(node_name)


def _remove_node(nodes: List[str], node_name: str) -> None:
    while node_name in nodes:
        nodes.remove(node_name)


def _format_node_list(nodes: List[str], start_index: int = 1) -> List[str]:
    return [f"[{index}] {_to_cn(node_name)}" for index, node_name in enumerate(nodes, start_index)]


def create_task(task_id: str, status: str = TASK_STATUS_UPLOADED) -> None:
    """创建或重置一个任务的追踪信息。"""

    if not task_id:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_keys(task_id)
        now = time.time()
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.delete(*keys)
        pipeline.hset(
            keys[0],
            mapping={"status": status, "created_at": now, "updated_at": now},
        )
        for key in keys:
            pipeline.expire(key, _task_ttl_seconds())
        pipeline.execute()
        return
    with _lock:
        _cleanup_expired_locked()
        now = time.time()
        _tasks_status[task_id] = status
        _tasks_running_list[task_id] = []
        _tasks_done_list[task_id] = []
        _tasks_failed_list[task_id] = []
        _tasks_error.pop(task_id, None)
        _tasks_result[task_id] = {}
        _tasks_created_at[task_id] = now
        _tasks_updated_at[task_id] = now


def update_task_status(task_id: str, status: str) -> None:
    """更新任务整体状态。"""

    if not task_id:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_ensure_task(redis_client, task_id)
        redis_client.hset(keys[0], mapping={"status": status, "updated_at": time.time()})
        _redis_expire(redis_client, keys)
        return
    with _lock:
        _ensure_task(task_id)
        _tasks_status[task_id] = status
        _touch_task(task_id)


def add_running_task(task_id: str, node_name: str) -> None:
    """标记某个节点开始运行。"""

    if not task_id or not node_name:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_ensure_task(redis_client, task_id)
        current_status = redis_client.hget(keys[0], "status")
        metadata = {"updated_at": time.time()}
        if current_status not in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED}:
            metadata["status"] = TASK_STATUS_PROCESSING
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.lrem(keys[2], 0, node_name)
        pipeline.lrem(keys[3], 0, node_name)
        pipeline.lrem(keys[1], 0, node_name)
        pipeline.rpush(keys[1], node_name)
        pipeline.hset(keys[0], mapping=metadata)
        pipeline.execute()
        _redis_expire(redis_client, keys)
        return
    with _lock:
        _ensure_task(task_id)
        if _tasks_status.get(task_id) not in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED}:
            _tasks_status[task_id] = TASK_STATUS_PROCESSING
        _remove_node(_tasks_done_list[task_id], node_name)
        _remove_node(_tasks_failed_list[task_id], node_name)
        _append_unique(_tasks_running_list[task_id], node_name)
        _touch_task(task_id)


def add_done_task(task_id: str, node_name: str) -> None:
    """标记某个节点完成，并从 running 列表中移除。"""

    if not task_id or not node_name:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_ensure_task(redis_client, task_id)
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.lrem(keys[1], 0, node_name)
        pipeline.lrem(keys[3], 0, node_name)
        pipeline.lrem(keys[2], 0, node_name)
        pipeline.rpush(keys[2], node_name)
        pipeline.hset(keys[0], "updated_at", time.time())
        pipeline.execute()
        _redis_expire(redis_client, keys)
        return
    with _lock:
        _ensure_task(task_id)
        _remove_node(_tasks_running_list[task_id], node_name)
        _remove_node(_tasks_failed_list[task_id], node_name)
        _append_unique(_tasks_done_list[task_id], node_name)
        _touch_task(task_id)


def add_failed_task(task_id: str, node_name: str, error: Optional[str] = None) -> None:
    """标记某个节点失败，并记录任务错误信息。"""

    if not task_id or not node_name:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_ensure_task(redis_client, task_id)
        mapping = {"status": TASK_STATUS_FAILED, "updated_at": time.time()}
        if error:
            mapping["error"] = error
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.lrem(keys[1], 0, node_name)
        pipeline.lrem(keys[3], 0, node_name)
        pipeline.rpush(keys[3], node_name)
        pipeline.hset(keys[0], mapping=mapping)
        pipeline.execute()
        _redis_expire(redis_client, keys)
        return
    with _lock:
        _ensure_task(task_id)
        _tasks_status[task_id] = TASK_STATUS_FAILED
        _remove_node(_tasks_running_list[task_id], node_name)
        _append_unique(_tasks_failed_list[task_id], node_name)
        if error:
            _tasks_error[task_id] = error
        _touch_task(task_id)


def get_task_status(task_id: str) -> str:
    if not task_id:
        return TASK_STATUS_NOT_FOUND

    redis_client = get_runtime_redis()
    if redis_client is not None:
        value = redis_client.hget(_redis_keys(task_id)[0], "status")
        return str(value or TASK_STATUS_NOT_FOUND)
    with _lock:
        _cleanup_expired_locked()
        return _tasks_status.get(task_id, TASK_STATUS_NOT_FOUND)


def get_done_task_list(task_id: str) -> List[str]:
    if not task_id:
        return []

    redis_client = get_runtime_redis()
    if redis_client is not None:
        return _format_node_list(redis_client.lrange(_redis_keys(task_id)[2], 0, -1))
    with _lock:
        done_nodes = list(_tasks_done_list.get(task_id, []))
        return _format_node_list(done_nodes)


def get_running_task_list(task_id: str) -> List[str]:
    if not task_id:
        return []

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_keys(task_id)
        done_count = redis_client.llen(keys[2])
        return _format_node_list(
            redis_client.lrange(keys[1], 0, -1), start_index=done_count + 1
        )
    with _lock:
        done_count = len(_tasks_done_list.get(task_id, []))
        running_nodes = list(_tasks_running_list.get(task_id, []))
        return _format_node_list(running_nodes, start_index=done_count + 1)


def get_failed_task_list(task_id: str) -> List[str]:
    if not task_id:
        return []

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_keys(task_id)
        done_count = redis_client.llen(keys[2])
        return _format_node_list(
            redis_client.lrange(keys[3], 0, -1), start_index=done_count + 1
        )
    with _lock:
        done_count = len(_tasks_done_list.get(task_id, []))
        failed_nodes = list(_tasks_failed_list.get(task_id, []))
        return _format_node_list(failed_nodes, start_index=done_count + 1)


def get_task_error(task_id: str) -> str:
    if not task_id:
        return ""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        return str(redis_client.hget(_redis_keys(task_id)[0], "error") or "")
    with _lock:
        return _tasks_error.get(task_id, "")


def set_task_result(task_id: str, key: str, value: object) -> None:
    """保存任务结果，查询流程会用它暂存最终答案。"""

    if not task_id or not key:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_ensure_task(redis_client, task_id)
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.hset(keys[4], key, json.dumps(value, ensure_ascii=False))
        pipeline.hset(keys[0], "updated_at", time.time())
        pipeline.execute()
        _redis_expire(redis_client, keys)
        return
    with _lock:
        _ensure_task(task_id)
        _tasks_result[task_id][key] = value
        _touch_task(task_id)


def get_task_result(task_id: str, key: str, default: object = None) -> object:
    """读取任务结果。"""

    if not task_id or not key:
        return default

    redis_client = get_runtime_redis()
    if redis_client is not None:
        value = redis_client.hget(_redis_keys(task_id)[4], key)
        return json.loads(value) if value is not None else default
    with _lock:
        return _tasks_result.get(task_id, {}).get(key, default)


def get_task_info(task_id: str) -> Dict[str, object]:
    """返回接口可直接序列化的任务状态快照。"""

    redis_client = get_runtime_redis()
    if redis_client is not None:
        keys = _redis_keys(task_id)
        metadata = redis_client.hgetall(keys[0])
        if not metadata:
            return {
                "task_id": task_id,
                "status": TASK_STATUS_NOT_FOUND,
                "done_list": [],
                "running_list": [],
                "failed_list": [],
                "error": "",
                "created_at": None,
                "updated_at": None,
            }
        done_nodes = redis_client.lrange(keys[2], 0, -1)
        running_nodes = redis_client.lrange(keys[1], 0, -1)
        failed_nodes = redis_client.lrange(keys[3], 0, -1)
        result = {
            key: json.loads(value)
            for key, value in redis_client.hgetall(keys[4]).items()
        }
        return {
            "task_id": task_id,
            "status": metadata.get("status", TASK_STATUS_NOT_FOUND),
            "done_list": _format_node_list(done_nodes),
            "running_list": _format_node_list(running_nodes, len(done_nodes) + 1),
            "failed_list": _format_node_list(failed_nodes, len(done_nodes) + 1),
            "error": metadata.get("error", ""),
            "created_at": float(metadata["created_at"]) if metadata.get("created_at") else None,
            "updated_at": float(metadata["updated_at"]) if metadata.get("updated_at") else None,
            **result,
        }
    with _lock:
        _cleanup_expired_locked()
        done_nodes = list(_tasks_done_list.get(task_id, []))
        running_nodes = list(_tasks_running_list.get(task_id, []))
        failed_nodes = list(_tasks_failed_list.get(task_id, []))
        status = _tasks_status.get(task_id, TASK_STATUS_NOT_FOUND)
        error = _tasks_error.get(task_id, "")
        result = dict(_tasks_result.get(task_id, {}))
        created_at = _tasks_created_at.get(task_id)
        updated_at = _tasks_updated_at.get(task_id)

    done_list = _format_node_list(done_nodes)
    running_list = _format_node_list(running_nodes, start_index=len(done_nodes) + 1)
    failed_list = _format_node_list(failed_nodes, start_index=len(done_nodes) + 1)

    return {
        "task_id": task_id,
        "status": status,
        "done_list": done_list,
        "running_list": running_list,
        "failed_list": failed_list,
        "error": error,
        "created_at": created_at,
        "updated_at": updated_at,
        **result,
    }


def clear_task(task_id: str) -> None:
    """清理某个任务的追踪信息，主要用于测试或手动释放内存。"""

    if not task_id:
        return

    redis_client = get_runtime_redis()
    if redis_client is not None:
        redis_client.delete(*_redis_keys(task_id))
        return
    with _lock:
        _clear_task_locked(task_id)
