# Edge-case fixtures

Six more synthetic `.eml` files, on top of the original 6 in
`tests/fixtures/emails/`, each aimed at one specific checkpoint or code path.
**All data is synthetic.** These live in a separate directory on purpose —
they are for manual/demo testing via `ar-pipeline ingest-eml`, not wired into
the pytest parametrized suites that key off the original 6 by name.

Regenerate with `tests/fixtures/_generator/build_edge_case_fixtures.py`
(needs `reportlab`, `openpyxl`, `Pillow` — all already present).

```bash
uv run ar-pipeline ingest-eml tests/fixtures/edge_cases/*.eml
uv run ar-pipeline tick --repeat 4
```

| Fixture | Tests | Works offline (`LLM_PROVIDER=stub`)? |
|---|---|---|
| `07_edge_foreign_currency.eml` | A USD wire transfer — the non-INR currency checkpoint | **Yes** — the stub detects `USD`/`EUR`/`GBP`/etc. codes and the `non-INR currency` flag fires |
| `08_edge_duplicate_invoice.eml` | The same invoice number listed twice in one payment's table — the duplicate-invoice-number checkpoint | **No** — the offline stub only ever produces ONE synthetic line item (it doesn't parse a whole table into rows), so a duplicate across rows can't be represented. **Needs `LLM_PROVIDER=anthropic`.** |
| `09_edge_not_a_remittance.eml` | A payment-status chase ("your invoice is overdue"), not a payment advice — the `is_remittance=false` path | **Yes** — the stub recognises reminder language with no payment-done signal and returns `is_remittance=false`, `payments=[]` |
| `10_edge_scanned_image_only.eml` | No usable body text; the only settlement data is an image attachment — the vision / scanned-image path | **Partially** — offline, it correctly classifies as `image`, routes to the vision extractor, and lands in review with a "not transcribed" placeholder (proves the code path doesn't error). **Real transcription of the image needs `LLM_PROVIDER=anthropic`** — the embedded image is a legible rendered payment slip so a real vision call has something to read. |
| `11_edge_stale_and_future_dates.eml` | Invoice dated years before the payment; the payment itself dated in the future (a typo'd year) — the payment-date sanity checkpoint | **No** — the offline stub never extracts dates at all (`payment_date`/`invoice_date` are always left `null`), so this checkpoint can't fire offline. **Needs `LLM_PROVIDER=anthropic`.** |
| `12_edge_mismatched_totals.eml` | The stated total doesn't equal the sum of the line items, plus an unsupported `.docx` attachment alongside the real data — the reconciliation checkpoint AND the classifier's skip-unknown-type path | **Half-and-half** — the `.docx` is correctly classified as unsupported and skipped without erroring (confirmed offline). The reconciliation mismatch itself is invisible offline for the same reason as #08 (one synthetic line item can't disagree with itself). **Needs `LLM_PROVIDER=anthropic` to see the reconciliation flag.** |

## Why some checkpoints need the real model

The offline stub (`ar_pipeline/normalize/stub_client.py`) is a deliberately
crude regex pass — good enough to demonstrate the auto-send/review split and
most of the deterministic checkpoints, but it always emits **exactly one**
line item per payment with no dates, because it never parses a table into
structured rows. Three checkpoints are consequences of that:

- **Duplicate invoice numbers** — needs ≥2 line items to compare.
- **Reconciliation (totals don't add up)** — a single line item's amount
  always trivially equals the header total by construction.
- **Payment-date sanity** — no dates are ever populated.

This isn't a bug to fix in the stub (a full table parser would just be
reimplementing the LLM) — it's the honest boundary of what "offline mode"
can prove. Use these three fixtures specifically when you're ready to test
with a real Anthropic key.
