# Invoice ledger & partial payments — design

Date: 2026-09-29 · Status: approved in brainstorming, awaiting spec review

## Problem

Every payment is checked in isolation. The line rule
`invoice_amount − deductions = amount_paid` (validators.py, check 1) flags
every partial payment, and nothing remembers what was already paid on an
invoice. An invoice for ₹100 paid as ₹25, ₹50 and ₹30 produces three flagged
items and no view of progress — and the ₹5 overpayment in that sequence goes
unnoticed.

## Decisions (from brainstorming)

| # | Question | Decision |
|---|---|---|
| Q1 | Source of the true invoice amount | Both: an uploaded CSV of open invoices ("your books") wins; otherwise the amount the first approved remittance quoted ("unverified") |
| Q2 | Routing a consistent partial payment | Auto-approve allowed only when the invoice comes from the books; a partial on an unverified invoice goes to review |
| Q3 | When a payment counts toward the balance | Only once approved (auto or human). Pending payments are shown as "awaiting review" and never reduce the balance |
| Q4 | Invoice-number matching | Normalized exact match is automatic; looser matches are only *suggested* on the review screen and a person chooses |
| — | Architecture | Ledger tables in the pipeline's own database (not in the backend, not computed on the fly) |

Invoice numbers are the client's own numbers, so they are unique across the
whole book, not per payer.

## 1. Data model

New Alembic migration adding two tables (named `invoice` and `invoice_payment`,
singular like every other table in this repo). Downgrade drops them; nothing else
changes.

**`invoices`**

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `invoice_number` | as displayed, e.g. `INV-2026-0412` |
| `number_key` | normalized: uppercase, whitespace and `- / . _` removed. **Unique** |
| `payer_name` | nullable |
| `invoice_date` | nullable |
| `amount` | Decimal, > 0 |
| `currency` | ISO 4217, default `INR` |
| `source` | `books` \| `email` |
| `paid_before_import` | Decimal, default 0 — `amount − outstanding_amount` from the CSV |
| `note` | nullable — e.g. "email said ₹100, your books say ₹120" |
| `created_at`, `updated_at` | |

**`invoice_payments`** — one row per approved line item

| Column | Notes |
|---|---|
| `id` | UUID PK |
| `invoice_id` | FK → invoices |
| `extraction_id`, `line_index` | FK → extractions; **unique together** (approving twice cannot double-count) |
| `amount_paid` | Decimal |
| `deductions_total` | Decimal — the line's own deductions |
| `settled` | `amount_paid + deductions_total` |
| `currency` | from the payment header |
| `payment_reference` | nullable — UTR / cheque number, for duplicate detection |
| `payment_date` | nullable |
| `created_at` | |

### Balance rules

- A payment settles **cash plus that line's deductions**. Invoice ₹100, TDS ₹10,
  paid ₹90 → fully settled.
- Header-level deductions are not allocated to invoices (no correct automatic
  split) and stay out of the ledger.
- Only payments in the invoice's currency count toward its balance.
- Derived, never stored:
  - `paid = paid_before_import + Σ settled` (same-currency rows only)
  - `outstanding = amount − paid`
  - status, with the existing ₹0.02 tolerance: `open` (paid = 0),
    `partially_paid`, `paid` (|outstanding| ≤ 0.02), `overpaid`
    (outstanding < −0.02)
- `awaiting_review` = Σ settled of lines on `pending_review` extractions whose
  normalized invoice number matches. Display only.

### Lifecycle

- **CSV upload** upserts by `number_key`. An existing `email` invoice becomes
  `books`, its amount is replaced, and if the amount changed the old figure is
  kept in `note`. Its payment rows stay attached.
- **Re-uploading is safe.** The CSV's `outstanding_amount` is the ERP's view and
  already includes payments this pipeline delivered, so on import
  `paid_before_import = max(0, amount − outstanding − Σ settled of the invoice's
  posted payments in its currency)` (for a new invoice that is just
  `amount − outstanding`). A blank `outstanding_amount` gives a new invoice 0
  and leaves an existing invoice's `paid_before_import` unchanged. Assumption:
  the export includes payments already delivered; if the export lags delivery,
  outstanding reads high until the next import. A row that changes the currency
  of an invoice that already has payments is skipped.
- **Approval** (the auto-approve path and the reviewer's Save & Approve) posts
  one `invoice_payments` row per line. If no invoice matches, an `email`
  invoice is created from the line's `invoice_amount`, `payer_name` and
  `currency`, using insert-or-fetch so concurrent approvals cannot collide on
  `number_key`.
- **Ledger rows are never removed.** Found during planning: `reprocess_email`
  refuses when an email has any approved extraction, and nothing in the app
  moves an approved extraction back out of `approved`. The only way a payment
  row is written is approval, and there is no path that undoes approval, so
  there is no deletion path to build. (Reversal after approval stays a Known
  limit.)

## 2. Checks and routing

New module `ar_pipeline/ledger/checks.py`:
`check_against_ledger(session, payload) -> list[str]`. `validate_payload` and
`normalize_email` stay pure (no session), so the ledger check runs where a
session exists: in `normalize_one` for each new extraction row (after earlier
rows of the same email have been approved and posted), and in `save_edits`.
Its flags are added to the same `validation_flags` list and start with
`line {i}: ` so the review screen's existing `^line\s+(\d+):` pattern pins them
to the line. `CHECK_VERSION` becomes `"3"`.

### "Stated" invoice amount vs "settled"

Found during planning: when an email only describes the payment, the stub
always (and the real model often) sets a line's `invoice_amount` equal to what
it paid. The ledger therefore treats a line's `invoice_amount` as a real claim
about the invoice total **only when it differs from that line's settled
amount** (`amount_paid + deductions`). When they're equal, the line says
nothing about the invoice total, and it is simply a payment of `settled`
against whatever the ledger says the invoice is. This keeps installment emails
against a books invoice clean without an "amount differs" false alarm.

### Change to existing check 1 (line rule)

- `amount_paid + deductions > invoice_amount` → still flagged (unchanged wording).
- `amount_paid + deductions < invoice_amount` → no longer flagged by
  `validate_payload`. It is a **partial payment**, and the ledger check decides:
  - invoice from books, and settled ≤ outstanding → **no flag**
  - invoice unverified (`email`) → flag
  - invoice not in the ledger → flag (the stated amount is the only figure there
    is, and it's unverified)

### Ledger flags

| Flag | When | Wording (example) |
|---|---|---|
| Partial, unverified | partial against an `email` invoice or an unknown invoice | "line 0: INV-1 — partial payment ₹25 of ₹100 — invoice amount comes from an email, not your books" |
| Overpayment | settled > outstanding + 0.02 | "line 0: INV-1 — pays ₹30 but only ₹25 outstanding — overpaid by ₹5" |
| Already paid | invoice status is `paid` or `overpaid` | "line 0: INV-1 — invoice is already fully paid" |
| Amount differs | the line states an invoice total (its `invoice_amount` ≠ its settled amount) and that total ≠ the ledger amount (> 0.02) | "line 0: INV-1 — email says invoice ₹100, your books say ₹120" (or "…the earlier email said…" for `email` invoices) |
| Possible duplicate | an existing payment row has the same `payment_reference`, invoice and `amount_paid` | "line 0: INV-1 — reference UTR123 for ₹50 was already approved" |
| Currency mismatch | payment currency ≠ invoice currency | "line 0: INV-1 — payment in USD, invoice in INR — not applied to the balance" |
| Payer mismatch | both names present and they don't match (rule below) | "line 0: INV-1 — invoice belongs to Meridian Steel; payment is from Arcadia Foods" |
| Near match | no exact match, but a near match exists (rules below) | "line 0: MST/2026/780 not found — did you mean MST-2026-7801?" |
| Not in your books | no exact match and at least one `books` invoice exists | "line 0: INV-9999 isn't in your open invoices" |

When a line has a near match, "Near match" replaces "Not in your books" for
that line (one flag, the more useful one).

A full payment on an unknown invoice before any CSV has been uploaded produces
no ledger flag, the same as today.

Any flag sends the extraction to review (existing routing rule, unchanged).

### Re-check at approval

Two pending installments can each pass on their own and overpay together,
because pending payments don't count (Q3). So the ledger check runs again at
approval time, inside the approval transaction:

- **Auto-approve path:** new flags → the extraction goes to `pending_review`
  with the new flags instead.
- **Reviewer's Save & Approve:** if the re-check produces flags that aren't in
  the extraction's stored `validation_flags` (what the reviewer was shown), the
  stored flags are updated, the approval is stopped, the edits are saved, and the page
  reloads with the flags and a flash "Checks changed since you opened this —
  review the new flags and approve again." Approving a second time with the
  same flags succeeds (a human may overrule a flag after seeing it).

Approvals on the same invoice are serialized: the re-check takes a row lock on
the matched invoice (`SELECT … FOR UPDATE`), or a transaction-scoped advisory
lock on the number key for a not-yet-created invoice, held until the approval
commits. Lines in one payment that share a number key are checked against the
outstanding left after the earlier lines.

Pending payments against the same invoice appear on the review screen as a
note, not a flag: "₹50 more awaiting review on this invoice".

## 3. Screens

### Invoices tab (new) — `/review/invoices`

- Nav entry after Approved.
- Tiles, each filtering the table: Outstanding total (₹), Partially paid, Paid,
  Needs attention (overpaid, or amount differs from books).
- Table columns: invoice no., payer, amount, paid, awaiting review,
  outstanding, progress bar, status pill, source badge (*your books* /
  *unverified*).
- Live refresh through the existing `data-poll-url` fragment mechanism
  (`/review/invoices-rows`), with the existing guard against swapping while
  the user is interacting.
- Upload form: file input and "Import open invoices". A "Download a template"
  link serves the blank CSV.

### CSV import

- Required columns: `invoice_number`, `invoice_amount`.
- Optional: `payer_name`, `invoice_date` (YYYY-MM-DD), `currency` (default
  INR), `outstanding_amount`.
- Limits: 1 MB and 5,000 rows, rejected before parsing.
- A row is skipped with a numbered reason when: the invoice number is blank,
  an amount isn't a number or is ≤ 0, the outstanding amount is negative or
  greater than the amount, the date doesn't parse, or the number appears twice
  in the file (both rows are skipped).
- Valid rows are imported in one transaction. Result flash: "Imported 12 ·
  updated 3 · 2 rows skipped", with the skipped rows listed on the page.

### Invoice detail — `/review/invoices/{id}`

- Invoice facts, progress bar, and the `note` if there is one.
- Payment history as a running ledger: date, reference, paid, deductions,
  settled, balance after, and links to the email and review pages. "Paid before
  import" is the first row when it's non-zero.
- An "Awaiting review" list, excluded from the balance.

### Review screen (existing) — per-line ledger strip

Under each line item's fields:

> INV-2026-0412 · your books ₹100 · paid ₹25 · outstanding ₹75 · after this payment ₹25

When there's a near match, the strip shows a button per suggestion,
"Use INV-2026-0412". It posts to `/review/{id}/use-invoice`, which rewrites that
line's `invoice_number` through the existing edit path (so it lands in edit
history), re-runs the checks, and returns to the review screen. There is no
"it's a new invoice" button; approving with the near-match flag showing is that
decision.

### Unchanged

The backend and the payload delivered to it; the Journey, Queue and Approved
pages.

## 4. Matching rules

- **Normalized key** (automatic match): uppercase, remove whitespace and
  `- / . _`.
- **Near match** (suggestion only; up to 3, same payer first). A line's key
  suggests a known invoice's key when any of these holds:
  1. They are equal once leading zeros are removed from every number group
     of the *original* invoice number, before separators are stripped
     (`MST-2026-07550` ≈ `MST-2026-7550`; stripping separators first would
     merge `2026` and `07550` into one run and hide the zero).
  2. The line's key is a suffix of the invoice's key and is at least 4
     characters long (`7550` ≈ `MST20267550`).
  3. Both keys are at least 6 characters long and are one edit apart
     (insert, delete or substitute one character).
- **Payer match:** lowercase, remove punctuation, drop the words *pvt,
  private, ltd, limited, llp, inc, co, the*, and collapse whitespace. Names
  match when equal or one contains the other. Only compared when both are
  present.

## 5. Demo data

- `demo/open-invoices.csv`: the invoice numbers the existing demo emails quote
  (`MST-2026-7550`, `-7601`, `-7602`, `-7712`, `-7733`, `-7741`, `-7801`).
  `ORB-2026-3390` is deliberately absent (shows the unverified path). One row
  has `outstanding_amount` below its amount (shows "paid before import").
- A new ₹1,00,000 invoice for the installment story, plus three emails paying
  ₹25,000, ₹50,000 and ₹30,000, ending overpaid by ₹5,000.
- One email quoting `MST/2026/780`: its key `MST2026780` is one deletion away
  from `MST20267801` (rule 3), so the review screen suggests `MST-2026-7801`.
- Six new emails appended to `~/Downloads/demo-emails-round-2.md`, one payment
  per email so they work in stub mode.
- `demo/open-invoices-template.csv` (header row only), served by the template
  link.

## 6. Rollout

- One Alembic migration (two tables).
- `ar-pipeline ledger-backfill`: posts every already-approved extraction into
  the ledger, in approval order. Safe to re-run (unique rule).

## 7. Testing

- **Unit:** key normalization; each near-match rule and its non-matches; payer
  comparison; balance math (TDS settles in full, tolerance, overpaid,
  paid-before-import, other currency excluded).
- **Checks:** one test per ledger flag, plus "a partial against a books invoice
  gets no flag"; the changed check 1 (partial no longer flagged by
  `validate_payload`, over-line still flagged).
- **Approval:** posting rows; approving twice doesn't double-count; the
  two-pending-installments race on both the auto and reviewer paths;
  insert-or-fetch on a new invoice; an installment whose `invoice_amount`
  equals its settled amount raises no "amount differs" flag.
- **CSV:** a good file; each skip reason by row number; an `email` invoice
  upgraded to `books` keeps its payments and gets a note; the size limit.
- **Routes:** Invoices tab and fragment, tile filters, detail page, upload,
  template download, "Use INV-…".
- **End to end:** the 25k / 50k / 30k story through `advance_once` with the
  fake LLM and a books CSV, ending in status `overpaid` by ₹5,000.
- **Backfill:** re-running it posts nothing new.

## Known limits (out of scope)

- Reversing a payment after it has been approved (there is no un-approve path
  in the app today).
- Allocating header-level deductions across invoices.
- A live ERP connection (CSV import only).
- More than one currency on one invoice.
