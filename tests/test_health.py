from datetime import timedelta

from app import kv
from app.clients.gemini import LLMUnavailable
from app.db import session_scope
from app.mail.base import MailError, RawEmail
from app.models import Email
from app.services import health, pipeline
from app.services.decisions import Triage
from app.services.extraction import Extraction
from app.timeutil import utcnow


def _age(field: str, **delta):
    with session_scope() as s:
        st = kv.get(s, health.KEY)
        st[field] = (utcnow() - timedelta(**delta)).isoformat()
        kv.put(s, health.KEY, st)


def _check():
    with session_scope() as s:
        return health.check(s)


class _Source:
    def __init__(self):
        self.error = None
        self.mails = []

    def fetch_new(self):
        if self.error:
            raise self.error
        out, self.mails = self.mails, []
        return out


def test_mail_down_alerts_once_then_recovers(monkeypatch, sent):
    src = _Source()
    monkeypatch.setattr(pipeline, "get_mail_source", lambda: src)
    src.error = ConnectionResetError("reset by peer")  # not a MailError: must not crash the tick
    pipeline.poll_mail()
    assert _check() == 0  # a blip: no alert yet
    _age("mail_fail_since", minutes=31)
    assert _check() == 1
    text = sent[-1]["text"]
    assert "can't read your mail" in text and "Nothing is lost" in text and "may miss" in text
    pipeline.poll_mail()
    assert _check() == 0  # not repeated every tick
    src.error = None
    pipeline.poll_mail()
    assert _check() == 1 and "Mail is back" in sent[-1]["text"]


def test_wrong_password_alerts_sooner_with_fix(monkeypatch, sent):
    src = _Source()
    monkeypatch.setattr(pipeline, "get_mail_source", lambda: src)
    src.error = MailError("IMAP error: b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
    pipeline.poll_mail()
    _age("mail_fail_since", minutes=11)
    assert _check() == 1
    assert "app password" in sent[-1]["text"] and "IMAP_PASSWORD" in sent[-1]["text"]


def test_no_mail_for_two_days_suggests_forwarding_broke(monkeypatch, sent):
    src = _Source()
    monkeypatch.setattr(pipeline, "get_mail_source", lambda: src)
    pipeline.poll_mail()
    _age("first_seen", hours=49)
    assert _check() == 1 and "No new mail" in sent[-1]["text"] and "forwarding" in sent[-1]["text"]
    assert _check() == 0
    src.mails = [RawEmail("<m1>", "Moodle <m@x>", "Hi", utcnow(), "body")]
    monkeypatch.setattr(pipeline, "analyze_email",
                        lambda *a, **k: (Triage(importance=0, is_noise=1, source="gemini"), None))
    pipeline.poll_mail()
    assert _check() == 1 and "arriving again" in sent[-1]["text"]


def test_mail_read_without_ai_is_listed_as_maybe_missed(monkeypatch, sent):
    def unavailable(*a, allow_heuristic=False, **k):
        if not allow_heuristic:
            raise LLMUnavailable("quota")
        return Triage(category="coursework", importance=3, has_deadline=1.0, source="heuristic"), \
            Extraction("OS quiz", "Quiz on Friday")

    monkeypatch.setattr(pipeline, "analyze_email", unavailable)
    pipeline.ingest(RawEmail("<q1>", "Moodle <m@x>", "OS quiz on Friday", utcnow(), "body"))
    pipeline.process_pending()
    assert _check() == 0  # still waiting for quota: delayed, nothing to warn about yet
    with session_scope() as s:
        s.query(Email).update({Email.created_at: utcnow() - timedelta(minutes=50)})
    pipeline.process_pending()
    before = len(sent)
    assert _check() == 1
    alert = sent[before]["text"]
    assert "AI is down" in alert and "may have missed" in alert and "OS quiz on Friday" in alert

    monkeypatch.setattr(pipeline, "analyze_email",
                        lambda *a, **k: (Triage(importance=0, is_noise=1, source="gemini"), None))
    pipeline.ingest(RawEmail("<q2>", "x <x@y>", "later mail", utcnow(), "body"))
    pipeline.process_pending()
    assert _check() == 1 and "AI is back" in sent[-1]["text"]


def test_mail_that_fails_three_times_is_reported(monkeypatch, sent):
    monkeypatch.setattr(pipeline, "analyze_email", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    pipeline.ingest(RawEmail("<b1>", "Dean <d@x>", "Fee deadline", utcnow(), "body"))
    for _ in range(3):
        pipeline.process_pending()
    assert _check() == 1 and "couldn't process" in sent[-1]["text"] and "Fee deadline" in sent[-1]["text"]
    assert _check() == 0


def test_ongoing_problem_is_repeated_in_digest(monkeypatch, sent):
    from app.services.digest import build_digest

    src = _Source()
    src.error = MailError("IMAP error: login failed")
    monkeypatch.setattr(pipeline, "get_mail_source", lambda: src)
    pipeline.poll_mail()
    _age("mail_fail_since", minutes=20)
    _check()
    with session_scope() as s:
        assert "still can't read your mail" in build_digest(s)


def test_status_shows_whatsapp_window():
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/status", params={"token": "test-admin"}).json()
    assert body["whatsapp_24h_window_open"] is True and "health" in body
