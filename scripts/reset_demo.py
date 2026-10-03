"""Wipe demo data so a run-through starts from an empty system.

    uv run python scripts/reset_demo.py          # show what would be deleted
    uv run python scripts/reset_demo.py --yes    # back up, then delete

Deletes every email, attachment, extraction, edit, delivery, invoice and
ledger payment, the stored attachment files, and everything the stub backend
has received. Keeps the mailbox position (``poll_state``) on purpose: wiping
it would make the next poll re-read the whole inbox, pulling every old test
email back in. Keeps the Gmail sign-in and vendor table too.

A data-only SQL backup and a copy of the stub backend's store are written to
``data/backups/<timestamp>/`` first. To undo, restore that dump with psql.

The stub backend holds received payments in memory and writes them back to
its store file on the next delivery, so it must be stopped while this runs
(the script refuses otherwise) and started again afterwards.
"""

from __future__ import annotations

import pathlib
import shutil
import socket
import subprocess
import sys
from datetime import datetime

import pgserver
from dev_db import ROOT, ensure_server
from sqlalchemy import text

from ar_pipeline.db.base import get_engine

# Children before parents (foreign keys).
TABLES = (
    "invoice_payment",
    "invoice",
    "delivery",
    "extraction_edit",
    "extraction",
    "raw_extraction",
    "extraction_source",
    "attachment",
    "email",
)
BLOB_DIR = ROOT / "data" / "blob"
STUB_STORE = ROOT / "data" / "stub_backend_store.json"
BACKUP_ROOT = ROOT / "data" / "backups"
STUB_PORT = 9000


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _counts() -> dict[str, int]:
    with get_engine().connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in TABLES}


def main(argv: list[str]) -> int:
    server = ensure_server()
    counts = _counts()
    blobs = sum(1 for p in BLOB_DIR.rglob("*") if p.is_file()) if BLOB_DIR.exists() else 0
    print("Would delete:")
    for t, n in counts.items():
        print(f"  {t:18} {n} row(s)")
    print(f"  attachment files   {blobs}")
    print(f"  stub backend store {'present' if STUB_STORE.exists() else 'absent'}")
    print("Keeps: poll_state (mailbox position), vendor, Gmail sign-in.")

    if "--yes" not in argv:
        print("\nDry run. Re-run with --yes to back up and delete.")
        return 0
    if _port_open(STUB_PORT):
        print(
            f"\nThe stub backend is running on port {STUB_PORT}. Stop it first (Ctrl+C in its "
            "terminal) — it would write the old payments back — then re-run, then start it again.",
            file=sys.stderr,
        )
        return 2

    backup = BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True)
    pg_dump = pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_dump"
    tables = [arg for t in TABLES for arg in ("-t", t)]
    subprocess.run(
        [
            str(pg_dump),
            "--data-only",
            *tables,
            "-f",
            str(backup / "data.sql"),
            server.get_uri(database="ar_pipeline"),
        ],
        check=True,
    )
    if STUB_STORE.exists():
        shutil.copy2(STUB_STORE, backup / "stub_backend_store.json")
    print(f"\nBackup written to {backup.relative_to(ROOT)}/")

    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)}"))
    if BLOB_DIR.exists():
        shutil.rmtree(BLOB_DIR)
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    STUB_STORE.unlink(missing_ok=True)

    after = _counts()
    left = {t: n for t, n in after.items() if n}
    if left:
        print(f"Some rows remain: {left}", file=sys.stderr)
        return 1
    print("Demo data cleared. Start the stub backend again, then reload the review app.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
