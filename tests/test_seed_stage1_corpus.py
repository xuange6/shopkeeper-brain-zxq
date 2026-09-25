from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from knowledge.document_ir.serialization import load_document_ir
from knowledge.processor.import_process.config import ImportConfig
from scripts import seed_stage1_corpus as seed


_CONFIG = "knowledge.processor.import_process.config.get_config"
_ENRICH_CLIENT = "knowledge.processor.import_process.nodes.document_enrich_node.get_minio_client"
_EMBED_MODULE = "knowledge.processor.import_process.nodes.bge_embedding_chunks_node"
_INDEX_MODULE = "knowledge.processor.import_process.nodes.import_milvus"


class Stage1CorpusSeedTests(unittest.TestCase):
    def _fixture(self, root: Path) -> list[str]:
        # Deliberately separate source and artifact roots: assets belong to the
        # parser directory, not the PDF directory or the serialized IR metadata.
        source = root / "manual.pdf"
        source.write_bytes(b"%PDF-1.4 synthetic adapter input")
        artifacts = root / "parser"
        artifacts.mkdir()
        (artifacts / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-test-image")
        content_list = artifacts / "content_list_v2.json"
        content_list.write_text(json.dumps([[{
            "type": "image",
            "content": {
                "image_source": {"path": "diagram.png"},
                "image_caption": ["Synthetic control panel"],
            },
        }]]), encoding="utf-8")
        return ["--source", str(source), "--content-list", str(content_list)]

    def _services(self, stack: ExitStack, *, client=None):
        config = ImportConfig(
            chunks_collection="default_collection",
            minio_bucket="synthetic-assets",
            minio_endpoint="storage.example.invalid",
        )
        stack.enter_context(patch(_CONFIG, return_value=config))
        get_client = stack.enter_context(patch(_ENRICH_CLIENT, return_value=client or Mock()))
        embed_node = Mock()
        index_node = Mock()
        embed_node.process.side_effect = lambda state: state
        index_node.process.side_effect = lambda state: state
        embed = Mock(return_value=embed_node)
        index = Mock(return_value=index_node)
        # Do not load embedding models or initialize database modules in tests.
        stack.enter_context(patch.dict("sys.modules", {
            _EMBED_MODULE: SimpleNamespace(BgeEmbeddingNode=embed),
            _INDEX_MODULE: SimpleNamespace(ImportMilvusNode=index),
        }))
        return config, get_client, embed, index

    def _run(self, argv):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(seed.main(argv), 0)
        return json.loads(output.getvalue())

    def test_default_preparation_has_no_service_side_effects(self):
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            argv = self._fixture(root)
            output = root / "prepared.ir.json"
            _, get_client, embed, index = self._services(stack)
            with patch(_CONFIG) as get_config:
                summary = self._run([*argv, "--ir-output", str(output)])
            get_config.assert_not_called()
            get_client.assert_not_called()
            embed.assert_not_called()
            index.assert_not_called()
            self.assertFalse(summary["indexed"])
            self.assertFalse(summary["asset_uploads_requested"])
            self.assertNotIn("enrichment", load_document_ir(output).metadata)

    def test_index_reuses_enrichment_but_does_not_implicitly_upload(self):
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            argv = self._fixture(root)
            existing = root / "parser" / "document.ir.json"
            existing.write_text("untouched production artifact", encoding="utf-8")
            config, get_client, embed, index = self._services(stack)
            summary = self._run([*argv, "--index", "--collection", "shadow", "--item-name", "Example"])
            get_client.assert_not_called()
            self.assertEqual(config.minio_bucket, "synthetic-assets")
            state = embed.return_value.process.call_args.args[0]
            self.assertEqual(state["document_ir"].metadata["enrichment"]["image_count"], 1)
            self.assertEqual(state["file_dir"], str((root / "parser").resolve()))
            self.assertEqual(state["chunks"][0]["item_name"], "Example")
            self.assertEqual(state["chunks"][0]["has_image"], True)
            self.assertEqual(index.call_args.kwargs["config"].chunks_collection, "shadow")
            self.assertFalse(Path(state["ir_path"]).exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "untouched production artifact")
            self.assertTrue(summary["indexed"])
            self.assertEqual(summary["uploaded_images"], 0)

    def test_explicit_asset_upload_precedes_embedding_and_persists_final_ir(self):
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            argv = self._fixture(root)
            output = root / "enriched.ir.json"
            client = Mock()
            _, get_client, embed, index = self._services(stack, client=client)

            def verify_enriched(state):
                client.fput_object.assert_called_once()
                persisted = load_document_ir(output)
                self.assertEqual(persisted.metadata["enrichment"]["uploaded_image_count"], 1)
                self.assertIn("http://storage.example.invalid/synthetic-assets/", state["chunks"][0]["content"])
                self.assertEqual(persisted.chunks[0].contextual_text, state["chunks"][0]["content"])
                return state

            embed.return_value.process.side_effect = verify_enriched
            summary = self._run([
                *argv, "--index", "--collection", "shadow", "--enrich-assets",
                "--ir-output", str(output),
            ])
            get_client.assert_called_once()
            index.return_value.process.assert_called_once()
            self.assertTrue(summary["asset_uploads_requested"])
            self.assertEqual(summary["uploaded_images"], 1)

    def test_incomplete_authorized_upload_does_not_index_partial_assets(self):
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            argv = self._fixture(Path(temporary))
            client = Mock()
            client.fput_object.side_effect = OSError("synthetic storage failure")
            _, _, embed, index = self._services(stack, client=client)
            with self.assertRaisesRegex(SystemExit, "asset enrichment incomplete"):
                seed.main([*argv, "--index", "--collection", "shadow", "--enrich-assets"])
            embed.assert_not_called()
            index.assert_not_called()

    def test_asset_upload_flag_requires_index(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            seed.parse_args(["--source", "manual.pdf", "--content-list", "content.json", "--enrich-assets"])

    def test_default_collection_is_rejected_before_preparation_or_upload(self):
        with ExitStack() as stack:
            _, get_client, embed, index = self._services(stack)
            with patch.object(seed, "prepare") as prepare, self.assertRaisesRegex(SystemExit, "current CHUNKS_COLLECTION"):
                seed.main([
                    "--source", "manual.pdf", "--content-list", "content.json",
                    "--index", "--collection", "default_collection", "--enrich-assets",
                ])
            prepare.assert_not_called()
            get_client.assert_not_called()
            embed.assert_not_called()
            index.assert_not_called()

    def test_authorized_upload_requires_configured_bucket(self):
        with ExitStack() as stack:
            config, get_client, embed, index = self._services(stack)
            config.minio_bucket = ""
            with patch.object(seed, "prepare") as prepare, self.assertRaisesRegex(SystemExit, "configured image storage bucket"):
                seed.main([
                    "--source", "manual.pdf", "--content-list", "content.json",
                    "--index", "--collection", "shadow", "--enrich-assets",
                ])
            prepare.assert_not_called()
            get_client.assert_not_called()
            embed.assert_not_called()
            index.assert_not_called()


if __name__ == "__main__":
    unittest.main()
