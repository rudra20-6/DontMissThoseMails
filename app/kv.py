import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import KV


def get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(KV, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return row.value


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(KV, key)
    encoded = json.dumps(value, default=str)
    if row is None:
        session.add(KV(key=key, value=encoded))
    else:
        row.value = encoded
