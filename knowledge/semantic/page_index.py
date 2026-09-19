"""轻量 PageIndex-style 层级索引。

PageIndex 的核心价值是“先导航，再读取证据”，而不是把所有文本压成
无结构 chunk。这里实现一个适合当前项目的本地适配层：索引文件是普通
JSON，旁边可选 SQLite FTS5；没有 SQLite FTS5 或中文分词能力时，会自动
退回到确定性的 Python lexical scorer，因此开发环境不需要额外服务。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]*")
_PAGE_RE = re.compile(r"(?:page|页|第)\s*([0-9]{1,5})", re.IGNORECASE)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _strip_heading(title: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", _clean(title)).strip()


def _tokens(text: str) -> List[str]:
    """Extract stable tokens for both Latin identifiers and Chinese text."""

    value = _clean(text).lower()
    result: List[str] = []
    result.extend(_WORD_RE.findall(value))
    cjk = "".join(_CJK_RE.findall(value))
    # Unigrams make short Chinese queries useful; bigrams improve precision
    # without requiring jieba or another heavyweight tokenizer.
    result.extend(list(cjk))
    result.extend(cjk[i : i + 2] for i in range(max(0, len(cjk) - 1)))
    return result


def _identifier_terms(text: str) -> List[str]:
    return re.findall(
        r"(?i)(?:[a-z]{1,12}[\-_ ]?\d+[a-z0-9\-_]*|\b\d{2,}[a-z]?\b|[a-z]{1,5}\d{1,})",
        _clean(text),
    )


def _stable_id(*parts: Any) -> str:
    raw = "|".join(_clean(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class PageIndexNode:
    node_id: str
    parent_id: str
    title: str
    level: int
    path: List[str] = field(default_factory=list)
    text: str = ""
    page_no: Optional[int] = None
    source_uri: str = ""
    file_title: str = ""
    chunk_id: str = ""
    quality_score: Optional[float] = None
    kind: str = "leaf"
    children: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PageIndexNode":
        return cls(
            node_id=_clean(value.get("node_id")),
            parent_id=_clean(value.get("parent_id")),
            title=_clean(value.get("title")),
            level=int(value.get("level") or 0),
            path=[_clean(item) for item in value.get("path") or []],
            text=_clean(value.get("text")),
            page_no=value.get("page_no"),
            source_uri=_clean(value.get("source_uri")),
            file_title=_clean(value.get("file_title")),
            chunk_id=_clean(value.get("chunk_id")),
            quality_score=value.get("quality_score"),
            kind=_clean(value.get("kind")) or "leaf",
            children=[_clean(item) for item in value.get("children") or []],
        )


def _infer_page(chunk: Dict[str, Any]) -> Optional[int]:
    for key in ("page_no", "page", "page_number"):
        value = chunk.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    match = _PAGE_RE.search(" ".join(_clean(chunk.get(key)) for key in ("title", "content")))
    return int(match.group(1)) if match else None


class PageIndex:
    """Serializable tree plus deterministic lexical retrieval."""

    schema_version = "pageindex-lite/v1"

    def __init__(
        self,
        document_id: str,
        file_title: str = "",
        version: str = "",
        nodes: Optional[Sequence[PageIndexNode]] = None,
        source_uri: str = "",
    ) -> None:
        self.document_id = _clean(document_id) or _stable_id(file_title, version)
        self.file_title = _clean(file_title)
        self.version = _clean(version)
        self.source_uri = _clean(source_uri)
        self.nodes: List[PageIndexNode] = list(nodes or [])

    @property
    def root_id(self) -> str:
        return f"{self.document_id}:root"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "file_title": self.file_title,
            "version": self.version,
            "source_uri": self.source_uri,
            "root_id": self.root_id,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "PageIndex":
        return cls(
            document_id=_clean(value.get("document_id")),
            file_title=_clean(value.get("file_title")),
            version=_clean(value.get("version")),
            source_uri=_clean(value.get("source_uri")),
            nodes=[PageIndexNode.from_dict(item) for item in value.get("nodes") or []],
        )

    def save(self, path: str | Path, create_fts: bool = True) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if create_fts:
            _write_fts(target.with_suffix(".sqlite3"), self)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "PageIndex":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def tree_summary(self, max_nodes: int = 80) -> List[Dict[str, Any]]:
        """Return compact metadata for an LLM navigation step."""

        summaries = []
        for node in self.nodes[:max_nodes]:
            summaries.append(
                {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "title": node.title,
                    "path": node.path,
                    "page_no": node.page_no,
                    "kind": node.kind,
                    "child_count": len(node.children),
                }
            )
        return summaries

    def search(self, query: str, top_k: int = 8) -> List[Dict[str, Any]]:
        query_text = _clean(query)
        query_tokens = _tokens(query_text)
        query_identifiers = {item.lower() for item in _identifier_terms(query_text)}
        if not query_tokens:
            return []

        scored: List[tuple[float, PageIndexNode]] = []
        for node in self.nodes:
            if node.kind != "leaf" or not node.text:
                continue
            title_tokens = set(_tokens(" ".join(node.path + [node.title])))
            body_tokens = _tokens(node.text)
            body_set = set(body_tokens)
            overlap = sum(1 for token in query_tokens if token in body_set)
            title_overlap = sum(1 for token in query_tokens if token in title_tokens)
            phrase = query_text.lower() in node.text.lower() or query_text.lower() in node.title.lower()
            node_identifiers = {item.lower() for item in _identifier_terms(node.text + " " + node.title)}
            identifier_hits = len(query_identifiers & node_identifiers)
            if overlap == 0 and title_overlap == 0 and not phrase and identifier_hits == 0:
                continue

            # A bounded BM25-like score. Exact identifiers and titles are
            # deliberately stronger than generic token overlap.
            doc_len = max(1, len(body_tokens))
            avg_len = 180.0
            tf = overlap / max(1, len(query_tokens))
            length_norm = 0.75 + 0.25 * min(1.0, avg_len / doc_len)
            score = 0.35 * tf * length_norm
            score += 0.12 * min(1.0, title_overlap / max(1, len(query_tokens)))
            score += 0.30 * min(1.0, identifier_hits / max(1, len(query_identifiers)))
            score += 0.20 if phrase else 0.0
            score = min(1.0, score)
            scored.append((score, node))

        scored.sort(key=lambda pair: (pair[0], pair[1].level == 0), reverse=True)
        results: List[Dict[str, Any]] = []
        for score, node in scored[: max(1, top_k)]:
            results.append(
                {
                    "source": "pageindex",
                    "retrieval_mode": "pageindex_fts",
                    "document_id": self.document_id,
                    "version": self.version,
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "page_no": node.page_no,
                    "title": node.title,
                    "parent_title": node.path[-2] if len(node.path) > 1 else (node.path[0] if node.path else ""),
                    "path": node.path,
                    "file_title": node.file_title or self.file_title,
                    "chunk_id": node.chunk_id or node.node_id,
                    "source_uri": node.source_uri or self.source_uri,
                    "quality_score": node.quality_score,
                    "score": round(score, 6),
                    "content": node.text,
                }
            )
        return results


def build_page_index(
    chunks: Iterable[Dict[str, Any]],
    document_id: str = "",
    file_title: str = "",
    version: str = "",
    source_uri: str = "",
) -> PageIndex:
    """Build a tree from existing chunks without throwing away their metadata."""

    rows = [chunk for chunk in chunks if isinstance(chunk, dict) and _clean(chunk.get("content"))]
    doc_id = _clean(document_id) or _stable_id(file_title, version, len(rows))
    index = PageIndex(doc_id, file_title=file_title, version=version, source_uri=source_uri)
    root = PageIndexNode(
        node_id=index.root_id,
        parent_id="",
        title=file_title or "Document",
        level=0,
        path=[file_title] if file_title else [],
        file_title=file_title,
        source_uri=source_uri,
        kind="root",
    )
    index.nodes.append(root)
    parent_nodes: Dict[str, PageIndexNode] = {}

    for ordinal, chunk in enumerate(rows):
        title = _strip_heading(chunk.get("title")) or f"Section {ordinal + 1}"
        parent_title = _strip_heading(chunk.get("parent_title"))
        key = parent_title or title
        parent = parent_nodes.get(key)
        if parent is None:
            parent = PageIndexNode(
                node_id=f"{doc_id}:section:{_stable_id(key)}",
                parent_id=index.root_id,
                title=parent_title or title,
                level=1,
                path=[file_title, parent_title or title] if file_title else [parent_title or title],
                file_title=_clean(chunk.get("file_title")) or file_title,
                source_uri=_clean(chunk.get("source_uri")) or source_uri,
                kind="section",
            )
            parent_nodes[key] = parent
            index.nodes.append(parent)
            root.children.append(parent.node_id)

        node_id = _clean(chunk.get("node_id")) or f"{doc_id}:leaf:{ordinal:05d}:{_stable_id(title, chunk.get('content'))}"
        path = list(parent.path)
        if title != parent.title:
            path.append(title)
        leaf = PageIndexNode(
            node_id=node_id,
            parent_id=parent.node_id,
            title=title,
            level=max(2, parent.level + 1),
            path=path,
            text=_clean(chunk.get("content")),
            page_no=_infer_page(chunk),
            source_uri=_clean(chunk.get("source_uri")) or source_uri,
            file_title=_clean(chunk.get("file_title")) or file_title,
            chunk_id=_clean(chunk.get("chunk_id")),
            quality_score=chunk.get("quality_score"),
            kind="leaf",
        )
        index.nodes.append(leaf)
        parent.children.append(leaf.node_id)

    return index


def _write_fts(path: Path, index: PageIndex) -> None:
    """Create an optional FTS5 sidecar; JSON remains the source of truth."""

    try:
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE IF EXISTS documents")
        connection.execute("DROP TABLE IF EXISTS documents_fts")
        connection.execute(
            "CREATE TABLE documents (node_id TEXT PRIMARY KEY, title TEXT, path TEXT, content TEXT, page_no INTEGER, payload TEXT)"
        )
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE documents_fts USING fts5(node_id UNINDEXED, title, path, content)"
            )
            fts = True
        except sqlite3.OperationalError:
            fts = False
        for node in index.nodes:
            if node.kind != "leaf":
                continue
            payload = json.dumps(node.to_dict(), ensure_ascii=False)
            connection.execute(
                "INSERT OR REPLACE INTO documents(node_id,title,path,content,page_no,payload) VALUES (?,?,?,?,?,?)",
                (node.node_id, node.title, " / ".join(node.path), node.text, node.page_no, payload),
            )
            if fts:
                connection.execute(
                    "INSERT INTO documents_fts(node_id,title,path,content) VALUES (?,?,?,?)",
                    (node.node_id, node.title, " / ".join(node.path), node.text),
                )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_documents_page ON documents(page_no)")
        connection.commit()
        connection.close()
    except Exception:
        # A read-only JSON index is still fully functional. Never make an
        # optional accelerator capable of breaking document ingestion.
        try:
            connection.close()
        except Exception:
            pass


def load_page_index(path: str | Path) -> PageIndex:
    return PageIndex.load(path)


def discover_index_paths(index_dir: str | Path | None = None) -> List[Path]:
    """Find semantic indexes without scanning arbitrary user directories."""

    raw = str(index_dir or os.getenv("SEMANTIC_INDEX_DIR", "")).strip()
    if not raw:
        raw = str(Path(__file__).resolve().parents[1] / "data" / "semantic_indexes")
    paths: List[Path] = []
    for part in raw.split(os.pathsep):
        root = Path(part.strip())
        if root.is_file() and root.suffix.lower() == ".json":
            paths.append(root)
        elif root.exists():
            paths.extend(sorted(root.glob("*.json")))
            # Imported task folders may keep a local sidecar.
            paths.extend(sorted(root.glob("**/semantic_index.json")))
    seen = set()
    return [path for path in paths if not (str(path.resolve()) in seen or seen.add(str(path.resolve())))]
