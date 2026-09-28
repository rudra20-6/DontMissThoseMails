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
