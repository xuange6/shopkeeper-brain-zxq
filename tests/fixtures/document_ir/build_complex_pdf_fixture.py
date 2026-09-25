"""Regenerate the deterministic complex-layout PDF used by stage-1 tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Table, TableStyle
from reportlab.pdfgen import canvas


OUTPUT = Path(__file__).with_name("complex_layout.pdf")
SCRATCH_IMAGE = Path(__file__).with_name("complex_layout_scan.png")
WIDTH, HEIGHT = A4


def footer(pdf: canvas.Canvas, page: int) -> None:
    pdf.setStrokeColor(colors.HexColor("#CBD5E1"))
    pdf.line(18 * mm, 15 * mm, WIDTH - 18 * mm, 15 * mm)
    pdf.setFillColor(colors.HexColor("#475569"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(18 * mm, 10 * mm, "Shopkeeper Brain - Document IR fixture")
    pdf.drawRightString(WIDTH - 18 * mm, 10 * mm, str(page))


def page_one(pdf: canvas.Canvas) -> None:
    pdf.setFillColor(colors.HexColor("#0F172A"))
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(18 * mm, HEIGHT - 25 * mm, "Industrial Equipment Manual")
    pdf.setFillColor(colors.HexColor("#2563EB"))
    pdf.rect(18 * mm, HEIGHT - 32 * mm, WIDTH - 36 * mm, 2 * mm, fill=1, stroke=0)

    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    body.fontName = "Helvetica"
    body.fontSize = 9
    body.leading = 13
    left = Paragraph(
        "<b>Left column.</b> Disconnect power before installation. This paragraph "
        "tests reading order and source coordinates in a two-column layout.",
        body,
    )
    right = Paragraph(
        "<b>Right column.</b> Verify the emergency stop and protective cover. "
        "The second column must follow the first in logical reading order.",
        body,
    )
    left.wrapOn(pdf, 78 * mm, 60 * mm)
    left.drawOn(pdf, 18 * mm, HEIGHT - 70 * mm)
    right.wrapOn(pdf, 78 * mm, 60 * mm)
    right.drawOn(pdf, 112 * mm, HEIGHT - 70 * mm)

    pdf.setStrokeColor(colors.HexColor("#334155"))
    pdf.setFillColor(colors.HexColor("#F8FAFC"))
    pdf.roundRect(40 * mm, 75 * mm, 130 * mm, 90 * mm, 4 * mm, fill=1, stroke=1)
    pdf.setFillColor(colors.HexColor("#0F172A"))
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawCentredString(WIDTH / 2, 155 * mm, "Figure 1 - Control Panel")
    labels = [("START", colors.HexColor("#16A34A")), ("STOP", colors.HexColor("#F59E0B")), ("E-STOP", colors.HexColor("#DC2626"))]
    for index, (label, color) in enumerate(labels):
        x = 55 * mm + index * 42 * mm
        pdf.setFillColor(color)
        pdf.circle(x, 115 * mm, 12 * mm, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawCentredString(x, 112 * mm, label)
    pdf.setFillColor(colors.HexColor("#334155"))
    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawString(18 * mm, 22 * mm, "Footnote: control symbols are intentionally embedded in the figure.")
    footer(pdf, 1)


def table_page(pdf: canvas.Canvas, page: int, rows: list[list[str]], continued: bool) -> None:
    pdf.setFillColor(colors.HexColor("#0F172A"))
    pdf.setFont("Helvetica-Bold", 20)
    title = "Media Specifications" + (" - continued" if continued else "")
    pdf.drawString(18 * mm, HEIGHT - 25 * mm, title)
    data = [["Media", "Weight", "Temperature", "Notes"], *rows]
    table = Table(data, colWidths=[38 * mm, 32 * mm, 38 * mm, 66 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1D4ED8")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEADING", (0, 0), (-1, -1), 12),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#94A3B8")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EFF6FF")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    table.wrapOn(pdf, WIDTH - 36 * mm, HEIGHT - 70 * mm)
    table.drawOn(pdf, 18 * mm, HEIGHT - 72 * mm - table._height)
    pdf.setFillColor(colors.HexColor("#334155"))
    pdf.setFont("Helvetica-Oblique", 8)
    note = "Table continues on next page." if not continued else "* Values measured at 50% relative humidity."
    pdf.drawString(18 * mm, 25 * mm, note)
    footer(pdf, page)


def page_four(pdf: canvas.Canvas) -> None:
    image = Image.new("L", (1400, 1900), color=245)
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    bold_path = Path("C:/Windows/Fonts/arialbd.ttf")
    font = ImageFont.truetype(str(font_path), 44) if font_path.exists() else ImageFont.load_default()
    bold = ImageFont.truetype(str(bold_path), 76) if bold_path.exists() else font
    draw.rectangle((70, 70, 1330, 1830), outline=40, width=6)
    draw.text((140, 180), "SCANNED SAFETY NOTICE", fill=20, font=bold)
    draw.line((140, 300, 1260, 300), fill=70, width=4)
    lines = [
        "HIGH TEMPERATURE HAZARD",
        "Wear protective gloves.",
        "Disconnect power before service.",
        "OCR confidence sample: 0.91",
    ]
    for index, line in enumerate(lines):
        draw.text((150, 450 + index * 180), line, fill=30, font=font)
    image.save(SCRATCH_IMAGE, format="PNG", optimize=True)

    pdf.setFillColor(colors.HexColor("#0F172A"))
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(18 * mm, HEIGHT - 25 * mm, "Scanned OCR Page")
    pdf.drawImage(
        str(SCRATCH_IMAGE),
        24 * mm,
        28 * mm,
        width=WIDTH - 48 * mm,
        height=HEIGHT - 65 * mm,
        preserveAspectRatio=True,
        anchor="c",
        mask="auto",
    )
    footer(pdf, 4)


def main() -> None:
    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, pageCompression=1)
    pdf.setTitle("Complex Document IR Fixture")
    page_one(pdf)
    pdf.showPage()
    table_page(
        pdf,
        2,
        [
            ["Plain paper", "80 g/m2", "15-30 C", "Standard feed"],
            ["Card stock", "160 g/m2", "15-25 C", "Manual feed"],
            ["Label", "100 g/m2", "18-25 C", "One sheet"],
            ["Envelope", "90 g/m2", "15-25 C", "Close flap"],
            ["Synthetic", "120 g/m2", "18-22 C", "Low speed"],
        ],
        continued=False,
    )
    pdf.showPage()
    table_page(
        pdf,
        3,
        [
            ["Coated paper", "200 g/m2", "18-22 C", "Dry storage"],
            ["Heavy card", "220 g/m2", "18-22 C", "Single sheet"],
            ["Film", "110 g/m2", "20-24 C", "Avoid static"],
        ],
        continued=True,
    )
    pdf.showPage()
    page_four(pdf)
    pdf.save()
    SCRATCH_IMAGE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
