"""Deterministic WhatsApp message templates (WhatsApp formatting: *bold*, _italic_)."""

from __future__ import annotations

from app.models import Item
from app.services import routines
from app.services.decisions import importance_label
from app.timeutil import fmt, fmt_clock, humanize_delta, local, to_utc_naive, utcnow

CATEGORY_EMOJI = {
    "coursework": "📚",
    "academic_admin": "🏛️",
    "event": "🎉",
    "opportunity": "💼",
    "campus_admin": "🏠",
    "personal": "✉️",
    "newsletter_promo": "📰",
    "other": "📩",
}
CATEGORY_NAME = {
    "coursework": "Coursework",
    "academic_admin": "Academic notice",
    "event": "Event",
    "opportunity": "Opportunity",
    "campus_admin": "Campus notice",
    "personal": "Personal",
    "newsletter_promo": "Newsletter",
    "other": "Mail",
}
IMPORTANCE_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪", "ignore": "⚪"}


def tag(item: Item) -> str:
    return f"`#{item.id}`"


def quote(text: str) -> str:
    """WhatsApp block quote: renders as an indented grey block."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().splitlines())


def header(item: Item) -> str:
    label = importance_label(item.importance)
    return (
        f"{CATEGORY_EMOJI.get(item.category, '📩')} *{CATEGORY_NAME.get(item.category, 'Mail')}*  ·  "
        f"{IMPORTANCE_EMOJI[label]} {label.title()}  ·  {tag(item)}"
    )


def item_card(item: Item, sender: str = "") -> str:
    if item.kind == "routine":
        return routine_card(item)
    lines = [header(item), "", f"*{item.title}*"]
    if sender:
        lines.append(f"_from {sender}_")
    if item.summary.strip():
        lines += ["", quote(item.summary)]
    facts = []
    if item.kind == "deadline" and item.due_at:
        facts.append(f"⏰ *Due {fmt(item.due_at)}*  _({humanize_delta(item.due_at)})_")
    if item.kind == "event":
        if item.event_start:
            facts.append(f"📅 *When:* {fmt(item.event_start)}  _({humanize_delta(item.event_start)})_")
        if item.venue:
            facts.append(f"📍 *Where:* {item.venue}")
        if item.reg_deadline:
            facts.append(f"📝 *Register by:* {fmt(item.reg_deadline)}  _({humanize_delta(item.reg_deadline)})_")
        if item.reg_link:
            facts.append(f"🔗 *Register:* {item.reg_link}")
    if item.link and item.link != item.reg_link:
        facts.append(f"🔗 {item.link}")
    if facts:
        lines += [""] + facts
    return "\n".join(lines)


def deadline_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"done:{item.id}", "✅ Done"), (f"snooze:{item.id}:3", "⏳ Snooze 3h"), (f"dismiss:{item.id}", "🗑️ Ignore")]


def event_interest_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"int:{item.id}", "👍 Interested"), (f"notint:{item.id}", "👎 Not interested"), (f"reg:{item.id}", "✅ Already registered")]


def registration_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"reg:{item.id}", "✅ Registered"), (f"snooze:{item.id}:24", "⏳ Tomorrow"), (f"notint:{item.id}", "👎 Not going")]


def routine_buttons(item: Item) -> list[tuple[str, str]]:
    one_off = not (item.schedule or {}).get("days")
    return [(f"done:{item.id}", "✅ Done" if one_off else "✅ Done for today"), (f"snooze:{item.id}:0.25", "⏳ 15 min"),
            (f"dismiss:{item.id}", "🛑 Stop")]


def deadline_reminder(item: Item) -> str:
    assert item.due_at
    left = item.due_at - utcnow()
    if left.total_seconds() <= 6 * 3600:
        top = f"🚨 *Due {humanize_delta(item.due_at)}!*  ·  {tag(item)}"
    else:
        top = f"⏰ *Reminder:* due {humanize_delta(item.due_at)}  ·  {tag(item)}"
    lines = [top, f"*{item.title}*", f"📅 {fmt(item.due_at)}"]
    if item.link:
        lines.append(f"🔗 {item.link}")
    lines += ["", f"_Submitted? Tap ✅ Done or reply_ `done {item.id}`"]
    return "\n".join(lines)


def deadline_passed(item: Item) -> str:
    return (f"⌛ *Deadline passed*  ·  {tag(item)}\n*{item.title}* was due {fmt(item.due_at)}.\n\n"
            f"_Hope you made it! Reply_ `done {item.id}` _to confirm._")


def registration_reminder(item: Item) -> str:
    lines = [f"📝 *Have you registered yet?*  ·  {tag(item)}", f"*{item.title}*"]
    if item.reg_deadline:
        lines.append(f"⏳ Registration closes *{humanize_delta(item.reg_deadline)}* ({fmt(item.reg_deadline)})")
    if item.event_start:
        lines.append(f"📅 Event: {fmt(item.event_start)}")
    if item.reg_link or item.link:
        lines.append(f"🔗 {item.reg_link or item.link}")
    return "\n".join(lines)


def registration_closed(item: Item) -> str:
    return (
        f"⌛ *Registration closed*  ·  {tag(item)}\n*{item.title}*\n\n"
        f"_If you did register, reply_ `registered {item.id}` _and I'll remind you before it starts._"
    )


def event_reminder(item: Item) -> str:
    lines = [f"🎉 *Starting {humanize_delta(item.event_start)}!*  ·  {tag(item)}", f"*{item.title}*",
             f"📅 {fmt(item.event_start)}"]
    if item.venue:
        lines.append(f"📍 {item.venue}")
    return "\n".join(lines)


def reask_interest(item: Item) -> str:
    return f"🤔 *Still deciding?*  ·  {tag(item)}\nAre you interested in *{item.title}*?"


def routine_card(item: Item, nxt=None) -> str:
    sch = item.schedule or {}
    one_off = not sch.get("days")
    every = int(sch.get("every") or 0)
    lines = [f"{'⏰' if one_off else '🔁'} *{'Reminder' if one_off else 'Routine'}*  ·  {tag(item)}", f"*{item.title}*", ""]
    if one_off:
        if nxt:
            lines.append(f"⏭️ *{fmt(to_utc_naive(nxt))}*  _({humanize_delta(to_utc_naive(nxt))})_")
        if every:
            lines.append(f"🔂 then every {routines.every_text(every)} until you tap ✅ Done")
    else:
        lines.append(f"🗓️ {routines.describe(sch)}")
        if every:
            lines.append("⏹️ Stops for the day when you tap ✅ Done")
        if nxt:
            lines.append(f"⏭️ Next: *{fmt(to_utc_naive(nxt))}*")
    return "\n".join(lines)


def routine_created(item: Item, nxt) -> str:
    stop = "delete it" if not (item.schedule or {}).get("days") else "delete the routine"
    done = "done" if not (item.schedule or {}).get("days") else "done for today"
    return routine_card(item, nxt) + f"\n\n`done {item.id}` {done}  ·  `stop {item.id}` {stop}"


def routine_reminder(item: Item, count: int | None, nxt) -> str:
    sch = item.schedule or {}
    lines = [f"{'⏰' if not sch.get('days') else '🔁'} *{item.title}*  ·  {tag(item)}"]
    every = int(sch.get("every") or 0)
    if every:
        today = local(utcnow()).date()
        more = nxt is not None and nxt.date() == today
        nth = f"Reminder {count} today" if count and count > 1 else ("Snoozed reminder" if count is None else "")
        after = f"next at {fmt_clock(nxt.hour, nxt.minute)}" if more else "last one today"
        lines.append("_" + " · ".join(x for x in (nth, after) if x) + "_")
    lines.append("")
    lines.append("_Tap ✅ when it's done._")
    return "\n".join(lines)


EVENT_STATE = {"asked": "❔ _interested?_", "interested": "📝 _register!_", "registered": "✅ _registered_"}


def item_line(item: Item, sent: set[str] | None = None) -> str:
    if item.kind == "deadline" and item.due_at:
        return f"• {tag(item)} *{item.title}*\n      ⏰ {fmt(item.due_at)}  ·  _{humanize_delta(item.due_at)}_"
    if item.kind == "event":
        state = EVENT_STATE.get(item.status, item.status)
        detail = []
        if item.event_start:
            detail.append(f"📅 {fmt(item.event_start)}")
        if item.status == "interested" and item.reg_deadline:
            detail.append(f"reg by {fmt(item.reg_deadline)}")
        return f"• {tag(item)} *{item.title}*  {state}" + (f"\n      {'  ·  '.join(detail)}" if detail else "")
    if item.kind == "routine" and item.schedule:
        now = utcnow()
        done_today = sent is not None and routines.done_key(local(now).date()) in sent
        nxt = routines.next_slot(item.schedule, now, item.created_at, sent or set())
        state = "✅ _done today_" if done_today else (f"⏭️ {fmt(to_utc_naive(nxt))}" if nxt else "")
        return (f"• {tag(item)} *{item.title}*\n      🔁 {routines.describe(item.schedule)}"
                + (f"  ·  {state}" if state else ""))
    return f"• {tag(item)} {item.title}"


HELP = """🤖 *DontMissThoseMails*

*📋 Overview*
`list` · everything pending
`digest` · today's summary

*⏰ Deadlines & events*
`done 12` · submitted _(also_ `done 12 14 15`_)_
`registered 12` · you registered for event 12
`interested 12` / `no 12` · answer an event invite
`snooze 12 3h` · or `snooze 12 till 8pm`
`move 12 to Fri 5pm` · the date changed
`details 12` · full summary + links
`add DBMS project due Fri 5pm` · your own deadline

*🔁 Reminders*
_remind me to put attendance on ISB every hour after 9am until I say done, every day_
_remind me at 5pm to call home_
`done 14` · done for today
`stop 14` · delete it

*✨ Handy*
↩️ *Swipe-reply* to any of my messages with `done`, `snooze 2h`, `no`… no number needed
`undo` · take back your last change
`pause` / `resume` · mute non-urgent messages

Or just talk normally: _"I submitted the OS assignment"_"""
