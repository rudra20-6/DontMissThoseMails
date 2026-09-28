"""Every *decision* in the app lives here and is made by Jev.

Order of precedence for each decision:
  1. Deterministic rules (sender allow/deny lists, exact commands) - free and instant.
  2. Jev typed decision (choice / score / noul).
  3. Fallback if Jev is down or not configured: Gemini JSON, then keyword heuristics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass

from app.clients.gemini import GeminiClient, GeminiError
from app.clients.jev import Choice, JevClient, JevError, Noul, Score
from app.config import get_settings
from app.textutil import truncate

log = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "coursework": "Course work from Moodle/LMS or a professor/TA: assignments, quizzes, labs, project submissions, grades, course material",
    "academic_admin": "Official academic notices: exams, timetable, registration of courses, deans/registrar/academic office/board announcements, holidays",
    "event": "Club activity, talk, workshop, seminar, fest, hackathon, competition, sports or cultural event the student can attend",
    "opportunity": "Internship, job, research position, scholarship, fellowship, exchange program or call for applications",
    "campus_admin": "Hostel, mess, fees, IT/network, library, medical, transport or other campus-services notices",
    "personal": "A message written personally to the student by an individual (not a mass mailing)",
    "newsletter_promo": "Newsletter, marketing, promotional offer, social media or automated notification with nothing to act on",
    "other": "Anything that fits none of the above",
}

IMPORTANCE_LEVELS = [
    "ignore - irrelevant to the student, safe to never see",
    "low - nice to know, can wait for a daily digest",
    "medium - relevant, should be seen today",
    "high - important: official deadline, exam, graded work, or time-sensitive opportunity",
    "critical - urgent action needed within ~24 hours or serious consequence if missed",
]
IMPORTANCE_LABELS = ["ignore", "low", "medium", "high", "critical"]


@dataclass
class Triage:
    category: str = "other"
    category_confidence: float = 0.0
    importance: float = 2.0  # 0..4
    has_deadline: float = 0.0  # probabilities 0..1
    is_event: float = 0.0
    needs_registration: float = 0.0
    is_noise: float = 0.0
    source: str = "heuristic"  # rule | jev | gemini | heuristic

    def to_dict(self) -> dict:
        return asdict(self)


def importance_label(value: float) -> str:
    return IMPORTANCE_LABELS[max(0, min(4, round(value)))]


# ---------------------------------------------------------------------------
# Email triage
# ---------------------------------------------------------------------------

def rule_filter(sender: str, subject: str) -> str | None:
    """Returns 'drop', 'priority' or None (no rule matched)."""
    s = get_settings()
    sender_l, subject_l = sender.lower(), subject.lower()
    if any(x in sender_l for x in s.ignore_sender_list) or any(x in subject_l for x in s.ignore_subject_list):
        return "drop"
    if any(x in sender_l for x in s.priority_sender_list):
        return "priority"
    return None


def _triage_questions() -> dict:
    return {
        "category": Choice(
            "Which category best describes this email received by a college student?",
            dict(CATEGORIES),
        ),
        "importance": Score(
            "How important is it that the student sees this email promptly?",
            IMPORTANCE_LEVELS,
        ),
        "has_deadline": Noul(
            "Does the email ask the student to submit, complete, pay, apply, or respond by a specific date or time?",
            yes="there is an explicit or clearly implied due date/time for an action by the student",
            no="no action with a due date is requested",
        ),
        "is_event": Noul(
            "Does the email announce a specific event (talk, workshop, club meet, fest, hackathon, competition) the student could attend or participate in?",
        ),
        "needs_registration": Noul(
            "Does taking part require the student to register, sign up, RSVP, or fill a form?",
        ),
        "is_noise": Noul(
            "Is this purely promotional, marketing, social-media or automated noise with nothing a student needs to know or do?",
        ),
    }


def _email_state(sender: str, subject: str, body: str, received_local: str) -> dict:
    return {
        "recipient": "an undergraduate college student",
        "from": sender,
        "subject": subject,
        "received": received_local,
        "body": truncate(body, 4000),
    }


def _apply_rules(t: Triage, rule: str | None) -> Triage:
    if rule == "priority":
        t.importance = max(t.importance, 3.0)
        t.is_noise = 0.0
    return t


def triage_email(sender: str, subject: str, body: str, received_local: str) -> Triage:
    rule = rule_filter(sender, subject)
    if rule == "drop":
        return Triage(importance=0.0, is_noise=1.0, source="rule")

    state = _email_state(sender, subject, body, received_local)
    jev = JevClient()
    if jev.enabled:
        try:
            a = jev.ask(state, _triage_questions())
            category, conf = a.choice("category")
            t = Triage(
                category=category,
                category_confidence=conf,
                importance=max(0.0, min(4.0, a.score("importance"))),
                has_deadline=a.noul("has_deadline"),
                is_event=a.noul("is_event"),
                needs_registration=a.noul("needs_registration"),
                is_noise=a.noul("is_noise"),
                source="jev",
            )
            return _apply_rules(t, rule)
        except JevError as exc:
            log.warning("Jev triage failed, falling back: %s", exc)

    t = _gemini_triage(state) or _heuristic_triage(sender, subject, body)
    return _apply_rules(t, rule)


def _gemini_triage(state: dict) -> Triage | None:
    g = GeminiClient()
    if not g.enabled:
        return None
    schema = {
        "type": "OBJECT",
        "properties": {
            "category": {"type": "STRING", "enum": list(CATEGORIES)},
            "importance": {"type": "INTEGER", "description": "0=ignore 1=low 2=medium 3=high 4=critical"},
            "has_deadline": {"type": "BOOLEAN"},
            "is_event": {"type": "BOOLEAN"},
            "needs_registration": {"type": "BOOLEAN"},
            "is_noise": {"type": "BOOLEAN"},
        },
        "required": ["category", "importance", "has_deadline", "is_event", "needs_registration", "is_noise"],
    }
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    try:
        r = g.generate_json(
            "You triage a college student's email. Categories:\n" + cats,
            f"Email:\n{state}",
            schema,
        )
    except GeminiError as exc:
        log.warning("Gemini triage failed: %s", exc)
        return None
    return Triage(
        category=r.get("category", "other") if r.get("category") in CATEGORIES else "other",
        category_confidence=0.6,
        importance=float(max(0, min(4, int(r.get("importance", 2))))),
        has_deadline=1.0 if r.get("has_deadline") else 0.0,
        is_event=1.0 if r.get("is_event") else 0.0,
        needs_registration=1.0 if r.get("needs_registration") else 0.0,
        is_noise=1.0 if r.get("is_noise") else 0.0,
        source="gemini",
    )


_KW = {
    "coursework": r"moodle|assignment|quiz|submission|submit|lab\b|grade|course",
    "academic_admin": r"exam|mid-?sem|end-?sem|timetable|registrar|dean|academic|holiday",
    "event": r"workshop|talk|seminar|hackathon|fest|competition|club|event|session|meetup",
    "opportunity": r"internship|job|hiring|scholarship|fellowship|position|apply",
    "campus_admin": r"hostel|mess|fee|library|wifi|network|medical",
    "newsletter_promo": r"unsubscribe|newsletter|offer|discount|sale|linkedin|promo",
}


def _heuristic_triage(sender: str, subject: str, body: str) -> Triage:
    text = f"{subject}\n{body[:2000]}".lower()
    category = next((c for c, pat in _KW.items() if re.search(pat, text)), "other")
    deadline = bool(re.search(r"deadline|due\b|last date|submit by|before \d|by \d", text))
    event = category == "event"
    importance = {"coursework": 3, "academic_admin": 3, "event": 2, "opportunity": 2, "campus_admin": 2,
                  "newsletter_promo": 0.4, "other": 1.5}.get(category, 1.5)
    return Triage(
        category=category,
        category_confidence=0.3,
        importance=float(importance),
        has_deadline=0.8 if deadline else 0.1,
        is_event=0.8 if event else 0.1,
        needs_registration=0.8 if re.search(r"regist|sign ?up|rsvp|form", text) else 0.1,
        is_noise=0.9 if category == "newsletter_promo" else 0.1,
        source="heuristic",
    )


# ---------------------------------------------------------------------------
# WhatsApp free-text intent
# ---------------------------------------------------------------------------

INTENTS: dict[str, str] = {
    "mark_done": "User says they finished/submitted/completed a task or deadline",
    "mark_registered": "User says they registered / signed up for an event",
    "interested": "User says they are interested in / want to attend an event",
    "not_interested": "User says they are not interested / want to ignore or drop an item",
    "snooze": "User asks to be reminded later / snooze an item",
    "details": "User asks for more details, the link, or the full summary of an item",
    "list": "User asks what is pending, upcoming, due, or wants an overview",
    "add": "User wants to add a new personal deadline, task or reminder",
    "help": "User asks how to use the bot",
    "other": "Greeting, thanks, or anything else",
}


@dataclass
class Intent:
    intent: str
    item_id: int | None
    confidence: float
    source: str


def classify_intent(message: str, candidates: list[tuple[int, str]]) -> Intent:
    """candidates: (item_id, short description) of the user's active items."""
    target_options = {f"item_{iid}": desc[:200] for iid, desc in candidates[:20]}
    target_options["none"] = "The message does not refer to any of these items"
    jev = JevClient()
    state = {"user_message": message, "active_items": {k: v for k, v in target_options.items() if k != "none"}}
    if jev.enabled:
        try:
            a = jev.ask(
                state,
                {
                    "intent": Choice("What does the user want the reminder bot to do?", dict(INTENTS)),
                    "target": Choice("Which of the active items is the user's message about?", target_options),
                },
            )
            intent, conf = a.choice("intent")
            target, _ = a.choice("target")
            item_id = int(target.split("_", 1)[1]) if target.startswith("item_") else None
            return Intent(intent, item_id, conf, "jev")
        except (JevError, ValueError) as exc:
            log.warning("Jev intent failed, falling back: %s", exc)

    g = GeminiClient()
    if g.enabled:
        try:
            r = g.generate_json(
                "Classify a WhatsApp message sent to a student's reminder bot.\nIntents:\n"
                + "\n".join(f"- {k}: {v}" for k, v in INTENTS.items()),
                f"Active items: {state['active_items']}\nMessage: {message}",
                {
                    "type": "OBJECT",
                    "properties": {
                        "intent": {"type": "STRING", "enum": list(INTENTS)},
                        "target": {"type": "STRING", "enum": list(target_options)},
                    },
                    "required": ["intent", "target"],
                },
            )
            target = r.get("target", "none")
            item_id = int(target.split("_", 1)[1]) if str(target).startswith("item_") else None
            return Intent(r.get("intent", "other"), item_id, 0.6, "gemini")
        except (GeminiError, ValueError) as exc:
            log.warning("Gemini intent failed: %s", exc)
    return Intent("other", None, 0.0, "none")
