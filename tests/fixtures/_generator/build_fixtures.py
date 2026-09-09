"""Build 6 redacted .eml fixtures mirroring the structure of the real client
samples. All names / amounts / invoice numbers / UTRs are synthetic. Structure
(MIME layout, forwarded chains, table vs free-text vs attachment) is faithful."""

from __future__ import annotations

import io
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path("/tmp/claude-1000/-home-atharvatare-AR-Automation-Agent/49984f41-bb34-439e-9248-dc6a2bf2697c/scratchpad/fixtures")
OUT.mkdir(parents=True, exist_ok=True)

import base64

# valid 1x1 transparent PNG (67 bytes) — stands in for a logo / signature image
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _mkmsg(frm, subject, date, plain, html):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "AR Remittances <ar-remittances@acmemetals.example>"
    m["Subject"] = subject
    m["Date"] = date
    m["Message-ID"] = f"<{abs(hash(subject)) % 10**12}@{frm.split('@')[-1].rstrip('>')}>"
    m.set_content(plain)
    m.add_alternative(html, subtype="html")
    return m


def f1_autoneum_hsbc_pdf():
    """Forwarded internal -> HSBC-style payment advice as a PDF attachment
    (native text) + an inline logo image. multipart/mixed>related>alternative."""
    pdf = io.BytesIO()
    c = canvas.Canvas(pdf, pagesize=A4)
    y = 280 * mm
    def line(t, dy=6 * mm, size=9):
        nonlocal y
        c.setFont("Helvetica", size)
        c.drawString(20 * mm, y, t)
        y -= dy
    line("Global Trade Bank  -  Payment Advice", size=12)
    line("Advice sending date: 19 Feb 2026")
    line("Advice reference no: GT9KX2M4P1Q7-IN")
    line("Beneficiary's name: ACME METALS LIMITED")
    line("Beneficiary's bank: STATE INDUSTRIAL BANK // RENUKOOT BRANCH")
    line("Beneficiary's account: ACMEROL4001*****")
    line("Customer reference: NORDICAUTO26021908")
    line("Debit amount: INR 6,633,624.61")
    line("Remittance amount: INR 6,633,624.61")
    line("Value date: 19 Feb 2026")
    line("Remitter's name: NORDIC AUTO COMPONENTS PRIVATE LIMITED")
    line("Instruction reference: 90012GT9KYSF6")
    line("Other reference: GTBN52026021920531478")
    line("")
    line("Remitter to beneficiary information: VENDOR PAYMENT")
    line("")
    line("Invoice no        Invoice Date   Gross Amount   Deduction   Net Amount")
    line("ACM2510006275     30.12.2025     6,633,624.61   0.00        6,633,624.61")
    c.showPage()
    c.save()

    plain = (
        "Regards,\r\nShwetha M\r\nACME Metals Limited\r\n\r\n"
        "From: Mayur P <mayur.p@acmemetals.example>\r\n"
        "Sent: 19 February 2026 09:54\r\nTo: Shwetha M <shwetha.m@acmemetals.example>\r\n"
        "Subject: FW: Payment Advice - NORDICAUTO26021908\r\n\r\nKindly adjust\r\n\r\n"
        "From: Global Trade Bank Advising <advising@globaltradebank.example>\r\n"
        "Sent: 19 February 2026 05:19\r\n"
        "Subject: Payment Advice - Advice Ref GT9KX2M4P1Q7-IN\r\n\r\n"
        "Dear Sir/Madam,\r\nThe attached payment advice is issued at the request of our "
        "customer. The advice is for your reference only.\r\n\r\nGlobal Payments and Cash Management\r\n"
    )
    html = (
        "<html><body><p>Regards,<br>Shwetha M<br>ACME Metals Limited</p>"
        "<p><b>From:</b> Global Trade Bank Advising &lt;advising@globaltradebank.example&gt;<br>"
        "<b>Sent:</b> 19 February 2026 05:19<br>"
        "<b>Subject:</b> Payment Advice - Advice Ref GT9KX2M4P1Q7-IN</p>"
        "<p>Dear Sir/Madam,<br>The attached payment advice is issued at the request of our customer.</p>"
        '<img src="cid:logo001">'
        "</body></html>"
    )
    m = _mkmsg(
        "Shwetha M <shwetha.m@acmemetals.example>",
        "FW: Payment Advice - Advice Ref [GT9KX2M4P1Q7-IN] / Priority payment / Customer Ref [NORDICAUTO26021908]",
        formatdate(1771579874),
        plain,
        html,
    )
    m.get_payload()[1].add_related(TINY_PNG, "image", "png", cid="logo001",
                                   filename="image003.png")
    m.add_attachment(pdf.getvalue(), maintype="application", subtype="octet-stream",
                     filename="Payment_Advice.pdf")
    (OUT / "01_nordicauto_hsbc_pdf.eml").write_bytes(m.as_bytes())


def f2_fluorochem_body_table():
    rows = [
        (1, "2-Feb-26", "FCI2510007033", "39.702", "Invoice", "INR", "1,452,299.16", "1,452,299.16"),
        (2, "2-Feb-26", "FCI2510007039", "39.228", "Invoice", "INR", "1,434,960.24", "1,434,960.24"),
        (3, "2-Feb-26", "FCI2510007042", "39.247", "Invoice", "INR", "1,435,655.26", "1,435,655.26"),
        (4, "3-Feb-26", "FCI2510007049", "39.160", "Invoice", "INR", "1,432,472.80", "1,432,472.80"),
        (5, "3-Feb-26", "FCI2510007064", "39.259", "Invoice", "INR", "1,436,094.22", "1,436,094.22"),
        (6, "4-Feb-26", "FCI2510007070", "39.317", "Invoice", "INR", "1,438,215.86", "1,438,215.86"),
        (7, "4-Feb-26", "FCI2510007081", "39.493", "Invoice", "INR", "1,444,653.94", "1,444,653.94"),
        (8, "5-Feb-26", "FCI2510007105", "39.444", "Invoice", "INR", "1,442,861.52", "1,442,861.52"),
        (9, "5-Feb-26", "FCI2510007097", "40.188", "Invoice", "INR", "1,470,077.04", "1,470,077.04"),
        (10, "6-Feb-26", "FCI2510007116", "39.232", "Invoice", "INR", "1,435,106.56", "1,435,106.56"),
        (11, "6-Feb-26", "FCI2510007117", "33.601", "Invoice", "INR", "1,229,124.58", "1,229,124.58"),
        (12, "6-Feb-26", "FCI2510007123", "39.353", "Invoice", "INR", "1,439,532.74", "1,439,532.74"),
        (13, "6-Feb-26", "FCI2510007124", "20.259", "Invoice", "INR", "741,074.22", "741,074.22"),
    ]
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows
    )
    html = f"""<html><body>
<p>Dear All,</p><p>Please apply the payment below.</p><p>Regards,<br>Dharmendra</p>
<p><b>From:</b> Tejeswara Rao S &lt;str@fluorochem.example&gt;<br>
<b>Sent:</b> 18 February 2026 17:07<br>
<b>Subject:</b> Re: Payment Remittance details</p>
<p>Dear Dharmendra Ji,</p>
<p>We have remitted the payment of Rs.1,36,70,691.00 vide UTR no. STBK52026021813360279 on 18.02.2026.
Please find the below invoice details for your reference.</p>
<table border="1">
<tr><td colspan="8">ACME INVOICES DETAILS FOR PAYMENT</td></tr>
<tr><td>S.No</td><td>Invoice date</td><td>Invoice Number</td><td>Quantity (MT)</td>
<td>Class</td><td>Currency</td><td>Original</td><td>Balance Due</td></tr>
{body_rows}
<tr><td></td><td></td><td></td><td>487.483</td><td></td><td></td><td>17,832,128.14</td><td></td></tr>
<tr><td colspan="6">LESS: IT TDS on Goods 194Q (0.1%)</td><td>17,832.00</td><td></td></tr>
<tr><td colspan="6">NET AMOUNT REMITTED</td><td>13,670,691.00</td><td></td></tr>
</table>
<p><img src="cid:sig01"></p>
</body></html>"""
    plain = (
        "Dear All,\r\nPlease apply the payment below.\r\nRegards,\r\nDharmendra\r\n\r\n"
        "From: Tejeswara Rao S <str@fluorochem.example>\r\n"
        "Subject: Re: Payment Remittance details\r\n\r\n"
        "Dear Dharmendra Ji,\r\nWe have remitted the payment of Rs.1,36,70,691.00 vide UTR "
        "no. STBK52026021813360279 on 18.02.2026.\r\n"
    )
    m = _mkmsg(
        "Dharmendra Kumar <dharmendra.kumar@acmemetals.example>",
        "FW: Payment Remittance details",
        formatdate(1771416539),
        plain,
        html,
    )
    m.get_payload()[1].add_related(TINY_PNG, "image", "png", cid="sig01",
                                   filename="Outlook-signature.png")
    (OUT / "02_fluorochem_body_table.eml").write_bytes(m.as_bytes())


def f3_contibus_pdf():
    pdf = io.BytesIO()
    c = canvas.Canvas(pdf, pagesize=A4)
    for page in range(2):
        y = 285 * mm
        def line(t, dy=5.5 * mm, size=8):
            nonlocal y
            c.setFont("Helvetica", size)
            c.drawString(15 * mm, y, t)
            y -= dy
        if page == 0:
            line("CONTINENTAL BUS BODY BUILDERS LIMITED", size=11)
            line("PAYMENT ADVICE                    Date: 17.01.2026")
            line("Vendor Code : 220417    Vendor Name : ACME METALS LTD")
            line("Document No : 1500005408")
            line("We have made payment through Net Banking for rupees")
            line("(ONE CRORE THIRTY ONE LAKH THIRTEEN THOUSAND THREE HUNDRED")
            line("TWENTY FIVE Rupees THIRTY NINE Paise) as detailed below.")
            line("")
        line("Bill No          Bill Date    A/C RefNo     Gross Amount    TDS        Net Payment")
        data = [
            ("CBB2510004583", "27.11.2025", "5105847648", "2,804,859.17", "2,377.00", "2,802,482.17"),
            ("2510004583DISCO", "27.11.2025", "1700003207", "-15,924.00", "0.00", "-15,924.00"),
            ("2510004516DISCO", "24.11.2025", "1700003206", "-350.00", "0.00", "-350.00"),
            ("CBB2510026174", "24.09.2025", "5105849306", "7,197,057.78", "6,099.20", "7,190,958.58"),
            ("CBB2510035016", "28.11.2025", "5105847817", "1,177,993.02", "998.30", "1,176,994.72"),
            ("CBB2510035015", "28.11.2025", "5105847816", "591,244.76", "501.05", "590,743.71"),
            ("CBB2510035014", "28.11.2025", "5105847815", "1,524,652.29", "1,292.08", "1,523,360.21"),
            ("2510035017DISCO", "28.11.2025", "1700003193", "-12,346.00", "0.00", "-12,346.00"),
        ]
        for d in data[page * 4:] if page else data[:8]:
            line("  ".join(f"{v:<14}" for v in d), size=7)
        if page == 1:
            line("")
            line("Total             13,295,807.02   11,267.63   13,113,325.39")
            line("RTGS/NEFT Reference : RTGS PAYMENT")
        c.showPage()
    c.save()
    plain = (
        "Dear Sir,\r\nPlease find attached our payment advice.\r\nRegards,\r\n"
        "S. Vedaganesan\r\nContinental Bus Body Builders Limited\r\n"
    )
    html = ("<html><body><p>Dear Sir,</p><p>Please find attached our payment advice.</p>"
            "<p>Regards,<br>S. Vedaganesan<br>Continental Bus Body Builders Limited</p></body></html>")
    m = _mkmsg(
        "Geeta A <geeta.a@acmemetals.example>",
        "FW: RTGS payment to ACME METALS",
        formatdate(1768636800),
        plain,
        html,
    )
    m.add_attachment(pdf.getvalue(), maintype="application", subtype="pdf",
                     filename="220417-ACME-17.01.26.pdf")
    (OUT / "03_contibus_pdf.eml").write_bytes(m.as_bytes())


def f4_sunrise_body_multi_payment():
    def block(pay_date, utr, lines, total_rounded):
        rws = "".join(
            f"<tr><td>{d}</td><td>{n}</td><td style='text-align:right'>{a}</td></tr>"
            for d, n, a in lines
        )
        subtotal = "  ".join("")
        return f"""<p>Payment date: {pay_date} &nbsp; UTR: {utr}</p>
<table border="1"><tr><td>Inv date</td><td>Inv number</td><td>Amount</td></tr>
{rws}
<tr><td></td><td>Rounded total</td><td style='text-align:right'>{total_rounded}</td></tr></table>"""

    b1 = block("08-04-2026", "STBK52026040800971738",
               [("29-Mar-26", "SXE2510051648", "51,332.46"),
                ("29-Mar-26", "SXE2510051649", "5,01,808.11"),
                ("29-Mar-26", "SXE2510051652", "43,500.90"),
                ("29-Mar-26", "SXE2510051653", "9,44,872.82")],
               "15,41,514.00")
    b2 = block("13-04-2026", "STBK52026041300482210",
               [("02-Apr-26", "SXE2610000192", "4,56,148.65"),
                ("02-Apr-26", "SXE2610000193", "1,32,368.02"),
                ("02-Apr-26", "SXE2610000194", "4,72,763.98"),
                ("02-Apr-26", "SXE2610000200", "13,24,544.85")],
               "23,85,825.00")
    html = f"<html><body><p>Dear Sir,</p><p>Payment confirmation for extrusions product, as below.</p>{b1}{b2}<p>Regards,<br>Sunrise Extrusions</p></body></html>"
    plain = "Dear Sir,\r\nPayment confirmation for extrusions product.\r\nRegards,\r\nSunrise Extrusions\r\n"
    m = _mkmsg(
        "SUNRISE EXTRUSIONS <banking@sunrise-extrusions.example>",
        "PAYMENT CONFIRMATION FOR EXTRUSIONS PRODUCT",
        formatdate(1744156800),
        plain,
        html,
    )
    (OUT / "04_sunrise_body_multi_payment.eml").write_bytes(m.as_bytes())


def f5_bharat_body_freetext():
    plain = (
        "Dear Sir,\r\n"
        "Pls find below the RTGS DETAIL made towards your INVOICE NO.\r\n\r\n"
        "BMT2510005615 DT 31.01.2026 = Rs.3033732.66\r\n"
        "LESS- TDS .1% = Rs. 2570.96\r\n"
        "LESS- Credit note for Jan-26 = Rs. 287858.00\r\n"
        "------------------------\r\n"
        "PAYMENT DONE Rs. 2743303.70\r\n"
        "-------------------------\r\n"
        "BMT2510005971 DT 24.02.2026 = Rs.1139531.88\r\n"
        "LESS- TDS .1% = Rs. 965.70\r\n"
        "LESS- Credit note for Feb-26 = Rs. 856480.00\r\n"
        "------------------------\r\n"
        "PAYMENT DONE Rs. 282086.18\r\n"
        "-------------------------\r\n\r\n"
        "Amount: INR. 3025389.88\r\n"
        "Transfer Type: RTGS\r\n"
        "Beneficiary Name: ACME METALS LTD\r\n"
        "Beneficiary Account: xxxxxxxxxxxx0000\r\n"
        "Unique Transaction Reference Number (UTR): STBK52026032800800086\r\n\r\n"
        "Regards\r\nAmit Agarwal\r\n"
    )
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "BharatMetal <bharatmetal2020@example.com>",
        "RTGS DETAIL",
        formatdate(1774656000),
        plain,
        html,
    )
    (OUT / "05_bharat_body_freetext.eml").write_bytes(m.as_bytes())


def f6_zenith_excel():
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A3"] = "In case of Advance from Customer"
    ws.append([])  # spacer noise similar to real
    ws["A5"] = "Date of Advance"
    for col, val in zip("BCDE", ["Amount of Advance", "TDS Amount", "Net amount Remitted", "UTR Reference"]):
        ws[f"{col}5"] = val
    ws["A7"] = "In case of Payment against invoices"
    hdr = ["Inv no", "Inv Date", "Inv Amount", "Amount of which TDS is Deducted",
           "TDS amount", "Amount of CN/other Adjustment", "Net amount Remitted", "UTR Reference"]
    for col, val in zip("ABCDEFGH", hdr):
        ws[f"{col}8"] = val
    ws.append(["ZCC2610000038", "2026-04-30", 4643009, 4643009, 4643.009, None, 4638365.991, None])
    ws.append(["ZCC2610000037", "2026-04-30", 4799448, 4799448, 4799.448, None, 4794648.552, None])
    for _ in range(6):
        ws.append([None, None, None, 0, 0, None, 0, None])
    ws.append([None, None, None, "TOTAL", 9442.457, None, 9433014.543, "ZCC/AXIS/31/2026"])
    xbuf = io.BytesIO()
    wb.save(xbuf)

    plain = (
        "Dear Sir,\r\n"
        "We will be lifting FRP DIVISION material for Rs 9433015/- (Rupees Ninety Four Lakhs "
        "Thirty Three Thousand Fifteen Only), against our request number ZCC/AXIS/31/2026.\r\n"
        "TDS DETAILS AS PER ATTACHED FILE\r\n\r\n"
        "Regards,\r\nAnkit L\r\nDirector\r\nZenith Consumer Care Pvt Ltd\r\n"
    )
    html = "<html><body><pre>" + plain.replace("\r\n", "\n") + "</pre></body></html>"
    m = _mkmsg(
        "Ankit L <zenith.care99@example.com>",
        "Reg: Todays Axis CF Drawdown - FRP",
        formatdate(1746057600),
        plain,
        html,
    )
    m.add_attachment(
        xbuf.getvalue(),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="TDS DEDUCTION DETAIL AGAINST PAYMENT FRP 1.xlsx",
    )
    (OUT / "06_zenith_excel.eml").write_bytes(m.as_bytes())


for fn in [f1_autoneum_hsbc_pdf, f2_fluorochem_body_table, f3_contibus_pdf,
           f4_sunrise_body_multi_payment, f5_bharat_body_freetext, f6_zenith_excel]:
    fn()
    print("built", fn.__name__)

print("\nfiles:")
for p in sorted(OUT.glob("*.eml")):
    print(f"  {p.name}  {p.stat().st_size} bytes")
