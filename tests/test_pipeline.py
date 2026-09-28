from datetime import timedelta

from fastapi.testclient import TestClient

from app.clients.jev import Answers
from app.db import session_scope
from app.mail.base import RawEmail
from app.models import Email, Item
from app.services import pipeline
from app.services.decisions import Triage
from app.services.extraction import Deadline, EventInfo, Extraction
from app.timeutil import utcnow


def _mail(subject="Assignment 3 due", body="Submit on Moodle by Friday 11:59pm"):
    return RawEmail(message_id=f"<{subject}>", sender="Moodle <noreply@moodle.college.edu>", subject=subject,
                    received_at=utcnow(), body=body)


def test_deadline_email_creates_item_and_notifies(monkeypatch, sent):
    due = utcnow() + timedelta(days=3)
    monkeypatch.setattr(pipeline, "triage_email", lambda *a: Triage(category="coursework", importance=3.2, has_deadline=0.95, source="jev"))
    monkeypatch.setattr(pipeline, "extract_email", lambda *a: Extraction("DSA Assignment 3", "Submit A3 on Moodle.", [Deadline("submit", due)]))
    assert pipeline.process_email(_mail())
    assert not pipeline.process_email(_mail())  # dedupe
    with session_scope() as s:
        item = s.query(Item).one()
        assert item.kind == "deadline" and item.status == "pending"
    assert len(sent) == 1 and "DSA Assignment 3" in sent[0]["text"] and sent[0]["buttons"]


def test_event_email_asks_interest_then_button_marks_interested(monkeypatch, sent):
    start = utcnow() + timedelta(days=5)
    monkeypatch.setattr(pipeline, "triage_email", lambda *a: Triage(category="event", importance=2.0, is_event=0.9, needs_registration=0.9))
    monkeypatch.setattr(pipeline, "extract_email", lambda *a: Extraction(
        "Robotics Club Hackathon", "24h hackathon.", [], EventInfo("Robotics Hackathon", start, None, "H105",
                                                                   start - timedelta(days=2), "https://forms.gle/x")))
    pipeline.process_email(_mail("Hackathon!", "Register now"))
    assert "interested" in sent[-1]["text"].lower()
    with session_scope() as s:
        item_id = s.query(Item).one().id

    from app.main import app

    client = TestClient(app)
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999", "id": "wamid.1", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": f"int:{item_id}", "title": "Interested"}}}]}}]}]}
    assert client.post("/webhook/whatsapp", json=payload).status_code == 200
    with session_scope() as s:
        assert s.get(Item, item_id).status == "interested"
    assert "keep reminding" in sent[-1]["text"]


def test_noise_is_dropped(monkeypatch, sent):
    monkeypatch.setattr(pipeline, "triage_email", lambda *a: Triage(category="newsletter_promo", importance=0.2, is_noise=0.95))
    pipeline.process_email(_mail("50% off!", "sale"))
    with session_scope() as s:
        assert s.query(Item).count() == 0
        assert s.query(Email).one().action == "dropped"
    assert sent == []


def test_triage_uses_jev(monkeypatch):
    from app.clients import jev as jev_mod
    from app.services import decisions

    def fake_ask(self, state, questions):
        assert set(questions) == {"category", "importance", "has_deadline", "is_event", "needs_registration", "is_noise"}
        return Answers(raw={
            "category": {"choice": "coursework", "confidence": 0.9},
            "importance": {"score": 3.4},
            "has_deadline": {"noul": 0.97}, "is_event": {"noul": 0.02},
            "needs_registration": {"noul": 0.01}, "is_noise": {"noul": 0.01},
        }, questions=questions)

    monkeypatch.setattr(jev_mod.JevClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(jev_mod.JevClient, "ask", fake_ask)
    t = decisions.triage_email("prof@college.edu", "Quiz tomorrow", "Quiz 2 on Moodle closes tomorrow 5pm", "now")
    assert t.source == "jev" and t.category == "coursework" and t.has_deadline > 0.9


def test_free_text_uses_jev_intent(monkeypatch, sent):
    from app.clients import jev as jev_mod
    from app.services.commands import handle_message

    with session_scope() as s:
        item = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=1))
        s.add(item)
        s.flush()
        iid = item.id

    def fake_ask(self, state, questions):
        return Answers(raw={"intent": {"choice": "mark_done"}, "target": {"choice": f"item_{iid}"}}, questions=questions)

    monkeypatch.setattr(jev_mod.JevClient, "enabled", property(lambda self: True))
    monkeypatch.setattr(jev_mod.JevClient, "ask", fake_ask)
    with session_scope() as s:
        handle_message(s, text="finally submitted the operating systems thing")
    with session_scope() as s:
        assert s.get(Item, iid).status == "done"


def test_pause_holds_messages_until_resume(sent):
    from app.services import notifier
    from app.services.commands import handle_message

    with session_scope() as s:
        handle_message(s, text="pause")
        notifier.notify(s, "non-urgent update")
    with session_scope() as s:
        handle_message(s, text="list")  # an inbound message must not flush while paused
    assert not any(m["text"] == "non-urgent update" for m in sent)
    with session_scope() as s:
        handle_message(s, text="resume")
    assert any(m["text"] == "non-urgent update" for m in sent)
