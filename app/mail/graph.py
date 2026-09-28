"""Outlook / Microsoft 365 mail via Microsoft Graph (OAuth2 authorization-code flow).

One-time login: open  {APP_BASE_URL}/auth/microsoft/login?token={ADMIN_TOKEN}
The refresh token is stored in the database and refreshed automatically.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from app import kv
from app.config import get_settings
from app.db import session_scope
from app.mail.base import MailError, RawEmail
from app.textutil import clean, extract_urls, html_to_text
from app.timeutil import parse_local, utcnow

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
TOKENS_KEY = "ms_tokens"
CURSOR_KEY = "graph_cursor"


def _authority() -> str:
    return f"https://login.microsoftonline.com/{get_settings().ms_tenant}/oauth2/v2.0"


def redirect_uri() -> str:
    return get_settings().app_base_url.rstrip("/") + "/auth/microsoft/callback"


def build_login_url() -> tuple[str, str]:
    s = get_settings()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": s.ms_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "response_mode": "query",
        "scope": s.ms_scopes,
        "state": state,
        "prompt": "select_account",
    }
    return f"{_authority()}/authorize?{urlencode(params)}", state


def _token_request(data: dict) -> dict:
    s = get_settings()
    data = {"client_id": s.ms_client_id, "scope": s.ms_scopes, **data}
    if s.ms_client_secret:
        data["client_secret"] = s.ms_client_secret
    resp = httpx.post(f"{_authority()}/token", data=data, timeout=30)
    if resp.status_code != 200:
        raise MailError(f"Microsoft token error {resp.status_code}: {resp.text[:400]}")
    tok = resp.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    return tok


def exchange_code(code: str) -> None:
    tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})
    with session_scope() as s:
        kv.put(s, TOKENS_KEY, tok)


def is_connected() -> bool:
    with session_scope() as s:
        return bool((kv.get(s, TOKENS_KEY) or {}).get("refresh_token"))


def _access_token() -> str:
    with session_scope() as s:
        tok = kv.get(s, TOKENS_KEY) or {}
    if not tok.get("refresh_token"):
        raise MailError("Outlook is not connected. Visit /auth/microsoft/login?token=<ADMIN_TOKEN> once.")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    new.setdefault("refresh_token", tok["refresh_token"])  # Microsoft rotates it; keep old if absent
    with session_scope() as s:
        kv.put(s, TOKENS_KEY, new)
    return new["access_token"]


class GraphSource:
    def fetch_new(self) -> list[RawEmail]:
        s = get_settings()
        with session_scope() as db:
            cursor = kv.get(db, CURSOR_KEY)
        since = parse_local(cursor) if cursor else utcnow() - timedelta(hours=s.mail_lookback_hours)
        headers = {
            "Authorization": f"Bearer {_access_token()}",
            "Prefer": 'outlook.body-content-type="html"',
        }
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime asc",
            "$top": str(s.mail_max_per_poll),
            "$select": "id,internetMessageId,subject,from,receivedDateTime,body,webLink",
        }
        resp = httpx.get(f"{GRAPH}/me/mailFolders/inbox/messages", headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise MailError(f"Graph error {resp.status_code}: {resp.text[:400]}")
        out: list[RawEmail] = []
        for m in resp.json().get("value", []):
            body = m.get("body") or {}
            raw = body.get("content") or ""
            text = html_to_text(raw) if body.get("contentType", "").lower() == "html" else clean(raw)
            sender = (m.get("from") or {}).get("emailAddress") or {}
            out.append(
                RawEmail(
                    message_id=m.get("internetMessageId") or m["id"],
                    sender=f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
                    subject=m.get("subject") or "(no subject)",
                    received_at=parse_local(m.get("receivedDateTime")) or utcnow(),
                    body=text,
                    web_link=m.get("webLink") or "",
                    links=extract_urls(text),
                )
            )
        if out:
            newest = max(e.received_at for e in out)
            with session_scope() as db:
                kv.put(db, CURSOR_KEY, newest.isoformat() + "Z")
        return out
