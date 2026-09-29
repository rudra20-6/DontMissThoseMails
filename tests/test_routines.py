from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.models import Item, Outbox, ReminderLog
from app.services import decisions, notifier, routines
from app.services.commands import handle_message
from app.services.reminders import plan, run_reminders
from app.timeutil import local, to_utc_naive, utcnow

IST = ZoneInfo("Asia/Kolkata")


def at(d, h, m=0, s=0):  # 2030-01-07 is a Monday
    return to_utc_naive(datetime(2030, 1, d, h, m, s, tzinfo=IST))


def _routine(created, **sch):
    base = {"start": "09:00", "every": 60, "end": "", "days": list(range(7))}
    base.update(sch)
    return Item(id=1, kind="routine", title="Put attendance on ISB", status="active", schedule=base,
                created_at=created, category="personal", importance=2.5, summary="", link="")


def _run(item, now, sent):
    actions = plan(item, now, sent)
    for a in actions:
        sent.update(a.keys)
        if a.new_status:
            item.status = a.new_status
        if a.clear_snooze:
            item.snoozed_until = None
    return [a for a in actions if a.text]


class _FakePool:
    enabled = True

    def __init__(self, reply=None):
        self.reply = reply
        self.calls = 0

    def generate_json(self, system, prompt, schema, max_tokens=2048):
        self.calls += 1
        if self.reply is None:
            raise AssertionError("AI should not be called")
        return self.reply


def test_hourly_routine_until_done_every_day():
    item = _routine(at(7, 8))
    sent: set[str] = set()
    assert _run(item, at(7, 8, 59), sent) == []
    first = _run(item, at(7, 9, 0, 30), sent)
    assert len(first) == 1 and first[0].keys == ["rt2030-01-07T09:00"] and first[0].extra["scheduled"]
    assert _run(item, at(7, 9, 30), sent) == []
    # bot was asleep 09:30 -> 12:10: ONE reminder, not three
    late = _run(item, at(7, 12, 10), sent)
    assert len(late) == 1 and late[0].keys == ["rt2030-01-07T12:00"]
    sent.add(routines.done_key(datetime(2030, 1, 7).date()))  # "done 1"
    assert _run(item, at(7, 13, 5), sent) == []
    assert _run(item, at(7, 22, 5), sent) == []
    again = _run(item, at(8, 9, 1), sent)  # resets the next day
    assert len(again) == 1 and item.status == "active"


def test_routine_created_mid_morning_starts_at_next_slot():
    item = _routine(at(7, 10, 30))
    sent: set[str] = set()
    assert _run(item, at(7, 10, 45), sent) == []
    assert len(_run(item, at(7, 11, 0, 20), sent)) == 1
    assert routines.next_slot(item.schedule, at(7, 11, 1), item.created_at, sent).hour == 12


def test_weekdays_only_and_end_time():
    item = _routine(at(4, 8), days=list(range(5)), end="11:00")
    sent: set[str] = set()
    assert _run(item, at(5, 9, 1), sent) == []  # Saturday
    assert len(_run(item, at(7, 11, 1), sent)) == 1  # Monday, 11:00 is the last slot
    assert _run(item, at(7, 12, 1), sent) == []
    assert routines.describe(item.schedule) == "Weekdays · from 9:00 AM, every 1 h until 11:00 AM"


def test_one_off_reminder_fires_once_then_finishes():
    item = _routine(at(7, 10), days=None, date="2030-01-07", start="17:00", every=0)
    sent: set[str] = set()
    assert _run(item, at(7, 16, 59), sent) == []
    fired = _run(item, at(7, 17, 0, 30), sent)
    assert len(fired) == 1 and item.status == "done"
    assert _run(item, at(7, 18), sent) == []


def test_one_off_nagging_expires_next_day():
    item = _routine(at(7, 10), days=None, date="2030-01-07", start="17:00", every=30)
    sent: set[str] = set()
    assert len(_run(item, at(7, 17, 1), sent)) == 1
    assert len(_run(item, at(7, 17, 31), sent)) == 1
    _run(item, at(8, 9), sent)
    assert item.status == "expired"


def test_snooze_replaces_current_slot():
    item = _routine(at(7, 8))
    sent: set[str] = set()
    _run(item, at(7, 9, 1), sent)
    item.snoozed_until = at(7, 9, 16)
    assert _run(item, at(7, 9, 10), sent) == []
    snoozed = _run(item, at(7, 9, 16, 30), sent)
    assert len(snoozed) == 1 and item.snoozed_until is None
    assert _run(item, at(7, 9, 30), sent) == []
    assert len(_run(item, at(7, 10, 0, 30), sent)) == 1


def test_parse_days():
    assert routines.parse_days("daily") == list(range(7))
    assert routines.parse_days("weekdays") == [0, 1, 2, 3, 4]
    assert routines.parse_days("mon, wed & fri") == [0, 2, 4]
    assert routines.parse_days("") == []


# ---------------------------------------------------------------- through WhatsApp


def test_remind_me_creates_routine_with_one_ai_call(monkeypatch, sent):
    tomorrow9 = (local(utcnow()) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    pool = _FakePool({"intent": "remind", "item_id": 0, "task_title": "Put attendance on ISB",
                      "remind_first": tomorrow9.isoformat(), "remind_days": "daily", "remind_every_minutes": 60,
                      "remind_until": ""})
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text='remind me to "put attendance on ISB" every hour after 9am until I say i have marked it, EVERY day')
    with session_scope() as s:
        item = s.query(Item).filter_by(kind="routine").one()
        assert item.schedule == {"start": "09:00", "every": 60, "end": "", "days": list(range(7))}
        assert item.status == "active"
    assert pool.calls == 1
    text = sent[-1]["text"]
    assert "Routine" in text and "Every day · from 9:00 AM, every 1 h" in text and "Next:" in text


def test_one_off_time_already_passed_moves_to_tomorrow(monkeypatch, sent):
    earlier = local(utcnow()) - timedelta(minutes=30)
    pool = _FakePool({"intent": "remind", "item_id": 0, "task_title": "Call home", "remind_first": earlier.isoformat(),
                      "remind_days": "", "remind_every_minutes": 0})
    monkeypatch.setattr(decisions, "get_pool", lambda: pool)
    with session_scope() as s:
        handle_message(s, text="remind me at 5 to call home")
    with session_scope() as s:
        item = s.query(Item).filter_by(kind="routine").one()
        assert item.schedule["date"] == (earlier.date() + timedelta(days=1)).isoformat()


def test_swipe_reply_done_without_number(monkeypatch, sent):
    monkeypatch.setattr(decisions, "get_pool", lambda: _FakePool())  # exact commands never call the AI
    with session_scope() as s:
        a = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=1))
        b = Item(kind="deadline", title="DBMS quiz", status="pending", due_at=utcnow() + timedelta(days=2))
        s.add_all([a, b])
        s.flush()
        notifier.notify(s, "card A", item_id=a.id)
        wamid_a = f"wamid.test{len(sent)}"
        notifier.notify(s, "card B", item_id=b.id)
        ids = a.id, b.id
    with session_scope() as s:
        handle_message(s, text="Done", context_id=wamid_a)  # replying to A, although B was sent last
    with session_scope() as s:
        assert s.get(Item, ids[0]).status == "done" and s.get(Item, ids[1]).status == "pending"
    with session_scope() as s:
        handle_message(s, text="done")  # no reply context: the item messaged about last that's still open
    with session_scope() as s:
        assert s.get(Item, ids[1]).status == "done"
    assert "undo" in sent[-1]["text"]
    with session_scope() as s:
        handle_message(s, text="undo")
    with session_scope() as s:
        assert s.get(Item, ids[1]).status == "pending" and s.get(Item, ids[0]).status == "done"


def test_done_on_routine_is_for_today_and_undoable(sent):
    with session_scope() as s:
        r = Item(kind="routine", title="Attendance", status="active",
                 schedule={"start": "09:00", "every": 60, "end": "", "days": list(range(7))})
        s.add(r)
        s.flush()
        rid = r.id
    with session_scope() as s:
        handle_message(s, text=f"done {rid}")
    today_key = routines.done_key(local(utcnow()).date())
    with session_scope() as s:
        assert s.get(Item, rid).status == "active"
        assert s.query(ReminderLog).filter_by(item_id=rid, key=today_key).count() == 1
    assert "Done for today" in sent[-1]["text"]
    with session_scope() as s:
        handle_message(s, text="undo")
    with session_scope() as s:
        assert s.query(ReminderLog).filter_by(item_id=rid, key=today_key).count() == 0
    with session_scope() as s:
        handle_message(s, text=f"stop {rid}")
    with session_scope() as s:
        assert s.get(Item, rid).status == "stopped"


def test_multiple_ids(sent):
    with session_scope() as s:
        items = [Item(kind="deadline", title=f"T{n}", status="pending", due_at=utcnow() + timedelta(days=1)) for n in range(3)]
        s.add_all(items)
        s.flush()
        ids = [i.id for i in items]
    with session_scope() as s:
        handle_message(s, text=f"done {ids[0]} {ids[2]}")
    with session_scope() as s:
        assert [s.get(Item, i).status for i in ids] == ["done", "pending", "done"]


def test_move_deadline_replans_reminders(monkeypatch, sent):
    with session_scope() as s:
        d = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(hours=20),
                 created_at=utcnow() - timedelta(days=3))
        s.add(d)
        s.flush()
        s.add(ReminderLog(item_id=d.id, key="d24"))
        did = d.id
    new_due = local(utcnow()) + timedelta(days=4)
    monkeypatch.setattr(decisions, "get_pool",
                        lambda: _FakePool({"intent": "reschedule", "item_id": did, "new_time": new_due.isoformat()}))
    with session_scope() as s:
        handle_message(s, text="the OS assignment got extended by 3 days")
    with session_scope() as s:
        item = s.get(Item, did)
        assert abs((item.due_at - to_utc_naive(new_due)).total_seconds()) < 1
        assert s.query(ReminderLog).filter_by(item_id=did).count() == 0
    assert "Moved" in sent[-1]["text"]


def test_snooze_to_a_time_via_swipe_reply(monkeypatch, sent):
    with session_scope() as s:
        d = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=2))
        s.add(d)
        s.flush()
        notifier.notify(s, "card", item_id=d.id)
        did, wamid = d.id, f"wamid.test{len(sent)}"
    until = (local(utcnow()) + timedelta(hours=5)).replace(second=0, microsecond=0)
    monkeypatch.setattr(decisions, "get_pool",
                        lambda: _FakePool({"intent": "snooze", "item_id": 0, "snooze_until": until.isoformat()}))
    with session_scope() as s:
        handle_message(s, text="snooze till tonight", context_id=wamid)
    with session_scope() as s:
        assert s.get(Item, did).snoozed_until == to_utc_naive(until)


def test_paused_routine_pings_are_skipped_not_queued(sent):
    now_l = local(utcnow())
    if now_l.hour == 0 and now_l.minute < 3:
        return
    start = (now_l - timedelta(minutes=1)).strftime("%H:%M")
    with session_scope() as s:
        s.add(Item(kind="routine", title="Attendance", status="active", created_at=utcnow() - timedelta(hours=1),
                   schedule={"start": start, "every": 0, "end": "", "days": list(range(7))}))
        handle_message(s, text="pause")
    before = len(sent)
    with session_scope() as s:
        run_reminders(s)
    with session_scope() as s:
        assert s.query(Outbox).filter(Outbox.sent_at.is_(None)).count() == 0
    assert len(sent) == before


def test_expired_outbox_messages_are_dropped(sent):
    with session_scope() as s:
        s.add(Outbox(payload={"text": "stale ping", "buttons": [], "expires": (utcnow() - timedelta(minutes=1)).isoformat()}))
        s.add(Outbox(payload={"text": "fresh", "buttons": []}))
    with session_scope() as s:
        notifier.flush_outbox(s, force=True)
    assert [m["text"] for m in sent] == ["fresh"]


def test_webhook_swipe_reply_passes_context(sent):
    from fastapi.testclient import TestClient

    from app.main import app

    with session_scope() as s:
        d = Item(kind="deadline", title="OS assignment", status="pending", due_at=utcnow() + timedelta(days=1))
        s.add(d)
        s.flush()
        notifier.notify(s, "card", item_id=d.id)
        did, wamid = d.id, f"wamid.test{len(sent)}"
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999", "id": "wamid.in1", "type": "text", "text": {"body": "done"},
        "context": {"from": "15550000000", "id": wamid}}]}}]}]}
    TestClient(app).post("/webhook/whatsapp", json=payload)
    with session_scope() as s:
        assert s.get(Item, did).status == "done"
