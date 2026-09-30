"""Daily digest: everything upcoming + low-priority mail that was held back."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kv
from app.config import get_settings
from app.models import Item, ReminderLog
from app.services import messages, notifier, routines
from app.timeutil import fmt, local, utcnow

DIGEST_KEY = "last_digest_date"


def build_digest(session: Session) -> str:
    now = utcnow()
    week = now + timedelta(days=7)
    deadlines = session.scalars(
        select(Item).where(Item.kind == "deadline", Item.status == "pending").order_by(Item.due_at)
    ).all()
    events = session.scalars(
        select(Item).where(Item.kind == "event", Item.status.in_(("asked", "interested", "registered")))
        .order_by(Item.event_start)
    ).all()
    held = session.scalars(
        select(Item).where(Item.in_digest.is_(True), Item.digested.is_(False)).order_by(Item.created_at)
    ).all()

    today = local(now).date()
    todays_routines = [
        r for r in session.scalars(select(Item).where(Item.kind == "routine", Item.status == "active")).all()
        if r.schedule and routines.runs_on(r.schedule, today)
    ]

    parts = [f"☀️ *Good morning!*  ·  _{local(now).strftime('%A %d %b')}_"]
    soon = [d for d in deadlines if d.due_at and d.due_at <= week]
    later = [d for d in deadlines if d.due_at and d.due_at > week]
    if soon:
        parts.append("\n⏰ *Deadlines (next 7 days)*\n" + "\n".join(messages.item_line(d) for d in soon))
    if later:
        parts.append(f"\n🗓️ _+{len(later)} later deadline(s). Send_ `list` _to see all._")
    todo = [e for e in events if e.status in ("asked", "interested")]
    going = [e for e in events if e.status == "registered"]
    if todo:
        parts.append("\n📝 *Events needing an answer / registration*\n" + "\n".join(messages.item_line(e) for e in todo))
    if going:
        parts.append("\n🎉 *Events you're registered for*\n" + "\n".join(messages.item_line(e) for e in going))
    if todays_routines:
        parts.append("\n🔁 *Today's reminders*\n" + "\n".join(
            messages.item_line(r, set(session.scalars(select(ReminderLog.key).where(ReminderLog.item_id == r.id))))
            for r in todays_routines))
    if held:
        lines = [f"• {messages.CATEGORY_EMOJI.get(i.category, '📩')} *{i.title}*\n      _{i.summary[:160]}_"
                 for i in held[:15]]
        parts.append("\n📬 *Other mail (low priority)*\n" + "\n".join(lines))
        for i in held:
            i.digested = True
    if len(parts) == 1:
        parts.append("\n🎈 Nothing pending. Enjoy your day!")
    parts.append("\n_Reply_ `details <name>` _for more,_ `help` _for commands._")
    return "\n".join(parts)


def maybe_send_daily_digest(session: Session) -> bool:
    s = get_settings()
    now_l = local(utcnow())
    today = now_l.date().isoformat()
    if now_l.hour < s.digest_hour or kv.get(session, DIGEST_KEY) == today:
        return False
    kv.put(session, DIGEST_KEY, today)
    notifier.notify(session, build_digest(session), [("ack:digest", "👍 Got it"), ("cmd:list", "📋 Full list")])
    return True


def maybe_send_catchup(session: Session) -> bool:
    """After a batch of OLD mails (e.g. last week's inbox) has been analysed, send ONE summary of what matters."""
    from app.models import Email

    items = session.scalars(select(Item).where(Item.catchup.is_(True)).order_by(Item.id)).all()
    if not items:
        return False
    if session.scalar(select(Email.id).where(Email.action == "pending").limit(1)):
        return False  # still analysing the backlog: wait so the summary is complete
    deadlines = sorted([i for i in items if i.kind == "deadline" and i.status == "pending"], key=lambda i: i.due_at)
    events = sorted([i for i in items if i.kind == "event" and i.status in ("asked", "interested", "registered")],
                    key=lambda i: i.event_start or i.reg_deadline or utcnow())
    notices = [i for i in items if i.kind == "announcement"]

    parts = ["📥 *Catch-up: your recent mail*",
             "_I went through your older emails. Here's what still matters. Reminders are now on for all of it._"]
    if deadlines:
        parts.append("\n⏰ *Upcoming deadlines*\n" + "\n".join(messages.item_line(d) for d in deadlines))
    if events:
        lines = []
        for e in events:
            reg = f" · register by {fmt(e.reg_deadline)}" if e.reg_deadline else ""
            lines.append(f"• {messages.tag(e)} *{e.title}*" + (f"\n      📅 {fmt(e.event_start)}" if e.event_start else "") + reg)
        parts.append("\n🎉 *Events you can still join*\n" + "\n".join(lines)
                     + "\n_Reply_ `interested <name>` _or_ `no <name>` _for each, e.g._ `interested "
                     + messages.ref(events[0]) + "`")
    if notices:
        parts.append("\n📢 *Important notices*\n" + "\n".join(
            f"• {messages.CATEGORY_EMOJI.get(n.category, '📩')} *{n.title}*\n      _{n.summary[:140]}_"
            for n in notices[:12]))
    if len(parts) == 2:
        parts.append("\nNothing from those mails needs your attention anymore. 🎈")
    parts.append("\n_Reply_ `details <name>` _for more about any item._")
    notifier.notify(session, "\n".join(parts))
    for i in items:
        i.catchup = False
    return True
