"""``python -m ar_pipeline`` — operator/demo commands.

Subcommands:

* ``ingest-eml FILE [FILE ...]`` — import settlement emails from ``.eml`` files
  into the database as ``status="new"`` (the offline equivalent of a mailbox
  poll). Does not touch the Graph delta cursor.
* ``tick [--repeat N]`` — run the pipeline-advance and delivery jobs once
  (or N times), synchronously — step a demo instead of waiting on the
  in-process scheduler.
* ``status`` — print a summary of emails, extractions and deliveries by state.

All commands use the configured ``DATABASE_URL`` / ``BLOB_DIR``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path


def _cmd_ingest_eml(paths: Sequence[str]) -> int:
    from ar_pipeline.db.base import get_session
    from ar_pipeline.ingest.eml import ingest_eml_file
    from ar_pipeline.storage import get_blob_store

    blob_store = get_blob_store()
    added = skipped = errors = 0
    with get_session() as session:
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                errors += 1
                print(f"  ERROR  {raw}: not a file", file=sys.stderr)
                continue
            try:
                # per-file savepoint so a mid-file failure (bad flush, blob
                # write) rolls back only that file and the batch continues —
                # same isolation the Graph poller uses per message.
                with session.begin_nested():
                    row = ingest_eml_file(session, blob_store, path)
                session.commit()
            except Exception as exc:  # noqa: BLE001 — one bad file shouldn't abort the rest
                errors += 1
                print(f"  ERROR  {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if row is None:
                skipped += 1
                print(f"  skip   {path.name} (already ingested)")
            else:
                added += 1
                print(f"  new    {row.id}  {row.subject[:70]}")
    print(f"\n{added} ingested, {skipped} already present, {errors} errored")
    return 1 if errors else 0


def _cmd_tick(repeat: int) -> int:
    from ar_pipeline.config import get_settings
    from ar_pipeline.db.base import get_session
    from ar_pipeline.deliver import backend_client, deliverer
    from ar_pipeline.extract.vision import get_vision_extractor
    from ar_pipeline.normalize.llm_client import get_llm_client
    from ar_pipeline.pipeline.advance import advance_once
    from ar_pipeline.storage import get_blob_store

    for i in range(1, repeat + 1):
        with get_session() as session:
            adv = advance_once(session, get_blob_store(), get_vision_extractor(), get_llm_client())
        line = (
            f"tick {i}/{repeat}  advance: "
            f"{adv.classified} classified, {adv.extracted} extracted, "
            f"{adv.normalized} normalized, {adv.errored} errored"
        )
        if get_settings().backend_url:
            with get_session() as session:
                dlv = deliverer.run_deliveries(session, backend_client.get_backend_client())
            line += (
                f"  |  deliver: {dlv.delivered} delivered, "
                f"{dlv.failed} failed, {dlv.retrying} retrying"
            )
        else:
            line += "  |  deliver: skipped (BACKEND_URL not set)"
        print(line)
    return 0


def _cmd_graph_login() -> int:
    from ar_pipeline.config import get_settings
    from ar_pipeline.ingest.auth import DelegatedGraphAuth, GraphNotConfigured

    if get_settings().graph_auth_mode != "delegated":
        print("GRAPH_AUTH_MODE is not 'delegated' — set it in .env first.", file=sys.stderr)
        return 2
    try:
        auth = DelegatedGraphAuth.from_settings()
    except GraphNotConfigured:
        print("GRAPH_CLIENT_ID is not set — see the Azure app registration steps.", file=sys.stderr)
        return 2
    account = auth.login_device_code()
    print(f"\nSigned in as {account}. Token cache saved — poll_inbox can run unattended now.")
    return 0


def _cmd_gmail_login() -> int:
    from ar_pipeline.ingest.gmail_auth import GmailAuth, GmailNotConfigured

    try:
        auth = GmailAuth.from_settings()
    except GmailNotConfigured:
        print(
            "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not set — see the Google Cloud"
            " OAuth setup steps.",
            file=sys.stderr,
        )
        return 2
    email = auth.login_interactive()
    print(f"\nSigned in as {email}. Token cache saved — poll_inbox can run unattended now.")
    return 0


def _cmd_status() -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import InstrumentedAttribute, Session

    from ar_pipeline.db.base import get_session
    from ar_pipeline.db.models import Delivery, Email, Extraction

    def _counts(session: Session, col: InstrumentedAttribute[str]) -> dict[str, int]:
        return {
            str(status): int(n)
            for status, n in session.execute(select(col, func.count()).group_by(col))
        }

    with get_session() as session:
        emails = _counts(session, Email.status)
        extractions = _counts(session, Extraction.status)
        deliveries = _counts(session, Delivery.status)

    def _block(title: str, counts: dict[str, int]) -> None:
        print(title)
        if not counts:
            print("  (none)")
        for k in sorted(counts):
            print(f"  {k:<16} {counts[k]}")
        print()

    _block("emails", emails)
    _block("extractions", extractions)
    _block("deliveries", deliveries)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ar-pipeline", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest-eml", help="import settlement emails from .eml files")
    p_ingest.add_argument("paths", nargs="+", metavar="FILE", help=".eml file(s)")

    p_tick = sub.add_parser("tick", help="run the advance + delivery jobs once")
    p_tick.add_argument(
        "--repeat", type=int, default=1, metavar="N", help="run N cycles (default 1)"
    )

    sub.add_parser("status", help="print emails/extractions/deliveries by state")
    sub.add_parser(
        "graph-login",
        help="one-time device-code sign-in for GRAPH_AUTH_MODE=delegated (personal accounts)",
    )
    sub.add_parser(
        "gmail-login",
        help="one-time OAuth sign-in for MAILBOX_PROVIDER=gmail",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)

    if args.command == "ingest-eml":
        return _cmd_ingest_eml(args.paths)
    if args.command == "tick":
        if args.repeat < 1:
            print("--repeat must be >= 1", file=sys.stderr)
            return 2
        return _cmd_tick(args.repeat)
    if args.command == "status":
        return _cmd_status()
    if args.command == "graph-login":
        return _cmd_graph_login()
    if args.command == "gmail-login":
        return _cmd_gmail_login()
    return 2  # unreachable: subparser is required


if __name__ == "__main__":
    raise SystemExit(main())
