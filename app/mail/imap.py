"""Generic IMAP source (fallback when Microsoft Graph OAuth is not allowed by your college tenant).

Works with any mailbox that supports IMAP + password / app-password, e.g. a Gmail account
to which your Outlook mail is auto-forwarded.
"""

from __future__ import annotations

import email
import imaplib
import logging
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from app import kv
from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail, unwrap_forward_with_date
from app.textutil import clean, extract_urls, html_to_text
from app.timeutil import parse_local, to_utc_naive, utcnow

log = logging.getLogger(__name__)
CURSOR_KEY = "imap_cursor"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


def _attached_messages(msg: email.message.Message) -> list[email.message.Message]:
    """Emails attached to this one (Outlook 'forward as attachment' of many mails at once)."""
    out = []
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else payload
            if isinstance(inner, email.message.Message):
                out.append(inner)
    return out


def _body(msg: email.message.Message) -> str:
    plain, html = "", ""
    parts = []
    stack = [msg]
    while stack:  # walk, but don't descend into attached emails
        part = stack.pop(0)
        if part.get_content_type() == "message/rfc822":
            continue
        if part.is_multipart():
            stack[0:0] = list(part.get_payload())
            continue
        parts.append(part)
    for part in parts:
        if part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/plain" and not plain:
            plain = text
        elif part.get_content_type() == "text/html" and not html:
            html = text
    return html_to_text(html) if html else clean(plain)


def _date(msg: email.message.Message):
    try:
        return to_utc_naive(parsedate_to_datetime(msg.get("Date")))
    except (TypeError, ValueError):
        return None


class ImapSource:
    def fetch_new(self) -> list[RawEmail]:
        s = get_settings()
        if not (s.imap_username and s.imap_password):
            raise MailError("IMAP_USERNAME / IMAP_PASSWORD not configured")
        with session_scope() as db:
            cursor = kv.get(db, CURSOR_KEY)
        since = parse_local(cursor) if cursor else utcnow() - timedelta(hours=s.mail_lookback_hours)
        out: list[RawEmail] = []
        try:
            conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
            conn.login(s.imap_username, s.imap_password)
            conn.select(s.imap_folder, readonly=True)
            # IMAP SINCE has day granularity: list candidates, then use each mail's arrival time
            # (INTERNALDATE) to keep only mails newer than the cursor, oldest first.
            typ, data = conn.search(None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            arrivals: list[tuple[datetime, bytes]] = []
            if ids:
                typ, meta = conn.fetch(b",".join(ids), "(INTERNALDATE)")
                for line in meta if typ == "OK" else []:
                    raw = line[0] if isinstance(line, tuple) else line
                    if not isinstance(raw, bytes):
                        continue
                    tt = imaplib.Internaldate2tuple(raw)
                    if tt:
                        arrived = datetime.fromtimestamp(time.mktime(tt), timezone.utc).replace(tzinfo=None)
                        if arrived >= since:
                            arrivals.append((arrived, raw.split(b" ", 1)[0]))
            arrivals.sort()
            batch = arrivals[: s.mail_max_per_poll * 4]  # the rest is picked up on the next poll
            for received, num in batch:
                typ, parts = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1])
                attached = _attached_messages(msg)
                if attached:
                    # one wrapper mail carrying many forwarded mails -> each becomes its own email
                    for k, inner in enumerate(attached):
                        text = _body(inner)
                        out.append(RawEmail(
                            message_id=(inner.get("Message-ID") or f"imap-{num.decode()}-att{k}").strip(),
                            sender=_decode(inner.get("From")),
                            subject=_decode(inner.get("Subject")) or "(no subject)",
                            received_at=received,
                            original_date=_date(inner),
                            body=text,
                            links=extract_urls(text),
                        ))
                    continue
                sender, subject, text, original = unwrap_forward_with_date(
                    _decode(msg.get("From")), _decode(msg.get("Subject")) or "(no subject)", _body(msg))
                out.append(
                    RawEmail(
                        message_id=(msg.get("Message-ID") or f"imap-{num.decode()}-{received.isoformat()}").strip(),
                        sender=sender,
                        subject=subject,
                        received_at=received,
                        original_date=original or _date(msg),
                        body=text,
                        links=extract_urls(text),
                    )
                )
            conn.logout()
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP error: {exc}") from exc
        # No cap here: storing is cheap and analysis is queued (a bulk forward can carry dozens of mails).
        out.sort(key=lambda e: e.received_at)
        if out:
            with session_scope() as db:
                kv.put(db, CURSOR_KEY, max(e.received_at for e in out).isoformat() + "Z")
        return out
