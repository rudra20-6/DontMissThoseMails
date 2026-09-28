"""Structured facts pulled out of an email (produced by the LLM analysis in decisions.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.timeutil import parse_local


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


def extraction_from_llm(r: dict, subject: str, links: list[str]) -> Extraction:
    ev = r.get("event") or {}
    event = None
    if any(ev.get(k) for k in ("name", "start", "registration_deadline")):
        event = EventInfo(
            name=ev.get("name", "") or r.get("title", subject),
            start=parse_local(ev.get("start")),
            end=parse_local(ev.get("end")),
            venue=ev.get("venue", "") or "",
            registration_deadline=parse_local(ev.get("registration_deadline")),
            registration_link=ev.get("registration_link", "") or "",
        )
    return Extraction(
        title=(r.get("title") or subject)[:200],
        summary=r.get("summary") or "",
        deadlines=[Deadline(d.get("what", ""), parse_local(d.get("due"))) for d in (r.get("deadlines") or [])][:3],
        event=event,
        primary_link=r.get("primary_link") or (links[0] if links else ""),
    )


def fallback_extraction(subject: str, body: str, links: list[str]) -> Extraction:
    first = re.sub(r"\s+", " ", body).strip()
    summary = first[:300] + ("..." if len(first) > 300 else "")
    return Extraction(title=subject[:200], summary=summary, primary_link=links[0] if links else "", source="fallback")
