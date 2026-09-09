from __future__ import annotations

import logging
from functools import lru_cache

from ar_pipeline.db.base import get_session
from ar_pipeline.ingest.auth import GraphNotConfigured
from ar_pipeline.ingest.client import HttpGraphClient
from ar_pipeline.ingest.poller import PollStats, poll_once
from ar_pipeline.storage import get_blob_store

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _graph_client() -> HttpGraphClient:
    """One Graph client (MSAL token cache + httpx pool) shared across polls.

    If ``from_settings`` raises it propagates without being cached, so the next
    tick retries cleanly.
    """
    return HttpGraphClient.from_settings()


def run_poll() -> PollStats | None:
    try:
        graph = _graph_client()
    except GraphNotConfigured:
        log.info("poll_inbox: Graph not configured — skipping")
        return None
    blob_store = get_blob_store()
    with get_session() as session:
        stats = poll_once(graph, blob_store, session)
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
