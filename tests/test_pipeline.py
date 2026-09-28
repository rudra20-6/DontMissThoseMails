from datetime import timedelta

from fastapi.testclient import TestClient

from app.clients.gemini import LLMUnavailable
from app.db import session_scope
from app.mail.base import RawEmail
from app.models import Email, Item
from app.services import decisions, pipeline
from app.services.decisions import Triage
from app.services.extraction import Deadline, EventInfo, Extraction
from app.timeutil import utcnow


def _mail(subject="Assignment 3 due", body="Submit on Moodle by Friday 11:59pm"):
    return RawEmail(message_id=f"<{subject}>", sender="Moodle <noreply@moodle.college.edu>", subject=subject,
                    received_at=utcnow(), body=body)


def _analysis(monkeypatch, triage, ext):
    monkeypatch.setattr(pipeline, "analyze_email", lambda *a, **k: (triage, ext))


def test_deadline_email_creates_item_and_notifies(monkeypatch, sent):
    due = utcnow() + timedelta(days=3)
    _analysis(monkeypatch, Triage(category="coursework", importance=3, has_deadline=1.0, source="gemini"),
              Extraction("DSA Assignment 3", "Submit A3 on Moodle.", [Deadline("submit", due)]))
    assert pipeline.ingest(_mail())
    assert not pipeline.ingest(_mail())  # dedupe
    assert pipeline.process_pending() == 1
    with session_scope() as s:
        item = s.query(Item).one()
        assert item.kind == "deadline" and item.status == "pending"
        assert s.query(Email).one().body == ""  # body not kept after analysis
    assert len(sent) == 1 and "DSA Assignment 3" in sent[0]["text"] and sent[0]["buttons"]


def test_quota_exhausted_keeps_email_queued_then_falls_back(monkeypatch, sent):
    def unavailable(*a, allow_heuristic=False, **k):
        if not allow_heuristic:
            raise LLMUnavailable("all exhausted")
        return Triage(category="coursework", importance=3, source="heuristic"), Extraction("Assignment 3 due", "Submit")

    monkeypatch.setattr(pipeline, "analyze_email", unavailable)
    pipeline.ingest(_mail())
    assert pipeline.process_pending() == 0
    with session_scope() as s:
        assert s.query(Email).one().action == "pending"
        s.query(Email).one().created_at = utcnow() - timedelta(hours=2)  # waited past LLM_RETRY_MAX_MINUTES
    assert pipeline.process_pending() == 1
    with session_scope() as s:
        assert s.query(Email).one().action == "notify"
    assert len(sent) == 1


def test_event_email_asks_interest_then_button_marks_interested(monkeypatch, sent):
    start = utcnow() + timedelta(days=5)
    _analysis(monkeypatch, Triage(category="event", importance=2.0, is_event=1.0, needs_registration=1.0),
              Extraction("Robotics Club Hackathon", "24h hackathon.", [],
                         EventInfo("Robotics Hackathon", start, None, "H105", start - timedelta(days=2), "https://forms.gle/x")))
    pipeline.ingest(_mail("Hackathon!", "Register now"))
    pipeline.process_pending()
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
    _analysis(monkeypatch, Triage(category="newsletter_promo", importance=0, is_noise=1.0), Extraction("Sale", "50% off"))
    pipeline.ingest(_mail("50% off!", "sale"))
    pipeline.process_pending()
    with session_scope() as s:
        assert s.query(Item).count() == 0
        assert s.query(Email).one().action == "dropped"
    assert sent == []


class _FakePool:
    enabled = True

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate_json(self, system, prompt, schema, max_tokens=2048):
        self.calls += 1
        return self.reply


def test_analyze_email_single_call_triage_and_extraction(monkeypatch):
    pool = _FakePool({
        "category": "coursework", "importance": 3, "has_deadline": True, "is_event": False,
        "needs_registration": False, "is_noise": False, "title": "Quiz 2", "summary": "Quiz 2 closes tomorrow.",
        "deadlines": [{"what": "Quiz 2", "due": "2030-10-03T17:00:00+05:30"}], "event": {}, "primary_link": "https://m/q2",
    })
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    t, ext = decisions.analyze_email("prof@college.edu", "Quiz tomorrow", "Quiz 2 on Moodle closes tomorrow 5pm", "now", [])
    assert pool.calls == 1
    assert t.source == "gemini" and t.category == "coursework" and t.has_deadline == 1.0
    assert ext.title == "Quiz 2" and ext.deadlines[0].due is not None and ext.event is None


def test_rule_dropped_mail_costs_no_ai(monkeypatch):
    monkeypatch.setattr(decisions, "rule_filter", lambda *a: "drop")
    monkeypatch.setattr(decisions, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("called AI")))
    t, ext = decisions.analyze_email("x@linkedin.com", "jobs", "", "now", [])
    assert t.source == "rule" and ext is None


def test_free_text_and_add_use_one_ai_call(monkeypatch, sent):
    from app.services import decisions as d
    from app.services.commands import handle_message

    with session_scope() as s:
        item = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=1))
        s.add(item)
        s.flush()
        iid = item.id

    pool = _FakePool({"intent": "mark_done", "item_id": iid})
    monkeypatch.setattr(d, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="finally submitted the operating systems thing")
    with session_scope() as s:
        assert s.get(Item, iid).status == "done"

    pool.reply = {"intent": "add", "item_id": 0, "task_title": "DBMS project", "task_due": "2030-10-04T17:00:00+05:30",
                  "task_is_event": False}
    with session_scope() as s:
        handle_message(s, text="add DBMS project due friday 5pm")
    with session_scope() as s:
        assert s.query(Item).filter_by(title="DBMS project", kind="deadline").count() == 1
    assert pool.calls == 2

    with session_scope() as s:
        handle_message(s, text="done 1")  # exact command: no AI
    assert pool.calls == 2


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


def test_webhook_from_wrong_number_is_diagnosed(sent):
    from app import diag
    from app.main import app

    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "918888888888", "id": "wamid.x", "type": "text", "text": {"body": "hi"}}]}}]}]}
    TestClient(app).post("/webhook/whatsapp", json=payload)
    last = diag.read("last_webhook_message")
    assert last["accepted"] is False and "918888888888" in last["reason"]
    assert sent == []

    payload["entry"][0]["changes"][0]["value"]["messages"][0].update({"from": "919999999999", "id": "wamid.y"})
    TestClient(app).post("/webhook/whatsapp", json=payload)
    assert "help" in sent[-1]["text"].lower() or "hey" in sent[-1]["text"].lower()
