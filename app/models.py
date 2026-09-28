from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.timeutil import utcnow


class KV(Base):
    """Small key/value store: OAuth tokens, last poll time, pause flag, last inbound message time..."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(512), default="")
    subject: Mapped[str] = mapped_column(String(1024), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    web_link: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)  # raw decision signals, for auditing
    action: Mapped[str] = mapped_column(String(32), default="")  # dropped | digest | notified | error
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Item(Base):
    """Something the user must not miss: a deadline, an event, or an announcement."""

    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int | None] = mapped_column(ForeignKey("emails.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))  # deadline | event | announcement
    category: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[float] = mapped_column(Float, default=2.0)

    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # deadlines
    event_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    event_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    venue: Mapped[str] = mapped_column(String(512), default="")
    reg_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reg_link: Mapped[str] = mapped_column(Text, default="")

    # deadline: pending | done | dismissed | expired
    # event: asked | interested | registered | not_interested | expired
    # announcement: info
    status: Mapped[str] = mapped_column(String(32), index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_nag_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    in_digest: Mapped[bool] = mapped_column(Boolean, default=False)  # low-priority, deliver via digest
    digested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReminderLog(Base):
    """One row per reminder sent, so every reminder is sent exactly once (idempotent ticks)."""

    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("item_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Outbox(Base):
    """Messages held back (quiet hours / WhatsApp 24h window closed) and delivered later."""

    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
