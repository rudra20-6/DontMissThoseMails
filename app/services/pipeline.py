"""Email -> (store) -> AI analysis -> items -> WhatsApp notification.

Every new email is first stored as `pending`, then analysed. If Gemini quota is exhausted the email just
stays pending and is retried on the next tick, so nothing is lost. After LLM_RETRY_MAX_MINUTES it is
processed with keyword rules instead, so an important mail is never delayed for long.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.gemini import LLMUnavailable
from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail, get_mail_source
from app.models import Email, Item
from app.services import labels, messages, notifier
from app.services.decisions import Triage, analyze_email
from app.services.extraction import Extraction
from app.textutil import truncate
from app.timeutil import local, utcnow

log = logging.getLogger(__name__)
MAX_PER_TICK = 20
CATCHUP_AFTER_HOURS = 24  # mails originally sent longer ago than this are summarised, not pushed one by one


def poll_mail() -> int:
    """Fetch new mail into the queue, then work through the queue. Returns emails processed."""
    try:
        for raw in get_mail_source().fetch_new():
            ingest(raw)
    except MailError as exc:
        log.warning("Mail fetch failed: %s", exc)
    return process_pending()


def ingest(raw: RawEmail) -> bool:
    sent_at = raw.original_date or raw.received_at
    with session_scope() as session:
        if session.scalar(select(Email.id).where(Email.message_id == raw.message_id)):
            return False
        # The same mail can arrive twice with different Message-IDs (forwarded singly AND in a bulk forward).
        twin = session.scalar(select(Email.id).where(
            Email.subject == raw.subject[:1000], Email.sender == raw.sender[:500],
            Email.received_at >= sent_at - timedelta(minutes=3), Email.received_at <= sent_at + timedelta(minutes=3),
        ))
        if twin:
            return False
        session.add(Email(
            message_id=raw.message_id, sender=raw.sender[:500], subject=raw.subject[:1000],
            received_at=sent_at, web_link=raw.web_link, body=truncate(raw.body, 20000),
            links=raw.links[:20], action="pending",
        ))
        return True


def process_pending(limit: int = MAX_PER_TICK) -> int:
    with session_scope() as session:
        ids = session.scalars(
            select(Email.id).where(Email.action == "pending").order_by(Email.received_at).limit(limit)
        ).all()
    done = 0
    for email_id in ids:
        try:
            process_one(email_id)
            done += 1
        except LLMUnavailable as exc:
            log.warning("AI quota exhausted, %d email(s) stay queued: %s", len(ids) - done, exc)
            break
        except Exception:  # noqa: BLE001 - one bad email must not block the queue
            log.exception("Failed processing email %s", email_id)
            with session_scope() as session:
                email = session.get(Email, email_id)
                email.attempts += 1
                if email.attempts >= 3:
                    email.action = "error"
    return done


def process_one(email_id: int) -> None:
    s = get_settings()
    with session_scope() as session:
        email = session.get(Email, email_id)
        if email is None or email.action != "pending":
            return
        waited = utcnow() - (email.created_at or utcnow())
        allow_heuristic = waited >= timedelta(minutes=s.llm_retry_max_minutes)
        triage, ext = analyze_email(email.sender, email.subject, email.body, local(email.received_at).isoformat(),
                                    email.links or [], allow_heuristic=allow_heuristic)
        email.category = triage.category
        email.importance = triage.importance
        email.decision = triage.to_dict()
        action = decide_action(triage)
        email.action = action
        email.processed_at = utcnow()
        log.info("Mail %r -> %s (%s, importance %.0f via %s)", email.subject, action, triage.category,
                 triage.importance, triage.source)
        body, links = email.body, email.links or []
        email.body = ""  # don't keep mail bodies around once analysed
        if action == "dropped" or ext is None:
            email.action = "dropped"
            return
        raw = RawEmail(email.message_id, email.sender, email.subject, email.received_at, body, email.web_link, links)
        items = build_items(session, email, triage, ext, raw)
        old = utcnow() - email.received_at > timedelta(hours=CATCHUP_AFTER_HOURS)
        for item in items:
            item.in_digest = action == "digest"
            if old:
                # Old mail (e.g. a one-time import of last week's inbox): no individual message, it goes
                # into one catch-up summary instead. Old low-value announcements are simply archived.
                # (an "announcement" from an old deadline/event mail means the date already passed: skip it)
                item.catchup = item.kind != "announcement" or (
                    item.importance >= 3 and triage.has_deadline < 0.5 and triage.is_event < 0.5)
                item.in_digest = False
                item.digested = True
        session.flush()
        if action == "notify" and not old:
            for item in items:
                send_new_item(session, item, _short_sender(email.sender))


def decide_action(t: Triage) -> str:
    """Thresholds on the AI's signals -> dropped | digest | notify."""
    s = get_settings()
    actionable = t.has_deadline >= 0.5 or (t.is_event >= 0.5 and t.importance >= 1.0)
    if t.is_noise >= 0.7 and t.importance < 2.5:
        return "dropped"
    if t.importance < s.importance_drop_below and not actionable:
        return "dropped"
    if t.importance >= s.importance_immediate_min or actionable:
        return "notify"
    return "digest"


def build_items(session: Session, email: Email, t: Triage, ext: Extraction, raw: RawEmail) -> list[Item]:
    now = utcnow()
    link = ext.primary_link or raw.web_link
    base = dict(email_id=email.id, category=t.category, importance=t.importance, summary=ext.summary, link=link)
    items: list[Item] = []

    ev = ext.event
    if t.is_event >= 0.5 and ev and ((ev.start and ev.start > now) or (ev.registration_deadline and ev.registration_deadline > now)):
        items.append(Item(
            kind="event", title=ev.name or ext.title, event_start=ev.start, event_end=ev.end, venue=ev.venue,
            reg_deadline=ev.registration_deadline, reg_link=ev.registration_link, status="asked", **base,
        ))

    if t.has_deadline >= 0.5:
        for d in ext.deadlines:
            if not d.due or d.due <= now:
                continue
            # an event's registration deadline is tracked on the event itself
            if items and items[0].kind == "event" and items[0].reg_deadline and abs((d.due - items[0].reg_deadline).total_seconds()) < 3600:
                continue
            title = ext.title if len(ext.deadlines) == 1 else f"{ext.title}: {d.what}"[:200]
            items.append(Item(kind="deadline", title=title, due_at=d.due, status="pending", **base))

    if not items:
        items.append(Item(kind="announcement", title=ext.title, status="info", **base))
    for item in items:
        session.add(item)
        labels.assign(session, item)
    return items


def send_new_item(session: Session, item: Item, sender: str = "") -> None:
    text = messages.item_card(item, sender)
    urgent = item.kind == "deadline" and item.due_at and (item.due_at - utcnow()).total_seconds() < 6 * 3600
    if item.kind == "event":
        text += "\n\n🙋 *Are you interested in this?*"
        notifier.notify(session, text, messages.event_interest_buttons(item), item_id=item.id)
    elif item.kind == "deadline":
        notifier.notify(session, text, messages.deadline_buttons(item), urgent=bool(urgent), item_id=item.id)
    else:
        notifier.notify(session, text, item_id=item.id)


def _short_sender(sender: str) -> str:
    name = sender.split("<")[0].strip().strip('"')
    return name or sender
