# brag plan: AR Automation Agent

**What it is:** an agent that reads customer payment advices from an accounts-receivable inbox and turns them into checked, structured payments.
**For:** AR / finance teams at Indian B2B companies drowning in forwarded remittance emails and multi-page payment advice PDFs.
**What sets it apart:** it splits forwarded threads and reads only what's new, reads 20-row advices row by row in code, and checks every read against the document's own total and amount in words before anything reaches the books.
**Most impressive claim:** a payment advice says its total in words ("Sixty Eight Lakh…") and the agent checks the extraction against it.
**Visual hook:** a single forwarded email that unfolds into a stack of separate payments.
**Real UI shown:** the AR Review app's own stylesheet (review.css): topbar, badges, ledger strip, "Use …" button, flags.
**Tone:** default, clean and confident, warm cream + burnt-orange brand (the app's palette).
**Share caption:** see share-copy.txt.

Data on screen is the invented test fixture (Continental Bus Body Builders paying Acme Metals, CBB… invoice numbers). No client names, emails or real figures.

## Identity
- Colors from review.css: bg #faf8f4, surface #fffcf8, text #211d17, brand #7f3f00 / fill #994b00, brand-soft #fbe9d9, good #1f6f45 / #e6f2ea, warn #7a5e00 / #faf1d6.
- Type: Noto Sans (what the app renders with on Linux), 800 weight for headlines.

## Music
120 BPM (beat = 0.5s, bar = 2s), A minor, warm plucked synth over soft kick/hat, bass and pad. Every cut lands on a bar or beat. SFX in key: soft ticks for rows, a chime (A major-ish triad from the key) on "totals match", a click on the button press, filtered-noise swells into scene changes.

## Storyboard (20s)
| # | Time | Beat | Scene | On-screen text |
|---|---|---|---|---|
| 1 | 0–3s | hook | A forwarded email "FW: FW: RE: Payment Remittance details" lands; it unfolds downward into 6 quoted messages, each with a ₹ amount. | "One forwarded email. Six payments buried inside." |
| 2 | 3–6s | reveal | Title card | "AR Automation Agent" / "Reads every remittance in your AR inbox." |
| 3 | 6–10s | highlight 1 | The thread's messages as app rows with badges: read / seen before / internal forward. Only the new one glows. | "Splits the thread. Reads only what's new." |
| 4 | 10–14s | highlight 2 | A 20-row advice table fills row by row; totals bar compares document vs extracted and ticks green: "Totals match the document". | "Every row copied. Checked against the document's own total." |
| 5 | 14–17s | highlight 3 | Review screen: an adjustment line "2510035016DISCO — Which invoice does it reduce?" A cursor clicks "Use CBB25100035016"; strip turns to "Reduces CBB25100035016". | "Anything doubtful waits for a person." |
| 6 | 17–20s | punchline | Approved payments slide into "Delivered" with green badges; logo lockup. | "Clean payments go straight to your books." / "AR Automation Agent · MLDeep Systems" |
