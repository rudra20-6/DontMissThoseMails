"""Handles messages the user sends to the WhatsApp bot.

Button taps and exact commands are parsed deterministically (no AI call). Free text, "add …", "remind me …"
and "move …" cost exactly one Gemini call, which returns the intent, the item meant, and any new time.
Commands without a number ("done", "snooze 2h") apply to the message being swipe-replied to, or else to
the item the bot messaged about last.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import kv
from app.clients.gemini import LLMUnavailable
from app.models import Item, ReminderLog
from app.services import messages, notifier, routines
from app.services.decisions import Intent, classify_message
from app.services.digest import build_digest
from app.timeutil import fmt, humanize_delta, local, parse_local, to_utc_naive, utcnow

log = logging.getLogger(__name__)

ACTIVE = ("pending", "asked", "interested", "registered", "active")
ITEM_INTENTS = ("mark_done", "mark_registered", "interested", "not_interested", "snooze", "details", "reschedule")
UNDO_KEY = "undo"

Parsed = tuple[str, list[int], float | None]

_VERBS: list[tuple[str, str]] = [
    ("mark_done", r"done|submitted|finished|completed|complete|marked|did it"),
    ("mark_registered", r"registered|reg|signed up"),
    ("not_interested", r"not interested|no|n|skip|ignore|dismiss|cancel|delete|remove|stop"),
    ("interested", r"interested|yes|y|going"),
    ("details", r"details|detail|info|more|link"),
]
_FILLER = re.compile(r"\b(?:and|it|this|that|for today|today|now|already|pls|please)\b|[,&#]")
_SNOOZE = re.compile(
    r"^(?:snooze|later|remind)(?:\s*#?(\d+)(?![\d.]*\s*[hdm]))?"
    r"(?:\s*(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|d|days?|m|mins?|minutes?)?)?$"
)
_SIMPLE = {
    "list": "list", "ls": "list", "pending": "list", "upcoming": "list", "todo": "list", "status": "list",
    "routines": "list", "reminders": "list",
    "digest": "digest", "today": "digest", "show": "flush",
    "help": "help", "commands": "help", "?": "help", "menu": "help", "hi": "hello", "hello": "hello", "hey": "hello",
    "pause": "pause", "mute": "pause", "resume": "resume", "unmute": "resume", "undo": "undo",
}


def parse(text: str) -> Parsed | None:
    """Deterministic parse -> (intent, item_ids, snooze_hours) or None (needs the AI)."""
    t = text.strip().lower().rstrip(".!")
    if t in _SIMPLE:
        return _SIMPLE[t], [], None
    if t.startswith("add "):
        return "add", [], None
    m = _SNOOZE.match(t)
    if m and not (t.startswith("remind") and not m.group(1)):
        hours = None
        if m.group(2):
            qty, unit = float(m.group(2)), (m.group(3) or "h")[0]
            hours = qty * 24 if unit == "d" else qty / 60 if unit == "m" else qty
        return "snooze", [int(m.group(1))] if m.group(1) else [], hours
    for intent, verbs in _VERBS:
        m = re.match(rf"^(?:{verbs})\b(.*)$", t)
        if not m:
            continue
        rest = _FILLER.sub(" ", m.group(1)).strip()
        if rest and not re.fullmatch(r"\d+(?:\s+\d+)*", rest):
            return None  # e.g. "done with the OS assignment": let the AI work out which item
        return intent, [int(x) for x in rest.split()], None
    return None


def parse_button(button_id: str) -> Parsed | None:
    parts = button_id.split(":")
    mapping = {"done": "mark_done", "reg": "mark_registered", "int": "interested", "notint": "not_interested",
               "dismiss": "not_interested", "snooze": "snooze", "details": "details"}
    if parts[0] == "ack":
        return "ack", [], None
    if parts[0] == "cmd" and len(parts) > 1:
        return parts[1], [], None
    if parts[0] in mapping and len(parts) > 1 and parts[1].isdigit():
        hours = float(parts[2]) if parts[0] == "snooze" and len(parts) > 2 else (3.0 if parts[0] == "snooze" else None)
        return mapping[parts[0]], [int(parts[1])], hours
    return None


def _active_items(session: Session) -> list[Item]:
    return list(session.scalars(select(Item).where(Item.status.in_(ACTIVE)).order_by(Item.id.desc())).all())


def _sent_keys(session: Session, item_id: int) -> set[str]:
    return set(session.scalars(select(ReminderLog.key).where(ReminderLog.item_id == item_id)).all())


def _list_text(session: Session) -> str:
    items = _active_items(session)
    if not items:
        return "🎈 *All clear!* Nothing pending right now."
    deadlines = sorted([i for i in items if i.kind == "deadline"], key=lambda i: i.due_at or utcnow())
    events = [i for i in items if i.kind == "event"]
    routines_ = [i for i in items if i.kind == "routine"]
    parts = []
    if deadlines:
        parts.append("⏰ *Deadlines*\n" + "\n".join(messages.item_line(i) for i in deadlines))
    if events:
        parts.append("🎉 *Events*\n" + "\n".join(messages.item_line(i) for i in events))
    if routines_:
        parts.append("🔁 *Reminders*\n" + "\n".join(messages.item_line(i, _sent_keys(session, i.id)) for i in routines_))
    return "\n\n".join(parts) + "\n\n_Reply_ `details <id>` _for more._"


def _now_text() -> str:
    now = local(utcnow())
    return f"{now.isoformat(timespec='minutes')} ({now.strftime('%A')})"


def handle_message(session: Session, text: str | None = None, button_id: str | None = None,
                   context_id: str | None = None) -> None:
    # Any inbound message re-opens the WhatsApp 24h window: deliver everything we held back first.
    notifier.flush_outbox(session, force=True)

    ctx_item = notifier.item_for_message(session, context_id)
    parsed = parse_button(button_id) if button_id else None
    if not parsed and text:
        parsed = parse(text)
    ai: Intent | None = None
    if (not parsed or parsed[0] == "add") and text:
        items = _active_items(session)
        try:
            ai = classify_message(text, [(i.id, f"{i.kind}: {i.title}") for i in items], _now_text(),
                                  replying_to=f"{ctx_item.id} ({ctx_item.kind}: {ctx_item.title})" if ctx_item else "")
        except LLMUnavailable:
            notifier.reply(session, "😵 My AI quota is used up for the moment, so I can only follow exact commands "
                                    "right now (e.g. `done 12`, `list`). Send `help` for the list.")
            return
        log.info("Free-text %r -> %s", text, ai)
        if parsed and parsed[0] == "add":
            ai.intent = "add"
        ids = [ai.item_id] if ai.item_id else ([ctx_item.id] if ctx_item and ai.intent in ITEM_INTENTS else [])
        parsed = (ai.intent, ids, ai.snooze_hours if ai.intent == "snooze" else None)
    if not parsed:
        return
    intent, ids, hours = parsed
    guessed = False
    if intent in ITEM_INTENTS and not ids and ai is None:
        target = ctx_item or notifier.last_item(session, ACTIVE)
        guessed = ctx_item is None and target is not None
        ids = [target.id] if target else []
    _execute(session, intent, ids, hours, ai, guessed)


def _need_item(session: Session, ids: list[int], verb: str) -> list[Item]:
    items = [i for i in (session.get(Item, iid) for iid in ids) if i is not None]
    if not items:
        missing = f"I couldn't find #{ids[0]}. " if ids else ""
        notifier.reply(session, f"{missing}Which one? Reply with the number, e.g. `{verb} 12`, or swipe-reply to my "
                                f"message about it.\n\n{_list_text(session)}")
    return items


# ---------------------------------------------------------------- undo

def _snap(item: Item, add_keys: list[str] | None = None, del_keys: list[str] | None = None) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"id": item.id, "status": item.status, "snoozed_until": iso(item.snoozed_until), "due_at": iso(item.due_at),
            "event_start": iso(item.event_start), "schedule": item.schedule, "add_keys": add_keys or [],
            "del_keys": del_keys or []}


def _save_undo(session: Session, label: str, snaps: list[dict]) -> None:
    kv.put(session, UNDO_KEY, {"label": label, "items": snaps})


def _undo(session: Session) -> None:
    data = kv.get(session, UNDO_KEY)
    if not data:
        notifier.reply(session, "🤷 Nothing to undo.")
        return
    dt = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
    for snap in data["items"]:
        item = session.get(Item, snap["id"])
        if item is None:
            continue
        item.status, item.snoozed_until = snap["status"], dt(snap["snoozed_until"])
        item.due_at, item.event_start, item.schedule = dt(snap["due_at"]), dt(snap["event_start"]), snap["schedule"]
        if snap["add_keys"]:
            session.execute(delete(ReminderLog).where(ReminderLog.item_id == item.id,
                                                      ReminderLog.key.in_(snap["add_keys"])))
        existing = _sent_keys(session, item.id)
        for key in snap["del_keys"]:
            if key not in existing:
                session.add(ReminderLog(item_id=item.id, key=key))
    kv.put(session, UNDO_KEY, None)
    notifier.reply(session, f"↩️ *Undone:* {data['label']}")


# ---------------------------------------------------------------- actions

def _execute(session: Session, intent: str, ids: list[int], hours: float | None, ai: Intent | None = None,
             guessed: bool = False) -> None:
    hint = "\n_Wrong one? Send_ `undo`" if guessed else ""
    if intent in ("help", "hello"):
        notifier.reply(session, ("👋 *Hey!* I'm watching your inbox.\n\n" if intent == "hello" else "") + messages.HELP)
    elif intent == "list":
        notifier.reply(session, _list_text(session))
    elif intent == "digest":
        notifier.reply(session, build_digest(session))
    elif intent in ("ack", "flush", "other"):
        if intent == "other":
            notifier.reply(session, "🙂 Noted. Send `help` to see what I can do.")
    elif intent == "undo":
        _undo(session)
    elif intent == "pause":
        kv.put(session, notifier.PAUSE_KEY, True)
        notifier.reply(session, "🔕 *Paused.* I'll only message you for urgent reminders. Send `resume` to unmute.")
    elif intent == "resume":
        kv.put(session, notifier.PAUSE_KEY, False)
        notifier.reply(session, "🔔 *Resumed.*")
        notifier.flush_outbox(session, force=True)
    elif intent == "add":
        _add_task(session, ai)
    elif intent == "remind":
        _add_routine(session, ai)
    elif intent == "mark_done":
        _mark_done(session, _need_item(session, ids, "done"), hint)
    elif intent == "mark_registered":
        items = _need_item(session, ids, "registered")
        _save_undo(session, "registered", [_snap(i) for i in items])
        for item in items:
            item.status = "registered"
            when = f"\n📅 I'll remind you before it starts ({fmt(item.event_start)})." if item.event_start else ""
            notifier.reply(session, f"🎟️ *Registered* for *{item.title}* {messages.tag(item)}.{when}{hint}", item_id=item.id)
    elif intent == "interested":
        for item in _need_item(session, ids, "interested")[:1]:
            _save_undo(session, "interested", [_snap(item)])
            item.status = "interested"
            item.snoozed_until = None
            by = f" before *{fmt(item.reg_deadline)}*" if item.reg_deadline else ""
            link = f"\n🔗 {item.reg_link or item.link}" if (item.reg_link or item.link) else ""
            notifier.reply(
                session,
                f"👍 *Great!* I'll keep reminding you to register{by} until you tell me you've registered.{link}{hint}",
                [(f"reg:{item.id}", "✅ Registered now")], item_id=item.id,
            )
    elif intent == "not_interested":
        items = _need_item(session, ids, "no")
        _save_undo(session, "dropped " + ", ".join(f"#{i.id}" for i in items), [_snap(i) for i in items])
        for item in items:
            if item.kind == "routine":
                item.status = "stopped"
                text = f"🛑 *Stopped* {messages.tag(item)} *{item.title}*. No more reminders."
            else:
                item.status = "not_interested" if item.kind == "event" else "dismissed"
                text = f"👌 *Dropped* {messages.tag(item)} *{item.title}*. No more reminders about it."
            notifier.reply(session, text + hint, item_id=item.id)
    elif intent == "snooze":
        for item in _need_item(session, ids, "snooze")[:1]:
            _snooze(session, item, hours, ai, hint)
    elif intent == "reschedule":
        for item in _need_item(session, ids, "move")[:1]:
            _reschedule(session, item, ai, hint)
    elif intent == "details":
        for item in _need_item(session, ids, "details")[:1]:
            nxt = routines.next_slot(item.schedule, utcnow(), item.created_at, _sent_keys(session, item.id)) \
                if item.kind == "routine" and item.schedule else None
            text = messages.routine_card(item, nxt) if item.kind == "routine" else messages.item_card(item)
            notifier.reply(session, text, item_id=item.id)


def _mark_done(session: Session, items: list[Item], hint: str) -> None:
    if not items:
        return
    snaps, lines = [], []
    today = local(utcnow()).date()
    for item in items:
        sch = item.schedule or {}
        if item.kind == "routine" and not routines.is_one_off(sch):
            key = routines.done_key(today)
            added = [] if key in _sent_keys(session, item.id) else [key]
            snaps.append(_snap(item, add_keys=added))
            for k in added:
                session.add(ReminderLog(item_id=item.id, key=k))
            session.flush()
            item.snoozed_until = None
            nxt = routines.next_slot(sch, utcnow(), item.created_at, _sent_keys(session, item.id))
            again = f" Next reminder *{fmt(to_utc_naive(nxt))}*." if nxt else ""
            lines.append(f"✅ *Done for today:* {messages.tag(item)} *{item.title}*.{again}")
        else:
            snaps.append(_snap(item))
            item.status = "done"
            item.snoozed_until = None
            lines.append(f"✅ *Nice!* {messages.tag(item)} *{item.title}* is done. No more reminders.")
    _save_undo(session, "done " + ", ".join(f"#{i.id}" for i in items), snaps)
    notifier.reply(session, "\n".join(lines) + hint, item_id=items[0].id if len(items) == 1 else None)


def _snooze(session: Session, item: Item, hours: float | None, ai: Intent | None, hint: str) -> None:
    until = parse_local(ai.snooze_until) if ai and ai.snooze_until else None
    if until is None or until <= utcnow():
        hours = hours or (ai.snooze_hours if ai and ai.snooze_hours else None) or (0.5 if item.kind == "routine" else 3.0)
        until = utcnow() + timedelta(hours=hours)
    _save_undo(session, f"snooze #{item.id}", [_snap(item)])
    item.snoozed_until = until
    if item.kind == "routine" and item.status in ("done", "expired"):
        item.status = "active"  # "remind me again in 15 min" after a one-off reminder fired
    notifier.reply(session, f"⏳ *Snoozed* {messages.tag(item)} *{item.title}* until *{fmt(until)}*.{hint}",
                   item_id=item.id)


def _reschedule(session: Session, item: Item, ai: Intent | None, hint: str) -> None:
    new = parse_local(ai.new_time) if ai else None
    if new is None:
        notifier.reply(session, f"🤔 What's the new date? Try `move {item.id} to Friday 5pm`.")
        return
    keys = _sent_keys(session, item.id)
    if item.kind == "deadline":
        stale = [k for k in keys if k.startswith("d") or k == "passed"]
    elif item.kind == "event":
        stale = [k for k in keys if k.startswith("e")]
    elif item.kind == "routine":
        stale = []
    else:
        notifier.reply(session, f"ℹ️ {messages.tag(item)} is a notice, it has no date to move.")
        return
    _save_undo(session, f"move #{item.id}", [_snap(item, del_keys=stale)])
    if stale:
        session.execute(delete(ReminderLog).where(ReminderLog.item_id == item.id, ReminderLog.key.in_(stale)))
    item.snoozed_until = None
    if item.kind == "deadline":
        item.due_at = new
        item.status = "pending"
        what = f"📌 *Moved* {messages.tag(item)} *{item.title}*\n⏰ Now due *{fmt(new)}*  _({humanize_delta(new)})_"
    elif item.kind == "event":
        item.event_start = new
        if item.status == "expired":
            item.status = "registered"
        what = f"📌 *Moved* {messages.tag(item)} *{item.title}*\n📅 Now *{fmt(new)}*  _({humanize_delta(new)})_"
    else:
        sch = dict(item.schedule or {})
        sch["start"] = local(new).strftime("%H:%M")
        if routines.is_one_off(sch):
            sch["date"] = local(new).date().isoformat()
        item.schedule = sch
        item.status = "active"
        what = f"📌 *Changed* {messages.tag(item)} *{item.title}*\n🗓️ {routines.describe(sch)}"
    notifier.reply(session, what + "\n_Reminders re-planned._" + hint, item_id=item.id)


def _add_task(session: Session, ai: Intent | None) -> None:
    due = parse_local(ai.task_due) if ai else None
    title, is_event = (ai.task_title or "My task") if ai else "", bool(ai and ai.task_is_event)
    if due is None:
        notifier.reply(session, "🤔 I couldn't find a date in that. Try `add OS quiz prep due Monday 9am`.")
        return
    if is_event:
        item = Item(kind="event", category="personal", title=title, event_start=due, status="registered", importance=2.5)
    else:
        item = Item(kind="deadline", category="personal", title=title, due_at=due, status="pending", importance=3.0)
    session.add(item)
    session.flush()
    what = "event" if is_event else "deadline"
    notifier.reply(session, f"📌 *Added {what}* {messages.tag(item)}\n*{title}*\n"
                            f"{'📅' if is_event else '⏰'} {fmt(due)}  _({humanize_delta(due)})_\n\n_I'll remind you._",
                   item_id=item.id)


def _add_routine(session: Session, ai: Intent | None) -> None:
    first = parse_local(ai.remind_first) if ai else None
    if ai is None or first is None:
        notifier.reply(session, "🤔 When should I remind you? For example:\n"
                                "• _remind me at 5pm to call home_\n"
                                "• _remind me every day at 9am to put attendance on ISB, every hour until I say done_")
        return
    first_l = local(first)
    every = ai.remind_every
    if 0 < every < routines.MIN_EVERY:
        every = routines.MIN_EVERY
    sch: dict = {"start": first_l.strftime("%H:%M"), "every": every, "end": routines.clean_hhmm(ai.remind_until)}
    days = routines.parse_days(ai.remind_days)
    if days:
        sch["days"] = days
    else:
        sch["date"] = first_l.date().isoformat()
    item = Item(kind="routine", category="personal", title=(ai.task_title or "Reminder")[:200], schedule=sch,
                status="active", importance=2.5, summary="")
    session.add(item)
    session.flush()
    nxt = routines.next_slot(sch, utcnow(), item.created_at, set())
    if nxt is None and not days:  # "at 9am" said at 10am: they mean tomorrow
        sch = {**sch, "date": (first_l.date() + timedelta(days=1)).isoformat()}
        item.schedule = sch
        nxt = routines.next_slot(sch, utcnow(), item.created_at, set())
    notifier.reply(session, messages.routine_created(item, nxt), item_id=item.id)
