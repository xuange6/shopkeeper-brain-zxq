from __future__ import annotations

from html.parser import HTMLParser

from knowledge.document_ir.models import TableCell


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[TableCell] = []
        self.row = -1
        self.column = 0
        self._cell: dict | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self.row += 1
            self.column = 0
        elif tag in {"td", "th"}:
            values = dict(attrs)
            self._cell = {
                "row": max(0, self.row),
                "column": self.column,
                "row_span": _positive_int(values.get("rowspan")),
                "column_span": _positive_int(values.get("colspan")),
                "is_header": tag == "th",
            }
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"td", "th"} or self._cell is None:
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
    except Exception:
        return []
    return parser.cells


def cells_to_text(cells: list[TableCell]) -> str:
    if not cells:
        return ""
    rows: dict[int, list[TableCell]] = {}
    for cell in cells:
        rows.setdefault(cell.row, []).append(cell)
    return "\n".join(
        " | ".join(cell.text for cell in sorted(row, key=lambda item: item.column))
        for _, row in sorted(rows.items())
    )
