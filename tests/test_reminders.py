from datetime import timedelta

from app.models import Item
from app.services.reminders import plan
from app.timeutil import utcnow


def _deadline(created, due):
    return Item(id=1, kind="deadline", title="OS assignment", status="pending", due_at=due, created_at=created,
                category="coursework", importance=3, summary="", link="")


def _run(item, now, sent):
    actions = plan(item, now, sent)
    for a in actions:
        sent.update(a.keys)
        if a.new_status:
            item.status = a.new_status
        if a.clear_snooze:
            item.snoozed_until = None
    return [a for a in actions if a.text]


def test_deadline_offsets_fire_once_each():
    now = utcnow()
    due = now + timedelta(days=5)
    item = _deadline(now, due)
    sent: set[str] = set()
    assert _run(item, now + timedelta(days=1), sent) == []  # 4 days left: nothing yet
    assert len(_run(item, due - timedelta(hours=71), sent)) == 1  # 72h reminder
    assert _run(item, due - timedelta(hours=70), sent) == []  # not repeated
    assert len(_run(item, due - timedelta(hours=23), sent)) == 1  # 24h
    assert len(_run(item, due - timedelta(minutes=50), sent)) == 1  # skipped 6h tick -> single catch-up
    assert sent >= {"d72", "d24", "d6", "d1"}
    passed = _run(item, due + timedelta(minutes=1), sent)
    assert len(passed) == 1 and item.status == "expired"


def test_deadline_created_late_skips_old_offsets():
    now = utcnow()
    item = _deadline(now, now + timedelta(hours=5))
    sent: set[str] = set()
    # 72h/24h/6h fire-times are before creation -> covered by the initial notification
    assert _run(item, now + timedelta(hours=1), sent) == []
    assert len(_run(item, now + timedelta(hours=4, minutes=1), sent)) == 1  # 1h reminder


def test_snooze_delays_and_then_reminds():
    now = utcnow()
    item = _deadline(now - timedelta(days=1), now + timedelta(days=3))
    item.snoozed_until = now + timedelta(hours=3)
    sent: set[str] = set()
    assert _run(item, now + timedelta(hours=1), sent) == []
    msgs = _run(item, now + timedelta(hours=3, minutes=1), sent)
    assert len(msgs) == 1 and item.snoozed_until is None


def test_event_flow_interest_registration_and_start():
    now = utcnow()
    start = now + timedelta(days=4)
    reg = now + timedelta(days=2)
    item = Item(id=2, kind="event", title="Hackathon", status="asked", event_start=start, reg_deadline=reg,
                created_at=now, category="event", importance=2, summary="", link="", reg_link="https://x")
    sent: set[str] = set()
    assert _run(item, now + timedelta(hours=2), sent) == []
    assert len(_run(item, now + timedelta(hours=25), sent)) == 1  # re-ask once
    assert _run(item, now + timedelta(hours=30), sent) == []
    item.status = "interested"
    msgs = _run(item, reg - timedelta(hours=23), sent)  # 24h-before-registration reminder (+ maybe daily nag)
    assert len(msgs) == 1
    item.last_nag_at = reg - timedelta(hours=23)
    item.status = "registered"
    assert len(_run(item, start - timedelta(hours=1), sent)) == 1  # 2h before start
    _run(item, start + timedelta(hours=4), sent)
    assert item.status == "expired"


def test_interested_event_registration_closes():
    now = utcnow()
    item = Item(id=3, kind="event", title="Workshop", status="interested", event_start=now + timedelta(days=3),
                reg_deadline=now + timedelta(hours=1), created_at=now - timedelta(days=1), category="event",
                importance=2, summary="", link="", reg_link="")
    sent: set[str] = set()
    msgs = _run(item, now + timedelta(hours=2), sent)
    assert len(msgs) == 1 and item.status == "expired"
