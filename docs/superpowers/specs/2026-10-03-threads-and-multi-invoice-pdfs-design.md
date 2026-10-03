# Email threads & multi-invoice PDFs — design

Date: 2026-10-03 · Status: approved section by section in brainstorming; awaiting spec review

## Problem

1. **Threads.** A forwarded chain (the Hindalco "Payment Remittance details"
   thread) carries 14 payments, each in its own quoted reply. Today the whole
   email is one AI input: 66k chars, silently truncated at 40k, older payments
   lost, and every new reply re-pays to read the entire history. Nothing
   stops a re-forward from creating the same payments again.
2. **Multi-invoice PDFs.** A payment advice (Global TVS → Hindalco) lists 4
   invoices and 16 "…DISCO" adjustment rows in a table spanning 2 pages, with
   wrapped invoice numbers, trailing-minus TDS, no bank reference, and the
   client (Hindalco) printed as "Vendor Name". Today the AI must re-type every
   row (output cap ~250 lines), nothing checks the result against the
   document's own totals, adjustments have no meaning in the ledger, and
   payer/beneficiary are easy to swap.

## Decisions (from brainstorming)

| # | Question | Decision |
|---|---|---|
| 1 | Shape of multi-invoice PDFs | One payment, many lines (type A), incl. adjustment rows |
| 2 | "DISCO" rows | Discounts/debit notes against specific earlier invoices: reduce that invoice's balance; uncertain matches confirmed by a reviewer |
| 3 | Older payments on first sight of a thread | Process every unrecorded payment, never auto-send historical ones, go-live cut-off, bulk "already recorded" |
| 4 | Bank / ERP receipts data | Not available now; design a pluggable `ReceiptsSource` for later |
| 5 | Architecture | Split threads into messages before the AI (approach 1), using the mailbox thread id only for grouping |
| — | Process rule | "Unread" and "latest only" were rejected: unread is mailbox state other people change, and latest-only silently drops payments on first sight. The rule is **process every payment not yet recorded**. |

## 1. Data model

**`email`** gains `thread_key` (Gmail threadId / Graph conversationId /
`References` root for .eml) — for grouping on screen only — and
`rfc_message_id` (`Message-ID` header).

**New table `email_message`** — one row per message found inside an email:

| Column | Notes |
|---|---|
| `id`, `email_id` | |
| `position` | 0 = top (newest) message, 1 = first quoted, … |
| `sender`, `sent_at`, `raw_header` | parsed from the quoted header; top message uses the email's own `From`/`Date`; `raw_header` kept because dates like `02/03/2026` are ambiguous |
| `is_internal` | sender domain ∈ `CLIENT_DOMAINS` |
| `body_html`, `body_text` | that message's piece; **stored only when status is `new`** |
| `fingerprint` | see §2 |
| `status` | `new` · `seen` · `no_content` · `failed` |
| `seen_reason` | `fingerprint` or `references_recorded` |
| `seen_in_message_id` | for `seen`: the earlier row it repeats |
| `error_detail` | for `failed` |

**`extraction_source`** gains `email_message_id`. **`extraction`** gains
`email_message_id`, `payment_key`, `payment_key_strength` (`strong`/`weak`),
`historical_reason` (`earlier_message` / `before_go_live` / null),
`duplicate_of_id`. **`invoice_payment`** gains `kind` (`payment` /
`adjustment`). `EXTRACTION_STATUSES` gains `already_recorded` and `duplicate`
(a detected duplicate is stored as a row — auditable and shown on the email
view — but never routed, delivered or posted).

**"Recorded"** — used by §2 and §3 alike — means an extraction in
`pending_review`, `approved` or `already_recorded`. A rejected extraction
counts as recorded **only** when it was a not-a-remittance rejection
(`is_remittance = false`), i.e. a person confirmed "not a payment"; any other
rejection, and `superseded` / `duplicate`, do not.

**Payment format (backend contract)**: each line item gains
`kind: "invoice" | "adjustment"` (default `invoice`) and `applies_to: str | null`;
the envelope gains `schema_version` (`"2"`). The stub backend is updated.

**Settings** (all optional): `CLIENT_DOMAINS` (comma list), `CLIENT_NAMES`
(comma list), `GO_LIVE_DATE` (YYYY-MM-DD).

## 2. Thread splitter

`split_email(email) -> list[MessagePart]`, pure (no AI, no network), called at
the start of classify.

**Boundaries**, most reliable first:
1. HTML markers when the email has HTML — Gmail `div.gmail_quote`, Outlook
   `divRplyFwdMsg` / header-block separators, `<blockquote>`. The **HTML** is
   split, so each message keeps its own tables for the `body_table` extractor.
2. Plain-text header blocks (only when there is no HTML): Outlook
   `From: / Sent: / To: / Subject:` (incl. `*From:*`), Gmail
   `On <date>, <name> <<email>> wrote:`, forward banners
   (`-------- Forwarded Message --------`, `---------- Forwarded message ---------`).
   Patterns live in one list (English only for now).
3. Fallback: the whole body is one message — today's behaviour.

**Invariant:** the parts, concatenated, contain all of the original body text
(tested on every fixture).

**Per message:** sender, `sent_at`, raw header; body (signatures **kept** —
payer names often live there); `is_internal`; `no_content` when internal
**and** no payment signal (no amount, no reference-shaped code, no table).

**Fingerprint** = hash of sender address (lowercased) + first ~60 normalised
words of the body (quote markers `>`, "CAUTION…" banners, whitespace and
punctuation removed). `sent_at` is **excluded** — the quoted copy's time has
no timezone and differs from the original `Date` by the sender's UTC offset.

**Seen rule:** a message is `seen` when an earlier `new` message **in any
email** (excluding this email itself) has the same fingerprint **and** every
extraction from that earlier message is *recorded* (§1). If the earlier copy
failed, was rejected as a bad extraction, or was superseded, the message is
read again (the payment key prevents duplicates).

**Reprocess** reuses the stored split; it never re-splits or self-matches.

**Failures:** a splitter exception is logged and the whole body becomes one
message; splitting never errors an email.

**Truncation:** a single message still over 40,000 chars is truncated as now,
but its payments get the flag "content was truncated — check nothing is
missing" (never auto-approved).

**Known weak spot:** inline replies that edit quoted text miss the
fingerprint; the reference check (§3) usually still skips them, and the
payment key prevents duplicates regardless.

## 3. Payment keys, history, go-live

**Step 1 — reference check before the AI.** Code extracts every
reference-shaped code from the message (UTR/RTGS/NEFT/IMPS, payer document
numbers). If **every** one already belongs to a *recorded* payment (§1), the message
becomes `seen` (`references_recorded`) and **no AI call is made**. One new
reference, or none found → read normally.

**Step 2 — payment key** (code, after extraction, from the header):

| Available | Key | Strength |
|---|---|---|
| Bank reference | `utr:<ref>` | strong |
| Cheque number | `chq:<payer>:<number>` | strong |
| Payer document number | `doc:<payer>:<docno>:<FY>` (Indian FY Apr–Mar from payment date) | strong |
| none | `soft:<payer>:<amount>:<date>` | weak |

`<payer>` uses the ledger's payer normalisation. Document numbers go into
`payment_reference` with the new type `payer_document`.

**Enforcement**
- Partial unique index: one row per strong key among `pending_review`,
  `approved`, `already_recorded`.
- Check-then-insert under a per-key advisory lock taken **before** the lookup.
- Arrival with an existing strong key: amount within ₹1 → stored with status
  `duplicate`, keyless, `duplicate_of_id` set, shown on the email view as
  "duplicate of payment #…" (never routed, delivered or posted); amount
  differs more → created **without a key**, linked via `duplicate_of_id`,
  flagged "this UTR was already used for ₹X".
- Weak keys never block: flag "possible duplicate of payment #…".
- Rejected/superseded rows don't count; a re-arrival carries "rejected before
  on <date> by <name>: <reason>".

**History.** `historical_reason` = `earlier_message` (below the newest
content message) or `before_go_live` (payment date, else message `sent_at`,
earlier than `GO_LIVE_DATE`). Historical payments are never auto-approved and
carry an explanatory flag.

**`already_recorded`** (new status): posts to the invoice ledger, is **never**
delivered to the backend. Queue bulk action "Mark selected as already
recorded" for historical items only. Consistent with the CSV import rule,
which subtracts recorded payments.

**`ReceiptsSource.match(key, amount, date, payer) -> Receipt | None`** — a
`NullReceiptsSource` now (always None). A future bank-statement/ERP source
auto-resolves historical items to `already_recorded` / "received, never
applied" / "not found" with no other change.

## 4. Multi-invoice PDFs, adjustments, payer, totals

**4.1 PDF reading (code)**
- Wrapped cells rejoined: fragments without spaces glued (`WBBEL2510004⏎583`
  → `WBBEL2510004583`); fragments with spaces joined with a space.
- Tables continuing across pages with an identical header row are merged.
- Numbers: trailing minus (`2,377.00-`), parentheses, Indian grouping.
- Page text inside table bounding boxes is dropped (no duplicate input).

**4.2 Table mapping.** When a source has one clear table:
1. The AI receives the header row + first 3 rows and returns a column mapping
   (`Bill No`→invoice_number, `Gross Amount`→invoice_amount, `TDS`→tds
   deduction, `Adv/Debit`→adjustment amount, `Net Payment`→amount_paid) and
   the adjustment-row rule (e.g. Gross empty and Adv/Debit present).
2. Code applies it to every row — figures copied exactly, any row count.
3. The mapping is saved per payer in `vendor.column_hints`; later advices
   from that payer need no AI call for rows (header details by pattern, AI
   only if missing).
4. Fallback to today's full-AI read when there is no clean table, the mapping
   is invalid, or the totals check (4.4) fails — a saved mapping that fails
   the totals check is discarded and re-learned.

**4.3 Adjustment lines.** `kind: "adjustment"`, `applies_to`. Representation:
invoice_amount 0, deduction X (`discount` / `debit_note` / `credit_note`),
amount_paid −X — the existing line identity holds and the payment total
balances. The negative-amount check exempts adjustment lines.
Matching `applies_to` (code): exact digit match in the same payment, then the
ledger → auto; one digit off → suggestion ("Use …" button, reviewer confirms);
no candidate → flag "adjustment of ₹X — which invoice does it reduce?".
Unresolved adjustments block auto-approve. Ledger: posts `kind=adjustment`
settling X against `applies_to`.

**4.4 Totals check (code).** Read the document's Total row
(`Total`/`Grand Total`/`Net Payable`: gross, debit, TDS, net) and the amount
in words (Indian parser: crore/lakh/thousand/hundred/paise). Extracted lines
must equal the column totals; extracted net must equal the words. A
difference over ₹1 → flag "doesn't match the document's own totals (document
₹X, extracted ₹Y)", never auto-approved. No totals found → no check (noted).

**4.5 Payer vs beneficiary.** Prompt: "the receiving company is
<CLIENT_NAMES>; the payer is the other party". Code guard: extracted payer
matches a client name → flag "payer looks like the receiving company" and
never auto-approve.

## 5. Flow, failures, UI, rollout

**Flow.** Classify: split → reference check → sources per new message
(attachments on the topmost message with content). Extract: existing
extractors per source with 4.1. Normalize per new message, **oldest first**
(so installments post in order): table mapping or full AI → payment key &
duplicates → history → totals → payer guard → adjustments → existing payment
and ledger checks → routing (auto only if not historical, no flags,
confidence ≥ threshold). Email done when every new message is processed.

**Failures.** Per message: AI refusal / output cap / bad document marks that
message `failed`; siblings still produce payments; the Errors tab lists
failed messages with per-message Retry. DB duplicate rejection is a normal
outcome. Splitter or mapping failure falls back to the safer path.

**Cost.** Prompt caching on the system prompt (calls 2…N of a thread reuse
it). Calls avoided: seen messages, reference check, saved mappings. Per-email
log line: messages new/seen/skipped, AI calls made vs avoided.

**Review screen.** Source message shown (with "show full thread": each
message and its status); badges for totals match/mismatch, historical
reason, duplicate-of; adjustment strip with match / "Use …" / "which
invoice?"; Queue bulk "Mark as already recorded" for historical items.

**Stub.** Keyword column mapper (`Bill No`/`Invoice`, `Gross`, `TDS`, `Net`,
`Adv/Debit`) so table PDFs work offline; split messages also suit the stub.

**Rollout.** One additive migration (table, columns, status). Backfill: one
`email_message` per existing email; `payment_key` for existing extractions;
**existing duplicate keys** are resolved before the unique index is created
(later rows keep no key and get "possible duplicate"). New settings in
`.env.example` and `scripts/setup`.

**Stages** (separate plans, each usable alone):
- **Stage 1 — threads:** splitter, `email_message`, thread memory, reference
  check, payment keys & duplicates, history & `already_recorded`, per-message
  failure isolation, prompt caching, review thread view, Queue bulk action.
- **Stage 2 — multi-invoice PDFs:** PDF reading fixes, table mapping + stub
  mapper, totals check, adjustments + ledger posting, payer guard, payment
  format change (`kind`, `applies_to`, `schema_version`).

## Testing

- Synthetic Hindalco-style chain (redacted): first arrival → 14 payments, 13
  historical held, 1 routed; re-forward with one new reply → exactly 1 new
  payment and no AI calls for the rest; inline-edited copy → reference check
  skips it; reprocess → no self-match.
- Splitter fixtures: Outlook chain, Gmail "wrote:", forward banner, no
  quotes, HTML-with-tables chain; "nothing lost" invariant on all.
- Payment keys: each key type; FY rollover of document numbers; ₹1 rounding
  duplicate; same UTR different amount; weak key flag; concurrent arrivals.
- Synthetic TVS-style PDF (reportlab): 1 payment, 4 invoices, 16 adjustments,
  1 auto-matched adjustment, totals reconciled incl. amount in words; second
  advice uses the saved mapping; corrupted mapping caught and discarded.
- Words-to-amount parser; number formats; 2,000-row mapping.
- Migration with pre-existing duplicate keys still creates the unique index.
- Full suite green; offline stub rehearsal end to end.

## Known limits (out of scope)

- Non-English quote headers fall back to whole-email processing.
- First sight of a long thread costs one AI call per new message.
- Payments with no reference rely on a person for duplicate decisions.
- `already_recorded` is a human judgement until a receipts source exists.
- One go-live date for all customers.
- PDFs without a ruled table use the full-AI path and keep its size limits;
  advices without totals get no totals check.
- Several separate payment advices bundled in one PDF (type B) is not
  designed here.

## Planning-time corrections (Stage 1 plan, 2026-10-03)

Found while planning against the code; these supersede the sections above
where they differ.

1. **No `rfc_message_id` column.** `email.internet_message_id` already holds
   the RFC `Message-ID` (Gmail `Message-Id` header, Graph
   `internetMessageId`, `.eml` `Message-ID`). It is reused.
2. **Fingerprint = first 60 normalised words of the message body only** (no
   sender). The same quoted message shows the sender as a bare name in one
   copy and `name <address>` in another. And the fingerprint "seen" rule
   applies **only** to messages whose own text has a payment signal (an
   amount or a reference) **and** that do not carry the email's attachments —
   a generic "please find attached" note is identical across different
   payments and must never be skipped.
3. **`email_message` stores `body_text` + extracted `tables` (JSONB), not HTML
   fragments.** The splitter walks the HTML once, cuts at header blocks, and
   assigns each innermost table to the message it falls in.
4. **Reference pattern** accepts `UTR no.`/`UTR No:`/`RTGS ref` forms and only
   counts keyword-anchored references (bare code-shaped tokens include invoice
   numbers and would make the reference check never fire).
5. **All-seen emails finish `done`.** When splitting leaves no new content
   message and no attachments, the email goes straight to `done` instead of
   the "no extractable content" error.
6. **Payment keys for existing rows** are assigned by an
   `ar-pipeline threads-backfill` command after the migration (the migration
   creates the unique index while every key is still empty, so it cannot
   fail); conflicts found during backfill leave the later row keyless with a
   "possible duplicate" flag.
7. **A `duplicate` extraction counts as recorded for the "seen" rule** (§2):
   the payment it duplicates is already recorded, so re-reading that message
   on every later forward would only spend AI calls. It still never counts
   for payment-key uniqueness.

## Planning-time corrections (Stage 2, 2026-10-04)

Found while planning against the code; these supersede spec §4 where they differ.

1. **Saved mappings are keyed by the table's header layout, in a new `column_mapping` table, not per payer in `vendor.column_hints`.** The payer is only known after the header is read, so a per-payer lookup cannot run first; the header signature (cells lower-cased, punctuation removed) identifies the advice format, and the totals check catches a mapping reused on the wrong document. The payer slug is stored for display only.
2. **The header is always read by one small AI call** (the table cut to its header, 3 rows and the Total row). Rows are never re-typed by the AI, which was the cost and output-cap problem; reading the header "by pattern" was dropped as fragile.
3. **Adjustment rows are recognised in code, not by an AI-returned rule:** a row is an adjustment when its net is negative, or its gross is empty/zero and its adjustment column has a value. Its type is `discount` when its number contains "DISC", else `debit_note`.
4. **"One digit off" means one inserted or missing digit only.** A substituted digit is usually the neighbouring invoice (…4515 vs …4516) and would be a wrong suggestion.
5. **The column-total check runs on the table path only** (it needs the mapping); every single-payment read also gets the amount-in-words check. A multi-payment message gets no totals check.
6. **On the table path `total_paid_amount` is the sum of the lines** and header deductions are empty; the totals check compares it with the document.
7. **The table path needs one clear table:** exactly one table in the message's sources with ≥ 6 data rows, ≥ 3 columns and ≥ 2 mostly-numeric columns. Smaller tables stay on the full-AI path, which handles them well.
8. **Adjustment matching looks at this payment's invoice lines first, then the ledger.** An adjustment is posted to the ledger only when its target invoice exists there (after this payment's own invoice lines are posted); an adjustment is never used to create an invoice.
9. **CSV refresh keeps sticky flags.** `refresh_pending_flags` used to drop history, truncation and duplicate flags (a Stage 1 gap); all flag recomputation now goes through `normalize/recheck.py`.
10. **Totals status is derived, not stored:** `extraction.read_info` stores the document's printed totals; match / mismatch is recomputed whenever the payment is shown or rechecked, so a reviewer's correction clears the mismatch.

## Execution-time rulings (Stage 2)

Behaviour decisions made while building.

- An adjustment reduces the invoice it names even if this payment already settles it (the line is then flagged "already fully paid").
- A page-top continuation row is glued only onto text cells, never amounts or totals.
- Total-ish rows (including sub-totals) are never lines; the last non-sub-total total is the Total row, and rows after it are dropped.
- An unlabelled last row with a blank number that equals the column sums is the Total row.
- The header is the last name row above the first row of figures (within the first 5 rows); banner rows above it are dropped.
- Padding rows (blank number, zero amounts) are skipped.
- A bare "Amount" column is `amount_paid` only when no net/paid column exists.
- A row with a negative gross is an adjustment.
- Amounts written "Rs. 1,000/-" are read.
- An adjustment against an invoice in another currency is flagged and not applied.
- An adjustment larger than the outstanding balance is flagged.
- A table-path header message over the prompt cap gets the truncation flag.
