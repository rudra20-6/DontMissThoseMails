"""Language work done by the LLM (Gemini): long email -> short summary + structured dates/links."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.clients.gemini import GeminiClient, GeminiError
from app.config import get_settings
from app.textutil import truncate
from app.timeutil import local, parse_local

log = logging.getLogger(__name__)

SYSTEM = """You condense emails for a busy college student who reads them on WhatsApp.
Rules:
- summary: 2-4 short sentences, plain text, no greetings. Say WHAT it is, WHEN/WHERE, and WHAT the student must do.
- title: at most 8 words.
- All dates/times MUST be ISO 8601 with the timezone offset of {tz} (e.g. 2026-10-03T23:59:00+05:30).
- Resolve relative dates ("tomorrow", "this Friday", "EOD") relative to the email's received time.
- If a deadline has a date but no time, use 23:59. If an event has a date but no time, use 09:00.
- Use empty strings when something is not stated. Never invent dates or links.
- deadlines: only things the student must do by a time (submissions, payments, forms, applications). Max 3.
- registration_link / primary_link: copy exactly from the email if present."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "deadlines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"what": {"type": "STRING"}, "due": {"type": "STRING"}},
                "required": ["what", "due"],
            },
        },
        "event": {
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING"},
                "start": {"type": "STRING"},
                "end": {"type": "STRING"},
                "venue": {"type": "STRING"},
                "registration_deadline": {"type": "STRING"},
                "registration_link": {"type": "STRING"},
            },
        },
        "primary_link": {"type": "STRING"},
    },
    "required": ["title", "summary", "deadlines", "primary_link"],
}


@dataclass
class Deadline:
    what: str
    due: datetime | None  # naive UTC


@dataclass
class EventInfo:
    name: str = ""
    start: datetime | None = None
    end: datetime | None = None
    venue: str = ""
    registration_deadline: datetime | None = None
    registration_link: str = ""


@dataclass
class Extraction:
    title: str
    summary: str
    deadlines: list[Deadline] = field(default_factory=list)
    event: EventInfo | None = None
    primary_link: str = ""
    source: str = "gemini"


def extract_email(sender: str, subject: str, body: str, received_at: datetime, links: list[str],
                  hint_category: str) -> Extraction:
    tz = get_settings().timezone
    received_local = local(received_at).isoformat()
    g = GeminiClient()
    if g.enabled:
        prompt = (
            f"Received: {received_local}\nCategory hint: {hint_category}\nFrom: {sender}\nSubject: {subject}\n\n"
            f"Body:\n{truncate(body, 12000)}"
        )
        try:
            r = g.generate_json(SYSTEM.format(tz=tz), prompt, SCHEMA)
            ev = r.get("event") or {}
            event = None
            if any(ev.get(k) for k in ("name", "start", "registration_deadline")):
                event = EventInfo(
                    name=ev.get("name", "") or r.get("title", subject),
                    start=parse_local(ev.get("start")),
                    end=parse_local(ev.get("end")),
                    venue=ev.get("venue", ""),
                    registration_deadline=parse_local(ev.get("registration_deadline")),
                    registration_link=ev.get("registration_link", ""),
                )
            return Extraction(
                title=(r.get("title") or subject)[:200],
                summary=r.get("summary") or "",
                deadlines=[Deadline(d.get("what", ""), parse_local(d.get("due"))) for d in r.get("deadlines", [])][:3],
                event=event,
                primary_link=r.get("primary_link") or (links[0] if links else ""),
            )
        except GeminiError as exc:
            log.warning("Gemini extraction failed, using fallback: %s", exc)
    return _fallback(subject, body, links)


def _fallback(subject: str, body: str, links: list[str]) -> Extraction:
    first = re.sub(r"\s+", " ", body).strip()
    summary = first[:300] + ("..." if len(first) > 300 else "")
    return Extraction(title=subject[:200], summary=summary, primary_link=links[0] if links else "", source="fallback")


USER_TASK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "due": {"type": "STRING"},
        "is_event": {"type": "BOOLEAN"},
    },
    "required": ["title", "due", "is_event"],
}


def parse_user_task(text: str, now: datetime) -> tuple[str, datetime | None, bool]:
    """Turn 'add DSA assignment due friday 5pm' into (title, due, is_event)."""
    g = GeminiClient()
    if not g.enabled:
        return text[:200], None, False
    try:
        r = g.generate_json(
            f"Extract a personal task/deadline or event from a student's message. Current time: "
            f"{local(now).isoformat()}. Output 'due' as ISO 8601 with the {get_settings().timezone} offset; "
            f"if no time is given use 23:59 for tasks, 09:00 for events; empty string if no date. Title max 8 words.",
            text,
            USER_TASK_SCHEMA,
        )
        return (r.get("title") or text)[:200], parse_local(r.get("due")), bool(r.get("is_event"))
    except GeminiError as exc:
        log.warning("Gemini task parse failed: %s", exc)
        return text[:200], None, False
