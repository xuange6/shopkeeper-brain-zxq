from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from knowledge.document_ir.adapters import MarkdownAdapter, MinerUAdapter
from knowledge.document_ir.adapters.common import UnsafeImagePath
from knowledge.document_ir.chunking import chunk_document
from knowledge.document_ir.normalize import normalize_document
from knowledge.processor.import_process.config import ImportConfig
from knowledge.processor.import_process.exceptions import ValidationError
from knowledge.processor.import_process.nodes.document_enrich_node import DocumentEnrichNode


_ENRICH_CLIENT = "knowledge.processor.import_process.nodes.document_enrich_node.get_minio_client"
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nsynthetic-test-image"


class DocumentIRAssetSafetyTests(unittest.TestCase):
    def _markdown(self, root: Path, reference: str):
        source = root / "manual.md"
        source.write_text(f"# Example\n\n![Diagram]({reference})\n", encoding="utf-8")
        return source

    def _mineru(self, root: Path, reference: str):
        source = root / "manual.pdf"
        source.write_bytes(b"%PDF-1.4 synthetic adapter fixture")
        artifact = root / "manual_content_list_v2.json"
        artifact.write_text(json.dumps([[{
            "type": "image",
            "content": {
                "image_source": {"path": reference},
                "image_caption": ["Synthetic diagram"],
            },
        }]]), encoding="utf-8")
        return source, artifact

    def test_markdown_rejects_traversal_before_asset_hashing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "import"
            root.mkdir()
            (root.parent / "outside.png").write_bytes(_IMAGE_BYTES)
            for reference in ("../outside.png", "..\\outside.png"):
                with self.subTest(reference=reference):
                    source = self._markdown(root, reference)
                    with patch("knowledge.document_ir.adapters.markdown.sha256_optional") as asset_hash:
                        with self.assertRaises(UnsafeImagePath):
                            MarkdownAdapter().convert(source)
                        asset_hash.assert_not_called()

    def test_adapters_reject_absolute_paths_before_asset_hashing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image.png"
            image.write_bytes(_IMAGE_BYTES)
            for reference in (image.as_posix(), "C:/outside/image.png", "//server/share/image.png"):
                with self.subTest(reference=reference):
                    source = self._markdown(root, reference)
                    with patch("knowledge.document_ir.adapters.markdown.sha256_optional") as asset_hash:
                        with self.assertRaises(UnsafeImagePath):
                            MarkdownAdapter().convert(source)
                        asset_hash.assert_not_called()
                    pdf, artifact = self._mineru(root, reference)
                    with patch("knowledge.document_ir.adapters.mineru.sha256_optional") as asset_hash:
                        with self.assertRaises(UnsafeImagePath):
                            MinerUAdapter(parser_version="fixture").convert(
                                source_path=pdf, content_list_path=artifact
                            )
                        asset_hash.assert_not_called()

    def test_mineru_rejects_traversal_before_asset_hashing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "artifacts"
            root.mkdir()
            (root.parent / "outside.png").write_bytes(_IMAGE_BYTES)
            source, artifact = self._mineru(root, "../outside.png")
            with patch("knowledge.document_ir.adapters.mineru.sha256_optional") as asset_hash:
                with self.assertRaises(UnsafeImagePath):
                    MinerUAdapter(parser_version="fixture").convert(
                        source_path=source, content_list_path=artifact
                    )
                asset_hash.assert_not_called()

    def test_non_image_extensions_and_file_urls_are_not_read(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.txt").write_text("synthetic-only", encoding="utf-8")
            for reference in ("settings.txt", "file:///outside/image.png", "diagram.svg"):
                with self.subTest(reference=reference):
                    source = self._markdown(root, reference)
                    with patch("knowledge.document_ir.adapters.markdown.sha256_optional") as asset_hash:
                        with self.assertRaises(UnsafeImagePath):
                            MarkdownAdapter().convert(source)
                        asset_hash.assert_not_called()

    def test_resolved_link_escape_is_rejected_without_reading_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "import"
            root.mkdir()
            outside = root.parent / "outside.png"
            outside.write_bytes(_IMAGE_BYTES)
            link = root / "link.png"
            source = self._markdown(root, "link.png")
            original_resolve = Path.resolve

            def resolve_link(path, *args, **kwargs):
                # Exercise the resolved-link boundary on Windows hosts where
                # creating a real symlink requires additional OS privileges.
                if path == link:
                    return outside
                return original_resolve(path, *args, **kwargs)

            try:
                link.symlink_to(outside)
                resolved_link = nullcontext()
            except OSError:
                resolved_link = patch.object(Path, "resolve", resolve_link)
            with resolved_link, patch(
                "knowledge.document_ir.adapters.markdown.sha256_optional"
            ) as asset_hash:
                with self.assertRaises(UnsafeImagePath):
                    MarkdownAdapter().convert(source)
                asset_hash.assert_not_called()

    def test_relative_images_in_both_adapters_remain_hashable(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "images"
            assets.mkdir()
            image = assets / "diagram.png"
            image.write_bytes(_IMAGE_BYTES)
            source = self._markdown(root, "images/diagram.png")
            markdown = MarkdownAdapter().convert(source)
            pdf, artifact = self._mineru(root, "images/diagram.png")
            mineru = MinerUAdapter(parser_version="fixture").convert(
                source_path=pdf, content_list_path=artifact
            )
            markdown_image = next(block.image for block in markdown.blocks if block.image)
            mineru_image = next(block.image for block in mineru.blocks if block.image)
            self.assertEqual(markdown_image.local_path, str(image.resolve()))
            self.assertEqual(mineru_image.local_path, str(image.resolve()))
            self.assertTrue(markdown_image.sha256)
            self.assertEqual(markdown_image.sha256, mineru_image.sha256)

    def test_missing_and_remote_images_remain_references_without_reads(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for reference in ("missing.png", "https://example.invalid/diagram.png?version=1"):
                with self.subTest(reference=reference):
                    source = self._markdown(root, reference)
                    with patch("knowledge.document_ir.adapters.markdown.sha256_optional") as asset_hash:
                        document = MarkdownAdapter().convert(source)
                        asset_hash.assert_not_called()
                    image = next(block.image for block in document.blocks if block.image)
                    self.assertEqual(image.uri, reference)
                    self.assertEqual(image.local_path, "")
                    self.assertIsNone(image.sha256)
                    pdf, artifact = self._mineru(root, reference)
                    with patch("knowledge.document_ir.adapters.mineru.sha256_optional") as asset_hash:
                        document = MinerUAdapter(parser_version="fixture").convert(
                            source_path=pdf, content_list_path=artifact
                        )
                        asset_hash.assert_not_called()
                    self.assertEqual(document.blocks[0].image.local_path, "")

    def test_enrichment_rechecks_forged_ir_before_any_upload(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "import"
            root.mkdir()
            (root / "diagram.png").write_bytes(_IMAGE_BYTES)
            outside = root.parent / "outside.png"
            outside.write_bytes(_IMAGE_BYTES)
            source = self._markdown(root, "diagram.png")
            source.write_text(source.read_text(encoding="utf-8") + "\n![Other](diagram.png)\n", encoding="utf-8")
            document = MarkdownAdapter().convert(source)
            image = [block.image for block in document.blocks if block.image][-1]
            image.local_path = str(outside)
            # IR fields must not supply or expand the trusted directory.
            document.source.uri = outside.as_uri()
            document.metadata["content_list_path"] = str(outside)
            node = DocumentEnrichNode(config=ImportConfig(minio_bucket="test-assets"))
            with patch(_ENRICH_CLIENT) as get_client:
                with self.assertRaisesRegex(ValidationError, "unsafe image asset"):
                    node.process({"document_ir": document, "file_dir": str(root)})
                get_client.assert_not_called()

    def test_enrichment_rechecks_a_link_changed_after_parsing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "import"
            root.mkdir()
            image = root / "diagram.png"
            image.write_bytes(_IMAGE_BYTES)
            outside = root.parent / "outside.png"
            outside.write_bytes(_IMAGE_BYTES)
            source = self._markdown(root, "diagram.png")
            document = MarkdownAdapter().convert(source)
            original_resolve = Path.resolve

            def resolve_changed_link(path, *args, **kwargs):
                if path == image:
                    return outside
                return original_resolve(path, *args, **kwargs)

            node = DocumentEnrichNode(config=ImportConfig(minio_bucket="test-assets"))
            with patch.object(Path, "resolve", resolve_changed_link), patch(_ENRICH_CLIENT) as get_client:
                with self.assertRaisesRegex(ValidationError, "escapes the trusted asset directory"):
                    node.process({"document_ir": document, "file_dir": str(root)})
                get_client.assert_not_called()

    def test_enrichment_rejects_non_image_and_missing_trusted_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._markdown(root, "missing.png")
            document = MarkdownAdapter().convert(source)
            image = next(block.image for block in document.blocks if block.image)
            image.local_path = str(root / "settings.txt")
            (root / "settings.txt").write_text("synthetic-only", encoding="utf-8")
            node = DocumentEnrichNode(config=ImportConfig(minio_bucket="test-assets"))
            with patch(_ENRICH_CLIENT) as get_client:
                with self.assertRaisesRegex(ValidationError, "unsupported file extension"):
                    node.process({"document_ir": document, "file_dir": str(root)})
                with self.assertRaisesRegex(ValidationError, "trusted image asset directory is missing"):
                    node.process({"document_ir": document})
                get_client.assert_not_called()

    def test_enrichment_uploads_only_validated_local_images(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "diagram.png"
            image.write_bytes(_IMAGE_BYTES)
            source = self._markdown(root, "diagram.png")
            document = chunk_document(normalize_document(MarkdownAdapter().convert(source)))
            client = Mock()
            node = DocumentEnrichNode(config=ImportConfig(
                minio_bucket="test-assets", minio_endpoint="storage.example.invalid"
            ))
            with patch(_ENRICH_CLIENT, return_value=client):
                result = node.process({"document_ir": document, "file_dir": str(root)})
            client.fput_object.assert_called_once()
            self.assertEqual(client.fput_object.call_args.args[2], str(image.resolve()))
            self.assertEqual(result["document_ir"].metadata["enrichment"]["uploaded_image_count"], 1)


if __name__ == "__main__":
    unittest.main()
