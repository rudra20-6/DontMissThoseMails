from app.services.commands import parse, parse_button


def test_parse_commands():
    assert parse("done 12") == ("mark_done", [12], None)
    assert parse("Submitted #7") == ("mark_done", [7], None)
    assert parse("done 12 14, 15") == ("mark_done", [12, 14, 15], None)
    assert parse("done #3 and #4") == ("mark_done", [3, 4], None)
    assert parse("done") == ("mark_done", [], None)
    assert parse("done for today") == ("mark_done", [], None)
    assert parse("registered 3") == ("mark_registered", [3], None)
    assert parse("no 4") == ("not_interested", [4], None)
    assert parse("not interested 4") == ("not_interested", [4], None)
    assert parse("stop 14") == ("not_interested", [14], None)
    assert parse("yes") == ("interested", [], None)
    assert parse("snooze 5 2d") == ("snooze", [5], 48.0)
    assert parse("snooze 5") == ("snooze", [5], None)
    assert parse("snooze 5 30m") == ("snooze", [5], 0.5)
    assert parse("snooze 2h") == ("snooze", [], 2.0)
    assert parse("snooze") == ("snooze", [], None)
    assert parse("undo") == ("undo", [], None)
    assert parse("list") == ("list", [], None)
    assert parse("add DBMS project due friday")[0] == "add"
    assert parse("I finished the OS assignment") is None
    assert parse("done with the OS assignment") is None  # needs the AI
    assert parse("no worries") is None
    assert parse("nice") is None
    assert parse("remind me at 5pm to call home") is None
    assert parse("snooze 12 till 8pm") is None
    assert parse("move 12 to friday") is None


def test_parse_buttons():
    assert parse_button("done:9") == ("mark_done", [9], None)
    assert parse_button("snooze:9:24") == ("snooze", [9], 24.0)
    assert parse_button("snooze:9:0.25") == ("snooze", [9], 0.25)
    assert parse_button("int:2") == ("interested", [2], None)
    assert parse_button("cmd:list") == ("list", [], None)
    assert parse_button("ack:digest") == ("ack", [], None)
