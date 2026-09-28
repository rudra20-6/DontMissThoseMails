"""Daily digest: everything upcoming + low-priority mail that was held back."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kv
from app.config import get_settings
from app.models import Item
from app.services import messages, notifier
from app.timeutil import local, utcnow

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

    parts = [f"☀️ *Daily digest — {local(now).strftime('%a %d %b')}*"]
    soon = [d for d in deadlines if d.due_at and d.due_at <= week]
    later = [d for d in deadlines if d.due_at and d.due_at > week]
    if soon:
        parts.append("\n⏰ *Deadlines (next 7 days)*\n" + "\n".join(messages.item_line(d) for d in soon))
    if later:
        parts.append(f"\n🗓️ _+{len(later)} later deadline(s) — send *list* to see all_")
    todo = [e for e in events if e.status in ("asked", "interested")]
    going = [e for e in events if e.status == "registered"]
    if todo:
        parts.append("\n📝 *Events needing an answer / registration*\n" + "\n".join(messages.item_line(e) for e in todo))
    if going:
        parts.append("\n🎉 *Events you're registered for*\n" + "\n".join(messages.item_line(e) for e in going))
    if held:
        lines = [f"• #{i.id} {messages.CATEGORY_EMOJI.get(i.category, '📩')} *{i.title}* — {i.summary[:160]}" for i in held[:15]]
        parts.append("\n📬 *Other mail (low priority)*\n" + "\n".join(lines))
        for i in held:
            i.digested = True
    if len(parts) == 1:
        parts.append("\nNothing pending. Enjoy your day! 🎈")
    parts.append("\n_Reply *details <id>* for more, *help* for commands._")
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
