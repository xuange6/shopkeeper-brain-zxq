"""Regression: a figure must not detach a warning from its instructions."""
from pathlib import Path
import unittest
from unittest.mock import patch

from knowledge.document_ir.adapters import MinerUAdapter
from knowledge.document_ir.chunking import ChunkingConfig, chunk_document, _semantic_split
from knowledge.document_ir.citations import build_chunk_citation
from knowledge.document_ir.indexing import chunks_to_index_rows
from knowledge.document_ir.normalize import normalize_document
from knowledge.processor.query_process.config import QueryConfig
from knowledge.processor.query_process.nodes.answer_output import AnswerOutputNode
from knowledge.utils.query_result_utils import build_source_references, build_retrieval_trace, source_image_urls

FIXTURES = Path(__file__).parent / "fixtures" / "document_ir"


class ImageInstructionTests(unittest.TestCase):
    def document(self):
        return normalize_document(MinerUAdapter(parser_version="fixture").convert(
            source_path=FIXTURES / "complex_layout.pdf",
            content_list_path=FIXTURES / "image_instructions_mineru_content_list_v2.json",
        ))

    def test_instruction_figures_and_warning_stay_together(self):
        document = chunk_document(self.document())
        self.assertEqual(len(document.chunks), 3)
        warning = document.chunks[0]
        for text in ("等待加热部件冷却", "锁定指示灯熄灭", "双手取出"):
            self.assertIn(text, warning.text)
        self.assertEqual(len(warning.image_block_ids), 2)
        self.assertEqual(len(warning.block_ids), 5)
        self.assertEqual({p.page_number for p in warning.provenance}, {1})
        self.assertEqual(len(build_chunk_citation(document, warning)["images"]), 2)

    def test_page_boundary_and_identical_headings_do_not_merge(self):
        document = chunk_document(self.document(), ChunkingConfig(min_characters=1200))
        first, second, third = document.chunks
        self.assertEqual(first.section_id, second.section_id)
        self.assertNotEqual(second.section_id, third.section_id)
        self.assertEqual(second.title_path, third.title_path)
        self.assertEqual([sorted({p.page_number for p in c.provenance}) for c in document.chunks], [[1], [2], [2]])
        self.assertNotIn("电路板", second.text)

    def test_rechunking_does_not_change_document_or_block_identity(self):
        original = self.document()
        first = chunk_document(original)
        second = chunk_document(original, ChunkingConfig(max_characters=25, min_characters=10))
        self.assertEqual(first.document_id, second.document_id)
        self.assertEqual(first.revision_id, second.revision_id)
        self.assertEqual([b.id for b in first.blocks], [b.id for b in second.blocks])
        self.assertNotEqual([c.id for c in first.chunks], [c.id for c in second.chunks])
        self.assertEqual(first.model_dump(), chunk_document(original).model_dump())

    def test_overlap_respects_character_limit(self):
        pieces = _semantic_split("a" * 15 + "." + "b" * 18 + ".", ChunkingConfig(max_characters=20, overlap_characters=10))
        self.assertTrue(all(len(piece) <= 20 for piece in pieces))

    def test_semantic_splits_preserve_line_separators_inside_each_piece(self):
        pieces = _semantic_split("1\n2\n" + "x" * 24, ChunkingConfig(max_characters=20, overlap_characters=0))
        self.assertEqual(pieces[0], "1\n2")

    def test_source_images_roundtrip_through_index_and_trace(self):
        document = self.document()
        images = [b for b in document.blocks if b.image]
        images[0].image.uri = "https://assets.example.invalid/front.png"
        document = chunk_document(document)
        row = chunks_to_index_rows(document)[0]
        references = build_source_references([row])
        self.assertEqual(references[0]["image_urls"], [images[0].image.uri])
        self.assertEqual(build_retrieval_trace({"reranked_docs": [row]})["stages"]["rerank"][0]["image_urls"], [images[0].image.uri])
        self.assertEqual(references[0]["page_numbers"], [1])
        self.assertEqual(references[0]["block_ids"], document.chunks[0].block_ids)

    def test_legacy_image_extraction_is_not_arbitrary_link_detection(self):
        content = "see https://example.invalid/page ![front](<https://example.invalid/front.png>) ![bad](file:///secret.png)"
        self.assertEqual(source_image_urls({"content": content}), ["https://example.invalid/front.png"])
        self.assertEqual(source_image_urls({"citation": {"images": []}, "content": content}), [])
        self.assertEqual(source_image_urls({"citation": {"images": None}, "content": content}), [])
        self.assertEqual(source_image_urls({"image_urls": ["https://user:secret@example.invalid/a.png", "file:///private", "https://example.invalid/a.png"]}), ["https://example.invalid/a.png"])

    def test_answer_image_requires_cited_source_asset(self):
        actual = "https://example.invalid/real.png"
        fake = "https://example.invalid/invented.png"
        node = AnswerOutputNode(config=QueryConfig())
        for answer, expected in (
            (f"说明 [1]\n【图片】\n{actual}\n{fake}", [actual]),
            (f"说明 [2]\n【图片】\n{actual}", []),
            (f"说明\n【图片】\n{actual}", []),
        ):
            with self.subTest(answer=answer), patch("knowledge.processor.query_process.nodes.answer_output.set_task_result"):
                state = node.process({"answer": answer, "reranked_docs": [{"content": f"![panel]({actual})"}]})
                self.assertEqual(state["image_urls"], expected)


if __name__ == "__main__":
    unittest.main()
