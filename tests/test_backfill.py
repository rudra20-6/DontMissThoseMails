import imaplib
import time
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from app.db import session_scope
from app.mail import imap as imap_mod
from app.mail.base import RawEmail
from app.models import Email, Item
from app.services import pipeline
from app.services.decisions import Triage
from app.services.digest import maybe_send_catchup
from app.services.extraction import Deadline, EventInfo, Extraction
from app.timeutil import utcnow

from datetime import timezone


def _mail(subject, sender, body, sent):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, "me@x"
    m["Date"] = format_datetime(sent.replace(tzinfo=timezone.utc))
    m["Message-ID"] = f"<{subject.replace(' ', '')}@x>"
    m.set_content(body)
    return m


def _bulk_forward(inner_mails, sent):
    wrapper = EmailMessage()
    wrapper["Subject"], wrapper["From"], wrapper["To"] = "FW: 3 messages", "Me <me@college>", "bot@gmail.com"
    wrapper["Date"] = format_datetime(sent.replace(tzinfo=timezone.utc))
    wrapper["Message-ID"] = "<wrapper@x>"
    wrapper.set_content("see attached")
    for m in inner_mails:
        wrapper.add_attachment(m)  # -> message/rfc822 parts, like Outlook "forward as attachment"
    return wrapper


class FakeIMAP:
    def __init__(self, messages):  # list of (arrival_datetime_utc, EmailMessage)
        self.messages = messages

    def login(self, *a):
        pass

    def select(self, *a, **k):
        return "OK", [b"1"]

    def search(self, *a):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, nums, what):
        if what == "(INTERNALDATE)":
            out = []
            for n in nums.split(b","):
                arrived = self.messages[int(n) - 1][0]
                stamp = imaplib.Time2Internaldate(arrived.replace(tzinfo=timezone.utc).timestamp())
                out.append(n + b" (INTERNALDATE " + stamp.encode() + b")")
            return "OK", out
        msg = self.messages[int(nums) - 1][1]
        return "OK", [(nums + b" (BODY[] {1}", msg.as_bytes()), b")"]

    def logout(self):
        pass


def test_bulk_forward_is_split_into_original_mails(monkeypatch):
    now = utcnow()
    inner = [_mail(f"Mail {i}", f"Sender{i} <s{i}@college>", f"Body {i}", now - timedelta(days=3 + i)) for i in range(3)]
    fake = FakeIMAP([(now, _bulk_forward(inner, now))])
    monkeypatch.setattr(imap_mod.imaplib, "IMAP4_SSL", lambda *a: fake)
    monkeypatch.setattr(imap_mod, "get_settings", lambda: type("S", (), {
        "imap_username": "u", "imap_password": "p", "imap_host": "h", "imap_port": 993, "imap_folder": "INBOX",
        "mail_lookback_hours": 24, "mail_max_per_poll": 25})())
    got = imap_mod.ImapSource().fetch_new()
    assert [g.subject for g in got] == ["Mail 0", "Mail 1", "Mail 2"]
    assert got[0].sender == "Sender0 <s0@college>" and "Body 0" in got[0].body
    assert abs((got[1].original_date - (now - timedelta(days=4))).total_seconds()) < 2
    assert "see attached" not in got[0].body


def test_imap_takes_oldest_first_and_continues_next_poll(monkeypatch):
    now = utcnow()
    msgs = [(now - timedelta(hours=20 - i), _mail(f"M{i}", "a@b", "x", now - timedelta(hours=20 - i))) for i in range(6)]
    fake = FakeIMAP(msgs)
    monkeypatch.setattr(imap_mod.imaplib, "IMAP4_SSL", lambda *a: fake)
    settings = type("S", (), {"imap_username": "u", "imap_password": "p", "imap_host": "h", "imap_port": 993,
                              "imap_folder": "INBOX", "mail_lookback_hours": 48, "mail_max_per_poll": 1})()
    monkeypatch.setattr(imap_mod, "get_settings", lambda: settings)
    first = imap_mod.ImapSource().fetch_new()  # batch = 4 (max_per_poll * 4)
    second = imap_mod.ImapSource().fetch_new()
    assert [m.subject for m in first] == ["M0", "M1", "M2", "M3"]
    assert [m.subject for m in second if m.subject not in {"M3"}] == ["M4", "M5"]


def test_old_mails_go_to_one_catchup_summary(monkeypatch, sent):
    now = utcnow()
    results = {
        "Quiz": (Triage(category="coursework", importance=3, has_deadline=1.0),
                 Extraction("OS Quiz 2", "Quiz 2 on Moodle.", [Deadline("quiz", now + timedelta(days=2))])),
        "Hack": (Triage(category="event", importance=2, is_event=1.0),
                 Extraction("BuildBots", "Hackathon.", [], EventInfo("BuildBots", now + timedelta(days=4), None, "H105",
                                                                     now + timedelta(days=2), "https://f"))),
        "Old": (Triage(category="coursework", importance=3, has_deadline=1.0),
                Extraction("Lab 1", "Lab 1 due.", [Deadline("lab", now - timedelta(days=1))])),
        "Meh": (Triage(category="campus_admin", importance=1.0), Extraction("Mess menu", "New menu.")),
    }
    monkeypatch.setattr(pipeline, "analyze_email", lambda sender, subject, *a, **k: results[subject])
    for i, subj in enumerate(results):
        pipeline.ingest(RawEmail(f"<{subj}>", "x@college", subj, now, "body", original_date=now - timedelta(days=5 - i)))
    pipeline.process_pending()
    assert sent == []  # nothing pushed one by one
    with session_scope() as s:
        assert maybe_send_catchup(s)
    text = sent[0]["text"]
    assert "Catch-up" in text and "OS Quiz 2" in text and "BuildBots" in text and "interested" in text
    assert "Lab 1" not in text  # already past: not tracked
    assert "Mess menu" not in text  # old, low-value: archived silently
    with session_scope() as s:
        assert not maybe_send_catchup(s)  # only once
        assert s.query(Item).filter_by(kind="deadline", status="pending").count() == 1  # reminders will run


def test_fresh_mail_is_still_pushed_immediately(monkeypatch, sent):
    now = utcnow()
    monkeypatch.setattr(pipeline, "analyze_email", lambda *a, **k: (
        Triage(category="coursework", importance=3, has_deadline=1.0),
        Extraction("A3", "Submit.", [Deadline("a3", now + timedelta(days=2))])))
    pipeline.ingest(RawEmail("<new>", "x@college", "A3", now, "body", original_date=now - timedelta(hours=1)))
    pipeline.process_pending()
    assert len(sent) == 1


def test_same_mail_forwarded_twice_is_ingested_once():
    now = utcnow()
    assert pipeline.ingest(RawEmail("<id-1>", "Moodle <m@x>", "Quiz", now, "b", original_date=now - timedelta(days=1)))
    assert not pipeline.ingest(RawEmail("<id-2>", "Moodle <m@x>", "Quiz", now, "b",
                                        original_date=now - timedelta(days=1, seconds=-40)))
    with session_scope() as s:
        assert s.query(Email).count() == 1
