"""Conservative recovery of section headings absorbed into table captions."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from knowledge.document_ir.adapters import MinerUAdapter
from knowledge.document_ir.chunking import chunk_document
from knowledge.document_ir.indexing import chunks_to_index_rows
from knowledge.document_ir.models import BlockType
from knowledge.document_ir.normalize import normalize_document


def heading(text, level=1):
    return {
        "type": "title",
        "content": {"title_content": [{"type": "text", "content": text}], "level": level},
        "bbox": [50, 50, 800, 90],
    }


def paragraph(text):
    return {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": text}]},
        "bbox": [50, 100, 800, 150],
    }


def table(captions):
    return {
        "type": "table",
        "content": {
            "table_caption": captions,
            "html": "<table><tr><th>Input</th><th>Limit</th></tr>"
            "<tr><td>Thick card</td><td>250 units</td></tr></table>",
        },
        "bbox": [50, 200, 800, 600],
    }


class CaptionSectionRecoveryTests(unittest.TestCase):
    def convert(self, pages, directory):
        source = directory / "synthetic.pdf"
        source.write_bytes(b"%PDF-1.4\n% synthetic parser-artifact test\n")
        artifact = directory / "synthetic_content_list_v2.json"
        artifact.write_text(json.dumps(pages), encoding="utf-8")
        return MinerUAdapter(parser_version="fixture").convert(
            source_path=source, content_list_path=artifact,
            logical_document_key="tests/caption-sections",
        )

    def test_next_numbered_table_caption_gets_its_own_section(self):
        pages = [
            [heading("2.4.1 Output defects"), paragraph("Check output quality.")],
            [table(["2.4.2 Input limits"]), paragraph("Keep the input flat.")],
            [heading("2.5 Maintenance"), paragraph("Clean the exterior.")],
        ]
        with TemporaryDirectory() as temporary:
            document = self.convert(pages, Path(temporary))
        block = next(b for b in document.blocks if b.table)
        self.assertEqual(block.type, BlockType.TABLE)
        self.assertIsNone(block.heading_level)
        self.assertEqual(block.title_path, ["2.4.2 Input limits"])
        section = next(s for s in document.sections if s.id == block.section_id)
        self.assertEqual(section.level, 1)
        self.assertEqual(document.blocks[-1].title_path, ["2.5 Maintenance"])
        self.assertEqual(document.blocks[3].title_path, ["2.4.2 Input limits"])
        inference = block.metadata["section_inference"]
        self.assertEqual(inference["rule"], "mineru.numbered_table_caption_next_sibling/1.0")
        self.assertIsNone(inference["parser_heading_level"])
        self.assertEqual(inference["derived_section_level"], 1)
        self.assertEqual(document.metadata["section_recovery"]["recovered_table_sections"], 1)
        row = next(r for r in chunks_to_index_rows(chunk_document(normalize_document(document))) if r["has_table"])
        self.assertEqual(row["title"], "2.4.2 Input limits")
        self.assertNotIn("2.4.1 Output defects", row["content"])
        self.assertEqual(json.loads(row["page_numbers"]), [2])

    def test_recovery_does_not_change_source_text_ranges_coordinates_or_block_ids(self):
        pages = [[heading("2.4.1 Output defects")], [table(["2.4.2 Input limits"])]]
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(MinerUAdapter, "_next_sibling_caption", return_value=None):
                original = self.convert(pages, directory)
            recovered = self.convert(pages, directory)
            repeated = self.convert(pages, directory)
        self.assertEqual(original.raw_text, recovered.raw_text)
        self.assertEqual(original.source, recovered.source)
        self.assertEqual(original.document_id, recovered.document_id)
        self.assertEqual(original.parser, recovered.parser)
        for before, after in zip(original.blocks, recovered.blocks):
            for field in ("id", "type", "text", "heading_level", "order", "raw_range", "provenance", "source_pointer", "table"):
                self.assertEqual(getattr(before, field), getattr(after, field), field)
        before = chunk_document(normalize_document(original))
        after = chunk_document(normalize_document(recovered))
        again = chunk_document(normalize_document(repeated))
        self.assertNotEqual(before.revision_id, after.revision_id)
        self.assertEqual(after.model_dump(), again.model_dump())
        self.assertEqual(len(before.chunks), len(after.chunks))

    def test_unnumbered_intermediate_heading_does_not_hide_nearby_sibling(self):
        pages = [[heading("4.1 Settings"), heading("Additional information")], [table(["4.2 Menu"])]]
        with TemporaryDirectory() as temporary:
            document = self.convert(pages, Path(temporary))
        block = next(b for b in document.blocks if b.table)
        self.assertEqual(block.title_path, ["4.2 Menu"])
        original = next(s for s in document.sections if s.id == block.metadata["section_inference"]["original_section_id"])
        self.assertEqual(original.title, "Additional information")

    def test_valid_parser_hierarchy_is_not_overridden(self):
        pages = [[heading("2 Manual", 1), heading("2.1 Output", 2)], [table(["2.2 Input"])]]
        with TemporaryDirectory() as temporary:
            document = self.convert(pages, Path(temporary))
        block = next(b for b in document.blocks if b.table)
        self.assertEqual(block.title_path, ["2 Manual", "2.1 Output"])
        self.assertNotIn("section_inference", block.metadata)
        self.assertFalse(document.metadata["section_recovery"]["flat_parser_headings"])

    def test_ambiguous_captions_are_not_promoted(self):
        for captions in (
            [], ["Table 2.4.2 Input limits"], ["2.4.2Input limits"],
            ["2.4.1 Output defects"], ["2.4.3 Skipped sibling"],
            ["2.5.2 Different parent"], ["3 Next chapter"],
            ["2.4.2 Input limits", "Units: grams"],
        ):
            with self.subTest(captions=captions), TemporaryDirectory() as temporary:
                document = self.convert([[heading("2.4.1 Output defects")], [table(captions)]], Path(temporary))
                block = next(b for b in document.blocks if b.table)
                self.assertEqual(block.title_path, ["2.4.1 Output defects"])
                self.assertNotIn("section_inference", block.metadata)

    def test_distant_numbered_heading_is_not_used_as_anchor(self):
        pages = [[heading("2.4.1 Output defects")], [paragraph("Unrelated page")], [table(["2.4.2 Input limits"])]]
        with TemporaryDirectory() as temporary:
            document = self.convert(pages, Path(temporary))
        self.assertNotIn("section_inference", next(b for b in document.blocks if b.table).metadata)

    def test_new_numbered_chapter_invalidates_previous_anchor(self):
        pages = [[heading("2.4.1 Output defects"), heading("3 Different chapter")], [table(["2.4.2 Input limits"])]]
        with TemporaryDirectory() as temporary:
            document = self.convert(pages, Path(temporary))
        block = next(b for b in document.blocks if b.table)
        self.assertEqual(block.title_path, ["3 Different chapter"])
        self.assertNotIn("section_inference", block.metadata)

    def test_caption_without_explicit_numbered_anchor_stays_a_caption(self):
        with TemporaryDirectory() as temporary:
            document = self.convert([[heading("Specifications"), table(["7.2 Input limits"])]], Path(temporary))
        self.assertEqual(next(b for b in document.blocks if b.table).title_path, ["Specifications"])

    def test_unknown_parser_levels_are_not_treated_as_confirmed_flat_headings(self):
        for level in (None, "unknown"):
            with self.subTest(level=level), TemporaryDirectory() as temporary:
                document = self.convert([[heading("2.4.1 Output defects", level)], [table(["2.4.2 Input limits"])]], Path(temporary))
                self.assertNotIn("section_inference", next(b for b in document.blocks if b.table).metadata)
                self.assertFalse(document.metadata["section_recovery"]["flat_parser_headings"])


if __name__ == "__main__":
    unittest.main()
