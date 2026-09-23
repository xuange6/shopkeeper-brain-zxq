from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from knowledge.document_ir.models import DocumentIR


def save_document_ir(document: DocumentIR, path: Path) -> None:
    """Atomically save canonical, versioned JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        document.canonical_dict(), ensure_ascii=False, indent=2, sort_keys=True
    )
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_document_ir(path: Path) -> DocumentIR:
    return DocumentIR.model_validate_json(path.read_text(encoding="utf-8"))
