from __future__ import annotations

import logging

from ar_pipeline.db.base import get_session
from ar_pipeline.ingest.auth import GraphNotConfigured
from ar_pipeline.ingest.client import HttpGraphClient
from ar_pipeline.ingest.poller import PollStats, poll_once
from ar_pipeline.storage import get_blob_store

log = logging.getLogger(__name__)


def run_poll() -> PollStats | None:
    try:
        graph = HttpGraphClient.from_settings()
    except GraphNotConfigured:
        log.info("poll_inbox: Graph not configured — skipping")
        return None
    blob_store = get_blob_store()
    with get_session() as session:
        stats = poll_once(graph, blob_store, session)
    log.info(
        "poll_inbox: %d new, %d attachments, %d dup, %d removed%s",
        stats.new_emails,
        stats.attachments,
        stats.duplicates,
        stats.removed,
        " (resynced)" if stats.resynced else "",
    )
    return stats
