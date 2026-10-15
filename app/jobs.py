"""Background jobs. Correctness never depends on these: state is derived from timestamps on every
request. Jobs only (a) persist lazily-detected changes, (b) deliver email, (c) housekeeping."""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db as dbm
from app import emailer
from app.config import get_config

log = logging.getLogger("jobs")


def tick():
    """Every minute: run the lifecycle sweep (no-shows, auto-close, overdue, waitlist) and send email."""
    try:
        from app.services import lifecycle
        with dbm.session_scope() as s:
            lifecycle.sweep(s)
        with dbm.session_scope() as s:
            emailer.flush_outbox(s)
    except Exception:  # noqa: BLE001
        log.exception("tick failed")


def nightly():
    try:
        from app import security
        from app.services import backup
        with dbm.session_scope() as s:
            security.rate_gc(s)
        backup.run_backup()
    except Exception:  # noqa: BLE001
        log.exception("nightly job failed")


def start() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=get_config().timezone)
    sched.add_job(tick, "interval", seconds=60, id="tick", max_instances=1, coalesce=True)
    sched.add_job(nightly, CronTrigger(hour=3, minute=15), id="nightly", max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started")
    return sched
