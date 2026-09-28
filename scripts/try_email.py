"""Dry-run the brain on a sample email, without Outlook or WhatsApp.

    python -m scripts.try_email samples/moodle_assignment.txt

The file's first line is the subject, second line the sender, the rest is the body.
Prints the models each Gemini key will rotate through, the AI decision, and the WhatsApp preview.
"""

import os
import sys

os.environ.setdefault("WHATSAPP_DRY_RUN", "true")
os.environ.setdefault("RUN_SCHEDULER", "false")

from app.clients.gemini import get_pool  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import Item  # noqa: E402
from app.services import messages  # noqa: E402
from app.services.decisions import analyze_email  # noqa: E402
from app.services.pipeline import decide_action  # noqa: E402
from app.textutil import extract_urls  # noqa: E402
from app.timeutil import local, utcnow  # noqa: E402


def main(path: str) -> None:
    init_db()
    subject, sender, *rest = open(path, encoding="utf-8").read().split("\n")
    body = "\n".join(rest)
    pool = get_pool()
    print(f"Gemini keys: {len(pool.keys)}")
    for i, key in enumerate(pool.keys, 1):
        print(f"  key {i} models: {pool.models_for(key)}")
    t, ext = analyze_email(sender, subject, body, local(utcnow()).isoformat(), extract_urls(body), allow_heuristic=True)
    print("\nDECISION:", t.to_dict())
    action = decide_action(t)
    print("ACTION:", action)
    if action == "dropped" or ext is None:
        return
    print("EXTRACTION:", ext, "\n")
    kind = "event" if t.is_event >= 0.5 and ext.event else "deadline" if t.has_deadline >= 0.5 and ext.deadlines else "announcement"
    ev = ext.event
    item = Item(id=0, kind=kind, category=t.category, importance=t.importance, title=ext.title, summary=ext.summary,
                link=ext.primary_link, due_at=ext.deadlines[0].due if ext.deadlines else None,
                event_start=ev.start if ev else None, venue=ev.venue if ev else "",
                reg_deadline=ev.registration_deadline if ev else None, reg_link=ev.registration_link if ev else "")
    print("---- WhatsApp preview ----")
    print(messages.item_card(item, sender))
    print("\nPool status:", pool.status())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/moodle_assignment.txt")
