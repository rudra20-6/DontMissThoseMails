"""Email -> decision -> items -> WhatsApp notification."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail, get_mail_source
from app.models import Email, Item
from app.services import messages, notifier
from app.services.decisions import Triage, triage_email
from app.services.extraction import Extraction, extract_email
from app.timeutil import local, utcnow

log = logging.getLogger(__name__)


def poll_mail() -> int:
    """Fetch new mail and process it. Returns number of processed emails."""
    try:
        emails = get_mail_source().fetch_new()
    except MailError as exc:
        log.warning("Mail fetch failed: %s", exc)
        return 0
    count = 0
    for raw in emails:
        try:
            if process_email(raw):
                count += 1
        except Exception:  # noqa: BLE001 - one bad email must not stop the rest
            log.exception("Failed processing email %s", raw.subject)
    return count


def process_email(raw: RawEmail) -> bool:
    with session_scope() as session:
        if session.scalar(select(Email.id).where(Email.message_id == raw.message_id)):
            return False  # already seen
        email = Email(
            message_id=raw.message_id,
            sender=raw.sender[:500],
            subject=raw.subject[:1000],
            received_at=raw.received_at,
            web_link=raw.web_link,
        )
        session.add(email)
        session.flush()

        triage = triage_email(raw.sender, raw.subject, raw.body, local(raw.received_at).isoformat())
        email.category = triage.category
        email.importance = triage.importance
        email.decision = triage.to_dict()

        action = decide_action(triage)
        email.action = action
        log.info("Mail %r -> %s (%s, importance %.2f via %s)", raw.subject, action, triage.category,
                 triage.importance, triage.source)
        if action == "dropped":
            return True

        ext = extract_email(raw.sender, raw.subject, raw.body, raw.received_at, raw.links, triage.category)
        items = build_items(session, email, triage, ext, raw)
        for item in items:
            item.in_digest = action == "digest"
        session.flush()
        if action == "notify":
            for item in items:
                send_new_item(session, item, _short_sender(raw.sender))
        return True


def decide_action(t: Triage) -> str:
    """Thresholds on Jev's signals -> dropped | digest | notify."""
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
        items.append(
            Item(
                kind="event",
                title=ev.name or ext.title,
                event_start=ev.start,
                event_end=ev.end,
                venue=ev.venue,
                reg_deadline=ev.registration_deadline,
                reg_link=ev.registration_link,
                status="asked",
                **base,
            )
        )

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
    return items


def send_new_item(session: Session, item: Item, sender: str = "") -> None:
    text = messages.item_card(item, sender)
    urgent = item.kind == "deadline" and item.due_at and (item.due_at - utcnow()).total_seconds() < 6 * 3600
    if item.kind == "event":
        text += "\n\n*Are you interested in this?*"
        notifier.notify(session, text, messages.event_interest_buttons(item))
    elif item.kind == "deadline":
        notifier.notify(session, text, messages.deadline_buttons(item), urgent=bool(urgent))
    else:
        notifier.notify(session, text)


def _short_sender(sender: str) -> str:
    name = sender.split("<")[0].strip().strip('"')
    return name or sender
