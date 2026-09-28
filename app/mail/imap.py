"""Generic IMAP source (fallback when Microsoft Graph OAuth is not allowed by your college tenant).

Works with any mailbox that supports IMAP + password / app-password, e.g. a Gmail account
to which your Outlook mail is auto-forwarded.
"""

from __future__ import annotations

import email
import imaplib
import logging
from datetime import timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from app import kv
from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail, unwrap_forward
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


def _body(msg: email.message.Message) -> str:
    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
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
            # IMAP SINCE has day granularity; filter precisely below and dedupe by Message-ID upstream.
            typ, data = conn.search(None, "SINCE", (since - timedelta(days=1)).strftime("%d-%b-%Y"))
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            for num in ids[-s.mail_max_per_poll * 4:]:
                typ, parts = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                msg = email.message_from_bytes(parts[0][1])
                try:
                    received = to_utc_naive(parsedate_to_datetime(msg.get("Date")))
                except (TypeError, ValueError):
                    received = utcnow()
                if received < since:
                    continue
                sender, subject, text = unwrap_forward(
                    _decode(msg.get("From")), _decode(msg.get("Subject")) or "(no subject)", _body(msg))
                out.append(
                    RawEmail(
                        message_id=(msg.get("Message-ID") or f"imap-{num.decode()}-{received.isoformat()}").strip(),
                        sender=sender,
                        subject=subject,
                        received_at=received,
                        body=text,
                        links=extract_urls(text),
                    )
                )
            conn.logout()
        except imaplib.IMAP4.error as exc:
            raise MailError(f"IMAP error: {exc}") from exc
        out.sort(key=lambda e: e.received_at)
        out = out[: s.mail_max_per_poll]
        if out:
            with session_scope() as db:
                kv.put(db, CURSOR_KEY, max(e.received_at for e in out).isoformat() + "Z")
        return out
