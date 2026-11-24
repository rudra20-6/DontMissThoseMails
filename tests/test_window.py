from datetime import timedelta

from app import kv
from app.clients.whatsapp import WhatsAppClient
from app.db import session_scope
from app.models import Outbox
from app.services import notifier
from app.services.commands import handle_message
from app.timeutil import utcnow


def _templates(monkeypatch):
    calls = []
    monkeypatch.setattr(WhatsAppClient, "send_template", lambda self, param=None: calls.append(param) or {})
    return calls


def _queued():
    with session_scope() as s:
        return s.query(Outbox).filter(Outbox.sent_at.is_(None)).count()


def test_window_closed_queues_and_sends_template_once(monkeypatch, sent):
    templates = _templates(monkeypatch)
    with session_scope() as s:
        kv.put(s, notifier.INBOUND_KEY, (utcnow() - timedelta(hours=30)).isoformat())
        notifier.notify(s, "OS quiz due in 6 h")
        notifier.notify(s, "Attendance reminder")
    assert sent == [] and _queued() == 2
    assert len(templates) == 1  # one nudge, not one per message
    with session_scope() as s:
        notifier.flush_outbox(s)  # scheduler tick: still closed, nothing sent into the void
    assert sent == []
    with session_scope() as s:
        handle_message(s, text="list")  # the user writes back: everything is delivered
    assert [m["text"] for m in sent[:2]] == ["OS quiz due in 6 h", "Attendance reminder"]
    assert _queued() == 0


def test_async_131047_requeues_the_message(monkeypatch, sent):
    templates = _templates(monkeypatch)
    with session_scope() as s:
        notifier.notify(s, "Hackathon registration closes in 1 h")
    assert len(sent) == 1
    wamid = f"wamid.test{len(sent)}"
    with session_scope() as s:  # Meta said OK, then reports it undelivered
        notifier.delivery_failed(s, wamid, 131047)
    assert _queued() == 1 and len(templates) == 1
    with session_scope() as s:
        notifier.notify(s, "next one")  # now known to be closed: queued without trying
    assert len(sent) == 1 and _queued() == 2
    with session_scope() as s:
        notifier.delivery_failed(s, wamid, 131047)  # duplicate status webhook: not queued twice
    assert _queued() == 2


def test_other_async_failures_are_ignored(sent):
    with session_scope() as s:
        notifier.notify(s, "x")
        notifier.delivery_failed(s, "wamid.test1", 131026)
    assert _queued() == 0


def test_webhook_status_failure_triggers_requeue(monkeypatch, sent):
    from fastapi.testclient import TestClient

    from app.main import app

    _templates(monkeypatch)
    with session_scope() as s:
        notifier.notify(s, "Fee deadline tomorrow")
    payload = {"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.test1", "status": "failed",
        "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    TestClient(app).post("/webhook/whatsapp", json=payload)
    assert _queued() == 1


def test_delivery_report_for_another_bots_user_is_ignored(monkeypatch, sent):
    from fastapi.testclient import TestClient

    from app.main import app

    templates = _templates(monkeypatch)
    payload = {"entry": [{"changes": [{"value": {"statuses": [{
        "id": "wamid.other", "status": "failed", "recipient_id": "918888888888",
        "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    TestClient(app).post("/webhook/whatsapp", json=payload)
    with session_scope() as s:
        assert notifier.window_open(s)
    assert templates == []
