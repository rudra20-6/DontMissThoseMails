"""Short handles for items: D1, D2 for deadlines, E1 for events, R1 for routines. Notices get none.

Numbers are per kind and reused: a new item takes the smallest number that isn't held by an open item and
wasn't freed in the last day, so an old message's handle doesn't suddenly point at something new.
Buttons and swipe-replies use the real database id, never the handle.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Item
from app.timeutil import utcnow

PREFIX = {"deadline": "D", "event": "E", "routine": "R"}
KIND_OF = {v: k for k, v in PREFIX.items()}
OPEN = ("pending", "asked", "interested", "registered", "active")
REUSE_AFTER = timedelta(hours=24)
LABEL_RE = re.compile(r"^([der])(\d{1,3})$", re.IGNORECASE)


def assign(session: Session, item: Item) -> None:
    """Give an open item a handle (or keep its current one if nobody else took it meanwhile)."""
    prefix = PREFIX.get(item.kind)
    if not prefix or item.status not in OPEN:
        return
    others = select(Item).where(Item.kind == item.kind, Item.label.is_not(None))
    if item.id is not None:
        others = others.where(Item.id != item.id)
    if item.label and not session.scalar(others.where(Item.label == item.label, Item.status.in_(OPEN)).limit(1)):
        return
    held = session.scalars(
        others.with_only_columns(Item.label)
        .where(or_(Item.status.in_(OPEN), Item.updated_at >= utcnow() - REUSE_AFTER))
    ).all()
    used = {int(lbl[1:]) for lbl in held if lbl and lbl[1:].isdigit()}
    n = 1
    while n in used:
        n += 1
    item.label = f"{prefix}{n}"


def backfill(session: Session) -> None:
    """Open items from before handles existed (or that came back via undo) get one."""
    items = session.scalars(
        select(Item).where(Item.kind.in_(tuple(PREFIX)), Item.status.in_(OPEN), Item.label.is_(None))
        .order_by(Item.created_at, Item.id)
    ).all()
    for item in items:
        assign(session, item)
        session.flush()


def find(session: Session, label: str) -> Item | None:
    """'d3' -> the open deadline D3, else the most recent item that had that handle."""
    m = LABEL_RE.match(label.strip())
    if not m:
        return None
    lbl = f"{m.group(1).upper()}{int(m.group(2))}"
    rows = session.scalars(select(Item).where(Item.label == lbl).order_by(Item.updated_at.desc())).all()
    return next((i for i in rows if i.status in OPEN), rows[0] if rows else None)


def by_number(session: Session, n: int) -> list[Item]:
    """'3' on its own -> every open item numbered 3 (D3, E3, R3)."""
    return list(session.scalars(
        select(Item).where(Item.label.in_([f"{p}{n}" for p in KIND_OF]), Item.status.in_(OPEN)).order_by(Item.label)
    ).all())
