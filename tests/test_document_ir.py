from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pymilvus import DataType

from knowledge.document_ir.adapters import MarkdownAdapter, MinerUAdapter
from knowledge.document_ir.chunking import ChunkingConfig, chunk_document
from knowledge.document_ir.citations import build_chunk_citation
from knowledge.document_ir.diff import diff_documents
from knowledge.document_ir.indexing import chunks_to_index_rows
from knowledge.document_ir.lineage import align_document
from knowledge.document_ir.models import BlockType, ParseStatus
from knowledge.document_ir.models import DocumentIR
from knowledge.document_ir.normalize import normalize_document
from knowledge.document_ir.policy import ParseAction, action_for_status
from knowledge.document_ir.revision_guard import assess_revision_continuity
from knowledge.processor.import_process.config import ImportConfig
from knowledge.processor.import_process.nodes.import_milvus import ImportMilvusNode
from knowledge.processor.import_process.nodes.item_name_recognition_load import ItemNameRecognitionNode
from knowledge.processor.import_process.nodes.document_parse_node import DocumentParseNode
from knowledge.processor.import_process.nodes.document_normalize_node import DocumentNormalizeNode
from knowledge.processor.import_process.nodes.document_spliter_node import DocumentSplitNode
from knowledge.processor.import_process.nodes.document_enrich_node import DocumentEnrichNode
from knowledge.processor.import_process.exceptions import FileProcessingError, MilvusError, ValidationError
from knowledge.document_ir.serialization import load_document_ir, save_document_ir
from knowledge.service.file_import_service import FileImportService
from knowledge.utils.query_result_utils import build_source_references


FIXTURES = Path(__file__).parent / "fixtures" / "document_ir"


class _FakeMilvus:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.delete_filters: list[str] = []
        self.next_pk = 1

    def has_collection(self, collection_name: str) -> bool:
        return True

    def describe_collection(self, collection_name: str):
        return {
            "fields": [
                {"name": "pk", "is_primary": True, "auto_id": True},
                {"name": "chunk_id", "is_primary": False, "auto_id": False},
            ]
        }

    def delete(self, collection_name: str, filter: str):
        self.delete_filters.append(filter)
        document_id = json.loads(filter.split("==", 1)[1].strip())
        self.rows = [row for row in self.rows if row.get("document_id") != document_id]
        return {"delete_count": 0}

    def insert(self, collection_name: str, data: list[dict]):
        ids = list(range(self.next_pk, self.next_pk + len(data)))
        self.next_pk += len(data)
        self.rows.extend(data)
        return {"insert_count": len(data), "ids": ids}


class _FakeSchemaMilvus(_FakeMilvus):
    def __init__(self) -> None:
        super().__init__()
        self.created = False
        self.schema_fields: list[dict] = []

    def has_collection(self, collection_name: str) -> bool:
        return self.created

    def create_schema(self, enable_dynamic_fields: bool):
        return self

    def add_field(self, **kwargs):
        self.schema_fields.append(kwargs)

    def prepare_index_params(self):
        return self

    def add_index(self, **kwargs):
        pass

    def create_collection(self, **kwargs):
        self.created = True


class DocumentIRTests(unittest.TestCase):
    def _mineru_document(self, directory: Path):
        source = directory / "manual.pdf"
        source.write_bytes((FIXTURES / "complex_layout.pdf").read_bytes())
        document = MinerUAdapter(parser_version="fixture-3.1").convert(
            source_path=source,
            content_list_path=FIXTURES / "complex_mineru_content_list_v2.json",
            middle_path=FIXTURES / "complex_mineru_middle.json",
        )
        return chunk_document(
            normalize_document(document),
            ChunkingConfig(max_characters=1200, min_characters=300),
        )

    def test_mineru_fixture_matches_golden_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))

        tables = [block for block in document.blocks if block.type == BlockType.TABLE]
        images = [block for block in document.blocks if block.type == BlockType.IMAGE]
        snapshot = {
            "block_count": len(document.blocks),
            "block_types": [block.type.value for block in document.blocks],
            "chunk_count": len(document.chunks),
            "document_id": "doc_" if document.document_id.startswith("doc_") else "",
            "image_pages": [block.provenance[0].page_number for block in images],
            "ocr_texts": [block.image.ocr_text for block in images],
            "parser": document.parser.name,
            "schema_version": document.schema_version,
            "section_paths": [section.title_path for section in document.sections],
            "status": document.status.value,
            "table_pages": [block.provenance[0].page_number for block in tables],
            "tables_linked": bool(
                tables[0].table.continues_to_block_id
                and tables[1].table.continues_from_block_id == tables[0].id
            ),
        }
        golden = json.loads(
            (FIXTURES / "complex_mineru.golden.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot, golden)

    def test_repeated_import_has_stable_document_block_and_chunk_ids(self) -> None:
        with TemporaryDirectory() as first_dir, TemporaryDirectory() as second_dir:
            first = self._mineru_document(Path(first_dir))
            second = self._mineru_document(Path(second_dir))

        self.assertEqual(first.document_id, second.document_id)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual([item.id for item in first.sections], [item.id for item in second.sections])
        self.assertEqual([item.id for item in first.blocks], [item.id for item in second.blocks])
        self.assertEqual([item.id for item in first.chunks], [item.id for item in second.chunks])

    def test_inserted_pdf_page_preserves_old_page_and_block_lineage(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            old_pdf = directory / "v1.pdf"
            new_pdf = directory / "v2.pdf"
            original_bytes = (FIXTURES / "complex_layout.pdf").read_bytes()
            old_pdf.write_bytes(original_bytes)
            new_pdf.write_bytes(original_bytes + b"\n% revision with inserted preface page\n")
            content = json.loads(
                (FIXTURES / "complex_mineru_content_list_v2.json").read_text(encoding="utf-8")
            )
            revised_content = directory / "v2_content_list_v2.json"
            revised_content.write_text(
                json.dumps(
                    [[{
                        "type": "paragraph",
                        "content": {"paragraph_content": [{"type": "text", "content": "新增前言页。"}]},
                        "bbox": [50, 100, 900, 200],
                    }], *content],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            adapter = MinerUAdapter(parser_version="fixture-3.1")
            before = normalize_document(
                adapter.convert(
                    source_path=old_pdf,
                    content_list_path=FIXTURES / "complex_mineru_content_list_v2.json",
                    logical_document_key="fixture/manual-a",
                )
            )
            after = normalize_document(
                adapter.convert(
                    source_path=new_pdf,
                    content_list_path=revised_content,
                    logical_document_key="fixture/manual-a",
                )
            )
            aligned = align_document(before, after)
            chunked = chunk_document(aligned)

        self.assertEqual(before.document_id, chunked.document_id)
        self.assertNotEqual(before.revision_id, chunked.revision_id)
        old_block = next(block for block in before.blocks if block.text == "左栏第一段。")
        new_block = next(block for block in chunked.blocks if block.text == "左栏第一段。")
        self.assertNotEqual(old_block.id, new_block.id)
        self.assertEqual(old_block.lineage_id, new_block.lineage_id)
        self.assertEqual(old_block.provenance[0].page_number, 1)
        self.assertEqual(new_block.provenance[0].page_number, 2)
        self.assertEqual(old_block.provenance[0].page_uid, new_block.provenance[0].page_uid)
        difference = diff_documents(before, chunked)
        self.assertTrue(difference.same_document)
        self.assertEqual(len(difference.page_moves), 4)
        self.assertTrue(any(move.before_number == 1 and move.after_number == 2 for move in difference.page_moves))
        citation = build_chunk_citation(
            chunked, next(chunk for chunk in chunked.chunks if new_block.id in chunk.block_ids)
        )
        self.assertIn(2, citation["page_numbers"])
        self.assertIn(new_block.provenance[0].page_uid, citation["page_uids"])
        self.assertIn(new_block.lineage_id, citation["block_lineage_ids"])

    def test_chunk_count_may_change_without_changing_block_lineage(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "long.md"
            source.write_text(
                "# Long section\n\n" + "This sentence is intentionally long. " * 50,
                encoding="utf-8",
            )
            document = normalize_document(MarkdownAdapter().convert(source))
        wide = chunk_document(
            document, ChunkingConfig(max_characters=1200, min_characters=0)
        )
        narrow = chunk_document(
            document, ChunkingConfig(max_characters=40, min_characters=0)
        )
        self.assertNotEqual(len(wide.chunks), len(narrow.chunks))
        self.assertTrue(set(chunk.id for chunk in wide.chunks).isdisjoint(
            chunk.id for chunk in narrow.chunks
        ))
        self.assertEqual(
            [block.lineage_id for block in wide.blocks],
            [block.lineage_id for block in narrow.blocks],
        )

    def test_revised_markdown_requires_explicit_logical_document_key(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            first_path = directory / "v1.md"
            second_path = directory / "v2.md"
            first_path.write_text("# Guide\n\nKeep this paragraph.\n", encoding="utf-8")
            second_path.write_text(
                "# Guide\n\nNew introductory paragraph.\n\nKeep this paragraph.\n",
                encoding="utf-8",
            )
            without_key = [
                normalize_document(MarkdownAdapter().convert(path))
                for path in (first_path, second_path)
            ]
            self.assertNotEqual(without_key[0].document_id, without_key[1].document_id)
            with self.assertRaises(ValueError):
                align_document(*without_key)
            before, after = [
                normalize_document(
                    MarkdownAdapter().convert(path, logical_document_key="manual/guide")
                )
                for path in (first_path, second_path)
            ]
            aligned = align_document(before, after)

        self.assertEqual(before.document_id, aligned.document_id)
        self.assertNotEqual(before.revision_id, aligned.revision_id)
        old_heading = next(block for block in before.blocks if block.text == "Guide")
        new_heading = next(block for block in aligned.blocks if block.text == "Guide")
        self.assertEqual(old_heading.lineage_id, new_heading.lineage_id)
        self.assertTrue(diff_documents(before, aligned).same_document)

    def test_changed_block_keeps_lineage_but_gets_a_new_content_id(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            first_path = directory / "v1.md"
            second_path = directory / "v2.md"
            first_path.write_text("# Guide\n\nRated operating voltage is 220 volts.\n", encoding="utf-8")
            second_path.write_text("# Guide\n\nRated operating voltage is 230 volts.\n", encoding="utf-8")
            before, after = [
                normalize_document(
                    MarkdownAdapter().convert(path, logical_document_key="fixture/voltage")
                )
                for path in (first_path, second_path)
            ]
            aligned = align_document(before, after)

        old = next(block for block in before.blocks if "voltage" in block.text)
        new = next(block for block in aligned.blocks if "voltage" in block.text)
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(old.lineage_id, new.lineage_id)
        difference = diff_documents(before, aligned)
        self.assertEqual(len(difference.modified_blocks), 1)
        self.assertEqual(difference.modified_blocks[0].anchor, old.lineage_id)

    def test_revision_guard_accepts_small_edit_and_rejects_same_key_collision(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            paths = [directory / name / "guide.md" for name in ("old", "minor", "different")]
            for path in paths:
                path.parent.mkdir()
            paths[0].write_text(
                "# Product Manual\n\nThe pump operating pressure is 220 PSI and temperature limit is 80 C.\n",
                encoding="utf-8",
            )
            paths[1].write_text(
                "# Product Manual\n\nThe pump operating pressure is 230 PSI and temperature limit is 80 C.\n",
                encoding="utf-8",
            )
            paths[2].write_text(
                "# Product Manual\n\nWarranty payments and reseller account schedules are explained here.\n",
                encoding="utf-8",
            )
            documents = [
                normalize_document(
                    MarkdownAdapter().convert(path, logical_document_key="fixture/product-a")
                )
                for path in paths
            ]
            without_key = [normalize_document(MarkdownAdapter().convert(path)) for path in paths]

        minor = assess_revision_continuity(documents[0], documents[1])
        collision = assess_revision_continuity(documents[0], documents[2])
        self.assertTrue(minor.accepted)
        self.assertFalse(collision.accepted)
        self.assertEqual(documents[0].document_id, documents[2].document_id)
        self.assertNotEqual(without_key[0].document_id, without_key[2].document_id)
        self.assertLess(collision.overlap, collision.minimum_overlap)

    def test_import_rejects_major_same_key_change_before_indexing(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            old_dir = directory / "old"
            new_dir = directory / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            old_path = old_dir / "guide.md"
            new_path = new_dir / "guide.md"
            old_path.write_text(
                "# Shared Title\n\nThe compressor pressure is 220 PSI with a ceramic valve.\n",
                encoding="utf-8",
            )
            new_path.write_text(
                "# Shared Title\n\nThe reseller contract lists payment dates and legal conditions.\n",
                encoding="utf-8",
            )
            previous = normalize_document(
                MarkdownAdapter().convert(old_path, logical_document_key="fixture/shared")
            )
            previous_ir = old_dir / "document.ir.json"
            save_document_ir(previous, previous_ir)
            state = {
                "import_file_path": str(new_path),
                "file_dir": str(new_dir),
                "is_md_read_enabled": True,
                "logical_document_key": "fixture/shared",
                "previous_ir_path": str(previous_ir),
            }
            config = ImportConfig(minio_bucket="")
            DocumentParseNode(config=config).process(state)
            with self.assertRaisesRegex(ValidationError, "已停止索引替换"):
                DocumentNormalizeNode(config=config).process(state)

            rejected = load_document_ir(new_dir / "document.ir.json")
            self.assertEqual(rejected.status, ParseStatus.REVIEW_REQUIRED)
            self.assertEqual(rejected.errors[-1].code, "possible_document_key_collision")
            self.assertFalse(rejected.chunks)
            self.assertEqual(load_document_ir(previous_ir).revision_id, previous.revision_id)

    def test_revision_guard_same_source_bytes_are_repeatable(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "guide.md"
            source.write_text("# Guide\n\nSame body text.\n", encoding="utf-8")
            old = normalize_document(
                MarkdownAdapter().convert(source, logical_document_key="fixture/guide")
            )
            incoming = normalize_document(
                MarkdownAdapter().convert(source, logical_document_key="fixture/guide")
            )
        result = assess_revision_continuity(old, incoming)
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "same_source_bytes")

    def test_revision_guard_accepts_one_character_change_in_short_chinese_body(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            old_path = directory / "v1.md"
            new_path = directory / "v2.md"
            old_path.write_text("# 设备说明\n\n额定电压为220伏。\n", encoding="utf-8")
            new_path.write_text("# 设备说明\n\n额定电压为230伏。\n", encoding="utf-8")
            old, new = [
                normalize_document(
                    MarkdownAdapter().convert(path, logical_document_key="fixture/device")
                )
                for path in (old_path, new_path)
            ]
        result = assess_revision_continuity(old, new)
        self.assertTrue(result.accepted)
        self.assertEqual(result.method, "short-body-sequence-v1")

    def test_ambiguous_duplicate_pages_do_not_claim_same_page_uid(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            first = directory / "v1.pdf"
            second = directory / "v2.pdf"
            original_bytes = (FIXTURES / "complex_layout.pdf").read_bytes()
            first.write_bytes(original_bytes)
            second.write_bytes(original_bytes + b"\n% duplicate-page revision\n")
            fixture = json.loads(
                (FIXTURES / "complex_mineru_content_list_v2.json").read_text(encoding="utf-8")
            )
            old_artifact = directory / "old.json"
            new_artifact = directory / "new.json"
            old_artifact.write_text(json.dumps([fixture[0], fixture[0]], ensure_ascii=False), encoding="utf-8")
            new_artifact.write_text(
                json.dumps([
                    [{
                        "type": "paragraph",
                        "content": {"paragraph_content": [{"type": "text", "content": "New preface."}]},
                        "bbox": [10, 20, 30, 40],
                    }],
                    fixture[0], fixture[0],
                ], ensure_ascii=False),
                encoding="utf-8",
            )
            adapter = MinerUAdapter(parser_version="fixture-3.1")
            before = normalize_document(adapter.convert(
                source_path=first, content_list_path=old_artifact,
                logical_document_key="fixture/duplicates",
            ))
            after = normalize_document(adapter.convert(
                source_path=second, content_list_path=new_artifact,
                logical_document_key="fixture/duplicates",
            ))
            aligned = align_document(before, after)

        self.assertEqual(aligned.metadata["alignment"]["matched_pages"], {})
        old_uids = {p.page_uid for b in before.blocks for p in b.provenance if p.page_uid}
        new_uids = {p.page_uid for b in aligned.blocks for p in b.provenance if p.page_uid}
        self.assertFalse(old_uids.intersection(new_uids))

    def test_changed_linked_image_changes_revision_and_block_but_not_lineage(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            source = directory / "guide.md"
            image = directory / "panel.png"
            source.write_text("# Guide\n\n![Panel](panel.png)\n", encoding="utf-8")
            image.write_bytes(b"image revision one")
            before = normalize_document(MarkdownAdapter().convert(source))
            image.write_bytes(b"image revision two")
            after = normalize_document(MarkdownAdapter().convert(source))
            aligned = align_document(before, after)

        old = next(block for block in before.blocks if block.image)
        new = next(block for block in aligned.blocks if block.image)
        self.assertEqual(before.document_id, aligned.document_id)
        self.assertNotEqual(before.revision_id, aligned.revision_id)
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(old.lineage_id, new.lineage_id)
        difference = diff_documents(before, aligned)
        self.assertEqual(len(difference.modified_blocks), 1)
        self.assertNotEqual(
            difference.modified_blocks[0].before_asset_sha256,
            difference.modified_blocks[0].after_asset_sha256,
        )

    def test_document_diff_explains_changed_table_cell(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            before = self._mineru_document(directory)
            changed_artifact = directory / "changed.json"
            changed_artifact.write_text(
                (FIXTURES / "complex_mineru_content_list_v2.json")
                .read_text(encoding="utf-8")
                .replace("80 g/m²", "90 g/m²"),
                encoding="utf-8",
            )
            source = directory / "manual.pdf"
            after = chunk_document(
                normalize_document(
                    MinerUAdapter(parser_version="fixture-3.1").convert(
                        source_path=source,
                        content_list_path=changed_artifact,
                        middle_path=FIXTURES / "complex_mineru_middle.json",
                    )
                )
            )

        difference = diff_documents(before, after)
        self.assertTrue(difference.same_document)
        self.assertEqual(len(difference.modified_blocks), 1)
        self.assertIn("80 g/m²", difference.modified_blocks[0].before_text)
        self.assertIn("90 g/m²", difference.modified_blocks[0].after_text)

    def test_markdown_adapter_preserves_ranges_tables_images_and_code(self) -> None:
        document = chunk_document(
            normalize_document(MarkdownAdapter().convert(FIXTURES / "complex.md"))
        )
        types = {block.type for block in document.blocks}
        self.assertTrue(
            {BlockType.TABLE, BlockType.IMAGE, BlockType.CODE}.issubset(types)
        )
        for block in document.blocks:
            self.assertIsNotNone(block.raw_range)
            source_slice = document.raw_text[block.raw_range.start : block.raw_range.end]
            self.assertTrue(source_slice.strip())
        table = next(block for block in document.blocks if block.type == BlockType.TABLE)
        self.assertEqual(len(table.table.cells), 6)

    def test_citation_round_trip_reaches_source_page_and_blocks(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        chunk = next(chunk for chunk in document.chunks if chunk.table_block_ids)
        citation = build_chunk_citation(document, chunk)
        self.assertEqual(citation["source_uri"], document.source.uri)
        self.assertEqual(citation["page_numbers"], [2])
        self.assertEqual(citation["block_ids"], chunk.block_ids)
        self.assertTrue(citation["locations"][0]["bbox"])

        row = chunks_to_index_rows(document)[document.chunks.index(chunk)]
        sources = build_source_references([{**row, "score": 0.9}])
        self.assertEqual(sources[0]["chunk_id"], chunk.id)
        self.assertEqual(sources[0]["page_numbers"], [2])
        self.assertEqual(sources[0]["block_ids"], chunk.block_ids)

    def test_invalid_artifact_and_status_policy_are_explicit(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            source = Path(temporary_dir) / "bad.pdf"
            source.write_bytes(b"bad")
            with self.assertRaises(json.JSONDecodeError):
                MinerUAdapter(parser_version="fixture").convert(
                    source_path=source,
                    content_list_path=FIXTURES / "malformed_mineru_content_list_v2.json",
                )

        self.assertEqual(
            action_for_status(ParseStatus.FAILED, attempt=1, max_attempts=2),
            ParseAction.RETRY,
        )
        self.assertEqual(
            action_for_status(ParseStatus.FAILED, attempt=2, max_attempts=2),
            ParseAction.QUARANTINE,
        )
        self.assertEqual(
            action_for_status(ParseStatus.REVIEW_REQUIRED), ParseAction.MANUAL_REVIEW
        )

    def test_failed_parser_writes_quarantine_manifest_after_retry_budget(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            source = directory / "bad.pdf"
            source.write_bytes(b"bad")
            state = {
                "import_file_path": str(source),
                "file_dir": str(directory),
                "md_path": str(directory / "bad.md"),
                "is_pdf_read_enabled": True,
            }
            with (
                patch.dict("os.environ", {"DOCUMENT_PARSE_MAX_ATTEMPTS": "1"}),
                patch.object(
                    MinerUAdapter,
                    "find_artifacts",
                    return_value=(
                        FIXTURES / "malformed_mineru_content_list_v2.json",
                        None,
                    ),
                ),
                self.assertRaises(FileProcessingError),
            ):
                DocumentParseNode().process(state)

            manifest = load_document_ir(directory / "document.ir.json")
            self.assertEqual(manifest.status, ParseStatus.QUARANTINED)
            self.assertEqual(manifest.errors[0].code, "parser_failure")
            self.assertEqual(state["parse_action"], ParseAction.QUARANTINE.value)

    def test_milvus_projection_is_idempotent_and_keeps_stable_ids(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        rows = chunks_to_index_rows(document, item_name="fixture")[:2]
        fake = _FakeMilvus()
        config = ImportConfig(chunks_collection="fixture_chunks", embedding_dim=2)
        node = ImportMilvusNode(config=config)

        def vectorized():
            return [
                {**row, "dense_vector": [0.1, 0.2], "sparse_vector": {1: 0.5}}
                for row in rows
            ]

        with (
            patch(
                "knowledge.processor.import_process.nodes.import_milvus.get_config",
                return_value=config,
            ),
            patch(
                "knowledge.processor.import_process.nodes.import_milvus.get_milvus_client",
                return_value=fake,
            ),
        ):
            first = node.process({"chunks": vectorized()})
            second = node.process({"chunks": vectorized()})

        self.assertEqual(len(fake.rows), 2)
        self.assertEqual([row["chunk_id"] for row in fake.rows], [row["chunk_id"] for row in rows])
        self.assertEqual(
            [row["chunk_id"] for row in first["chunks"]],
            [row["chunk_id"] for row in second["chunks"]],
        )
        self.assertEqual(len(fake.delete_filters), 2)

    def test_milvus_delete_failure_does_not_insert_duplicate_revision(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        row = {
            **chunks_to_index_rows(document)[0],
            "dense_vector": [0.1, 0.2],
            "sparse_vector": {1: 0.5},
        }
        fake = _FakeMilvus()
        config = ImportConfig(chunks_collection="fixture_chunks", embedding_dim=2)
        with (
            patch(
                "knowledge.processor.import_process.nodes.import_milvus.get_milvus_client",
                return_value=fake,
            ),
            patch.object(fake, "delete", side_effect=RuntimeError("delete unavailable")),
            self.assertRaises(MilvusError),
        ):
            ImportMilvusNode(config=config).process({"chunks": [row]})
        self.assertEqual(fake.rows, [])

    def test_item_projection_replaces_by_document_not_filename(self) -> None:
        fake = _FakeMilvus()
        config = ImportConfig(
            milvus_url="https://example.invalid",
            item_name_collection="fixture_items",
            embedding_dim=2,
        )
        node = ItemNameRecognitionNode(config=config)
        with patch(
            "knowledge.processor.import_process.nodes.item_name_recognition_load.get_milvus_client",
            return_value=fake,
        ):
            for document_id, item_name in (
                ("doc_first", "First Device"),
                ("doc_second", "Second Device"),
                ("doc_first", "Revised First Device"),
            ):
                node._save_to_milvus(
                    state={"document_id": document_id},
                    file_title="same-filename",
                    item_name=item_name,
                    dense_vector=[0.1, 0.2],
                    sparse_vector={1: 0.5},
                    config=config,
                )
        self.assertEqual(len(fake.rows), 2)
        self.assertEqual(
            {row["document_id"]: row["item_name"] for row in fake.rows},
            {"doc_first": "Revised First Device", "doc_second": "Second Device"},
        )
        self.assertTrue(all(value.startswith("document_id == ") for value in fake.delete_filters))

    def test_legacy_item_projection_does_not_delete_same_named_ir_document(self) -> None:
        fake = _FakeMilvus()
        fake.rows = [{"document_id": "doc_existing", "file_title": "same-filename"}]
        config = ImportConfig(
            milvus_url="https://example.invalid",
            item_name_collection="fixture_items",
            embedding_dim=2,
        )
        with patch(
            "knowledge.processor.import_process.nodes.item_name_recognition_load.get_milvus_client",
            return_value=fake,
        ):
            ItemNameRecognitionNode(config=config)._save_to_milvus(
                state={}, file_title="same-filename", item_name="Legacy Device",
                dense_vector=[0.1, 0.2], sparse_vector={1: 0.5}, config=config,
            )
        self.assertEqual(fake.delete_filters, [])
        self.assertEqual(fake.rows[0]["document_id"], "doc_existing")

    def test_incomplete_ir_embedding_cannot_be_partially_indexed(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        rows = chunks_to_index_rows(document)[:2]
        rows[0].update(dense_vector=[0.1, 0.2], sparse_vector={1: 0.5})
        config = ImportConfig(chunks_collection="fixture_chunks", embedding_dim=2)
        with self.assertRaises(MilvusError):
            ImportMilvusNode(config=config).process({"chunks": rows})

    def test_ir_vector_dimension_mismatch_cannot_be_partially_indexed(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        rows = chunks_to_index_rows(document)[:2]
        rows[0].update(dense_vector=[0.1, 0.2], sparse_vector={1: 0.5})
        rows[1].update(dense_vector=[0.1], sparse_vector={1: 0.5})
        fake = _FakeMilvus()
        config = ImportConfig(chunks_collection="fixture_chunks", embedding_dim=2)
        with patch(
            "knowledge.processor.import_process.nodes.import_milvus.get_milvus_client",
            return_value=fake,
        ):
            with self.assertRaisesRegex(MilvusError, "does not match"):
                ImportMilvusNode(config=config).process({"chunks": rows})
        self.assertEqual(fake.rows, [])

    def test_shadow_collection_schema_separates_pk_and_ir_identity(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        row = {
            **chunks_to_index_rows(document)[0],
            "dense_vector": [0.1, 0.2],
            "sparse_vector": {1: 0.5},
        }
        fake = _FakeSchemaMilvus()
        config = ImportConfig(chunks_collection="shadow_fixture", embedding_dim=2)
        with patch(
            "knowledge.processor.import_process.nodes.import_milvus.get_milvus_client",
            return_value=fake,
        ):
            ImportMilvusNode(config=config).process({"chunks": [row]})
        fields = {field["field_name"]: field for field in fake.schema_fields}
        self.assertEqual(fields["pk"]["auto_id"], True)
        self.assertNotIn("auto_id", fields["chunk_id"])
        self.assertIn("tenant_id", fields)
        self.assertIn("visibility", fields)
        self.assertEqual(fields["acl_readers"]["datatype"], DataType.ARRAY)
        self.assertIn("page_uids", fields)
        self.assertIn("block_lineage_ids", fields)
        self.assertEqual(fields["part"]["datatype"], DataType.INT64)
        self.assertEqual(fake.rows[0]["chunk_id"], row["chunk_id"])
        self.assertEqual(fake.rows[0]["tenant_id"], "public")
        self.assertEqual(fake.rows[0]["visibility"], "public")
        self.assertEqual(fake.rows[0]["acl_readers"], [])

    def test_markdown_import_nodes_write_ir_and_index_projection(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            source = directory / "complex.md"
            source.write_bytes((FIXTURES / "complex.md").read_bytes())
            config = ImportConfig(
                max_content_length=800,
                min_content_length=100,
                minio_bucket="",
            )
            state = {
                "import_file_path": str(source),
                "file_dir": str(directory),
                "md_path": str(source),
                "file_title": source.stem,
                "item_name": "fixture",
                "is_md_read_enabled": True,
                "is_pdf_read_enabled": False,
            }

            DocumentParseNode(config=config).process(state)
            DocumentNormalizeNode(config=config).process(state)
            DocumentSplitNode(config=config).process(state)
            DocumentEnrichNode(config=config).process(state)

            persisted = load_document_ir(directory / "document.ir.json")
            chunks_backup = json.loads(
                (directory / "chunks.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted.status, ParseStatus.SUCCESS)
            self.assertEqual(persisted.document_id, state["document_id"])
            self.assertEqual(len(persisted.chunks), len(state["chunks"]))
            self.assertEqual(chunks_backup, state["chunks"])
            self.assertTrue(all(row["citation"] for row in state["chunks"]))
            self.assertTrue(all(row["stable_id"] == row["chunk_id"] for row in state["chunks"]))

    def test_import_nodes_align_previous_revision_by_explicit_key(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            config = ImportConfig(minio_bucket="")
            states = []
            for version, text in enumerate((
                "# Guide\n\nKeep this paragraph.\n",
                "# Guide\n\nNew paragraph.\n\nKeep this paragraph.\n",
            ), start=1):
                task_dir = directory / f"task-{version}"
                task_dir.mkdir()
                source = task_dir / "guide.md"
                source.write_text(text, encoding="utf-8")
                state = {
                    "import_file_path": str(source),
                    "file_dir": str(task_dir),
                    "is_md_read_enabled": True,
                    "logical_document_key": "fixture/guide",
                    "previous_ir_path": states[0]["ir_path"] if states else "",
                }
                DocumentParseNode(config=config).process(state)
                DocumentNormalizeNode(config=config).process(state)
                states.append(state)

            first = load_document_ir(Path(states[0]["ir_path"]))
            second = load_document_ir(Path(states[1]["ir_path"]))
            old = next(block for block in first.blocks if block.text == "Guide")
            new = next(block for block in second.blocks if block.text == "Guide")
            self.assertEqual(first.document_id, second.document_id)
            self.assertEqual(old.lineage_id, new.lineage_id)
            self.assertEqual(second.metadata["alignment"]["previous_revision_id"], first.revision_id)
            self.assertTrue(second.metadata["revision_continuity"]["accepted"])

    def test_document_registry_reuses_only_a_valid_prior_ir(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            service = FileImportService(base_dir=directory)
            source = directory / "task-1" / "guide.md"
            source.parent.mkdir()
            source.write_text("# Guide\n", encoding="utf-8")
            document = normalize_document(
                MarkdownAdapter().convert(source, logical_document_key="fixture/guide")
            )
            ir_path = source.parent / "document.ir.json"
            save_document_ir(document, ir_path)
            state = {
                "document_id": document.document_id,
                "revision_id": document.revision_id,
                "ir_path": str(ir_path),
            }
            service._record_ir("fixture/guide", state)
            self.assertEqual(service._previous_ir_path("fixture/guide"), str(ir_path.resolve()))
            self.assertEqual(service._previous_ir_path("different/guide"), "")
            with self.assertRaises(ValueError):
                service._record_ir("different/guide", state)

    def test_schema_1_0_ir_remains_readable(self) -> None:
        document = normalize_document(MarkdownAdapter().convert(FIXTURES / "complex.md"))
        payload = document.model_dump(mode="json")
        payload["schema_version"] = "1.0.0"
        payload.pop("logical_document_key", None)
        for block in payload["blocks"]:
            block.pop("lineage_id", None)
            for provenance in block["provenance"]:
                provenance.pop("page_uid", None)
        restored = DocumentIR.model_validate(payload)
        self.assertEqual(restored.schema_version, "1.0.0")
        self.assertEqual(restored.document_id, document.document_id)

    def test_ir_rejects_duplicate_lineage_and_conflicting_page_identity(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            document = self._mineru_document(Path(temporary_dir))
        payload = document.model_dump(mode="json")
        payload["blocks"][2]["lineage_id"] = payload["blocks"][1]["lineage_id"]
        with self.assertRaises(ValueError):
            DocumentIR.model_validate(payload)

        payload = document.model_dump(mode="json")
        payload["blocks"][2]["provenance"][0]["page_uid"] = "page_conflict"
        with self.assertRaises(ValueError):
            DocumentIR.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
