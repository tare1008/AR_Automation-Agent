import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ar_pipeline.config import get_settings

log = logging.getLogger(__name__)


def poll_inbox() -> None:
    from ar_pipeline.ingest.service import run_poll

    run_poll()
    return None


def advance_pipeline() -> None:
    from ar_pipeline.db.base import get_session
    from ar_pipeline.extract.vision import get_vision_extractor
    from ar_pipeline.normalize.llm_client import get_llm_client
    from ar_pipeline.pipeline.advance import advance_once
    from ar_pipeline.storage import get_blob_store

    # advance_once commits per email; this wrapper's final commit is a no-op.
    with get_session() as session:
        stats = advance_once(session, get_blob_store(), get_vision_extractor(), get_llm_client())
    log.info(
        "advance_pipeline: %d classified, %d extracted, %d normalized, %d errored",
        stats.classified,
        stats.extracted,
        stats.normalized,
        stats.errored,
    )
    return None


def run_deliveries() -> None:
    log.info("run_deliveries: no-op")
    return None


def build_scheduler() -> BackgroundScheduler:
    s = get_settings()
    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    scheduler.add_job(poll_inbox, "interval", seconds=s.poll_interval_seconds, id="poll_inbox")
    scheduler.add_job(
        advance_pipeline, "interval", seconds=s.advance_interval_seconds, id="advance_pipeline"
    )
    scheduler.add_job(
        run_deliveries, "interval", seconds=s.deliver_interval_seconds, id="run_deliveries"
    )
    return scheduler
