"""Tiny in-memory diagnostics log behind /admin/whatsapp-check (resets when the app restarts)."""

from __future__ import annotations

import threading
from typing import Any

from app.timeutil import fmt, utcnow

_data: dict[str, dict] = {}
_lock = threading.Lock()


def record(key: str, **data: Any) -> None:
    with _lock:
        _data[key] = {"_t": utcnow(), **data}


def read(key: str) -> Any:
    """'at' is formatted when read, so 'Today' really means today."""
    with _lock:
        row = _data.get(key)
    if row is None:
        return None
    return {"at": fmt(row["_t"]) + " (local)", **{k: v for k, v in row.items() if k != "_t"}}
