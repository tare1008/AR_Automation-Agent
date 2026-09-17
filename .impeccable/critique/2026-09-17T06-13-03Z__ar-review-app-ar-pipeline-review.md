---
target: AR Review app (ar_pipeline/review)
total_score: 25
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 2
target_identity: "file:/home/atharvatare/AR_Automation-Agent/AR Review app (ar_pipeline/review)"
timestamp: 2026-09-17T06-13-03Z
slug: ar-review-app-ar-pipeline-review
---
Method: dual-agent (A: general-purpose design-review subagent · B: general-purpose detector subagent)

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3/4 | Badges/stat tiles communicate state well at rest, but every action (Save, Approve, Reject, Retry, Resend, Reprocess) is a bare form POST with no loading/disabled state. |
| 2 | Match Between System / Real World | 4/4 | Deduction taxonomy, payer/reference vocabulary, and the auto- vs. human-approval framing are authentically AR-specific. |
| 3 | User Control and Freedom | 2/4 | No cancel/discard-edits link on the edit form; no undo for an approval or rejection once submitted. |
| 4 | Consistency and Standards | 3/4 | Badge/color system is consistent app-wide, but Save / Save & Approve / Reject / Reprocess all render as visually equal-weight buttons despite very different stakes. |
| 5 | Error Prevention | 2/4 | Reject requires a typed reason (good), but Reprocess (likely destructive to unsaved edits) and Save & Approve (ships to a real backend) have zero confirmation. |
| 6 | Recognition Rather Than Recall | 2/4 | Validation flags render as free-floating badges with no link to the specific field that triggered them. |
| 7 | Flexibility and Efficiency of Use | 2/4 | No bulk-approve, no keyboard shortcuts, no "approve → next" flow for a reviewer doing this all day. |
| 8 | Aesthetic and Minimalist Design | 2/4 | Token system and chrome are restrained, but the detector confirmed a real WCAG AA contrast failure in dark mode (below), and `detail.html` stacks original + full edit form + reject form + reprocess form + edit history with no separation. |
| 9 | Error Recovery | 2/4 | `errors.html` dumps raw `error_detail`/`last_error` text verbatim with no plain-language framing or suggested next step beyond a bare Retry/Resend. |
| 10 | Help and Documentation | 2/4 | The journey page's info dialog is a real, well-executed help affordance, but it's the *only* one in the app — the deduction-type picker on the edit form (`tds`/`credit_note`/`advance_adjustment`/`discount`/`rounding`/`other`) offers no definitions anywhere. |
| **Total** | | **25/40** | **Acceptable (63%)** |

Most real interfaces score 20-32/40; 25/40 is Acceptable — a real, working tool with specific, fixable gaps rather than a broken one.

## Design Specificity Verdict

**LLM assessment**: This is genuinely authored for an AR reviewer's workflow, not a generic CRUD skin. The deduction taxonomy in `detail.html`'s `deduction_row` macro (`tds`, `credit_note`, `advance_adjustment`, `discount`, `rounding`, `other`) is real AR vocabulary, not placeholder enum values. The journey page's info dialog explicitly teaches the auto-approve vs. human-review distinction, confidence scoring, and delivery semantics — product-specific onboarding, not boilerplate. `stub_backend/app.py` frames itself as "Stand-in for the client's real system" and mirrors `POST /remittances` with idempotency-key semantics — a narratively honest demo device. Field sets (`payment_reference_type`, invoice-vs-payment amount split, edit history with old/new values) read as informed by real remittance-matching pain points.

Where it slips toward generic-admin: every input is an undifferentiated `<input type="text">`, including currency amounts and dates — no numeric/date input types, no currency formatting, no masked fields. That sits right next to genuinely domain-specific field names.

**Deterministic scan**: The bundled detector (`impeccable detect`) ran clean structurally — it found no slop-pattern hits (no gradient-text, no generic hero, etc., because this isn't that kind of surface) but did find **3 low-contrast findings, all in `ar_pipeline/review/templates/base.html`**, tracing back to `ar_pipeline/review/static/review.css`:

- White text (`#fff`) on `--brand` background (`#6a9bd1` in dark mode) = **2.9:1**, below the 4.5:1 AA minimum for normal text. Traced to `button[type="submit"], .btn-primary { background: var(--brand); color: #fff; }` (review.css:130) — this is every primary button (Log in, Save, Save & Approve, the info-dialog's "Got it") in dark mode specifically.
- A second, apparently duplicate report of the same pair (the detector gives no selector, so this may be one real instance counted twice, or two separate elements sharing the rule — genuinely ambiguous from the JSON).
- A `:hover` state reported at **1.0:1** (`#8db3e0` on `#8db3e0` — identical color on identical background, meaning literally invisible text). I traced the actual `:hover` cascade by hand and could not reproduce an exact same-selector pairing that produces this — `button[type="submit"]:hover` changes only the *background* to `--brand-strong`, not the text color, which should stay white. This finding is plausible evidence of the detector's non-cascade-aware pairing (matching any declared color against any declared background using the same variable value, without confirming one selector produces both) rather than a confirmed literal bug — but it correctly points at a real, confirmed root cause either way (see next paragraph), so I'm not discounting it, just flagging the confidence level honestly.

The real root cause both findings point to: **`--brand` and `--brand-strong` are dual-purpose tokens.** In dark mode they need to be *light* blues so link/nav text reads against the dark page background — which is why they were set to `#6a9bd1`/`#8db3e0` — but the same tokens are also used as *solid fill* behind hardcoded white button text, where a light-mode-appropriate dark navy (`#21456d`, which is exactly what light mode uses and passes at 10:1) is what that role actually needs. One token, two incompatible jobs. This is exactly the kind of structural finding a holistic design read tends to miss because it looks fine on a light-mode screenshot and only breaks in the theme variant a reader might not open.

**Likely false positives**: none of the 3 contrast findings look like the detector penalizing appropriate utilitarian styling (dense tables, flat color, etc.) — WCAG contrast is a computed pass/fail against a fixed threshold, not a stylistic preference, so it stands as real, actionable evidence regardless of this being an internal ops tool.

## Overall Impression

The bones are genuinely good: a coherent token system, real domain vocabulary, and — rare for an internal tool — an actual onboarding affordance on the landing page. What's missing is stakes-differentiation: the app treats "save a note" and "approve a payment that gets POSTed to a backend" as visually and interactionally identical actions, and treats a first-time reviewer the same as an expert one everywhere except the one page that happens to explain itself. The biggest opportunity is closing the gap between the journey page's teaching instinct and the rest of the app's silence.

## What's Working

- **The journey-page info dialog** is a well-executed, genuinely product-specific help affordance that correctly teaches pipeline stage vs. outcome vs. delivery as three distinct concepts a reviewer would otherwise have to infer by trial and error.
- **The light/dark token system** is disciplined and semantically named (`--good`/`--warn`/`--bad` with matching background tones), consistently reused across `review.css` and the stub backend's `backend.css`, with real dark-mode re-derivation rather than a blanket filter — the contrast bug above is a real gap in that system, not evidence it doesn't exist.
- **Domain modeling in the edit form** (deduction types, invoice-vs-payment amount split, per-line and per-header deductions, edit history with old/new values) reflects real AR reconciliation mechanics rather than a flattened generic form.

## Priority Issues

**[P0] No confirmation or visual distinction before high-stakes actions**
- Why it matters: `Save & Approve` sends canonical data to a real backend (`POST /remittances`), and `Reprocess` appears to discard a reviewer's in-progress edits and re-run extraction — both are one click away with the exact same button styling as a routine `Save`, on a tool that is actively demoed to a client for its trustworthiness.
- Fix: give `Save & Approve` a distinct color/weight (e.g. the `--accent` teal already reserved for positive/confirming actions elsewhere) and add a lightweight confirm step specifically for `Reprocess` since it looks destructive to unsaved state.
- Suggested command: `/impeccable harden`

**[P1] Dark-mode contrast failure on every primary button (confirmed by detector)**
- Why it matters: white text on `--brand`/`--brand-strong` fills falls to 2.9:1 in dark mode (need 4.5:1) because those tokens double as both "light text-on-dark-bg color" and "solid fill behind white text" — two incompatible jobs. Affects Log in, Save, Save & Approve, and the info dialog's "Got it" button for every dark-mode viewer.
- Fix: split the token — keep `--brand`/`--brand-strong` for text-on-background use (links, active nav), add a separate `--brand-fill`/`--brand-fill-hover` pair tuned dark enough in dark mode to pass 4.5:1 with white text, and point `button[type="submit"]`/`.btn-primary` at the new pair.
- Suggested command: `/impeccable harden`

**[P1] Validation flags aren't linked to the field that caused them**
- Why it matters: flags render as free-floating badges disconnected from the actual header/line-item input, forcing the reviewer to manually cross-reference the Original pane against every Canonical field — exactly the recall-not-recognition burden a tool for repetitive, money-adjacent verification should eliminate.
- Fix: tag the specific input (a colored left-border or inline badge next to its label) when a flag references a known field path.
- Suggested command: `/impeccable clarify`

**[P2] Zero contextual help outside the journey page**
- Why it matters: `detail.html` — the screen where a reviewer must pick a deduction `type` from `tds`/`credit_note`/`advance_adjustment`/`discount`/`rounding`/`other` — offers no definition of any of these, a real barrier for a new reviewer or a demo viewer who clicks in, despite the app already having the `data-open-dialog` mechanism built and proven on the journey page.
- Fix: reuse the existing dialog mechanism to add a small glossary/info dialog on the edit form (deduction types, confidence threshold).
- Suggested command: `/impeccable onboard`

**[P3] Deduction row inputs and remove buttons have no accessible name**
- Why it matters: the deduction-row macro relies solely on `placeholder="amount"`/`placeholder="reason"` (no `<label>`/`aria-label`), and the `×` remove button has no accessible name — a screen-reader user gets an anonymous "textbox," "textbox," "button."
- Fix: add `aria-label` to the select, both inputs, and the remove button in the macro.
- Suggested command: `/impeccable harden`

## Persona Red Flags

**Sam (accessibility-dependent)**: on the edit form, every deduction row's amount/reason inputs rely solely on placeholder text with no label, and the "×" remove button has zero accessible name — a screen-reader user navigating deductions gets anonymous "textbox," "textbox," "button" with no way to tell what they're editing or removing.

**Alex (power user / daily reviewer)**: the queue offers no bulk actions and no "approve → next item" flow — each of potentially dozens of daily reviews is Open → verify → Save & Approve → back to the queue → find the next row, a full round trip per item with no shortcut for the common case of "this looks right, approve it."

**Jordan (first-time / non-technical demo viewer)**: clicking into the read-only JSON view or the collapsed "Raw extraction" panel dumps unformatted JSON/OCR text with no framing for a non-technical audience, and the errors page shows a raw error string verbatim in a `<pre>` block — easy to read as "this tool is broken" rather than an expected, recoverable state, in front of exactly the audience this demo is built for.

## Minor Observations

- The login page's error message has no `role="alert"`, so a screen reader won't proactively announce a failed login.
- `approved.html`'s amount column concatenates currency and amount as plain text with no thousands separators — large amounts will be hard to scan at speed in a money-verification table.
- The journey page's auto-approved/awaiting-review counts are computed by a Jinja loop in the template rather than passed from the view layer — not a UX issue, but a sign the template is doing view-layer work.
- `#lineitems` has no scroll container or count guard; a remittance with dozens of invoices produces dozens of stacked, uncollapsed fieldsets.

## Questions to Consider

- If `Reprocess` discards a reviewer's unsaved edits, has anyone watched a reviewer actually lose work to it — or is this untested because reviewers instinctively avoid the unlabeled-risk button?
- The journey page teaches confidence scores and auto-approve thresholds in detail, but the edit screen — where a reviewer decides whether to override the model — never surfaces the threshold itself. If the number matters enough to explain on the landing page, why isn't it visible at the moment of the decision it drove?
