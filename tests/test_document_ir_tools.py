from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from scripts import seed_stage1_corpus


FIXTURES = Path(__file__).parent / "fixtures" / "document_ir"


@dataclass
class _FixtureConfig:
    chunks_collection: str = "current_fixture"
    minio_bucket: str = ""


@contextmanager
def _isolated_services():
    """Replace every external-service boundary before the CLI can import it."""
    config = Mock(return_value=_FixtureConfig())
    embedding = Mock()
    indexing = Mock()
    enrichment = Mock()

    def enrich(state):
        from knowledge.document_ir.indexing import chunks_to_index_rows

        document = state["document_ir"]
        document.metadata["enrichment"] = {"uploaded_image_count": 0}
        state["chunks"] = chunks_to_index_rows(document, item_name=state["item_name"])
        return state

    enrichment.return_value.process.side_effect = enrich
    embedding.return_value.process.side_effect = lambda state: state
    modules = {}
    for name, attribute, value in (
        ("knowledge.processor.import_process.config", "get_config", config),
        (
            "knowledge.processor.import_process.nodes.document_enrich_node",
            "DocumentEnrichNode", enrichment,
        ),
        (
            "knowledge.processor.import_process.nodes.bge_embedding_chunks_node",
            "BgeEmbeddingNode", embedding,
        ),
        (
            "knowledge.processor.import_process.nodes.import_milvus",
            "ImportMilvusNode", indexing,
        ),
    ):
        module = ModuleType(name)
        setattr(module, attribute, value)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        yield config, embedding, indexing


class DocumentIRToolTests(unittest.TestCase):
    def _arguments(self) -> list[str]:
        return [
            "--source", str(FIXTURES / "complex_layout.pdf"),
            "--content-list", str(FIXTURES / "complex_mineru_content_list_v2.json"),
        ]

    def test_seed_requires_explicit_source_and_content_list(self) -> None:
        for arguments in ([], ["--source", "sample.pdf"], ["--content-list", "sample.json"]):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    seed_stage1_corpus.parse_args(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_seed_without_middle_is_local_only_and_prints_summary(self) -> None:
        output = StringIO()
        with (
            _isolated_services() as (config, embedding, indexing),
            patch.object(seed_stage1_corpus, "prepare", wraps=seed_stage1_corpus.prepare) as prepare,
            redirect_stdout(output),
        ):
            result = seed_stage1_corpus.main(self._arguments())
        self.assertEqual(result, 0)
        self.assertIsNone(prepare.call_args.args[2])
        self.assertIsNone(prepare.call_args.kwargs["logical_document_key"])
        summary = json.loads(output.getvalue())
        self.assertFalse(summary["indexed"])
        self.assertEqual(summary["collection"], "")
        self.assertGreater(summary["chunks"], 0)
        self.assertEqual(summary["grounded_chunks"], summary["chunks"])
        config.assert_not_called()
        embedding.assert_not_called()
        indexing.assert_not_called()

    def test_distinct_sources_do_not_share_an_implicit_document_identity(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            first = directory / "first.pdf"
            second = directory / "second.pdf"
            content = (FIXTURES / "complex_layout.pdf").read_bytes()
            first.write_bytes(content)
            second.write_bytes(content + b"\n% synthetic revision\n")
            documents = [
                seed_stage1_corpus.prepare(
                    source, FIXTURES / "complex_mineru_content_list_v2.json"
                )
                for source in (first, second)
            ]
        self.assertNotEqual(documents[0].document_id, documents[1].document_id)
        self.assertNotEqual(documents[0].revision_id, documents[1].revision_id)

    def test_explicit_document_key_is_forwarded_to_the_adapter(self) -> None:
        output = StringIO()
        with (
            _isolated_services(),
            patch.object(seed_stage1_corpus, "prepare", wraps=seed_stage1_corpus.prepare) as prepare,
            redirect_stdout(output),
        ):
            seed_stage1_corpus.main(
                self._arguments() + ["--document-key", "fixture/example-manual"]
            )
        self.assertEqual(
            prepare.call_args.kwargs["logical_document_key"], "fixture/example-manual"
        )
        expected = seed_stage1_corpus.prepare(
            FIXTURES / "complex_layout.pdf",
            FIXTURES / "complex_mineru_content_list_v2.json",
            logical_document_key="fixture/example-manual",
        )
        self.assertEqual(expected.logical_document_key, "fixture/example-manual")
        self.assertEqual(json.loads(output.getvalue())["document_id"], expected.document_id)

    def test_index_without_collection_fails_before_preparing_or_embedding(self) -> None:
        with (
            _isolated_services() as (config, embedding, indexing),
            patch.object(seed_stage1_corpus, "prepare") as prepare,
            redirect_stderr(StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            seed_stage1_corpus.main(self._arguments() + ["--index"])
        self.assertEqual(raised.exception.code, 2)
        prepare.assert_not_called()
        config.assert_not_called()
        embedding.assert_not_called()
        indexing.assert_not_called()

    def test_current_collection_is_rejected_before_preparing_or_embedding(self) -> None:
        with (
            _isolated_services() as (config, embedding, indexing),
            patch.object(seed_stage1_corpus, "prepare") as prepare,
            self.assertRaisesRegex(SystemExit, "refusing to modify the current"),
        ):
            seed_stage1_corpus.main(
                self._arguments() + ["--index", "--collection", "current_fixture"]
            )
        config.assert_called_once_with()
        prepare.assert_not_called()
        embedding.assert_not_called()
        indexing.assert_not_called()

    def test_shadow_index_uses_explicit_label_and_collection(self) -> None:
        output = StringIO()
        with (
            _isolated_services() as (config, embedding, indexing),
            redirect_stdout(output),
        ):
            result = seed_stage1_corpus.main(self._arguments() + [
                "--index", "--collection", "shadow_fixture",
                "--item-name", "Example Device",
            ])
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())["indexed"])
        config.assert_called_once_with()
        self.assertEqual(embedding.call_args.kwargs["config"].chunks_collection, "shadow_fixture")
        self.assertEqual(indexing.call_args.kwargs["config"].chunks_collection, "shadow_fixture")
        state = indexing.return_value.process.call_args.args[0]
        self.assertTrue(state["chunks"])
        self.assertTrue(all(chunk["item_name"] == "Example Device" for chunk in state["chunks"]))
        embedding.return_value.process.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
