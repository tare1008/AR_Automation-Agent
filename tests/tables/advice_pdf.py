"""A synthetic multi-invoice payment advice in the style of a real one: a ruled
table over two pages, wrapped bill numbers, "…DISCO" adjustment rows,
trailing-minus TDS, a one-word continuation row at the top of page 2, a Total
row and the amount in words. Every name and number here is invented."""

from __future__ import annotations

import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HEADER = ["BillNo", "BillDate", "A/C RefNo", "GrossAmount", "Adv/Debit", "TDS", "Net Payment"]
NET = Decimal("6809764.78")
WORDS = "SIXTY EIGHT LAKH NINE THOUSAND SEVEN HUNDRED SIXTY FOUR RupeesSEVENTY EIGHT Paise"


def _adj(number: str, day: str, ref: str, amount: str) -> list[str]:
    return [number, day, ref, "", amount, "0.00", f"-{amount}"]


PAGE1 = [
    [
        "CBB2510004\n583",
        "27.11.2025",
        "5105847648",
        "1,250,000.00",
        "",
        "1,250.00-",
        "1,248,750.00",
    ],
    _adj("2510004583DIS\nCO", "27.11.2025", "1700003207", "12,500.00"),
    _adj("2510004516DIS\nCO", "24.11.2025", "1700003206", "300.00"),
    _adj("2510004515DIS\nCO", "24.11.2025", "1700003205", "11,200.00"),
    _adj("2510004431DIS\nCO", "19.11.2025", "1700003200", "7,450.00"),
    [
        "CBB2510026\n174",
        "24.09.2025",
        "5105849306",
        "3,400,500.50",
        "",
        "3,400.50-",
        "3,397,100.00",
    ],
    ["CBB2510003\n5016", "28.11.2025", "5105847817", "875,320.40", "", "875.32-", "874,445.08"],
    ["CBB2510003\n5015", "28.11.2025", "5105847816", "410,000.00", "", "410.00-", "409,590.00"],
    [
        "CBB2510003\n5014",
        "28.11.2025",
        "5105847815",
        "1,020,300.00",
        "",
        "1,020.30-",
        "1,019,279.70",
    ],
    _adj("2510035017DIS\nCO", "28.11.2025", "1700003193", "9,800.00"),
    _adj("2510035016DIS", "28.11.2025", "1700003192", "4,100.00"),
]
PAGE2 = [
    ["CO", "", "", "", "", "", ""],
    _adj("2510035015DIS\nCO", "28.11.2025", "1700003191", "2,250.00"),
    _adj("2510035014DIS\nCO", "28.11.2025", "1700003190", "5,600.00"),
    _adj("2510031885DIS\nCO", "05.11.2025", "1700003189", "3,300.00"),
    _adj("2510031884DIS\nCO", "05.11.2025", "1700003188", "10,150.00"),
    _adj("2510031883DIS\nCO", "05.11.2025", "1700003187", "29,900.00"),
    _adj("2510031882DIS\nCO", "05.11.2025", "1700003186", "3,350.00"),
    _adj("2510031881DIS\nCO", "05.11.2025", "1700003185", "13,700.00"),
    _adj("2510031879DIS\nCO", "05.11.2025", "1700003184", "17,000.00"),
    _adj("2510031878DIS\nCO", "05.11.2025", "1700003183", "8,800.00"),
    ["Total", "", "", "6,956,120.90", "139,400.00", "6,956.12-", "6,809,764.78"],
]


def build_advice_pdf(*, beneficiary: str = "ACME METALS LTD") -> bytes:
    body = getSampleStyleSheet()["Normal"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    grid = TableStyle(
        [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 7)]
    )
    widths = [28 * mm, 20 * mm, 22 * mm, 28 * mm, 22 * mm, 22 * mm, 28 * mm]

    def table(rows: list[list[str]]) -> Table:
        t = Table([HEADER, *rows], colWidths=widths)
        t.setStyle(grid)
        return t

    doc.build(
        [
            Paragraph("CONTINENTAL BUS BODY BUILDERS LIMITED", body),
            Paragraph("PAYMENT ADVICE", body),
            Paragraph(
                f"Vendor Code : 220417 Vendor Name : {beneficiary} Document No : 1500009001", body
            ),
            Paragraph("Document Date: 17.01.2026", body),
            Paragraph(
                f"We have made payment through Net Banking for rupees ( {WORDS} ) "
                "as detailed below.",
                body,
            ),
            Spacer(1, 4 * mm),
            table(PAGE1),
            PageBreak(),
            table(PAGE2),
            Spacer(1, 4 * mm),
            Paragraph("RTGS/NEFT Reference : RTGS PAYMENT", body),
        ]
    )
    return buf.getvalue()
