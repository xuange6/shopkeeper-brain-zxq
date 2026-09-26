"""Typed lifecycle contracts and deterministic fingerprints."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class SourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    RETIRED = "retired"


class SyncRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


class RevisionState(StrEnum):
    DISCOVERED = "discovered"
    PROCESSING = "processing"
    STAGED = "staged"
    VALIDATED = "validated"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"
    DELETED = "deleted"


class ReleaseState(StrEnum):
    BUILDING = "building"
    STAGING = "staging"
    VALIDATING = "validating"
    READY = "ready"
    ACTIVATING = "activating"
    ACTIVE = "active"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


class DeletionState(StrEnum):
    DETECTED = "detected"
    TOMBSTONED = "tombstoned"
    PROPAGATING = "propagating"
    VERIFIED = "verified"
    COMPLETED = "completed"
    FAILED = "failed"


class FailureCategory(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POISON = "poison"


class ChangeKind(StrEnum):
    CREATED = "created"
    CONTENT = "content"
    METADATA = "metadata"
    ACL_ONLY = "acl_only"
    RENAMED = "renamed"
    DELETED = "deleted"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class SourceItem:
    """A connector-neutral source snapshot item.

    ``external_id`` is stable when the upstream system provides one.  Local
    connectors use the relative path and the sync engine can reconcile a move
    when exactly one missing and one new item share the same content hash.
    """

    external_id: str
    path: str
    content: bytes
    modified_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    acl_readers: tuple[str, ...] = ()
    visibility: str = "public"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def metadata_hash(self) -> str:
        value = {"path": self.path, **dict(self.metadata)}
        return canonical_hash(value)

    @property
    def acl_hash(self) -> str:
        return canonical_hash(
            {"visibility": self.visibility, "readers": sorted(set(self.acl_readers))}
        )


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    source_revisions: tuple[str, ...]
    chunk_collection: str
    entity_collection: str
    graph_version: str
    object_asset_namespace: str
    schema_version: str = "knowledge-release-v1"
    evaluation_report: str = ""

    def canonical_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.canonical_dict())


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any, length: int = 32) -> str:
    return f"{prefix}_{canonical_hash(parts)[:length]}"


def file_source_item(path: Path, root: Path, *, acl_readers: Iterable[str] = ()) -> SourceItem:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    stat = path.stat()
    return SourceItem(
        external_id=relative,
        path=relative,
        content=path.read_bytes(),
        modified_at=stat.st_mtime,
        metadata={"size_bytes": stat.st_size, "suffix": path.suffix.lower()},
        acl_readers=tuple(acl_readers),
        visibility="private" if tuple(acl_readers) else "public",
    )
