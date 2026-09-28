import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
        "WHATSAPP_DRY_RUN": "true",
        "WHATSAPP_RECIPIENT": "919999999999",
        "ADMIN_TOKEN": "test-admin",
        "RUN_SCHEDULER": "false",
        "GEMINI_API_KEYS": "",
        "GEMINI_API_KEY": "",
        "GEMINI_API_KEY_2": "",
        "QUIET_HOURS_START": "0",
        "QUIET_HOURS_END": "0",
        "TIMEZONE": "Asia/Kolkata",
    }
)

import pytest  # noqa: E402

from app.db import Base, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    from app.clients import gemini

    Base.metadata.drop_all(engine)
    init_db()
    gemini._pool = None
    yield
    gemini._pool = None


@pytest.fixture
def sent(monkeypatch):
    """Capture every WhatsApp message instead of sending it."""
    out: list[dict] = []
    from app.services import notifier

    monkeypatch.setattr(notifier, "_deliver", lambda payload: out.append(payload))
    return out
