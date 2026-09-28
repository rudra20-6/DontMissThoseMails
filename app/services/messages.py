"""Deterministic WhatsApp message templates (WhatsApp formatting: *bold*, _italic_)."""

from __future__ import annotations

from app.models import Item
from app.services.decisions import importance_label
from app.timeutil import fmt, humanize_delta, utcnow

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


def header(item: Item) -> str:
    label = importance_label(item.importance)
    return (
        f"{CATEGORY_EMOJI.get(item.category, '📩')} *{CATEGORY_NAME.get(item.category, 'Mail')}* "
        f"{IMPORTANCE_EMOJI[label]} _{label}_  ·  #{item.id}"
    )


def item_card(item: Item, sender: str = "") -> str:
    lines = [header(item), f"*{item.title}*"]
    if sender:
        lines.append(f"_from {sender}_")
    lines += ["", item.summary.strip()]
    if item.kind == "deadline" and item.due_at:
        lines += ["", f"⏰ *Due:* {fmt(item.due_at)} ({humanize_delta(item.due_at)})"]
    if item.kind == "event":
        lines.append("")
        if item.event_start:
            lines.append(f"📅 *When:* {fmt(item.event_start)} ({humanize_delta(item.event_start)})")
        if item.venue:
            lines.append(f"📍 *Where:* {item.venue}")
        if item.reg_deadline:
            lines.append(f"📝 *Register by:* {fmt(item.reg_deadline)} ({humanize_delta(item.reg_deadline)})")
        if item.reg_link:
            lines.append(f"🔗 Register: {item.reg_link}")
    if item.link and item.link != item.reg_link:
        lines.append(f"🔗 {item.link}")
    return "\n".join(lines)


def deadline_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"done:{item.id}", "✅ Done"), (f"snooze:{item.id}:3", "⏳ Snooze 3h"), (f"dismiss:{item.id}", "🗑️ Ignore")]


def event_interest_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"int:{item.id}", "👍 Interested"), (f"notint:{item.id}", "👎 Not interested"), (f"reg:{item.id}", "✅ Already registered")]


def registration_buttons(item: Item) -> list[tuple[str, str]]:
    return [(f"reg:{item.id}", "✅ Registered"), (f"snooze:{item.id}:24", "⏳ Tomorrow"), (f"notint:{item.id}", "👎 Not going")]


def deadline_reminder(item: Item) -> str:
    assert item.due_at
    urgency = "🚨" if (item.due_at - utcnow()).total_seconds() <= 6 * 3600 else "⏰"
    return (
        f"{urgency} *Reminder* · #{item.id}\n*{item.title}* is due *{humanize_delta(item.due_at)}* "
        f"({fmt(item.due_at)}).\n\nReply *done {item.id}* once submitted."
        + (f"\n🔗 {item.link}" if item.link else "")
    )


def deadline_passed(item: Item) -> str:
    return f"⌛ The deadline for *{item.title}* (#{item.id}) has passed ({fmt(item.due_at)}). Hope you made it! Reply *done {item.id}* to confirm."


def registration_reminder(item: Item) -> str:
    when = f"Registration closes *{humanize_delta(item.reg_deadline)}* ({fmt(item.reg_deadline)})." if item.reg_deadline else ""
    start = f"Event: {fmt(item.event_start)}." if item.event_start else ""
    link = f"\n🔗 {item.reg_link or item.link}" if (item.reg_link or item.link) else ""
    return f"📝 *Have you registered yet?* · #{item.id}\n*{item.title}*\n{when} {start}".strip() + link


def registration_closed(item: Item) -> str:
    return (
        f"⌛ Registration for *{item.title}* (#{item.id}) has closed. If you did register, reply *registered {item.id}* "
        "and I'll remind you before it starts."
    )


def event_reminder(item: Item) -> str:
    venue = f" at {item.venue}" if item.venue else ""
    return f"🎉 *Starting {humanize_delta(item.event_start)}* · #{item.id}\n*{item.title}*{venue} — {fmt(item.event_start)}"


def reask_interest(item: Item) -> str:
    return f"🤔 Still deciding? Are you interested in *{item.title}* (#{item.id})?"


def item_line(item: Item) -> str:
    if item.kind == "deadline" and item.due_at:
        return f"• #{item.id} {item.title} — due {fmt(item.due_at)} ({humanize_delta(item.due_at)})"
    if item.kind == "event":
        state = {"asked": "❔ interested?", "interested": "📝 register!", "registered": "✅ registered"}.get(item.status, item.status)
        when = f" — {fmt(item.event_start)}" if item.event_start else ""
        reg = f", reg by {fmt(item.reg_deadline)}" if item.status == "interested" and item.reg_deadline else ""
        return f"• #{item.id} {item.title}{when}{reg} [{state}]"
    return f"• #{item.id} {item.title}"


HELP = """🤖 *DontMissThoseMails — commands*
• *list* — everything pending
• *done 12* — mark deadline #12 as submitted
• *registered 12* — you registered for event #12
• *interested 12* / *no 12* — answer an event invite
• *snooze 12 3h* (or *2d*) — remind me later
• *details 12* — full summary + links
• *add <anything>* — add your own deadline, e.g. _add DBMS project due Friday 5pm_
• *digest* — today's digest now
• *pause* / *resume* — mute / unmute non-urgent messages
You can also just talk normally: _"I submitted the OS assignment"_ works too."""
