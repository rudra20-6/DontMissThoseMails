from app.services.commands import parse, parse_button


def test_parse_commands():
    assert parse("done 12") == ("mark_done", 12, None)
    assert parse("Submitted #7") == ("mark_done", 7, None)
    assert parse("registered 3") == ("mark_registered", 3, None)
    assert parse("no 4") == ("not_interested", 4, None)
    assert parse("snooze 5 2d") == ("snooze", 5, 48.0)
    assert parse("snooze 5") == ("snooze", 5, 3.0)
    assert parse("list") == ("list", None, None)
    assert parse("add DBMS project due friday")[0] == "add"
    assert parse("I finished the OS assignment") is None


def test_parse_buttons():
    assert parse_button("done:9") == ("mark_done", 9, None)
    assert parse_button("snooze:9:24") == ("snooze", 9, 24.0)
    assert parse_button("int:2") == ("interested", 2, None)
    assert parse_button("cmd:list") == ("list", None, None)
    assert parse_button("ack:digest") == ("ack", None, None)
