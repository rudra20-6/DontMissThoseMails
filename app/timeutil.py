"""Time helpers. Everything is stored in the database as *naive UTC*."""

from datetime import datetime, timezone

from dateutil import parser as dateparser

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Interpret naive datetimes as local time of the user.
        dt = dt.replace(tzinfo=get_settings().tz)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_local(value: str | None) -> datetime | None:
    """Parse an ISO-ish string (as produced by the LLM) into naive UTC. Returns None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return to_utc_naive(dateparser.isoparse(value))
    except (ValueError, OverflowError):
        try:
            return to_utc_naive(dateparser.parse(value))
        except (ValueError, OverflowError, TypeError):
            return None


def local(dt: datetime) -> datetime:
    """naive UTC -> aware local datetime."""
    return dt.replace(tzinfo=timezone.utc).astimezone(get_settings().tz)


def fmt(dt: datetime | None, now: datetime | None = None) -> str:
    """Human friendly local time: 'Today, 5:00 PM', 'Tomorrow, 9:00 AM', 'Fri 3 Oct, 11:59 PM'."""
    if dt is None:
        return "-"
    d = local(dt)
    today = local(now or utcnow()).date()
    days = (d.date() - today).days
    day = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(days) or d.strftime("%a %d %b").replace(" 0", " ")
    return f"{day}, {fmt_clock(d.hour, d.minute)}"


def fmt_clock(hour: int, minute: int) -> str:
    """9, 0 -> '9:00 AM'."""
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def humanize_delta(target: datetime, now: datetime | None = None) -> str:
    now = now or utcnow()
    seconds = (target - now).total_seconds()
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 3600:
        text = f"{max(1, int(seconds // 60))} min"
    elif seconds < 86400:
        hours, minutes = int(seconds // 3600), int(seconds % 3600 // 60)
        text = f"{hours} h" + (f" {minutes} min" if minutes and hours < 6 else "")
    else:
        days = round(seconds / 86400)
        text = f"{days} days" if days >= 2 else "1 day"
    return f"{text} ago" if past else f"in {text}"
