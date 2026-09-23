"""Deterministic asset enrichment isolated from parsing and chunking."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from knowledge.document_ir.adapters.common import UnsafeImagePath, resolve_image_asset
from knowledge.document_ir.models import DocumentIR
from knowledge.document_ir.indexing import chunks_to_index_rows
from knowledge.document_ir.serialization import save_document_ir
from knowledge.processor.import_process.base import BaseNode
from knowledge.processor.import_process.exceptions import ValidationError
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.utils.minio_util import get_minio_client


class DocumentEnrichNode(BaseNode):
    name = "document_enrich"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        raw_document = state.get("document_ir")
        if raw_document is None:
            raise ValidationError("document_ir is missing", node_name=self.name)
        document = (
            raw_document
            if isinstance(raw_document, DocumentIR)
            else DocumentIR.model_validate(raw_document)
        ).model_copy(deep=True)

        image_blocks = [block for block in document.blocks if block.image is not None]
        table_blocks = [block for block in document.blocks if block.table is not None]
        image_summaries = {
            block.id: (block.image.description or block.image.caption or block.image.ocr_text)
            for block in image_blocks
        }
        # The boundary comes from the trusted pipeline state, never from IR
        # metadata or source_uri, both of which may come from stored input.
        trusted_directory = None
        if state.get("file_dir"):
            trusted_directory = Path(state["file_dir"])
        elif state.get("import_file_path"):
            trusted_directory = Path(state["import_file_path"]).parent
        uploaded = self._upload_assets(document, image_blocks, trusted_directory)
        blocks_by_id = {block.id: block for block in document.blocks}
        for chunk in document.chunks:
            # Rebuild the deterministic pre-enrichment context on retries.
            # Otherwise the same chunk ID accumulates duplicate/stale URLs.
            chunk.contextual_text = "\n".join([*chunk.title_path, chunk.text]).strip()
            chunk.metadata.pop("images", None)
            assets = []
            for block_id in chunk.image_block_ids:
                block = blocks_by_id.get(block_id)
                if block and block.image:
                    assets.append(
                        {
                            "block_id": block.id,
                            "uri": block.image.uri,
                            "caption": block.image.caption,
                            "description": block.image.description,
                        }
                    )
            if assets:
                chunk.metadata["images"] = assets
                image_markdown = "\n".join(
                    f"![{asset['caption'] or asset['description']}]({asset['uri']})"
                    for asset in assets
                    if asset["uri"].startswith(("http://", "https://"))
                )
                if image_markdown and not chunk.contextual_text.endswith(image_markdown):
                    chunk.contextual_text = (
                        chunk.contextual_text.rstrip() + "\n\n" + image_markdown
                    )

        document.metadata["enrichment"] = {
            "name": "shopkeeper.asset_enrichment",
            "version": "1.1",
            "image_count": len(image_blocks),
            "table_count": len(table_blocks),
            "uploaded_image_count": uploaded,
        }
        state["document_ir"] = document
        state["chunks"] = chunks_to_index_rows(
            document,
            item_name=state.get("item_name", ""),
        )
        state["image_summaries"] = image_summaries
        state["image_contexts"] = [
            (
                block.id,
                block.image.local_path,
                tuple(block.title_path),
            )
            for block in image_blocks
            if block.image
        ]
        output_path = Path(state.get("ir_path") or Path(state["file_dir"]) / "document.ir.json")
        save_document_ir(document, output_path)
        return state

    def _upload_assets(
        self, document: DocumentIR, image_blocks, trusted_directory: Path | None
    ) -> int:
        # Validate every asset before obtaining a client or uploading any file.
        # This also rejects forged IR local paths and links escaping the task.
        safe_assets = []
        for block in image_blocks:
            if not block.image.local_path:
                continue
            if trusted_directory is None:
                raise ValidationError("trusted image asset directory is missing", node_name=self.name)
            try:
                path = resolve_image_asset(
                    block.image.local_path, trusted_directory, allow_absolute=True
                )
            except UnsafeImagePath as exc:
                raise ValidationError(f"unsafe image asset {block.id}: {exc}", node_name=self.name) from exc
            if path is not None:
                safe_assets.append((block, path))
        if not self.config.minio_bucket:
            return 0
        if not safe_assets:
            return 0
        try:
            client = get_minio_client()
        except Exception as exc:
            self.logger.warning("MinIO unavailable, keeping local image references: %s", exc)
            return 0
        uploaded = 0
        for block, path in safe_assets:
            suffix = path.suffix.lower()
            object_name = f"documents/{document.document_id}/assets/{block.id}{suffix}"
            try:
                client.fput_object(self.config.minio_bucket, object_name, str(path))
                block.image.uri = (
                    f"{self.config.get_minio_base_url()}/{self.config.minio_bucket}/{object_name}"
                )
                block.image.mime_type = (
                    mimetypes.guess_type(path.name)[0] or block.image.mime_type
                )
                uploaded += 1
            except Exception as exc:
                self.logger.warning("image upload failed for %s: %s", block.id, exc)
        return uploaded
