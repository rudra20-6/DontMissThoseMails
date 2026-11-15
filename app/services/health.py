"""Tells the user when the bot can't do its job, and whether that means they might miss something.

Mail that can't be FETCHED is only delayed (it waits in the mailbox and is picked up later), so that alert
says "nothing lost yet". Info can really be missed when:
  * the AI is down so long that mail is handled by keyword rules (dates/links may be missed),
  * a mail fails to process at all,
  * no mail arrives for days (the Outlook forwarding rule probably stopped).
State lives in one KV row so alerts survive restarts and fire once per problem, not every tick.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import kv
from app.config import get_settings
from app.services import notifier
from app.timeutil import fmt, humanize_delta, utcnow

KEY = "health"
AUTH_DOWN_AFTER = timedelta(minutes=10)
REPEAT_EVERY = timedelta(hours=12)
AI_FOLLOWUP_EVERY = timedelta(hours=3)
_AUTH_WORDS = ("authenticat", "invalid credentials", "login", "password", "401", "403")


def _state(session: Session) -> dict:
    st = kv.get(session, KEY) or {}
    st.setdefault("first_seen", utcnow().isoformat())
    return st


def _save(session: Session, st: dict) -> None:
    kv.put(session, KEY, st)


def _t(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _since(value: str | None) -> timedelta:
    return utcnow() - _t(value) if value else timedelta(0)


def _duration(td: timedelta) -> str:
    return humanize_delta(utcnow() + td).removeprefix("in ")


# ---------------------------------------------------------------- recording (called by the pipeline)

def mail_ok(session: Session, fetched: int) -> None:
    st = _state(session)
    st.pop("mail_fail_since", None)
    st.pop("mail_error", None)
    if fetched:
        st["last_mail_at"] = utcnow().isoformat()
    _save(session, st)


def mail_error(session: Session, error: str) -> None:
    st = _state(session)
    st.setdefault("mail_fail_since", utcnow().isoformat())
    st["mail_error"] = error[:300]
    _save(session, st)


def ai_ok(session: Session) -> None:
    st = _state(session)
    st["ai_ok_at"] = utcnow().isoformat()
    _save(session, st)


def ai_fallback(session: Session, sender: str, subject: str, reason: str) -> None:
    st = _state(session)
    st.setdefault("ai_fallback", []).append({"from": sender[:80], "subject": subject[:120]})
    st["ai_reason"] = reason[:300]
    st["ai_fallback_at"] = utcnow().isoformat()
    _save(session, st)


def mail_failed(session: Session, sender: str, subject: str) -> None:
    st = _state(session)
    st.setdefault("failed_mails", []).append({"from": sender[:80], "subject": subject[:120]})
    _save(session, st)


# ---------------------------------------------------------------- explaining problems

def _mail_problem(error: str) -> tuple[bool, str, str]:
    """-> (is_auth, what went wrong, how to fix it)."""
    e = error.lower()
    if "not configured" in e:
        return True, "the mailbot login isn't set", "set IMAP_USERNAME and IMAP_PASSWORD on Render."
    if "not connected" in e or "token error" in e:
        return True, "the Outlook connection expired", "open /auth/microsoft/login?token=<ADMIN_TOKEN> once."
    if any(w in e for w in _AUTH_WORDS):
        return (True, "Gmail rejected the login, so the app password was probably revoked or changed",
                "make a new app password (myaccount.google.com/apppasswords) and put it in IMAP_PASSWORD on Render.")
    return False, f"I can't reach the mail server ({error[:120]})", "usually this fixes itself. If it doesn't, check /status."


def ai_reason() -> str:
    from app.clients import gemini

    pool = gemini.get_pool()
    if not pool.enabled:
        return "no Gemini API keys are set. Add GEMINI_API_KEYS on Render."
    status = pool.status()
    if status and all(k["key_cooldown_s"] > 0 for k in status.values()):
        return ("Gemini rejected your API key(s), so they may have been deleted. Make new ones at aistudio.google.com "
                "and update GEMINI_API_KEYS on Render.")
    reasons = [m.get("reason") or "" for k in status.values() for m in k["models"].values()]
    if reasons and all("daily" in r for r in reasons):
        reset = datetime.fromtimestamp(gemini._next_pacific_midnight(time.time()), timezone.utc).replace(tzinfo=None)
        return f"the free daily Gemini limit is used up on every key. It resets *{fmt(reset)}*."
    return "Gemini isn't responding right now."


def _mail_list(rows: list[dict], limit: int = 10) -> str:
    lines = [f"• *{r['subject']}*  _{r['from'].split('<')[0].strip()}_" for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"_…and {len(rows) - limit} more_")
    return "\n".join(lines)


# ---------------------------------------------------------------- the check (every tick)

def check(session: Session) -> int:
    s = get_settings()
    st = _state(session)
    sent = 0

    def send(text: str) -> None:
        nonlocal sent
        notifier.notify(session, text)
        sent += 1

    # 1) can't fetch mail: delayed, not lost (yet)
    fail_since = st.get("mail_fail_since")
    if fail_since:
        is_auth, what, fix = _mail_problem(st.get("mail_error", ""))
        threshold = AUTH_DOWN_AFTER if is_auth else timedelta(minutes=s.mail_down_alert_minutes)
        last = _t(st.get("mail_alerted_at"))
        if _since(fail_since) >= threshold and (last is None or utcnow() - last >= REPEAT_EVERY):
            send(f"⚠️ *I can't read your mail*  ·  _since {fmt(_t(fail_since))}_\n\n"
                 f"*Why:* {what}.\n\n"
                 "📭 Nothing is lost *yet*: new mail waits in the mailbot Gmail and I'll catch up once this works. "
                 "But until then you *may miss* deadlines and events, so keep an eye on Outlook.\n\n"
                 f"🔧 *Fix:* {fix}")
            st["mail_alerted_at"] = utcnow().isoformat()
            st["mail_down_from"] = fail_since
    elif st.get("mail_alerted_at"):
        down = _since(st.get("mail_down_from"))
        send(f"✅ *Mail is back.* I'm reading your inbox again (it was down {_duration(down)}). "
             "Anything that arrived meanwhile is being caught up now.")
        st.pop("mail_alerted_at", None)
        st.pop("mail_down_from", None)

    # 2) no mail at all for a long time: the forwarding rule probably broke, mail may never reach me
    hours = s.mail_silence_alert_hours
    quiet_for = _since(st.get("last_mail_at") or st["first_seen"])
    if hours and not fail_since and not st.get("silence_alerted_at") and quiet_for >= timedelta(hours=hours):
        send(f"🤔 *No new mail for {_duration(quiet_for)}*\n\n"
             "That's unusual. If your Outlook forwarding rule stopped (Microsoft sometimes turns off forwarding to "
             "outside addresses), your mail isn't reaching me and you *may lose* info.\n\n"
             "🔧 *Check:* send a test mail to your IIIT address. If it doesn't show up in the mailbot Gmail, "
             "set up the forwarding rule again (HOW_TO_RUN step 3).")
        st["silence_alerted_at"] = utcnow().isoformat()
    elif st.get("silence_alerted_at") and st.get("last_mail_at") and st["last_mail_at"] > st["silence_alerted_at"]:
        send("✅ *Mail is arriving again.* Thanks for checking!")
        st.pop("silence_alerted_at", None)

    # 3) mail handled without AI: details may have been missed
    fallback = st.get("ai_fallback") or []
    ai_alerted = _t(st.get("ai_alerted_at"))
    recovered = bool(st.get("ai_ok_at") and st["ai_ok_at"] > (st.get("ai_fallback_at") or ""))
    if fallback:
        head = None
        if recovered:
            head = "⚠️ *My AI was down for a while*, so I read these mails with basic keyword rules"
        elif ai_alerted is None:
            head = "⚠️ *My AI is down*, so I read these mails with basic keyword rules"
        elif utcnow() - ai_alerted >= AI_FOLLOWUP_EVERY:
            head = f"⚠️ *Still no AI:* {len(fallback)} more mail(s) read with keyword rules"
        if head:
            why = "" if recovered else f"\n\n*Why:* {st.get('ai_reason') or 'Gemini is unavailable.'}"
            back = "\n\n✅ It's working again now." if recovered else ""
            send(f"{head}. I *may have missed* deadlines, times or links in them. Please check them in Outlook:\n\n"
                 f"{_mail_list(fallback)}{why}{back}")
            st["ai_fallback"] = []
            if recovered:
                st.pop("ai_alerted_at", None)
            else:
                st["ai_alerted_at"] = utcnow().isoformat()
    elif ai_alerted and recovered:
        send("✅ *AI is back.* New mail is being read properly again.")
        st.pop("ai_alerted_at", None)

    # 4) mails that failed completely
    failed = st.get("failed_mails") or []
    if failed:
        send(f"⚠️ *I couldn't process {len(failed)} mail(s)*, so you *may miss* what's in them. "
             f"Please read them yourself:\n\n{_mail_list(failed)}")
        st["failed_mails"] = []

    _save(session, st)
    return sent


def digest_warnings(session: Session) -> list[str]:
    """Ongoing problems, repeated in the morning digest so they aren't forgotten."""
    st = kv.get(session, KEY) or {}
    out = []
    if st.get("mail_alerted_at") and st.get("mail_fail_since"):
        out.append(f"⚠️ _I still can't read your mail (since {fmt(_t(st['mail_fail_since']))}). "
                   "You may miss things, so check Outlook._")
    if st.get("silence_alerted_at"):
        out.append("⚠️ _Still no new mail arriving. Check your Outlook forwarding rule._")
    if st.get("ai_alerted_at") and not (st.get("ai_ok_at") and st["ai_ok_at"] > (st.get("ai_fallback_at") or "")):
        out.append("⚠️ _My AI is still down, so new mail is read with keyword rules._")
    return out
