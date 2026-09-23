from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from knowledge.document_ir.models import DocumentSource


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


class UnsafeImagePath(ValueError):
    """An asset reference is not safe to read from the import directory."""


def resolve_image_asset(
    reference: str | Path,
    trusted_directory: Path,
    *,
    allow_absolute: bool = False,
) -> Path | None:
    """Resolve a local raster image without reading files outside the boundary.

    Parser references must be relative. Enrichment may validate the absolute
    local path emitted by an adapter, but uses its own trusted task directory.
    Remote links and missing fixture assets remain references only: no download
    or file-content read is performed here.
    """

    value = str(reference).strip()
    if not value:
        return None
    if "\x00" in value:
        raise UnsafeImagePath("image reference contains a null character")
    windows_path = PureWindowsPath(value)
    local = Path(value)
    rooted = local.is_absolute() or bool(windows_path.drive or windows_path.root)
    if rooted and not allow_absolute:
        raise UnsafeImagePath("absolute image references are not allowed")
    if not rooted:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"}:
            return None
        if parsed.scheme or parsed.netloc:
            raise UnsafeImagePath("unsupported image reference scheme")
        # Interpret both separators consistently, even on non-Windows hosts.
        local = Path(value.replace("\\", "/"))
    if local.suffix.lower() not in _IMAGE_SUFFIXES:
        raise UnsafeImagePath("image reference has an unsupported file extension")
    try:
        root = trusted_directory.resolve()
        resolved = (local if rooted else root / local).resolve()
        if not resolved.is_relative_to(root):
            raise UnsafeImagePath("image reference escapes the trusted asset directory")
        if resolved.suffix.lower() not in _IMAGE_SUFFIXES:
            raise UnsafeImagePath("image target has an unsupported file extension")
        return resolved if resolved.is_file() else None
    except (OSError, RuntimeError) as exc:
        raise UnsafeImagePath("image reference could not be safely resolved") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_source(path: Path, mime_type: str | None = None) -> DocumentSource:
    resolved = path.resolve()
    guessed = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return DocumentSource(
        uri=resolved.as_uri(),
        filename=resolved.name,
        mime_type=mime_type or guessed,
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
    )


def sha256_optional(path: Path) -> str | None:
    try:
        return sha256_file(path) if path.is_file() else None
    except OSError:
        return None
