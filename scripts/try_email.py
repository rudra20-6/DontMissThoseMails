"""Dry-run the brain on a sample email, without Outlook or WhatsApp.

    python -m scripts.try_email samples/moodle_assignment.txt

The file's first line is the subject, second line the sender, the rest is the body.
Prints Jev's decision, Gemini's extraction and the WhatsApp message that would be sent.
"""

import os
import sys

os.environ.setdefault("WHATSAPP_DRY_RUN", "true")
os.environ.setdefault("RUN_SCHEDULER", "false")

from app.config import get_settings  # noqa: E402
from app.models import Item  # noqa: E402
from app.services import messages  # noqa: E402
from app.services.decisions import triage_email  # noqa: E402
from app.services.extraction import extract_email  # noqa: E402
from app.services.pipeline import decide_action  # noqa: E402
from app.textutil import extract_urls  # noqa: E402
from app.timeutil import local, utcnow  # noqa: E402


def main(path: str) -> None:
    subject, sender, *rest = open(path, encoding="utf-8").read().split("\n")
    body = "\n".join(rest)
    s = get_settings()
    print(f"Jev configured: {bool(s.jev_api_key)} | Gemini configured: {bool(s.gemini_api_key)}\n")
    t = triage_email(sender, subject, body, local(utcnow()).isoformat())
    print("DECISION:", t.to_dict())
    action = decide_action(t)
    print("ACTION:", action)
    if action == "dropped":
        return
    ext = extract_email(sender, subject, body, utcnow(), extract_urls(body), t.category)
    print("EXTRACTION:", ext, "\n")
    kind = "event" if t.is_event >= 0.5 and ext.event else "deadline" if t.has_deadline >= 0.5 and ext.deadlines else "announcement"
    item = Item(id=0, kind=kind, category=t.category, importance=t.importance, title=ext.title, summary=ext.summary,
                link=ext.primary_link, due_at=ext.deadlines[0].due if ext.deadlines else None,
                event_start=ext.event.start if ext.event else None, venue=ext.event.venue if ext.event else "",
                reg_deadline=ext.event.registration_deadline if ext.event else None,
                reg_link=ext.event.registration_link if ext.event else "")
    print("---- WhatsApp preview ----")
    print(messages.item_card(item, sender))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/moodle_assignment.txt")
