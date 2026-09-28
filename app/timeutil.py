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


def fmt(dt: datetime | None) -> str:
    """Human friendly local time, e.g. 'Tue 30 Sep, 11:59 PM'."""
    if dt is None:
        return "-"
    return local(dt).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def humanize_delta(target: datetime, now: datetime | None = None) -> str:
    now = now or utcnow()
    seconds = (target - now).total_seconds()
    past = seconds < 0
    seconds = abs(seconds)
    if seconds < 3600:
        text = f"{max(1, int(seconds // 60))} min"
    elif seconds < 86400:
        hours = seconds / 3600
        text = f"{hours:.0f} h" if hours >= 2 else f"{hours:.1f} h"
    else:
        days = seconds / 86400
        text = f"{days:.0f} days" if days >= 2 else "1 day"
    return f"{text} ago" if past else f"in {text}"
