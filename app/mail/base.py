import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawEmail:
    message_id: str
    sender: str
    subject: str
    received_at: datetime  # naive UTC
    body: str  # plain text
    web_link: str = ""
    links: list[str] = field(default_factory=list)


class MailError(RuntimeError):
    pass


def get_mail_source():
    from app.config import get_settings

    provider = get_settings().mail_provider.lower()
    if provider == "imap":
        from app.mail.imap import ImapSource

        return ImapSource()
    from app.mail.graph import GraphSource

    return GraphSource()


_FWD_PREFIX = re.compile(r"^\s*(?:(?:fw|fwd)\s*:\s*)+", re.I)
_HEADER_LINE = re.compile(r"^\s*\**\s*(from|sent|date|to|cc|subject)\s*:?\**\s*:?\s*(.*)$", re.I)


def unwrap_forward(sender: str, subject: str, body: str) -> tuple[str, str, str]:
    """Undo a forward (Outlook forwarding / Power Automate / Gmail) so the ORIGINAL sender and subject are used.

    'FW: Assignment 3' from you@college with a quoted 'From: Moodle <noreply@moodle...>' header block
    -> ('Moodle <noreply@moodle...>', 'Assignment 3', <original body>).
    Mails that are not forwards are returned unchanged.
    """
    if not _FWD_PREFIX.match(subject or ""):
        return sender, subject, body
    new_subject = _FWD_PREFIX.sub("", subject).strip() or subject
    lines = body.split("\n")
    for i, line in enumerate(lines[:60]):
        m = _HEADER_LINE.match(line)
        if not m or m.group(1).lower() != "from" or not m.group(2).strip():
            continue
        original_sender = m.group(2).strip()
        # skip the rest of the quoted header block (Sent/Date/To/Cc/Subject lines)
        j = i + 1
        while j < len(lines) and (_HEADER_LINE.match(lines[j]) or not lines[j].strip()) and j < i + 12:
            hm = _HEADER_LINE.match(lines[j])
            if hm and hm.group(1).lower() == "subject" and hm.group(2).strip():
                new_subject = hm.group(2).strip()
            j += 1
        return original_sender, new_subject, "\n".join(lines[j:]).strip()
    return sender, new_subject, body
