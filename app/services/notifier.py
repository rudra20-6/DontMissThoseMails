"""Delivers WhatsApp messages while respecting quiet hours, pause mode and the 24h service window.

WhatsApp only allows free-form messages within 24h of the user's last message. Outside it we queue the
message in the Outbox and send a single approved *template* to nudge the user; the moment the user
replies (anything, or taps the template), the webhook flushes the queue.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import diag, kv
from app.clients.whatsapp import REENGAGEMENT_ERROR, WhatsAppClient, WhatsAppError
from app.config import get_settings
from app.models import Item, Outbox, SentMessage
from app.timeutil import local, utcnow

log = logging.getLogger(__name__)

NUDGE_KEY = "last_template_nudge"
PAUSE_KEY = "paused"
NUDGE_EVERY = timedelta(hours=12)
SENT_MESSAGE_TTL = timedelta(days=30)

Buttons = list[tuple[str, str]] | None


def is_quiet_now() -> bool:
    s = get_settings()
    hour = local(utcnow()).hour
    start, end = s.quiet_hours_start, s.quiet_hours_end
    if start == end:
        return False
    return start <= hour or hour < end if start > end else start <= hour < end


def is_paused(session: Session) -> bool:
    return bool(kv.get(session, PAUSE_KEY, False))


def _deliver(payload: dict) -> list[str]:
    """Sends one payload, returns the WhatsApp message ids."""
    wa = WhatsAppClient()
    if payload.get("buttons"):
        resp = wa.send_buttons(payload["text"], [tuple(b) for b in payload["buttons"]])
    else:
        resp = wa.send_text(payload["text"])
    return [m["id"] for m in (resp or {}).get("messages", []) if m.get("id")]


def _remember(session: Session, ids: list[str] | None, item_id: int | None) -> None:
    if not item_id or not ids:
        return
    for wamid in ids:
        if session.get(SentMessage, wamid) is None:
            session.add(SentMessage(wamid=wamid, item_id=item_id))
    session.execute(delete(SentMessage).where(SentMessage.created_at < utcnow() - SENT_MESSAGE_TTL))


def item_for_message(session: Session, wamid: str | None) -> Item | None:
    """The item a message we sent was about (for swipe-replies)."""
    row = session.get(SentMessage, wamid) if wamid else None
    return session.get(Item, row.item_id) if row else None


def last_item(session: Session, statuses: tuple[str, ...], within: timedelta = timedelta(hours=6)) -> Item | None:
    """The item we most recently messaged about, if it was recent and is still in one of `statuses`."""
    rows = session.scalars(
        select(SentMessage).where(SentMessage.created_at >= utcnow() - within)
        .order_by(SentMessage.created_at.desc()).limit(10)
    ).all()
    for row in rows:
        item = session.get(Item, row.item_id)
        if item and item.status in statuses:
            return item
    return None


def _queue(session: Session, payload: dict) -> None:
    session.add(Outbox(payload=payload))


def _nudge(session: Session) -> None:
    last = kv.get(session, NUDGE_KEY)
    if last and utcnow() - _parse(last) < NUDGE_EVERY:
        return
    pending = session.query(Outbox).filter(Outbox.sent_at.is_(None)).count()
    try:
        WhatsAppClient().send_template(param=str(max(1, pending)))
        kv.put(session, NUDGE_KEY, utcnow().isoformat())
        log.info("Sent template nudge (%s queued)", pending)
    except WhatsAppError as exc:
        log.error("Template nudge failed: %s", exc)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def notify(session: Session, text: str, buttons: Buttons = None, urgent: bool = False, *,
           item_id: int | None = None, scheduled: bool = False, expires_at: datetime | None = None) -> bool:
    """Proactive message. Returns True if delivered now, False if queued.

    scheduled: a reminder at a time the user chose themselves, so it ignores quiet hours (not pause).
    expires_at: if it has to be queued, drop it instead of delivering it after this time.
    """
    payload = {"text": text, "buttons": buttons or [], "item": item_id,
               "expires": expires_at.isoformat() if expires_at else None}
    if not urgent and (is_paused(session) or (is_quiet_now() and not scheduled)):
        _queue(session, payload)
        return False
    return _send_or_queue(session, payload)


def reply(session: Session, text: str, buttons: Buttons = None, item_id: int | None = None) -> None:
    """Answer to a user message: the 24h window is open, so send immediately."""
    _send_or_queue(session, {"text": text, "buttons": buttons or [], "item": item_id})


def _send_or_queue(session: Session, payload: dict) -> bool:
    try:
        _remember(session, _deliver(payload), payload.get("item"))
        diag.record("last_send_ok", preview=payload["text"][:80])
        return True
    except WhatsAppError as exc:
        diag.record("last_send_error", error=str(exc)[:500], code=exc.code)
        _queue(session, payload)
        if exc.code == REENGAGEMENT_ERROR:
            _nudge(session)
        else:
            log.error("WhatsApp send failed, queued: %s", exc)
        return False


def flush_outbox(session: Session, force: bool = False) -> int:
    """Send queued messages. `force` ignores quiet hours (used when the user just messaged us)."""
    if is_paused(session) or (not force and is_quiet_now()):
        return 0
    sent = 0
    rows = session.scalars(select(Outbox).where(Outbox.sent_at.is_(None)).order_by(Outbox.id)).all()
    for row in rows:
        if row.attempts >= 5:
            row.sent_at = utcnow()  # give up on poison messages
            continue
        expires = (row.payload or {}).get("expires")
        if expires and _parse(expires) < utcnow():
            row.sent_at = utcnow()  # e.g. an hourly routine ping that's out of date by now
            continue
        try:
            _remember(session, _deliver(row.payload), row.payload.get("item"))
            row.sent_at = utcnow()
            sent += 1
        except WhatsAppError as exc:
            row.attempts += 1
            if exc.code == REENGAGEMENT_ERROR:
                _nudge(session)
                break
            log.error("Outbox delivery failed: %s", exc)
    return sent
