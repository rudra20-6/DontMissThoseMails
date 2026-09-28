"""Handles messages the user sends to the WhatsApp bot.

Button taps and exact commands are parsed deterministically (no AI call). Free text and "add <task>"
cost exactly one Gemini call, which returns the intent, the item meant, and the new task's date.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kv
from app.models import Item
from app.services import messages, notifier
from app.clients.gemini import LLMUnavailable
from app.services.decisions import Intent, classify_message
from app.services.digest import build_digest
from app.timeutil import fmt, local, parse_local, utcnow

log = logging.getLogger(__name__)

ACTIVE = ("pending", "asked", "interested", "registered")

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("mark_done", re.compile(r"^(?:done|submitted|finished|completed|complete)\s*#?(\d+)$")),
    ("mark_registered", re.compile(r"^(?:registered|reg|signed up)\s*#?(\d+)$")),
    ("interested", re.compile(r"^(?:interested|yes|y|going)\s*#?(\d+)$")),
    ("not_interested", re.compile(r"^(?:not interested|no|n|skip|ignore|dismiss|cancel|delete|remove)\s*#?(\d+)$")),
    ("details", re.compile(r"^(?:details|detail|info|more|show|link)\s*#?(\d+)$")),
    ("snooze", re.compile(r"^(?:snooze|later|remind)\s*#?(\d+)(?:\s+(\d+)\s*([hd]|hours?|days?|m|mins?|minutes?)?)?$")),
]
_SIMPLE = {
    "list": "list", "ls": "list", "pending": "list", "upcoming": "list", "todo": "list", "status": "list",
    "digest": "digest", "today": "digest", "show": "flush",
    "help": "help", "commands": "help", "?": "help", "menu": "help", "hi": "hello", "hello": "hello", "hey": "hello",
    "pause": "pause", "mute": "pause", "resume": "resume", "unmute": "resume",
}


def parse(text: str) -> tuple[str, int | None, float | None] | None:
    """Deterministic parse -> (intent, item_id, snooze_hours) or None."""
    t = text.strip().lower().rstrip(".!")
    if t in _SIMPLE:
        return _SIMPLE[t], None, None
    if t.startswith("add "):
        return "add", None, None
    for intent, pat in _PATTERNS:
        m = pat.match(t)
        if m:
            item_id = int(m.group(1))
            hours = None
            if intent == "snooze":
                qty = float(m.group(2)) if m.group(2) else 3.0
                unit = (m.group(3) or "h")[0]
                hours = qty * 24 if unit == "d" else qty / 60 if unit == "m" else qty
            return intent, item_id, hours
    return None


def parse_button(button_id: str) -> tuple[str, int | None, float | None] | None:
    parts = button_id.split(":")
    mapping = {"done": "mark_done", "reg": "mark_registered", "int": "interested", "notint": "not_interested",
               "dismiss": "not_interested", "snooze": "snooze", "details": "details"}
    if parts[0] == "ack":
        return "ack", None, None
    if parts[0] == "cmd" and len(parts) > 1:
        return parts[1], None, None
    if parts[0] in mapping and len(parts) > 1 and parts[1].isdigit():
        hours = float(parts[2]) if parts[0] == "snooze" and len(parts) > 2 else (3.0 if parts[0] == "snooze" else None)
        return mapping[parts[0]], int(parts[1]), hours
    return None


def _active_items(session: Session) -> list[Item]:
    return list(session.scalars(select(Item).where(Item.status.in_(ACTIVE)).order_by(Item.id.desc())).all())


def _list_text(session: Session) -> str:
    items = _active_items(session)
    if not items:
        return "🎈 Nothing pending right now."
    deadlines = sorted([i for i in items if i.kind == "deadline"], key=lambda i: i.due_at or utcnow())
    events = [i for i in items if i.kind == "event"]
    parts = []
    if deadlines:
        parts.append("⏰ *Deadlines*\n" + "\n".join(messages.item_line(i) for i in deadlines))
    if events:
        parts.append("🎉 *Events*\n" + "\n".join(messages.item_line(i) for i in events))
    return "\n\n".join(parts)


def handle_message(session: Session, text: str | None = None, button_id: str | None = None) -> None:
    # Any inbound message re-opens the WhatsApp 24h window: deliver everything we held back first.
    notifier.flush_outbox(session, force=True)

    parsed = parse_button(button_id) if button_id else None
    if not parsed and text:
        parsed = parse(text)
    ai: Intent | None = None
    if (not parsed or parsed[0] == "add") and text:
        items = _active_items(session)
        try:
            ai = classify_message(text, [(i.id, f"{i.kind}: {i.title}") for i in items], local(utcnow()).isoformat())
        except LLMUnavailable:
            notifier.reply(session, "😵 My AI quota is used up for the moment, so I can only follow exact commands "
                                    "right now (e.g. *done 12*, *list*). Send *help* for the list.")
            return
        log.info("Free-text %r -> %s", text, ai)
        if parsed and parsed[0] == "add":
            ai.intent = "add"
        parsed = (ai.intent, ai.item_id, (ai.snooze_hours or 3.0) if ai.intent == "snooze" else None)
    if not parsed:
        return
    intent, item_id, hours = parsed
    _execute(session, intent, item_id, hours, text or "", ai)


def _need_item(session: Session, item_id: int | None, verb: str) -> Item | None:
    item = session.get(Item, item_id) if item_id else None
    if item is None:
        lst = _list_text(session)
        notifier.reply(session, f"Which item do you want to {verb}? Reply with the number, e.g. *{verb} 12*.\n\n{lst}")
    return item


def _execute(session: Session, intent: str, item_id: int | None, hours: float | None, text: str,
             ai: Intent | None = None) -> None:
    if intent in ("help", "hello"):
        notifier.reply(session, ("👋 Hey! I'm watching your inbox.\n\n" if intent == "hello" else "") + messages.HELP)
    elif intent == "list":
        notifier.reply(session, _list_text(session))
    elif intent == "digest":
        notifier.reply(session, build_digest(session))
    elif intent in ("ack", "flush", "other"):
        if intent == "other":
            notifier.reply(session, "🙂 Noted. Send *help* to see what I can do.")
    elif intent == "pause":
        kv.put(session, notifier.PAUSE_KEY, True)
        notifier.reply(session, "🔕 Paused. I'll only message you for urgent reminders. Send *resume* to unmute.")
    elif intent == "resume":
        kv.put(session, notifier.PAUSE_KEY, False)
        notifier.reply(session, "🔔 Resumed.")
        notifier.flush_outbox(session, force=True)
    elif intent == "add":
        _add_task(session, ai)
    elif intent == "mark_done":
        if item := _need_item(session, item_id, "done"):
            item.status = "done"
            notifier.reply(session, f"✅ Nice! Marked *{item.title}* (#{item.id}) as done. No more reminders.")
    elif intent == "mark_registered":
        if item := _need_item(session, item_id, "registered"):
            item.status = "registered"
            when = f" I'll remind you before it starts ({fmt(item.event_start)})." if item.event_start else ""
            notifier.reply(session, f"🎟️ Registered for *{item.title}* (#{item.id}).{when}")
    elif intent == "interested":
        if item := _need_item(session, item_id, "interested"):
            item.status = "interested"
            item.snoozed_until = None
            by = f" before *{fmt(item.reg_deadline)}*" if item.reg_deadline else ""
            link = f"\n🔗 {item.reg_link or item.link}" if (item.reg_link or item.link) else ""
            notifier.reply(
                session,
                f"👍 Great! I'll keep reminding you to register{by} until you tell me you've registered.{link}",
                [(f"reg:{item.id}", "✅ Registered now")],
            )
    elif intent == "not_interested":
        if item := _need_item(session, item_id, "no"):
            item.status = "not_interested" if item.kind == "event" else "dismissed"
            notifier.reply(session, f"👌 Dropped *{item.title}* (#{item.id}). No more reminders about it.")
    elif intent == "snooze":
        if item := _need_item(session, item_id, "snooze"):
            item.snoozed_until = utcnow() + timedelta(hours=hours or 3)
            notifier.reply(session, f"⏳ Snoozed *{item.title}* until {fmt(item.snoozed_until)}.")
    elif intent == "details":
        if item := _need_item(session, item_id, "details"):
            notifier.reply(session, messages.item_card(item))


def _add_task(session: Session, ai: Intent | None) -> None:
    due = parse_local(ai.task_due) if ai else None
    title, is_event = (ai.task_title or "My task") if ai else "", bool(ai and ai.task_is_event)
    if due is None:
        notifier.reply(session, "🤔 I couldn't find a date in that. Try: *add OS quiz prep due Monday 9am*")
        return
    if is_event:
        item = Item(kind="event", category="personal", title=title, event_start=due, status="registered", importance=2.5)
    else:
        item = Item(kind="deadline", category="personal", title=title, due_at=due, status="pending", importance=3.0)
    session.add(item)
    session.flush()
    what = "event" if is_event else "deadline"
    notifier.reply(session, f"📌 Added {what} #{item.id}: *{title}* — {fmt(due)}. I'll remind you.")
