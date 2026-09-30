"""Handles messages the user sends to the WhatsApp bot.

Button taps and exact commands are parsed deterministically (no AI call). Free text, "add …", "remind me …"
and "move …" cost exactly one Gemini call, which returns the intent, the item meant, and any new time.
Commands without a number ("done", "snooze 2h") apply to the message being swipe-replied to, or else to
the item the bot messaged about last.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import kv
from app.clients.gemini import LLMUnavailable
from app.models import Item, ReminderLog
from app.services import labels, messages, notifier, routines
from app.services.decisions import Intent, classify_message
from app.services.digest import build_digest
from app.timeutil import fmt, humanize_delta, local, parse_local, to_utc_naive, utcnow

log = logging.getLogger(__name__)

ACTIVE = ("pending", "asked", "interested", "registered", "active")
ITEM_INTENTS = ("mark_done", "mark_registered", "interested", "not_interested", "snooze", "details", "reschedule")
UNDO_KEY = "undo"

@dataclass
class Cmd:
    intent: str
    refs: list[str] = field(default_factory=list)  # "D3" / "3" typed, or "id:45" from a button
    hours: float | None = None
    query: str = ""  # a name, e.g. "dbms" in "done dbms"


_VERBS: list[tuple[str, str]] = [
    ("mark_done", r"done|submitted|finished|completed|complete|marked|did it"),
    ("mark_registered", r"registered|reg|signed up"),
    ("not_interested", r"not interested|no|n|skip|ignore|dismiss|cancel|delete|remove|stop"),
    ("interested", r"interested|yes|y|going"),
    ("details", r"details|detail|info|more|link"),
]
_FILLER = re.compile(r"\b(?:and|it|this|that|for today|today|now|already|pls|please)\b|[,&#]")
_UNIT = r"(h|hrs?|hours?|d|days?|m|mins?|minutes?)"
_DURATION = re.compile(rf"(?:^|\s)(\d+(?:\.\d+)?)\s*{_UNIT}$")
_REF = re.compile(r"^(?:[der]\d{1,3}|\d{1,3})$", re.IGNORECASE)
_NEEDS_AI = ("remind me", "move ", "reschedule ", "postpone ", "prepone ", "extend ", "shift ", "change ")
_STOPWORDS = {"the", "a", "an", "my", "for", "to", "of", "on", "in", "with", "about", "me", "i", "have", "has", "was",
              "is", "one", "thing", "task", "event", "deadline", "reminder", "wala", "vala"}
_SIMPLE = {
    "list": "list", "ls": "list", "pending": "list", "upcoming": "list", "todo": "list", "status": "list",
    "routines": "list", "reminders": "list",
    "digest": "digest", "today": "digest", "show": "flush",
    "help": "help", "commands": "help", "?": "help", "menu": "help", "hi": "hello", "hello": "hello", "hey": "hello",
    "pause": "pause", "mute": "pause", "resume": "resume", "unmute": "resume", "undo": "undo",
}
_BUTTON_OF = {"mark_done": "done", "mark_registered": "reg", "interested": "int", "not_interested": "dismiss",
              "details": "details", "snooze": "snooze"}


def _hours(qty: str, unit: str | None) -> float:
    u = (unit or "h")[0]
    return float(qty) * 24 if u == "d" else float(qty) / 60 if u == "m" else float(qty)


def _targets(rest: str) -> tuple[list[str], str]:
    words = _FILLER.sub(" ", rest).split()
    if all(_REF.match(w) for w in words):
        return words, ""
    return [], " ".join(words)


def parse(text: str) -> Cmd | None:
    """Deterministic parse, no AI. None -> let the AI read it."""
    t = " ".join(text.strip().lower().rstrip(".!").split())
    if t in _SIMPLE:
        return Cmd(_SIMPLE[t])
    if t.startswith("add "):
        return Cmd("add")
    if t.startswith(_NEEDS_AI):
        return None
    m = re.match(r"^(?:snooze|later|remind)\b(.*)$", t)
    if m:
        rest, hours = m.group(1), None
        dur = _DURATION.search(rest)
        if dur:
            hours, rest = _hours(dur.group(1), dur.group(2)), rest[:dur.start()]
        refs, query = _targets(rest)
        if len(refs) == 2 and refs[1].isdigit() and hours is None:  # "snooze d3 3" -> 3 hours
            hours, refs = float(refs[1]), refs[:1]
        return Cmd("snooze", refs, hours, query)
    for intent, verbs in _VERBS:
        m = re.match(rf"^(?:{verbs})\b(.*)$", t)
        if m:
            refs, query = _targets(m.group(1))
            return Cmd(intent, refs, None, query)
    return None


def parse_button(button_id: str) -> Cmd | None:
    parts = button_id.split(":")
    mapping = {"done": "mark_done", "reg": "mark_registered", "int": "interested", "notint": "not_interested",
               "dismiss": "not_interested", "snooze": "snooze", "details": "details"}
    if parts[0] == "ack":
        return Cmd("ack")
    if parts[0] == "cmd" and len(parts) > 1:
        return Cmd(parts[1])
    if parts[0] in mapping and len(parts) > 1 and parts[1].isdigit():
        hours = float(parts[2]) if parts[0] == "snooze" and len(parts) > 2 else None
        return Cmd(mapping[parts[0]], [f"id:{parts[1]}"], hours)
    return None


def _active_items(session: Session) -> list[Item]:
    return list(session.scalars(select(Item).where(Item.status.in_(ACTIVE)).order_by(Item.id.desc())).all())


def _sent_keys(session: Session, item_id: int) -> set[str]:
    return set(session.scalars(select(ReminderLog.key).where(ReminderLog.item_id == item_id)).all())


def match_name(session: Session, query: str, include_notices: bool = False) -> list[Item]:
    """Open items whose title contains every word of `query` (word prefixes: 'os' matches 'OS Assignment 3')."""
    tokens = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOPWORDS]
    if not tokens:
        return []
    pool = _active_items(session)
    if include_notices:
        pool += list(session.scalars(select(Item).where(
            Item.kind == "announcement", Item.created_at >= utcnow() - timedelta(days=14))).all())
    out = []
    for item in pool:
        title = (item.title or "").lower()
        words = re.findall(r"[a-z0-9]+", title)
        if all(any(w.startswith(tok) for w in words) or (len(tok) >= 4 and tok in title) for tok in tokens):
            out.append(item)
    exact = [i for i in out if all(tok in re.findall(r"[a-z0-9]+", i.title.lower()) for tok in tokens)]
    return exact or out  # whole-word hits beat prefix hits ("os" -> "OS quiz", not "OSDG hackathon")


def _resolve(session: Session, cmd: Cmd) -> tuple[list[Item], list[Item], list[str]]:
    """-> (items, choices when ambiguous, refs not found)."""
    items: list[Item] = []
    choices: list[Item] = []
    missing: list[str] = []
    for r in cmd.refs:
        item = None
        if r.startswith("id:"):
            item = session.get(Item, int(r[3:]))
        elif r.isdigit():
            found = labels.by_number(session, int(r))
            if len(found) > 1:
                choices += found
                continue
            item = found[0] if found else None
        else:
            item = labels.find(session, r)
        if item is None:
            missing.append(r.upper())
        elif item not in items:
            items.append(item)
    if cmd.query:
        found = match_name(session, cmd.query, include_notices=cmd.intent == "details")
        if len(found) == 1:
            items += found
        else:
            choices += found
    return items, choices, missing


def _ask_which(session: Session, cmd: Cmd, choices: list[Item]) -> None:
    choices = sorted(choices, key=lambda i: (i.label or "~")[0] + (i.label or "")[1:].zfill(3))
    lines = "\n".join(messages.item_line(i) for i in choices[:8])
    key = _BUTTON_OF.get(cmd.intent)
    buttons = None
    if key and len(choices) <= 3:
        extra = f":{cmd.hours:g}" if cmd.intent == "snooze" and cmd.hours else ""
        buttons = [(f"{key}:{i.id}{extra}", f"{i.label or ''} {i.title}".strip()[:20]) for i in choices]
    tip = "_Tap one_" if buttons else f"_Reply with its tag, e.g._ `{cmd.intent.replace('mark_', '')} {messages.ref(choices[0])}`"
    notifier.reply(session, f"🤔 *Which one?*\n{lines}\n\n{tip}", buttons)


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
    return "\n\n".join(parts) + "\n\n_Say it by name, e.g._ `done dbms` _or_ `details hackathon`"


def _now_text() -> str:
    now = local(utcnow())
    return f"{now.isoformat(timespec='minutes')} ({now.strftime('%A')})"


def handle_message(session: Session, text: str | None = None, button_id: str | None = None,
                   context_id: str | None = None) -> None:
    # Any inbound message re-opens the WhatsApp 24h window: deliver everything we held back first.
    notifier.flush_outbox(session, force=True)
    labels.backfill(session)

    ctx_item = notifier.item_for_message(session, context_id)
    cmd = parse_button(button_id) if button_id else None
    if not cmd and text:
        cmd = parse(text)
    items: list[Item] = []
    if cmd and cmd.intent in ITEM_INTENTS and (cmd.refs or cmd.query):
        items, choices, missing = _resolve(session, cmd)
        if choices and not items:
            _ask_which(session, cmd, choices)
            return
        if missing and not items:
            notifier.reply(session, f"🤷 I couldn't find {', '.join(missing)}.\n\n{_list_text(session)}")
            return
        if cmd.query and not items:
            cmd = None  # the name matched nothing: let the AI read the whole message
    ai: Intent | None = None
    if (not cmd or cmd.intent == "add") and text:
        active = _active_items(session)
        try:
            ai = classify_message(text, [(i.id, f"{i.label or ''} {i.kind}: {i.title}".strip()) for i in active],
                                  _now_text(),
                                  replying_to=f"{ctx_item.id} ({ctx_item.kind}: {ctx_item.title})" if ctx_item else "")
        except LLMUnavailable:
            notifier.reply(session, "😵 My AI quota is used up for the moment, so I can only follow exact commands "
                                    "right now (e.g. `done dbms`, `list`). Send `help` for the list.")
            return
        log.info("Free-text %r -> %s", text, ai)
        if cmd and cmd.intent == "add":
            ai.intent = "add"
        target = session.get(Item, ai.item_id) if ai.item_id else (ctx_item if ai.intent in ITEM_INTENTS else None)
        items = [target] if target else []
        cmd = Cmd(ai.intent, [], ai.snooze_hours if ai.intent == "snooze" else None)
    if not cmd:
        return
    guessed = False
    if cmd.intent in ITEM_INTENTS and not items and ai is None:
        target = ctx_item or notifier.last_item(session, ACTIVE)
        guessed = ctx_item is None and target is not None
        items = [target] if target else []
    _execute(session, cmd.intent, items, cmd.hours, ai, guessed)


def _need_item(session: Session, items: list[Item], verb: str) -> list[Item]:
    if not items:
        notifier.reply(session, f"Which one? Say it by name, e.g. `{verb} dbms`, or swipe-reply to my message about "
                                f"it.\n\n{_list_text(session)}")
    return items


# ---------------------------------------------------------------- undo

def _name(item: Item) -> str:
    return f"{item.label} {item.title}" if item.label else item.title


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
        labels.assign(session, item)
    kv.put(session, UNDO_KEY, None)
    notifier.reply(session, f"↩️ *Undone:* {data['label']}")


# ---------------------------------------------------------------- actions

def _execute(session: Session, intent: str, targets: list[Item], hours: float | None, ai: Intent | None = None,
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
        _mark_done(session, _need_item(session, targets, "done"), hint)
    elif intent == "mark_registered":
        items = _need_item(session, targets, "registered")
        _save_undo(session, "registered", [_snap(i) for i in items])
        for item in items:
            item.status = "registered"
            when = f"\n📅 I'll remind you before it starts ({fmt(item.event_start)})." if item.event_start else ""
            notifier.reply(session, f"🎟️ *Registered* for {messages.pre(item)}*{item.title}*.{when}{hint}", item_id=item.id)
    elif intent == "interested":
        for item in _need_item(session, targets, "interested")[:1]:
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
        items = _need_item(session, targets, "no")
        _save_undo(session, "dropped " + ", ".join(_name(i) for i in items), [_snap(i) for i in items])
        for item in items:
            if item.kind == "routine":
                item.status = "stopped"
                text = f"🛑 *Stopped* {messages.pre(item)}*{item.title}*. No more reminders."
            else:
                item.status = "not_interested" if item.kind == "event" else "dismissed"
                text = f"👌 *Dropped* {messages.pre(item)}*{item.title}*. No more reminders about it."
            notifier.reply(session, text + hint, item_id=item.id)
    elif intent == "snooze":
        for item in _need_item(session, targets, "snooze")[:1]:
            _snooze(session, item, hours, ai, hint)
    elif intent == "reschedule":
        for item in _need_item(session, targets, "move")[:1]:
            _reschedule(session, item, ai, hint)
    elif intent == "details":
        for item in _need_item(session, targets, "details")[:1]:
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
            lines.append(f"✅ *Done for today:* {messages.pre(item)}*{item.title}*.{again}")
        else:
            snaps.append(_snap(item))
            item.status = "done"
            item.snoozed_until = None
            lines.append(f"✅ *Nice!* {messages.pre(item)}*{item.title}* is done. No more reminders.")
    _save_undo(session, "done " + ", ".join(_name(i) for i in items), snaps)
    notifier.reply(session, "\n".join(lines) + hint, item_id=items[0].id if len(items) == 1 else None)


def _snooze(session: Session, item: Item, hours: float | None, ai: Intent | None, hint: str) -> None:
    until = parse_local(ai.snooze_until) if ai and ai.snooze_until else None
    if until is None or until <= utcnow():
        hours = hours or (ai.snooze_hours if ai and ai.snooze_hours else None) or (0.5 if item.kind == "routine" else 3.0)
        until = utcnow() + timedelta(hours=hours)
    _save_undo(session, f"snooze {_name(item)}", [_snap(item)])
    item.snoozed_until = until
    if item.kind == "routine" and item.status in ("done", "expired"):
        item.status = "active"  # "remind me again in 15 min" after a one-off reminder fired
        labels.assign(session, item)
    notifier.reply(session, f"⏳ *Snoozed* {messages.pre(item)}*{item.title}* until *{fmt(until)}*.{hint}",
                   item_id=item.id)


def _reschedule(session: Session, item: Item, ai: Intent | None, hint: str) -> None:
    new = parse_local(ai.new_time) if ai else None
    if new is None:
        notifier.reply(session, f"🤔 What's the new date? Try `move {messages.ref(item)} to Friday 5pm`.")
        return
    keys = _sent_keys(session, item.id)
    if item.kind == "deadline":
        stale = [k for k in keys if k.startswith("d") or k == "passed"]
    elif item.kind == "event":
        stale = [k for k in keys if k.startswith("e")]
    elif item.kind == "routine":
        stale = []
    else:
        notifier.reply(session, f"ℹ️ *{item.title}* is a notice, it has no date to move.")
        return
    _save_undo(session, f"move {_name(item)}", [_snap(item, del_keys=stale)])
    if stale:
        session.execute(delete(ReminderLog).where(ReminderLog.item_id == item.id, ReminderLog.key.in_(stale)))
    item.snoozed_until = None
    if item.kind == "deadline":
        item.due_at = new
        item.status = "pending"
        labels.assign(session, item)
        what = f"📌 *Moved* {messages.pre(item)}*{item.title}*\n⏰ Now due *{fmt(new)}*  _({humanize_delta(new)})_"
    elif item.kind == "event":
        item.event_start = new
        if item.status == "expired":
            item.status = "registered"
        labels.assign(session, item)
        what = f"📌 *Moved* {messages.pre(item)}*{item.title}*\n📅 Now *{fmt(new)}*  _({humanize_delta(new)})_"
    else:
        sch = dict(item.schedule or {})
        sch["start"] = local(new).strftime("%H:%M")
        if routines.is_one_off(sch):
            sch["date"] = local(new).date().isoformat()
        item.schedule = sch
        item.status = "active"
        labels.assign(session, item)
        what = f"📌 *Changed* {messages.pre(item)}*{item.title}*\n🗓️ {routines.describe(sch)}"
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
    labels.assign(session, item)
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
    labels.assign(session, item)
    session.flush()
    nxt = routines.next_slot(sch, utcnow(), item.created_at, set())
    if nxt is None and not days:  # "at 9am" said at 10am: they mean tomorrow
        sch = {**sch, "date": (first_l.date() + timedelta(days=1)).isoformat()}
        item.schedule = sch
        nxt = routines.next_slot(sch, utcnow(), item.created_at, set())
    notifier.reply(session, messages.routine_created(item, nxt), item_id=item.id)
