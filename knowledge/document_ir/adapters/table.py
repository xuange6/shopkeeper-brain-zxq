from __future__ import annotations

from html.parser import HTMLParser

from knowledge.document_ir.models import TableCell


class TableSizeError(ValueError):
    """Untrusted table coordinates would require an excessive grid."""


def _check_grid_size(rows: int, columns: int) -> None:
    if rows * columns > 1_000_000:
        raise TableSizeError("table spans exceed the supported rendered grid size")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[TableCell] = []
        self.row = -1
        self.column = 0
        self._cell: dict | None = None
        self._text: list[str] = []
        # Columns remain occupied until the exclusive row at which a rowspan
        # ends. Resetting the cursor for <tr> must not forget these cells.
        self._occupied_until: dict[int, int] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._finish_cell()
            self.row += 1
            self.column = 0
        elif tag in {"td", "th"}:
            self._finish_cell()
            values = dict(attrs)
            row = max(0, self.row)
            column_span = _positive_int(values.get("colspan"))
            row_span = _positive_int(values.get("rowspan"))
            _check_grid_size(row + row_span, self.column + column_span)
            while any(
                self._occupied_until.get(column, 0) > row
                for column in range(self.column, self.column + column_span)
            ):
                self.column += 1
                _check_grid_size(row + row_span, self.column + column_span)
            self._cell = {
                "row": row,
                "column": self.column,
                "row_span": row_span,
                "column_span": column_span,
                "is_header": tag == "th",
            }
            for column in range(self.column, self.column + column_span):
                self._occupied_until[column] = row + row_span
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th", "tr", "table"}:
            self._finish_cell()

    def _finish_cell(self) -> None:
        if self._cell is None:
            return
        text = " ".join(" ".join(self._text).split())
        self.cells.append(TableCell(text=text, **self._cell))
        self.column += self._cell["column_span"]
        self._cell = None
        self._text = []


def _positive_int(value) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def parse_html_cells(html: str) -> list[TableCell]:
    parser = _TableParser()
    try:
        parser.feed(html or "")
        parser.close()
        parser._finish_cell()
    except TableSizeError:
        raise
    except Exception:
        return []
    return parser.cells


def cells_to_rows(cells: list[TableCell]) -> list[str]:
    """Render a rectangular row view without losing merged-cell relations.

    Repeat vertically merged labels on each covered row, so a split table row
    still says which subject its values describe. A horizontal span retains
    its empty slots instead of shifting later values into an earlier column.
    The source cells and their spans remain unchanged in the IR.
    """
    if not cells:
        return []
    row_count = max(cell.row + cell.row_span for cell in cells)
    column_count = max(cell.column + cell.column_span for cell in cells)
    # Fail closed for malformed spans instead of allocating an unbounded
    # rectangular grid from untrusted parser output.
    _check_grid_size(row_count, column_count)
    rows = [[""] * column_count for _ in range(row_count)]
    for cell in cells:
        for row in range(cell.row, cell.row + cell.row_span):
            rows[row][cell.column] = cell.text
    return [" | ".join(row) for row in rows]


def cells_to_text(cells: list[TableCell]) -> str:
    return "\n".join(cells_to_rows(cells))
