# Email fixtures

Six `.eml` files covering every remittance format the pipeline must handle.
**All data is synthetic** — names, amounts, invoice numbers, UTRs, and bank
references are invented. The MIME structure, forwarding chains, and the
table-vs-free-text-vs-attachment layout are faithful to real client emails
(kept out of git under `samples/`).

Regenerate with `tests/fixtures/_generator/build_fixtures.py` (needs
`reportlab` + `openpyxl`).

| Fixture | Arrives as | Remittance data | Exercises |
|---|---|---|---|
| `01_fwd_bank_advice_pdf.eml` | internal forward of a bank advice; inline logo + PDF | **native-text PDF** attachment — 1 line item, header fields (advice ref, value date, remitter, UTR-style "Other reference") | `pdf_text` extractor; sender ≠ vendor; skip inline logo |
| `02_fwd_body_table.eml` | internal forward; inline signature image | **HTML `<table>` in body** — 13 invoice lines, a payment-level TDS 194Q deduction, narrative UTR + total | `html_table` extractor; header-level deduction; forwarded chain noise |
| `03_fwd_multiline_pdf.eml` | internal forward; PDF attachment | **2-page native-text PDF** — ~8 lines mixing positive invoices and negative credit-note ("DISCO") lines, total row, "RTGS/NEFT Reference: RTGS PAYMENT" (no real UTR), amount in words | `pdf_text` extractor; multi-page; negative lines; missing UTR |
| `04_direct_body_multi_payment.eml` | direct from vendor | **HTML tables in body — TWO separate payments** (different UTRs + dates), each with several lines + a rounded total; Indian digit grouping (`5,01,808.11`) | one email → **N remittance payloads**; Indian number format |
| `05_direct_body_freetext.eml` | direct from vendor | **free text in body (no table)** — 2 invoice blocks, each `INV = Rs.X / LESS TDS / LESS Credit note / PAYMENT DONE`, then `Amount / Transfer Type / UTR` | `body_text` extractor; per-line multi-deduction; LLM-normalization (no deterministic parse possible) |
| `06_direct_excel.eml` | direct from vendor; minimal body | **`.xlsx` attachment** — vendor's TDS template: "Advance" vs "invoices" sections, 2 real rows + a TOTAL row, `UTR Reference` column holds a *request number* not a bank UTR | `excel` extractor; template with header sections + noise rows; UTR that isn't a UTR |

**Not yet covered:** a scanned PDF / photographed settlement (image → LLM
vision). The `vision` extractor is built and unit-tested with a synthetic
image; add a real fixture here when one is available.
