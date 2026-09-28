"""Background jobs. Also exposed via /cron/tick so an external pinger can drive them."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app import kv
from app.config import get_settings
from app.db import session_scope
from app.services import notifier
from app.services.digest import maybe_send_daily_digest
from app.services.pipeline import poll_mail
from app.services.reminders import run_reminders
from app.timeutil import utcnow

log = logging.getLogger(__name__)
_lock = threading.Lock()
_scheduler: BackgroundScheduler | None = None
LAST_POLL_KEY = "last_mail_poll"


def tick(force_poll: bool = False) -> dict:
    """One full cycle: poll mail if due, run reminders, digest and outbox. Safe to call any time."""
    if not _lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        result: dict = {}
        s = get_settings()
        with session_scope() as session:
            last = kv.get(session, LAST_POLL_KEY)
        due = force_poll or not last or (utcnow() - _dt(last)).total_seconds() >= s.mail_poll_minutes * 60 - 5
        if due:
            with session_scope() as session:
                kv.put(session, LAST_POLL_KEY, utcnow().isoformat())
            result["mails"] = poll_mail()
        with session_scope() as session:
            result["reminders"] = run_reminders(session)
        with session_scope() as session:
            result["digest"] = maybe_send_daily_digest(session)
        with session_scope() as session:
            result["outbox"] = notifier.flush_outbox(session)
        return result
    except Exception:  # noqa: BLE001
        log.exception("tick failed")
        return {"error": True}
    finally:
        _lock.release()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def start() -> None:
    global _scheduler
    if _scheduler:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(tick, "interval", minutes=1, id="tick", max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("Scheduler started")


def stop() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
