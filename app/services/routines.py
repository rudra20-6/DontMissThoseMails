"""Schedules for reminders the user sets up in chat ("remind me every hour after 9am until I say done").

A schedule is a small dict stored on the Item (see models.Item.schedule). Everything here is pure: given a
schedule and a day it returns the reminder times, so the reminder engine can stay deterministic.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from app.config import get_settings
from app.timeutil import fmt_clock, local

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_EVERY = 5  # minutes
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_days(text: str) -> list[int]:
    """'daily' / 'weekdays' / 'mon,wed,fri' -> weekday numbers (Mon=0). '' -> [] (one-off)."""
    t = (text or "").strip().lower()
    if not t or t in ("once", "none", "no"):
        return []
    if t in ("daily", "every day", "everyday", "all"):
        return list(range(7))
    if t in ("weekdays", "weekday"):
        return list(range(5))
    if t in ("weekends", "weekend"):
        return [5, 6]
    days = {DAY_NAMES.index(w[:3].title()) for w in re.split(r"[\s,/&]+|and", t) if w[:3].title() in DAY_NAMES}
    return sorted(days)


def clean_hhmm(value: str) -> str:
    m = _HHMM.match((value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def default_end() -> str:
    s = get_settings()
    if s.quiet_hours_start != s.quiet_hours_end and s.quiet_hours_start > 0:
        return f"{s.quiet_hours_start - 1:02d}:59"
    return "23:59"


def is_one_off(sch: dict) -> bool:
    return not sch.get("days")


def runs_on(sch: dict, day: date) -> bool:
    if is_one_off(sch):
        return sch.get("date") == day.isoformat()
    return day.weekday() in sch["days"]


def day_slots(sch: dict, day: date) -> list[datetime]:
    """Aware local datetimes of every reminder on `day` (ignores whether the schedule runs that day)."""
    tz = get_settings().tz
    first = datetime.combine(day, time(*_hm(sch["start"])), tzinfo=tz)
    every = int(sch.get("every") or 0)
    if every <= 0:
        return [first]
    last = datetime.combine(day, time(*_hm(clean_hhmm(sch.get("end", "")) or default_end())), tzinfo=tz)
    out, t = [], first
    while t <= last and len(out) < 300:
        out.append(t)
        t += timedelta(minutes=max(MIN_EVERY, every))
    return out or [first]


def slot_key(slot: datetime) -> str:
    return f"rt{slot.strftime('%Y-%m-%dT%H:%M')}"


def done_key(day: date) -> str:
    return f"done{day.isoformat()}"


def next_slot(sch: dict, now: datetime, created: datetime | None, sent: set[str]) -> datetime | None:
    """Next reminder strictly after `now` (naive UTC in, aware local out), skipping days marked done."""
    now_l = local(now)
    created_l = local(created) if created else None
    for offset in range(0, 15):
        day = now_l.date() + timedelta(days=offset)
        if not runs_on(sch, day) or done_key(day) in sent:
            continue
        for slot in day_slots(sch, day):
            if slot > now_l and (created_l is None or slot > created_l):
                return slot
    return None


def describe(sch: dict) -> str:
    """'Every day · 9:00 AM, then every 1 h until 10:59 PM'."""
    start = fmt_clock(*_hm(sch["start"]))
    days = sch.get("days") or []
    if is_one_off(sch):
        when = "Once"
    elif len(days) == 7:
        when = "Every day"
    elif days == list(range(5)):
        when = "Weekdays"
    elif days == [5, 6]:
        when = "Weekends"
    else:
        when = ", ".join(DAY_NAMES[d] for d in days)
    every = int(sch.get("every") or 0)
    if every <= 0:
        return f"{when} · {start}"
    end = clean_hhmm(sch.get("end", "")) or default_end()
    until = f" until {fmt_clock(*_hm(end))}" if end != "23:59" else ""
    return f"{when} · from {start}, every {every_text(every)}{until}"


def every_text(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    if minutes > 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"
