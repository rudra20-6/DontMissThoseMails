"""All AI decisions, made by Gemini (rotating free-tier models/keys, see clients/gemini.py).

To stay far inside free-tier limits:
  * ONE Gemini call per email does triage (category, importance, deadline/event/noise flags) AND
    extraction (summary, dates, event details, links) together.
  * ONE call per free-text WhatsApp message classifies the intent, the item it refers to, and - for
    "add ..." - the new task's date.
  * Sender/subject rules, button taps and exact commands never call the LLM.
  * If every model/key is exhausted, `LLMUnavailable` propagates so the caller can queue the work;
    keyword heuristics are only used as a last resort.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from app.clients.gemini import LLMUnavailable, get_pool
from app.config import get_settings
from app.services.extraction import Extraction, extraction_from_llm, fallback_extraction
from app.textutil import truncate

log = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "coursework": "Course work from Moodle/LMS or a professor/TA: assignments, quizzes, labs, project submissions, grades, course material",
    "academic_admin": "Official academic notices: exams, timetable, course registration, dean/registrar/academic office/board announcements, holidays",
    "event": "Club activity, talk, workshop, seminar, fest, hackathon, competition, sports or cultural event the student can attend",
    "opportunity": "Internship, job, research position, scholarship, fellowship, exchange program or call for applications",
    "campus_admin": "Hostel, mess, fees, IT/network, library, medical, transport or other campus-services notices",
    "personal": "A message written personally to the student by an individual (not a mass mailing)",
    "newsletter_promo": "Newsletter, marketing, promotional offer, social media or automated notification with nothing to act on",
    "other": "Anything that fits none of the above",
}
IMPORTANCE_LABELS = ["ignore", "low", "medium", "high", "critical"]


@dataclass
class Triage:
    category: str = "other"
    category_confidence: float = 0.0
    importance: float = 2.0  # 0..4
    has_deadline: float = 0.0  # 0..1
    is_event: float = 0.0
    needs_registration: float = 0.0
    is_noise: float = 0.0
    source: str = "heuristic"  # rule | gemini | heuristic

    def to_dict(self) -> dict:
        return asdict(self)


def importance_label(value: float) -> str:
    return IMPORTANCE_LABELS[max(0, min(4, round(value)))]


def rule_filter(sender: str, subject: str) -> str | None:
    """Returns 'drop', 'priority' or None. Free: no LLM call."""
    s = get_settings()
    sender_l, subject_l = sender.lower(), subject.lower()
    if any(x in sender_l for x in s.ignore_sender_list) or any(x in subject_l for x in s.ignore_subject_list):
        return "drop"
    if any(x in sender_l for x in s.priority_sender_list):
        return "priority"
    return None


def _apply_rules(t: Triage, rule: str | None) -> Triage:
    if rule == "priority":
        t.importance = max(t.importance, 3.0)
        t.is_noise = 0.0
    return t


# ---------------------------------------------------------------------------
# Email analysis (triage + extraction in one call)
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """You process emails for a busy undergraduate college student and output JSON only.

1) Classify:
- category: one of
{categories}
- importance (integer): 0=ignore (irrelevant), 1=low (nice to know, can wait for a daily digest),
  2=medium (relevant, see today), 3=high (official deadline, exam, graded work, time-sensitive opportunity),
  4=critical (action needed within ~24h or serious consequence if missed)
- has_deadline: true if the student must submit/complete/pay/apply/respond by a specific date/time
- is_event: true if it announces a specific event the student could attend or take part in
- needs_registration: true if taking part needs registering / signing up / RSVP / a form
- is_noise: true if it is purely promotional, marketing, social-media or automated noise

2) Condense for WhatsApp:
- title: at most 8 words. summary: 2-4 short plain sentences: WHAT it is, WHEN/WHERE, WHAT the student must do.
- deadlines: things the student must do by a time (max 3), each {{what, due}}.
- event: name, start, end, venue, registration_deadline, registration_link (empty strings if not an event).
- primary_link: the most useful link (submission / registration / details), copied exactly.

Date rules: every date/time is ISO 8601 with the {tz} offset (e.g. 2026-10-03T23:59:00+05:30).
Resolve relative dates ("tomorrow", "this Friday", "EOD") from the email's received time.
Deadline with a date but no time -> 23:59. Event with a date but no time -> 09:00.
Use "" when something is not stated. Never invent dates or links."""

_STR = {"type": "STRING"}
ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": list(CATEGORIES)},
        "importance": {"type": "INTEGER"},
        "has_deadline": {"type": "BOOLEAN"},
        "is_event": {"type": "BOOLEAN"},
        "needs_registration": {"type": "BOOLEAN"},
        "is_noise": {"type": "BOOLEAN"},
        "title": _STR,
        "summary": _STR,
        "deadlines": {
            "type": "ARRAY",
            "items": {"type": "OBJECT", "properties": {"what": _STR, "due": _STR}, "required": ["what", "due"]},
        },
        "event": {
            "type": "OBJECT",
            "properties": {k: _STR for k in ("name", "start", "end", "venue", "registration_deadline", "registration_link")},
        },
        "primary_link": _STR,
    },
    "required": ["category", "importance", "has_deadline", "is_event", "needs_registration", "is_noise",
                 "title", "summary", "deadlines", "primary_link"],
}


def analyze_email(sender: str, subject: str, body: str, received_local: str, links: list[str],
                  allow_heuristic: bool = False) -> tuple[Triage, Extraction | None]:
    """Returns (triage, extraction). Extraction is None for rule-dropped mail.

    Raises LLMUnavailable when Gemini quota is exhausted, unless allow_heuristic=True.
    """
    rule = rule_filter(sender, subject)
    if rule == "drop":
        return Triage(importance=0.0, is_noise=1.0, source="rule"), None

    pool = get_pool()
    if pool.enabled:
        try:
            r = pool.generate_json(
                ANALYSIS_SYSTEM.format(
                    categories="\n".join(f"  {k}: {v}" for k, v in CATEGORIES.items()),
                    tz=get_settings().timezone,
                ),
                f"Received: {received_local}\nFrom: {sender}\nSubject: {subject}\n\nBody:\n{truncate(body, 8000)}",
                ANALYSIS_SCHEMA,
            )
            category = r.get("category") if r.get("category") in CATEGORIES else "other"
            try:
                importance = float(max(0, min(4, int(r.get("importance", 2)))))
            except (TypeError, ValueError):
                importance = 2.0
            t = Triage(
                category=category,
                category_confidence=0.8,
                importance=importance,
                has_deadline=1.0 if r.get("has_deadline") else 0.0,
                is_event=1.0 if r.get("is_event") else 0.0,
                needs_registration=1.0 if r.get("needs_registration") else 0.0,
                is_noise=1.0 if r.get("is_noise") else 0.0,
                source="gemini",
            )
            return _apply_rules(t, rule), extraction_from_llm(r, subject, links)
        except LLMUnavailable:
            if not allow_heuristic:
                raise
            log.warning("Gemini unavailable; using keyword rules for %r", subject)
    elif not allow_heuristic:
        raise LLMUnavailable("no Gemini keys configured")
    return _apply_rules(heuristic_triage(sender, subject, body), rule), fallback_extraction(subject, body, links)


_KW = {
    "coursework": r"moodle|assignment|quiz|submission|submit|lab\b|grade|course",
    "academic_admin": r"exam|mid-?sem|end-?sem|timetable|registrar|dean|academic|holiday",
    "event": r"workshop|talk|seminar|hackathon|fest|competition|club|event|session|meetup",
    "opportunity": r"internship|job|hiring|scholarship|fellowship|position|apply",
    "campus_admin": r"hostel|mess|fee|library|wifi|network|medical",
    "newsletter_promo": r"unsubscribe|newsletter|offer|discount|sale|linkedin|promo",
}


def heuristic_triage(sender: str, subject: str, body: str) -> Triage:
    text = f"{subject}\n{body[:2000]}".lower()
    category = next((c for c, pat in _KW.items() if re.search(pat, text)), "other")
    deadline = bool(re.search(r"deadline|due\b|last date|submit by|before \d|by \d", text))
    importance = {"coursework": 3, "academic_admin": 3, "event": 2, "opportunity": 2, "campus_admin": 2,
                  "newsletter_promo": 0.4, "other": 1.5}.get(category, 1.5)
    return Triage(
        category=category,
        category_confidence=0.3,
        importance=float(importance),
        has_deadline=0.8 if deadline else 0.1,
        is_event=0.8 if category == "event" else 0.1,
        needs_registration=0.8 if re.search(r"regist|sign ?up|rsvp|form", text) else 0.1,
        is_noise=0.9 if category == "newsletter_promo" else 0.1,
        source="heuristic",
    )


# ---------------------------------------------------------------------------
# WhatsApp free text (intent + target item + optional new task, one call)
# ---------------------------------------------------------------------------

INTENTS: dict[str, str] = {
    "mark_done": "finished/submitted/completed a task or deadline",
    "mark_registered": "registered / signed up for an event",
    "interested": "interested in / wants to attend an event",
    "not_interested": "not interested / wants to ignore or drop an item",
    "snooze": "wants to be reminded later",
    "details": "wants details, the link, or the full summary of an item",
    "list": "asks what is pending / upcoming / due",
    "add": "wants to add a new personal deadline, task, reminder or event",
    "help": "asks how to use the bot",
    "other": "greeting, thanks, or anything else",
}

INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING", "enum": list(INTENTS)},
        "item_id": {"type": "INTEGER"},
        "snooze_hours": {"type": "NUMBER"},
        "task_title": _STR,
        "task_due": _STR,
        "task_is_event": {"type": "BOOLEAN"},
    },
    "required": ["intent", "item_id"],
}


@dataclass
class Intent:
    intent: str
    item_id: int | None
    confidence: float
    source: str
    snooze_hours: float | None = None
    task_title: str = ""
    task_due: str = ""
    task_is_event: bool = False


def classify_message(message: str, candidates: list[tuple[int, str]], now_local: str) -> Intent:
    """candidates: (item_id, description) of the user's active items. Raises LLMUnavailable."""
    items = "\n".join(f"  {iid}: {desc[:150]}" for iid, desc in candidates[:25]) or "  (none)"
    system = (
        "You interpret WhatsApp messages sent to a student's deadline/event reminder bot. Output JSON only.\n"
        "intent is one of:\n" + "\n".join(f"  {k}: {v}" for k, v in INTENTS.items()) + "\n"
        "item_id: the id of the active item the message refers to, or 0 if none/unclear.\n"
        "snooze_hours: for snooze, how many hours (default 3).\n"
        f"For intent=add: task_title (<=8 words), task_due as ISO 8601 with the {get_settings().timezone} offset "
        "(tasks without a time -> 23:59, events -> 09:00; empty if no date), task_is_event."
    )
    r = get_pool().generate_json(
        system, f"Current time: {now_local}\nActive items:\n{items}\n\nMessage: {message}", INTENT_SCHEMA, max_tokens=512
    )
    valid_ids = {iid for iid, _ in candidates}
    try:
        item_id = int(r.get("item_id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    try:
        hours = float(r["snooze_hours"]) if r.get("snooze_hours") else None
    except (TypeError, ValueError):
        hours = None
    return Intent(
        intent=r.get("intent") if r.get("intent") in INTENTS else "other",
        item_id=item_id if item_id in valid_ids else None,
        confidence=0.8,
        source="gemini",
        snooze_hours=hours,
        task_title=(r.get("task_title") or "")[:200],
        task_due=r.get("task_due") or "",
        task_is_event=bool(r.get("task_is_event")),
    )
