import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ar_pipeline.config import get_settings

log = logging.getLogger(__name__)


def poll_inbox() -> None:
    log.info("poll_inbox: no-op")
    return None


def advance_pipeline() -> None:
    log.info("advance_pipeline: no-op")
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
