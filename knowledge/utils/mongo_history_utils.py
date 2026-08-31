"""MongoDB helpers for chat history."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection


load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


@lru_cache(maxsize=1)
def _get_collection() -> Collection:
    mongo_url = os.getenv("MONGO_URL", "")
    db_name = os.getenv("MONGO_DB_NAME", "kb001")
    collection_name = os.getenv("MONGO_CHAT_COLLECTION", "chat_message")

    if not mongo_url:
        raise RuntimeError("MONGO_URL is not configured")

    try:
        timeout_ms = max(
            200,
            int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "2000")),
        )
    except ValueError:
        timeout_ms = 2000

    client = MongoClient(
        mongo_url,
        serverSelectionTimeoutMS=timeout_ms,
        connectTimeoutMS=timeout_ms,
        socketTimeoutMS=max(timeout_ms, 5000),
    )
    return client[db_name][collection_name]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_doc(doc: Dict) -> Dict:
    normalized = dict(doc)
    if "_id" in normalized:
        normalized["_id"] = str(normalized["_id"])
    return normalized


def get_recent_messages(session_id: str, limit: int = 10) -> List[Dict]:
    if not session_id:
        return []

    cursor = (
        _get_collection()
        .find({"session_id": session_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    docs = [_normalize_doc(doc) for doc in cursor]
    return list(reversed(docs))


def save_chat_message(
    session_id: str,
    role: str,
    text: str,
    rewritten_query: str = "",
    item_names: Optional[List[str]] = None,
    message_id: str = "",
    image_urls: Optional[List[str]] = None,
    sources: Optional[List[Dict[str, Any]]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> str:
    if not session_id or not role:
        return ""

    collection = _get_collection()
    now = _now()
    payload = {
        "session_id": session_id,
        "role": role,
        "text": text,
        "rewritten_query": rewritten_query,
        "item_names": item_names or [],
        "image_urls": image_urls or [],
        "sources": sources or [],
        "diagnostics": diagnostics or {},
        "updated_at": now,
    }

    if message_id:
        collection.update_one(
            {"_id": _coerce_object_id(message_id)},
            {"$set": payload},
            upsert=False,
        )
        return message_id

    payload["created_at"] = now
    result = collection.insert_one(payload)
    return str(result.inserted_id)


def update_message_item_names(message_ids: List[str], item_names: List[str]) -> None:
    ids = [_coerce_object_id(message_id) for message_id in message_ids if message_id]
    if not ids:
        return

    _get_collection().update_many(
        {"_id": {"$in": ids}},
        {"$set": {"item_names": item_names, "updated_at": _now()}},
    )


def clear_history(session_id: str) -> int:
    if not session_id:
        return 0

    result = _get_collection().delete_many({"session_id": session_id})
    return int(result.deleted_count)


def _coerce_object_id(message_id: str):
    try:
        from bson import ObjectId

        return ObjectId(message_id)
    except Exception:
        return message_id
