"""Replaceable source and identity connector contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from knowledge.lifecycle.models import SourceItem, canonical_hash, file_source_item


class SourceConnector(Protocol):
    def scan(self, cursor: str, *, full: bool = False) -> tuple[list[SourceItem], str]:
        """Return a durable page/snapshot and the cursor safe after persistence."""


class IdentityConnector(Protocol):
    def principals(self) -> Iterable["ExternalPrincipal"]:
        """Return the complete current principal mapping snapshot."""


@dataclass(frozen=True)
class ExternalPrincipal:
    external_id: str
    internal_principal: str
    principal_type: str = "subject"
    active: bool = True

    @property
    def external_hash(self) -> str:
        return hashlib.sha256(self.external_id.encode("utf-8")).hexdigest()


class LocalDirectoryConnector:
    """Full/incremental filesystem snapshot connector for Markdown and PDF files."""

    def __init__(self, root: str | Path, *, acl_by_path: Mapping[str, Sequence[str]] | None = None):
        self.root = Path(root).resolve()
        self.acl_by_path = dict(acl_by_path or {})

    def scan(self, cursor: str, *, full: bool = False) -> tuple[list[SourceItem], str]:
        if not self.root.is_dir():
            raise FileNotFoundError(self.root)
        items: list[SourceItem] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".markdown", ".pdf"}:
                continue
            relative = path.relative_to(self.root).as_posix()
            items.append(
                file_source_item(
                    path,
                    self.root,
                    acl_readers=self.acl_by_path.get(relative, ()),
                )
            )
        next_cursor = canonical_hash(
            [(item.external_id, item.content_hash, item.metadata_hash, item.acl_hash) for item in items]
        )
        # Full snapshots are returned even when the cursor is unchanged so
        # deletion and ACL reconciliation can still run deterministically.
        return items, next_cursor


class StaticIdentityConnector:
    """A real, replaceable simulated IdP used by acceptance tests and local sites."""

    def __init__(self, principals: Iterable[ExternalPrincipal]):
        self._principals = tuple(principals)

    def principals(self) -> Iterable[ExternalPrincipal]:
        return iter(self._principals)
