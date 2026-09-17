from __future__ import annotations

import logging
from functools import lru_cache

from ar_pipeline.config import get_settings
from ar_pipeline.db.base import get_session
from ar_pipeline.ingest.auth import GraphLoginRequired, GraphNotConfigured
from ar_pipeline.ingest.client import GraphClient, HttpGraphClient
from ar_pipeline.ingest.gmail_auth import GmailLoginRequired, GmailNotConfigured
from ar_pipeline.ingest.gmail_client import GmailClient
from ar_pipeline.ingest.poller import PollStats, poll_once
from ar_pipeline.storage import get_blob_store

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _mailbox_client() -> GraphClient:
    """One mailbox client (token cache + httpx pool) shared across polls,
    for whichever provider ``MAILBOX_PROVIDER`` selects.

    If construction raises it propagates without being cached, so the next
    tick retries cleanly.
    """
    if get_settings().mailbox_provider == "gmail":
        return GmailClient.from_settings()
    return HttpGraphClient.from_settings()


def run_poll() -> PollStats | None:
    try:
        client = _mailbox_client()
    except (GraphNotConfigured, GmailNotConfigured):
        log.info("poll_inbox: %s not configured — skipping", get_settings().mailbox_provider)
        return None
    blob_store = get_blob_store()
    try:
        with get_session() as session:
            stats = poll_once(client, blob_store, session)
    except (GraphLoginRequired, GmailLoginRequired) as exc:
        log.warning("poll_inbox: %s — skipping", exc)
        return None
    log.info(
        "poll_inbox: %d new, %d attachments, %d dup, %d removed, %d failed%s",
        stats.new_emails,
        stats.attachments,
        stats.duplicates,
        stats.removed,
        stats.failed,
        " (resynced)" if stats.resynced else "",
    )
    return stats
