"""Synthetic, redacted thread fixtures in the shapes seen in real AR mail."""

from __future__ import annotations


def remittance_html(n: int) -> str:
    amount = f"{(n + 1) * 1000:,}.00"
    return (
        "<p>Dear Dharmendra Ji,</p>"
        f"<p>We have remitted the payment of Rs.{amount} vide UTR no.PUNBR52026{n:011d} "
        f"on {10 + n:02d}.01.2026. Please find the below invoice details for your reference.</p>"
        "<table><tr><th>Invoice Number</th><th>Original</th><th>Balance Due</th></tr>"
        f"<tr><td>JHMUR25100{n:05d}</td><td>{amount}</td><td>{amount}</td></tr></table>"
        "<p>Thanks &amp; Regards<br>S Tejeswara Rao<br>Alufluoride Limited</p>"
    )


def outlook_header_html(sender: str, sent: str) -> str:
    return (
        '<div style="border:none;border-top:solid #E1E1E1 1.0pt">'
        f"<p><b>From:</b> {sender}<br><b>Sent:</b> {sent}<br>"
        "<b>To:</b> Dharmendra Kumar<br><b>Subject:</b> Re: Payment Remittance details</p></div>"
    )


def chain_html(
    payments: list[int], *, top_note: str = "Dear All, Please applied below payment."
) -> str:
    """Newest payment first, like a real reply chain, under an internal forward note."""
    parts = [f"<p>{top_note}</p><p>Regards,<br>Dharmendra</p>"]
    for i, n in enumerate(payments):
        sender = "Tejeswara Rao S" if i % 2 else "Tejeswara Rao S &lt;STR@alufluoride.com&gt;"
        parts.append(outlook_header_html(sender, f"{10 + n:02d} January 2026 03:24 PM"))
        parts.append(remittance_html(n))
    return "<html><body>" + "".join(parts) + "</body></html>"


def chain_text(payments: list[int]) -> str:
    out = ["Dear All,", "Please applied below payment.", "", "Regards,", "Dharmendra", ""]
    for n in payments:
        amount = f"{(n + 1) * 1000:,}.00"
        out += [
            "From: Tejeswara Rao S <STR@alufluoride.com>",
            f"Sent: {10 + n:02d} January 2026 03:24 PM",
            "To: Dharmendra Kumar",
            "Subject: Re: Payment Remittance details",
            "",
            "Dear Dharmendra Ji,",
            f"We have remitted the payment of Rs.{amount} vide UTR no.PUNBR52026{n:011d}.",
            f"Invoice JHMUR25100{n:05d}  {amount}",
            "Thanks & Regards",
            "Alufluoride Limited",
            "",
        ]
    return "\n".join(out)


GMAIL_REPLY_TEXT = (
    "Thanks, received.\n\n"
    "On Wed, Feb 18, 2026 at 5:07 PM Tejeswara Rao <str@alufluoride.com> wrote:\n"
    "> We have remitted Rs.1,000.00 vide UTR no.PUNBR52026000000000001.\n"
)

FORWARD_BANNER_TEXT = (
    "FYI\n\n"
    "-------- Forwarded Message --------\n"
    "Subject:\tFW: Payment Remittance details\n"
    "Date:\tWed, 18 Feb 2026 12:08:59 +0000\n"
    "From:\tDharmendra Kumar <dharmendra.p.kumar-c@adityabirla.com>\n"
    "To:\tShwetha M <shwetha.m@adityabirla.com>\n"
    "\n"
    "Dear All,\nPlease applied below payment.\n"
)
