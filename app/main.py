"""FastAPI entrypoint: WhatsApp webhook, Microsoft login, cron + admin endpoints."""

from __future__ import annotations

import logging
from collections import deque
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import func, select

from app import scheduler
from app.clients.whatsapp import WhatsAppClient
from app.config import get_settings
from app.db import init_db, session_scope
from app.mail import graph
from app.models import Email, Item, Outbox
from app.services import notifier
from app.services.commands import handle_message
from app.services.digest import build_digest
from app.timeutil import fmt, utcnow

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

_seen_message_ids: deque[str] = deque(maxlen=500)  # Meta retries webhooks; dedupe
_oauth_states: deque[str] = deque(maxlen=20)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.run_scheduler:
        scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="DontMissThoseMails", lifespan=lifespan)


def _check_admin(token: str | None) -> None:
    if not token or token != settings.admin_token or settings.admin_token == "change-me":
        raise HTTPException(status_code=401, detail="bad or unset ADMIN_TOKEN")


# ---------------------------------------------------------------- health / status

@app.get("/health")
def health() -> dict:
    return {"ok": True, "time": utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<h2>DontMissThoseMails is running ✅</h2>"
        "<p>Status: <code>/status?token=ADMIN_TOKEN</code> · Outlook login: "
        "<code>/auth/microsoft/login?token=ADMIN_TOKEN</code></p>"
    )


@app.get("/status")
def status(token: str | None = Query(None)) -> dict:
    _check_admin(token)
    with session_scope() as s:
        counts = dict(s.execute(select(Item.status, func.count()).group_by(Item.status)).all())
        emails = s.scalar(select(func.count()).select_from(Email))
        queued = s.scalar(select(func.count()).select_from(Outbox).where(Outbox.sent_at.is_(None)))
        paused = notifier.is_paused(s)
    return {
        "mail_provider": settings.mail_provider,
        "outlook_connected": graph.is_connected() if settings.mail_provider == "graph" else None,
        "whatsapp_configured": WhatsAppClient().configured,
        "jev_configured": bool(settings.jev_api_key),
        "gemini_configured": bool(settings.gemini_api_key),
        "emails_processed": emails,
        "items_by_status": counts,
        "outbox_queued": queued,
        "paused": paused,
        "quiet_hours_now": notifier.is_quiet_now(),
    }


# ---------------------------------------------------------------- cron (for external pingers)

@app.api_route("/cron/tick", methods=["GET", "POST", "HEAD"])
def cron_tick(background: BackgroundTasks, token: str | None = Query(None)) -> dict:
    _check_admin(token)
    background.add_task(scheduler.tick)
    return {"queued": True}


# ---------------------------------------------------------------- Microsoft OAuth

@app.get("/auth/microsoft/login")
def ms_login(token: str | None = Query(None)):
    _check_admin(token)
    if not settings.ms_client_id:
        raise HTTPException(400, "MS_CLIENT_ID is not set")
    url, state = graph.build_login_url()
    _oauth_states.append(state)
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url={url}"><a href="{url}">Continue to Microsoft</a>')


@app.get("/auth/microsoft/callback", response_class=HTMLResponse)
def ms_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                error_description: str | None = None) -> str:
    if error:
        return f"<h3>Microsoft returned an error</h3><pre>{error}: {error_description}</pre>"
    if not code or state not in _oauth_states:
        raise HTTPException(400, "invalid OAuth state - start again from /auth/microsoft/login")
    graph.exchange_code(code)
    return "<h2>✅ Outlook connected!</h2><p>You can close this tab. New mail will be checked every few minutes.</p>"


# ---------------------------------------------------------------- WhatsApp webhook

@app.get("/webhook/whatsapp")
def wa_verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == settings.whatsapp_verify_token:
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_incoming(request: Request, background: BackgroundTasks) -> dict:
    raw = await request.body()
    if not WhatsAppClient().verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "bad signature")
    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                sender = "".join(ch for ch in msg.get("from", "") if ch.isdigit())
                if sender != settings.whatsapp_recipient:
                    log.warning("Ignoring message from unknown number %s", sender)
                    continue
                if msg.get("id") in _seen_message_ids:
                    continue
                _seen_message_ids.append(msg.get("id"))
                text, button = _extract(msg)
                if text or button:
                    background.add_task(_handle, text, button)
    return {"ok": True}


def _extract(msg: dict) -> tuple[str | None, str | None]:
    kind = msg.get("type")
    if kind == "text":
        return msg.get("text", {}).get("body"), None
    if kind == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title"), reply.get("id")
    if kind == "button":  # quick-reply on a template
        return msg.get("button", {}).get("text") or "show", None
    return None, None


def _handle(text: str | None, button: str | None) -> None:
    try:
        with session_scope() as s:
            handle_message(s, text=text, button_id=button)
    except Exception:  # noqa: BLE001
        log.exception("Failed handling WhatsApp message")


# ---------------------------------------------------------------- admin helpers

@app.post("/admin/test-whatsapp")
def admin_test(token: str | None = Query(None)) -> dict:
    _check_admin(token)
    with session_scope() as s:
        delivered = notifier.notify(s, "👋 Test message from DontMissThoseMails. Reply *help*!", urgent=True)
    return {"delivered_now": delivered}


@app.post("/admin/poll-now")
def admin_poll(background: BackgroundTasks, token: str | None = Query(None)) -> dict:
    _check_admin(token)
    background.add_task(scheduler.tick, True)
    return {"queued": True}


@app.get("/admin/digest", response_class=PlainTextResponse)
def admin_digest(token: str | None = Query(None)) -> str:
    _check_admin(token)
    with session_scope() as s:
        return build_digest(s)


@app.get("/admin/items")
def admin_items(token: str | None = Query(None), limit: int = 50) -> list[dict]:
    _check_admin(token)
    with session_scope() as s:
        rows = s.scalars(select(Item).order_by(Item.id.desc()).limit(limit)).all()
        return [
            {"id": i.id, "kind": i.kind, "status": i.status, "category": i.category, "title": i.title,
             "importance": round(i.importance, 2), "due": fmt(i.due_at), "event_start": fmt(i.event_start),
             "reg_deadline": fmt(i.reg_deadline)}
            for i in rows
        ]
