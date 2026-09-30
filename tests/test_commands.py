from datetime import timedelta

from app.db import session_scope
from app.models import Item
from app.services import decisions, labels
from app.services.commands import Cmd, handle_message, parse, parse_button
from app.timeutil import utcnow


def test_parse_commands():
    assert parse("done D12") == Cmd("mark_done", ["d12"])
    assert parse("Submitted #d7") == Cmd("mark_done", ["d7"])
    assert parse("done d1 d3, e2") == Cmd("mark_done", ["d1", "d3", "e2"])
    assert parse("done 3") == Cmd("mark_done", ["3"])
    assert parse("done") == Cmd("mark_done")
    assert parse("done for today") == Cmd("mark_done")
    assert parse("done dbms") == Cmd("mark_done", [], None, "dbms")
    assert parse("done with the OS assignment") == Cmd("mark_done", [], None, "with the os assignment")
    assert parse("registered hackathon") == Cmd("mark_registered", [], None, "hackathon")
    assert parse("not interested e4") == Cmd("not_interested", ["e4"])
    assert parse("stop attendance") == Cmd("not_interested", [], None, "attendance")
    assert parse("yes") == Cmd("interested")
    assert parse("snooze d5 2d") == Cmd("snooze", ["d5"], 48.0)
    assert parse("snooze d5") == Cmd("snooze", ["d5"])
    assert parse("snooze d5 3") == Cmd("snooze", ["d5"], 3.0)
    assert parse("snooze hackathon 30m") == Cmd("snooze", [], 0.5, "hackathon")
    assert parse("snooze 2h") == Cmd("snooze", [], 2.0)
    assert parse("snooze") == Cmd("snooze")
    assert parse("undo") == Cmd("undo")
    assert parse("list") == Cmd("list")
    assert parse("add DBMS project due friday").intent == "add"
    assert parse("I finished the OS assignment") is None
    assert parse("remind me at 5pm to call home") is None
    assert parse("move os to friday") is None
    assert parse("nice") is None


def test_parse_buttons():
    assert parse_button("done:9") == Cmd("mark_done", ["id:9"])
    assert parse_button("snooze:9:24") == Cmd("snooze", ["id:9"], 24.0)
    assert parse_button("snooze:9:0.25") == Cmd("snooze", ["id:9"], 0.25)
    assert parse_button("int:2") == Cmd("interested", ["id:2"])
    assert parse_button("cmd:list") == Cmd("list")


def _deadline(title, status="pending"):
    return Item(kind="deadline", title=title, status=status, due_at=utcnow() + timedelta(days=1))


def test_labels_are_per_kind_small_and_reused_after_a_day():
    with session_scope() as s:
        a, b = _deadline("OS assignment"), _deadline("DBMS quiz")
        e = Item(kind="event", title="Hackathon", status="asked")
        n = Item(kind="announcement", title="Hostel notice", status="info")
        for i in (a, b, e, n):
            s.add(i)
            labels.assign(s, i)
        s.flush()
        assert (a.label, b.label, e.label, n.label) == ("D1", "D2", "E1", None)
        a.status = "done"
        s.flush()
        c = _deadline("CN lab")
        s.add(c)
        labels.assign(s, c)
        assert c.label == "D3"  # D1 was freed just now: not reused yet
        a.updated_at = utcnow() - timedelta(hours=25)
        s.flush()
        d = _deadline("Maths HW")
        s.add(d)
        labels.assign(s, d)
        assert d.label == "D1"
        assert labels.find(s, "d1") is d  # the open one wins over the old done one


def test_done_by_name_label_and_number(monkeypatch, sent):
    monkeypatch.setattr(decisions, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("called AI")))
    with session_scope() as s:
        s.add_all([_deadline("OS Assignment 3"), _deadline("DBMS quiz"), _deadline("CN lab report")])
    with session_scope() as s:
        handle_message(s, text="done dbms")
        handle_message(s, text="done with the os assignment")
        handle_message(s, text="done D3")
    with session_scope() as s:
        assert {i.title: i.status for i in s.query(Item)} == {
            "OS Assignment 3": "done", "DBMS quiz": "done", "CN lab report": "done"}


def test_ambiguous_name_asks_with_buttons(monkeypatch, sent):
    monkeypatch.setattr(decisions, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("called AI")))
    with session_scope() as s:
        s.add_all([_deadline("OS Assignment 3"), _deadline("OS quiz"), Item(kind="event", title="OSDG hackathon", status="asked")])
    with session_scope() as s:
        handle_message(s, text="snooze os 2h")
    assert "Which one" in sent[-1]["text"] and "OSDG" not in sent[-1]["text"]
    button_id = sent[-1]["buttons"][1][0]
    assert button_id.endswith(":2")
    with session_scope() as s:
        handle_message(s, button_id=button_id)
    with session_scope() as s:
        assert s.query(Item).filter_by(title="OS quiz").one().snoozed_until is not None


def test_unknown_name_falls_back_to_ai(monkeypatch, sent):
    class Pool:
        enabled, calls = True, 0

        def generate_json(self, *a, **k):
            Pool.calls += 1
            return {"intent": "mark_done", "item_id": 1}

    monkeypatch.setattr(decisions, "get_pool", lambda: Pool())
    with session_scope() as s:
        s.add(_deadline("Operating Systems assignment"))
    with session_scope() as s:
        handle_message(s, text="done with the OS thing prof gave")
    with session_scope() as s:
        assert s.get(Item, 1).status == "done"
    assert Pool.calls == 1


def test_prefix_match_when_no_whole_word(monkeypatch, sent):
    monkeypatch.setattr(decisions, "get_pool", lambda: (_ for _ in ()).throw(AssertionError("called AI")))
    with session_scope() as s:
        s.add_all([_deadline("Operating Systems assignment"), Item(kind="event", title="OSDG hackathon", status="asked")])
    with session_scope() as s:
        handle_message(s, text="details hack")
    assert "OSDG hackathon" in sent[-1]["text"]
