"""FastAPI entrypoint: WhatsApp webhook, Microsoft login, cron + admin endpoints."""

from __future__ import annotations

import logging
from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import func, select

from app import diag, scheduler
from app.clients.gemini import get_pool
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
        pending = s.scalar(select(func.count()).select_from(Email).where(Email.action == "pending"))
        queued = s.scalar(select(func.count()).select_from(Outbox).where(Outbox.sent_at.is_(None)))
        paused = notifier.is_paused(s)
    return {
        "mail_provider": settings.mail_provider,
        "outlook_connected": graph.is_connected() if settings.mail_provider == "graph" else None,
        "whatsapp_configured": WhatsAppClient().configured,
        "gemini_keys": len(settings.gemini_keys),
        "emails_seen": emails,
        "emails_waiting_for_ai_quota": pending,
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
        diag.record("last_webhook_rejected", reason="bad X-Hub-Signature-256: WHATSAPP_APP_SECRET is wrong "
                                                    "(copy it again from App settings -> Basic, or leave it empty)")
        raise HTTPException(403, "bad signature")
    payload = await request.json()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for st in value.get("statuses", []) or []:
                # Meta reports async delivery failures here (e.g. 131047 window closed, 131026 undeliverable)
                if st.get("status") == "failed":
                    err = (st.get("errors") or [{}])[0]
                    log.error("WhatsApp delivery failed: %s", err)
                    diag.record("last_delivery_failure", code=err.get("code"), title=err.get("title"),
                                details=(err.get("error_data") or {}).get("details"))
            for msg in value.get("messages", []) or []:
                sender = "".join(ch for ch in msg.get("from", "") if ch.isdigit())
                accepted = sender == settings.whatsapp_recipient
                diag.record("last_webhook_message", from_number=sender, type=msg.get("type"), accepted=accepted,
                            reason="" if accepted else f"WHATSAPP_RECIPIENT is {settings.whatsapp_recipient!r}, "
                                                       f"message came from {sender!r}")
                if not accepted:
                    log.warning("Ignoring message from %s (WHATSAPP_RECIPIENT=%s)", sender, settings.whatsapp_recipient)
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


@app.get("clearsapp-check")
def whatsapp_check(token: str | None = Query(None), send: bool = False) -> dict:
    """Step-by-step WhatsApp diagnosis. Add &send=true to send a test message and see Meta's raw answer."""
    _check_admin(token)
    s = settings
    report: dict = {
        "1_config": {
            "WHATSAPP_TOKEN set": bool(s.whatsapp_token),
            "WHATSAPP_PHONE_NUMBER_ID": s.whatsapp_phone_number_id or "MISSING",
            "WHATSAPP_RECIPIENT": s.whatsapp_recipient or "MISSING",
            "recipient looks valid": s.whatsapp_recipient.isdigit() and 10 <= len(s.whatsapp_recipient) <= 15,
            "WHATSAPP_APP_SECRET set": bool(s.whatsapp_app_secret),
            "WHATSAPP_DRY_RUN": s.whatsapp_dry_run,
            "webhook URL to paste in Meta": s.app_base_url.rstrip("/") + "/webhook/whatsapp",
        }
    }
    try:
        r = httpx.get(
            f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating"},
            headers={"Authorization": f"Bearer {s.whatsapp_token}"},
            timeout=20,
        )
        report["2_token_and_phone_id"] = {"ok": r.status_code == 200, "meta_says": r.json()}
    except (httpx.HTTPError, ValueError) as exc:
        report["2_token_and_phone_id"] = {"ok": False, "error": str(exc)}
    report["3_webhook"] = {
        "last message received from WhatsApp": diag.read("last_webhook_message") or "NONE since the app last started: Meta has not delivered any message to this app",
        "last rejected webhook": diag.read("last_webhook_rejected"),
    }
    report["4_sending"] = {
        "last successful send": diag.read("last_send_ok"),
        "last send error": diag.read("last_send_error"),
        "last async delivery failure": diag.read("last_delivery_failure"),
    }
    with session_scope() as db:
        report["4_sending"]["messages queued in outbox"] = db.scalar(
            select(func.count()).select_from(Outbox).where(Outbox.sent_at.is_(None)))
    if send:
        try:
            WhatsAppClient().send_text("🧪 whatsapp-check test message from DontMissThoseMails")
            report["5_test_send"] = "accepted by Meta - check your phone"
        except Exception as exc:  # noqa: BLE001
            report["5_test_send"] = f"FAILED: {exc}"
    return report


@app.post("/admin/poll-now")
def admin_poll(background: BackgroundTasks, token: str | None = Query(None)) -> dict:
    _check_admin(token)
    background.add_task(scheduler.tick, True)
    return {"queued": True}


@app.get("/admin/llm")
def admin_llm(token: str | None = Query(None)) -> dict:
    """Which Gemini models each key rotates through, and their current cooldowns."""
    _check_admin(token)
    pool = get_pool()
    for key in pool.keys:
        pool.models_for(key)  # trigger discovery so the list is visible
    return pool.status()


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
