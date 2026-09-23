from __future__ import annotations

import unittest

from knowledge.document_ir.adapters.table import (
    cells_to_rows,
    cells_to_text,
    parse_html_cells,
)
from knowledge.document_ir.chunking import ChunkingConfig, _split_block
from knowledge.document_ir.models import BlockType, DocumentBlock, TableCell, TablePayload


class DocumentIRTableTests(unittest.TestCase):
    def test_rowspan_reserves_columns_in_following_rows(self) -> None:
        cells = parse_html_cells(
            "<table><tr><th>Problem</th><th>Cause</th><th>Action</th></tr>"
            '<tr><td rowspan="2">No feed</td><td>Too thick</td><td>Use lighter media</td></tr>'
            "<tr><td>Wrong placement</td><td>Adjust edge</td></tr>"
            "<tr><td>Wrinkles</td><td>Too thin</td><td>Use heavier media</td></tr></table>"
        )
        second_cause = next(cell for cell in cells if cell.text == "Wrong placement")
        third_problem = next(cell for cell in cells if cell.text == "Wrinkles")
        self.assertEqual((second_cause.row, second_cause.column), (2, 1))
        self.assertEqual((third_problem.row, third_problem.column), (3, 0))
        self.assertEqual(
            cells_to_rows(cells)[2], "No feed | Wrong placement | Adjust edge"
        )
        self.assertEqual(cells[3].row_span, 2)

    def test_combined_rowspan_and_colspan_preserve_later_columns(self) -> None:
        cells = parse_html_cells(
            '<table><tr><td rowspan="2" colspan="2">Group</td><td>A</td></tr>'
            '<tr><td>B</td></tr><tr><td>C</td><td colspan="2">D</td></tr></table>'
        )
        self.assertEqual(
            [(cell.text, cell.row, cell.column) for cell in cells],
            [("Group", 0, 0), ("A", 0, 2), ("B", 1, 2), ("C", 2, 0), ("D", 2, 1)],
        )
        self.assertEqual(cells_to_rows(cells), ["Group |  | A", "Group |  | B", "C | D | "])

    def test_rowspan_in_middle_does_not_shift_later_values(self) -> None:
        cells = parse_html_cells(
            '<table><tr><td>A</td><td rowspan="2">Shared</td><td>C</td></tr>'
            "<tr><td>D</td><td>F</td></tr></table>"
        )
        self.assertEqual(cells[-1].column, 2)
        self.assertEqual(cells_to_rows(cells), ["A | Shared | C", "D | Shared | F"])

    def test_implied_cell_end_and_unclosed_final_cell_preserve_text(self) -> None:
        cells = parse_html_cells("<table><tr><td>A<td>B<tr><td>C<td>D")
        self.assertEqual(cells_to_text(cells), "A | B\nC | D")

    def test_rectangular_render_preserves_empty_cells_and_input_order(self) -> None:
        cells = [
            TableCell(row=1, column=1, text="Value"),
            TableCell(row=0, column=0, text="Name"),
            TableCell(row=0, column=1, text="Limit"),
        ]
        original = [cell.model_dump() for cell in cells]
        self.assertEqual(cells_to_rows(cells), ["Name | Limit", " | Value"])
        self.assertEqual([cell.model_dump() for cell in cells], original)
        self.assertEqual(cells_to_rows([]), [])

    def test_pathological_rendered_span_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "grid size"):
            cells_to_rows([TableCell(row=0, column=0, text="x", row_span=1_000_001)])

    def test_pathological_html_span_fails_before_allocating_columns(self) -> None:
        with self.assertRaisesRegex(ValueError, "grid size"):
            parse_html_cells('<table><tr><td colspan="1000000000">x</td></tr></table>')

    def test_long_table_slices_keep_rowspan_subject_and_header(self) -> None:
        cells = parse_html_cells(
            "<table><tr><th>Issue</th><th>Reason</th><th>Action</th></tr>"
            '<tr><td rowspan="4">No feed</td><td>Too thick</td><td>Use lighter media</td></tr>'
            "<tr><td>Wrong placement</td><td>Adjust edge</td></tr>"
            "<tr><td>Dirty roller</td><td>Clean roller</td></tr>"
            "<tr><td>Mixed sizes</td><td>Separate media</td></tr></table>"
        )
        block = DocumentBlock(
            id="table-block", type=BlockType.TABLE, order=0, section_id="section",
            text=cells_to_text(cells), table=TablePayload(cells=cells),
        )
        pieces = _split_block(block, ChunkingConfig(max_characters=80))
        self.assertEqual(len(pieces), 4)
        self.assertTrue(all(piece.startswith("Issue | Reason | Action\nNo feed | ") for piece in pieces))
        self.assertTrue(all(len(piece) <= 80 for piece in pieces))

    def _table_with_context(self, *, caption="Units: grams", footnote="Only dry media", value="value"):
        cells = parse_html_cells(
            "<table><tr><th>Part</th><th>Limit</th></tr>"
            + "".join(f"<tr><td>part{index}</td><td>{value}</td></tr>" for index in range(8))
            + "</table>"
        )
        return DocumentBlock(
            id="table-block", type=BlockType.TABLE, order=0, section_id="section",
            text="\n".join([caption, cells_to_text(cells), footnote]),
            table=TablePayload(cells=cells, captions=[caption], footnotes=[footnote]),
        )

    def test_long_table_slices_reserve_budget_for_caption_and_footnote(self) -> None:
        block = self._table_with_context()
        original = block.model_dump()
        pieces = _split_block(block, ChunkingConfig(max_characters=70))
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(len(piece), 70)
            self.assertTrue(piece.startswith("Units: grams\nPart | Limit\n"))
            self.assertTrue(piece.endswith("\nOnly dry media"))
        for index in range(8):
            self.assertEqual(sum(f"part{index} | value" in piece for piece in pieces), 1)
        self.assertEqual(block.model_dump(), original)

    def test_disabling_header_repetition_does_not_drop_table_conditions(self) -> None:
        block = self._table_with_context()
        pieces = _split_block(block, ChunkingConfig(max_characters=70, repeat_table_header=False))
        self.assertGreater(len(pieces), 1)
        self.assertIn("Part | Limit", pieces[0])
        self.assertTrue(all("Part | Limit" not in piece for piece in pieces[1:]))
        self.assertTrue(all(piece.startswith("Units: grams\n") for piece in pieces))
        self.assertTrue(all(piece.endswith("\nOnly dry media") for piece in pieces))
        self.assertTrue(all(len(piece) <= 70 for piece in pieces))

    def test_oversized_shared_context_falls_back_without_losing_source_text(self) -> None:
        block = self._table_with_context(caption="Important condition. " * 4)
        pieces = _split_block(block, ChunkingConfig(max_characters=45, overlap_characters=0))
        self.assertTrue(all(len(piece) <= 45 for piece in pieces))
        self.assertEqual("".join("".join(p.split()) for p in pieces), "".join(block.text.split()))
        self.assertEqual(sum("Only dry media" in piece for piece in pieces), 1)

    def test_oversized_row_falls_back_with_caption_and_footnote_intact(self) -> None:
        block = self._table_with_context(value="W" * 100)
        pieces = _split_block(block, ChunkingConfig(max_characters=45, overlap_characters=0))
        self.assertTrue(all(len(piece) <= 45 for piece in pieces))
        self.assertEqual("".join("".join(p.split()) for p in pieces), "".join(block.text.split()))


if __name__ == "__main__":
    unittest.main()
