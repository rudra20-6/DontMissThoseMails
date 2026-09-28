"""Tiny in-memory diagnostics log behind /admin/whatsapp-check (resets when the app restarts)."""

from __future__ import annotations

import threading
from typing import Any

from app.timeutil import fmt, utcnow

_data: dict[str, dict] = {}
_lock = threading.Lock()


def record(key: str, **data: Any) -> None:
    with _lock:
        _data[key] = {"at": fmt(utcnow()) + " (local)", **data}


def read(key: str) -> Any:
    with _lock:
        return _data.get(key)
