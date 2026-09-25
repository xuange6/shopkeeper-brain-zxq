"""Structural corrections preserve exact source-block lineage, including furniture."""

import unittest

from knowledge.document_ir.chunking import chunk_document
from knowledge.document_ir.diff import DocumentDiff, diff_documents
from knowledge.document_ir.lineage import align_document, seed_lineage
from knowledge.document_ir.models import (
    BlockType, DocumentBlock, DocumentIR, DocumentSource, ParserDescriptor,
    ParseStatus, Provenance, Section,
)


def block(identifier, text="Unchanged content", *, section="old-section", title="Old heading", page=1, furniture=False):
    return DocumentBlock(
        id=identifier, type=BlockType.PAGE_HEADER if furniture else BlockType.PARAGRAPH,
        text=text, order=0, section_id=section, title_path=[title],
        content_layer="furniture" if furniture else "body",
        provenance=[Provenance(page_number=page, source_pointer=f"/pages/{page - 1}/{identifier}")],
    )


def document(blocks, revision="old-revision", source_sha="a" * 64):
    sections = {}
    for index, item in enumerate(blocks):
        item.order = index
        if item.section_id not in sections:
            sections[item.section_id] = Section(
                id=item.section_id, title=item.title_path[-1], level=1,
                title_path=list(item.title_path), order=len(sections),
            )
        sections[item.section_id].block_ids.append(item.id)
    return seed_lineage(DocumentIR(
        document_id="doc-synthetic", logical_document_key="tests/structural-correction",
        revision_id=revision,
        source=DocumentSource(uri="file:///synthetic.pdf", filename="synthetic.pdf",
                              mime_type="application/pdf", sha256=source_sha, size_bytes=1),
        parser=ParserDescriptor(name="fixture", version="1"), status=ParseStatus.SUCCESS,
        sections=list(sections.values()), blocks=blocks,
    ))


class StructuralDiffTests(unittest.TestCase):
    def test_section_only_change_preserves_body_and_furniture_lineage(self):
        before = document([block("body"), block("footer", furniture=True)])
        after = document([
            block("body", section="new-section", title="Correct heading"),
            block("footer", section="new-section", title="Correct heading", furniture=True),
        ], revision="new-revision")
        aligned = align_document(before, after)
        self.assertEqual([b.lineage_id for b in before.blocks], [b.lineage_id for b in aligned.blocks])
        self.assertEqual(aligned.metadata["alignment"]["matched_by_stable_block_id"], 2)
        self.assertEqual(aligned.metadata["alignment"]["matched_furniture_blocks"], 1)
        difference = diff_documents(before, after)
        self.assertEqual(difference.unchanged_block_ids, ["body", "footer"])
        self.assertEqual(len(difference.unchanged_lineage_ids), 2)
        self.assertFalse(difference.added_block_ids)
        self.assertFalse(difference.removed_block_ids)
        self.assertFalse(difference.modified_blocks)
        self.assertEqual(len(difference.structural_changes), 2)
        for change in difference.structural_changes:
            self.assertEqual(change.before_section_id, "old-section")
            self.assertEqual(change.after_section_id, "new-section")
            self.assertEqual(change.before_title_path, ["Old heading"])
            self.assertEqual(change.after_title_path, ["Correct heading"])

    def test_exact_ids_disambiguate_repeated_body_and_furniture_on_same_source(self):
        before = document([block("body-a"), block("body-b"), block("footer-a", furniture=True), block("footer-b", furniture=True)])
        after = document([block("body-a"), block("body-b"), block("footer-a", furniture=True), block("footer-b", furniture=True)], revision="new-revision")
        self.assertNotEqual([b.lineage_id for b in before.blocks], [b.lineage_id for b in after.blocks])
        aligned = align_document(before, after)
        self.assertEqual([b.lineage_id for b in before.blocks], [b.lineage_id for b in aligned.blocks])
        self.assertEqual(len({b.lineage_id for b in aligned.blocks}), 4)
        difference = diff_documents(before, after)
        self.assertFalse(difference.added_block_ids or difference.removed_block_ids or difference.structural_changes)

    def test_added_block_cannot_take_inherited_lineage_from_old_heading_signature(self):
        before = document([block("retained")])
        after = document([
            block("retained", section="new-section", title="Correct heading"),
            block("added"),
        ], revision="new-revision")
        self.assertEqual(before.blocks[0].lineage_id, after.blocks[1].lineage_id)
        aligned = align_document(before, after)
        self.assertEqual(before.blocks[0].lineage_id, aligned.blocks[0].lineage_id)
        self.assertNotEqual(aligned.blocks[0].lineage_id, aligned.blocks[1].lineage_id)
        self.assertEqual(aligned.model_dump(), align_document(before, after).model_dump())
        difference = diff_documents(before, after)
        self.assertEqual(difference.added_block_ids, ["added"])
        self.assertFalse(difference.removed_block_ids)
        self.assertEqual(difference.unchanged_block_ids, ["retained"])
        self.assertEqual(len(difference.structural_changes), 1)

    def test_matching_identifier_does_not_override_changed_payload(self):
        before = document([block("retained", "Old exact evidence")])
        after = document([block("retained", "Unrelated replacement material", section="new-section", title="Different subject")], revision="new-revision")
        aligned = align_document(before, after)
        self.assertEqual(aligned.metadata["alignment"]["matched_by_stable_block_id"], 0)
        self.assertNotEqual(before.blocks[0].lineage_id, aligned.blocks[0].lineage_id)
        self.assertFalse(diff_documents(before, after).unchanged_block_ids)

    def test_changed_source_and_ambiguous_duplicate_pages_do_not_use_position_identity(self):
        before = document([block("first", page=1), block("second", page=2)])
        after = document([block("first", page=1), block("second", page=2), block("third", page=3)], revision="new-revision", source_sha="b" * 64)
        aligned = align_document(before, after)
        self.assertEqual(aligned.metadata["alignment"]["matched_pages"], {})
        self.assertEqual(aligned.metadata["alignment"]["matched_by_stable_block_id"], 0)
        self.assertFalse({b.lineage_id for b in before.blocks} & {b.lineage_id for b in aligned.blocks})
        difference = diff_documents(before, after)
        self.assertFalse(difference.unchanged_block_ids)
        self.assertFalse(set(difference.unchanged_block_ids) & (set(difference.added_block_ids) | set(difference.removed_block_ids)))

    def test_stable_match_does_not_make_remaining_global_duplicate_unique(self):
        before = document([block("retained"), block("removed")])
        after = document([block("retained"), block("added")], revision="new-revision")
        aligned = align_document(before, after)
        self.assertEqual(aligned.blocks[0].lineage_id, before.blocks[0].lineage_id)
        self.assertNotEqual(aligned.blocks[1].lineage_id, before.blocks[1].lineage_id)
        difference = diff_documents(before, after)
        self.assertEqual(difference.unchanged_block_ids, ["retained"])
        self.assertEqual(difference.added_block_ids, ["added"])
        self.assertEqual(difference.removed_block_ids, ["removed"])

    def test_type_or_content_layer_change_is_not_reported_unchanged(self):
        for change in ({"type": BlockType.OTHER}, {"content_layer": "furniture"}):
            with self.subTest(change=change):
                before = document([block("retained")])
                altered = block("retained").model_copy(update=change)
                after = document([altered], revision="new-revision")
                aligned = align_document(before, after)
                self.assertEqual(aligned.metadata["alignment"]["matched_by_stable_block_id"], 0)
                self.assertFalse(diff_documents(before, after).unchanged_block_ids)

    def test_alignment_preserves_document_revision_blocks_and_existing_chunk_identity(self):
        before = chunk_document(document([block("retained")]))
        after = chunk_document(document([block("retained", section="new-section", title="Correct heading")], revision="new-revision"))
        aligned = align_document(before, after)
        self.assertEqual(after.document_id, aligned.document_id)
        self.assertEqual(after.revision_id, aligned.revision_id)
        self.assertEqual([b.id for b in after.blocks], [b.id for b in aligned.blocks])
        self.assertEqual(after.chunks, aligned.chunks)
        self.assertEqual([c.id for c in chunk_document(after).chunks], [c.id for c in chunk_document(aligned).chunks])

    def test_structural_field_is_optional_for_legacy_diff_consumers(self):
        legacy = DocumentDiff.model_validate({"same_document": True, "unchanged_block_ids": ["retained"]})
        self.assertEqual(legacy.structural_changes, [])

    def test_different_logical_documents_cannot_inherit_same_block_identifier(self):
        before = document([block("retained")])
        after = document([block("retained")]).model_copy(update={"document_id": "different-document"})
        with self.assertRaisesRegex(ValueError, "different logical documents"):
            align_document(before, after)
        self.assertFalse(diff_documents(before, after).unchanged_block_ids)


if __name__ == "__main__":
    unittest.main()
