"""Build 6 additional .eml fixtures covering edge cases the original 6 don't:
foreign currency, a duplicate invoice number, a non-remittance email, an
image-only attachment (no extractable text), stale/future dates, and a
reconciliation mismatch alongside an unsupported attachment type.

All data is synthetic. Written to a SEPARATE directory
(``tests/fixtures/edge_cases/``) from the original 6
(``tests/fixtures/emails/``) so they are not auto-discovered by the
parametrized pytest suites that key off the original 6 by name — these are
for manual / demo testing via ``ar-pipeline ingest-eml``, not CI assertions.

Usage::

    uv run python tests/fixtures/_generator/build_edge_case_fixtures.py [OUT_DIR]

Needs ``reportlab``, ``openpyxl`` (both already a project dependency) and
``Pillow`` (already present transitively via ``reportlab``; not a direct
project dependency — this is a dev-time generator script only).
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _mkmsg(frm: str, subject: str, date: str, plain: str, html: str) -> EmailMessage:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "AR Remittances <ar-remittances@acmemetals.example>"
    m["Subject"] = subject
    m["Date"] = date
    digest = int(hashlib.sha256(subject.encode()).hexdigest(), 16) % 10**12
    m["Message-ID"] = f"<{digest}@{frm.split('@')[-1].rstrip('>')}>"
    m.set_content(plain)
    m.add_alternative(html, subtype="html")
    return m


def f7_edge_foreign_currency(out: Path) -> None:
    """A USD wire transfer — the non-INR currency checkpoint."""
    plain = (
        "Dear Sir,\r\n"
        "This is to confirm we have wired USD 45,000.00 via SWIFT (MT103) to your "
        "account towards Invoice EXP-2026-0456 dated 02-Mar-2026.\r\n\r\n"
        "SWIFT/UTR Reference: GTBUS20260304X7712\r\n"
        "Beneficiary's name: ACME METALS LTD\r\n"
        "Value Date: 04-Mar-2026\r\n\r\n"
        "Regards,\r\nAccounts Payable\r\nGlobal Parts Export Inc.\r\n"
    )
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "AP Team <accounts@globalparts-export.example>",
        "Wire Transfer Confirmation - Invoice EXP-2026-0456",
        formatdate(1772841600),
        plain,
        html,
    )
    (out / "07_edge_foreign_currency.eml").write_bytes(m.as_bytes())


def f8_edge_duplicate_invoice(out: Path) -> None:
    """The same invoice number listed twice in one payment's table — the
    duplicate-invoice-number checkpoint."""
    rows = [
        (1, "FCI2510009901", "234,500.00"),
        (2, "FCI2510009902", "189,300.00"),
        (3, "FCI2510009901", "234,500.00"),  # duplicate of row 1 — data entry error
    ]
    body_rows = "".join(
        f"<tr><td>{n}</td><td>{inv}</td><td style='text-align:right'>{amt}</td></tr>"
        for n, inv, amt in rows
    )
    html = f"""<html><body>
<p>Dear Team,</p>
<p>We have remitted the payment vide UTR no. STBK52026050100112233 on 01.05.2026.</p>
<table border="1">
<tr><td>S.No</td><td>Invoice Number</td><td>Amount</td></tr>
{body_rows}
<tr><td></td><td>Total remitted</td><td style='text-align:right'>658,300.00</td></tr>
</table>
<p>Regards,<br>Precision Tooling Works</p>
</body></html>"""
    plain = (
        "Dear Team,\r\nWe have remitted the payment vide UTR no. "
        "STBK52026050100112233 on 01.05.2026.\r\nRegards,\r\nPrecision Tooling Works\r\n"
    )
    m = _mkmsg(
        "AR Desk <ar@precision-tooling.example>",
        "Payment Advice - Invoice Batch 2026-05",
        formatdate(1746057600),
        plain,
        html,
    )
    (out / "08_edge_duplicate_invoice.eml").write_bytes(m.as_bytes())


def f9_edge_not_a_remittance(out: Path) -> None:
    """A payment-status chase, not an advice that a payment happened — the
    is_remittance=false path."""
    plain = (
        "Dear Team,\r\n"
        "This is a gentle reminder that Invoice INV-2026-3381 dated 12-Jan-2026 for "
        "INR 3,42,500.00 remains unpaid as of today. Kindly process the payment at "
        "the earliest and share the UTR once done.\r\n\r\n"
        "Regards,\r\nAccounts Receivable\r\nPrecision Tooling Works\r\n"
    )
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "AR Desk <ar@precision-tooling.example>",
        "Reminder: Invoice INV-2026-3381 payment overdue",
        formatdate(1770076800),
        plain,
        html,
    )
    (out / "09_edge_not_a_remittance.eml").write_bytes(m.as_bytes())


def f10_edge_scanned_image_only(out: Path) -> None:
    """No usable body text; the only settlement data is an image attachment
    (a rendered 'screenshot' of a payment confirmation) — the vision /
    scanned-image checkpoint. Offline (LLM_PROVIDER=stub) this always lands
    in review with a 'not transcribed' placeholder; with a real Anthropic
    key the text below is legible enough for real vision to read."""
    img = Image.new("RGB", (700, 420), "white")
    draw = ImageDraw.Draw(img)
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    font_bold: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
        font_bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
        font_bold = font
    lines = [
        ("Payment Successful", font_bold),
        ("", font),
        ("Amount: INR 87,450.00", font),
        ("To: ACME METALS LTD", font),
        ("UTR: HDFC0N26031512345", font),
        ("Reference: INV-2026-771", font),
        ("Date: 15 Mar 2026, 11:42 AM", font),
    ]
    y = 30
    for text, f in lines:
        draw.text((40, y), text, fill="black", font=f)
        y += 40
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    plain = "Hi,\r\nPlease find attached the payment screenshot. Kindly confirm receipt.\r\n"
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "Amit Agarwal <amit.agarwal@bharatmetal.example>",
        "Payment Proof - Please Confirm",
        formatdate(1773100800),
        plain,
        html,
    )
    m.add_attachment(
        buf.getvalue(), maintype="image", subtype="png", filename="payment_screenshot.png"
    )
    (out / "10_edge_scanned_image_only.eml").write_bytes(m.as_bytes())


def f11_edge_stale_and_future_dates(out: Path) -> None:
    """Invoice dated years before the payment, payment itself dated in the
    (typo'd) future — the payment-date sanity checkpoint(s)."""
    plain = (
        "Dear Sir,\r\n"
        "We have remitted payment against the following invoice:\r\n\r\n"
        "Invoice No: OLDINV2023-0087\r\n"
        "Invoice Date: 05-Jan-2023\r\n"
        "Payment Date: 15-Sep-2027\r\n"
        "Amount Paid: INR 1,25,000.00\r\n"
        "UTR: PNBN52027091500098\r\n\r\n"
        "Regards,\r\nLegacy Traders Pvt Ltd\r\n"
    )
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "Legacy Traders <accounts@legacy-traders.example>",
        "Payment Advice - Invoice Settlement OLDINV2023-0087",
        formatdate(1789430400),
        plain,
        html,
    )
    (out / "11_edge_stale_and_future_dates.eml").write_bytes(m.as_bytes())


def f12_edge_mismatched_totals(out: Path) -> None:
    """The stated total doesn't equal the sum of the line items, and an
    unsupported attachment type (.docx cover letter) rides along — the
    reconciliation checkpoint plus the classifier's skip-unknown-type path."""
    html = """<html><body>
<p>Dear Team,</p>
<p>Please find our remittance advice below, with a signed covering letter attached.</p>
<table border="1">
<tr><td>Invoice</td><td>Amount</td></tr>
<tr><td>INV-3301</td><td style='text-align:right'>120,000.00</td></tr>
<tr><td>INV-3302</td><td style='text-align:right'>95,500.00</td></tr>
<tr><td>INV-3303</td><td style='text-align:right'>60,000.00</td></tr>
<tr><td>Total remitted</td><td style='text-align:right'>250,000.00</td></tr>
</table>
<p>UTR: ICIC52026061512345</p>
<p>Regards,<br>Meridian Fasteners Ltd</p>
</body></html>"""
    plain = (
        "Dear Team,\r\nPlease find our remittance advice, with a signed covering "
        "letter attached. UTR: ICIC52026061512345\r\nRegards,\r\nMeridian Fasteners Ltd\r\n"
    )
    m = _mkmsg(
        "AR Team <ar@meridian-fasteners.example>",
        "FW: Remittance Advice with Covering Letter",
        formatdate(1749513600),
        plain,
        html,
    )
    # a minimal, syntactically-real .docx (a zip with the mandatory parts) —
    # unsupported by the classifier, must be skipped without erroring.
    docx_buf = io.BytesIO()
    with zipfile.ZipFile(docx_buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        z.writestr("word/document.xml", "<w:document/>")
    m.add_attachment(
        docx_buf.getvalue(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="Covering_Letter.docx",
    )
    (out / "12_edge_mismatched_totals.eml").write_bytes(m.as_bytes())


BUILDERS = [
    f7_edge_foreign_currency,
    f8_edge_duplicate_invoice,
    f9_edge_not_a_remittance,
    f10_edge_scanned_image_only,
    f11_edge_stale_and_future_dates,
    f12_edge_mismatched_totals,
]


def main(argv: list[str]) -> None:
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent.parent / "edge_cases"
    out.mkdir(parents=True, exist_ok=True)

    for fn in BUILDERS:
        fn(out)
        print("built", fn.__name__)

    print("\nfiles:")
    for p in sorted(out.glob("*.eml")):
        print(f"  {p.name}  {p.stat().st_size} bytes")


if __name__ == "__main__":
    main(sys.argv)
