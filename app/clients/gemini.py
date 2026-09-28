"""Gemini client that rotates over free-tier models and API keys.

Order of attempts:  key 1 -> every lightweight model (flash-lite first) -> key 2 -> ... -> Gemma fallback.

* Models are discovered per key with ListModels (so new/retired model names are picked up automatically),
  or pinned with GEMINI_MODELS.
* Every (key, model) pair has its own local requests-per-minute limiter, so we rarely even hit a 429.
* A 429 is classified from the error details: a *per-minute* quota cools that model for `retryDelay`;
  a *per-day* quota parks it until the next midnight Pacific time (when Google resets daily quotas).
  Daily exhaustion is persisted in the database so restarts / Render sleep don't waste requests.
* When nothing is left, `LLMUnavailable` is raised and callers keep the work queued for later.

NOTE: free-tier quota is per Google Cloud *project*. Two keys only double the quota if they were created
in two different projects (AI Studio -> Get API key -> Create API key in new project).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"
PACIFIC = ZoneInfo("America/Los_Angeles")
EXHAUSTED_KV = "gemini_exhausted"
DISCOVERY_TTL = 12 * 3600

# Used only if ListModels fails. "-latest" aliases always point at the current model of that tier.
FALLBACK_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemma-3-27b-it",
]
_EXCLUDE = ("image", "tts", "audio", "live", "embedding", "native", "robotics", "computer", "thinking", "-exp", "learnlm")


class LLMUnavailable(RuntimeError):
    """Every key/model is rate-limited or failing right now."""


class GeminiError(RuntimeError):
    pass


def _key_id(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def _next_pacific_midnight(now: float) -> float:
    dt = datetime.fromtimestamp(now, PACIFIC)
    nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp() + 60


def is_gemma(model: str) -> bool:
    return model.startswith("gemma")


def rank_models(names: list[str], max_models: int, use_gemma: bool) -> list[str]:
    """Pick lightweight text models from a ListModels result. Flash-Lite first, newest version first."""
    flash: dict[tuple[int, float], tuple[bool, str]] = {}
    gemma: list[tuple[float, int, str]] = []
    for name in names:
        n = name.removeprefix("models/")
        if any(x in n for x in _EXCLUDE):
            continue
        m = re.match(r"^gemini-(\d+(?:\.\d+)?)-(flash-lite|flash)(?:-(.+))?$", n)
        if m:
            version, tier, suffix = float(m.group(1)), 0 if m.group(2) == "flash-lite" else 1, m.group(3)
            if version < 2.5 or (suffix and re.fullmatch(r"\d{3}", suffix)):
                continue  # retired generations and pinned duplicates of a stable alias
            preview = bool(suffix)
            slot = (tier, version)
            if slot not in flash or (flash[slot][0] and not preview):
                flash[slot] = (preview, n)  # prefer the stable name over a preview of the same version
            continue
        g = re.match(r"^gemma-(\d+(?:\.\d+)?)-(\d+)b(?:-a\d+b)?-it$", n)
        if g:
            gemma.append((float(g.group(1)), int(g.group(2)), n))
    ordered = [flash[s][1] for s in sorted(flash, key=lambda s: (s[0], -s[1]))][:max_models]
    if use_gemma and gemma:
        ordered.append(sorted(gemma, reverse=True)[0][2])
    return ordered


def extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise GeminiError(f"no JSON object in model output: {text[:200]}")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise GeminiError("model output JSON is not an object")
    return value


@dataclass
class _Slot:
    cool_until: float = 0.0
    reason: str = ""
    calls: deque = field(default_factory=lambda: deque(maxlen=100))
    ok: int = 0
    failed: int = 0


class GeminiPool:
    def __init__(self, keys: list[str] | None = None, models: list[str] | None = None):
        s = get_settings()
        self.keys = keys if keys is not None else s.gemini_keys
        self._pinned = models if models is not None else s.gemini_model_list
        self._models: dict[str, tuple[float, list[str]]] = {}
        self._slots: dict[tuple[str, str], _Slot] = {}
        self._key_cool: dict[str, float] = {}
        self._no_thinking: set[str] = set()
        self._lock = threading.Lock()
        self._loaded_persisted = False

    @property
    def enabled(self) -> bool:
        return bool(self.keys)

    # ------------------------------------------------------------------ models
    def models_for(self, key: str) -> list[str]:
        if self._pinned:
            return self._pinned
        kid = _key_id(key)
        cached = self._models.get(kid)
        if cached and time.time() - cached[0] < DISCOVERY_TTL:
            return cached[1]
        s = get_settings()
        try:
            resp = httpx.get(f"{BASE}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=20)
            resp.raise_for_status()
            names = [m["name"] for m in resp.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            models = rank_models(names, s.gemini_max_models, s.gemini_use_gemma) or FALLBACK_MODELS
            log.info("Gemini key %s models: %s", kid, models)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Gemini ListModels failed for key %s (%s); using fallback list", kid, exc)
            models = FALLBACK_MODELS
        self._models[kid] = (time.time(), models)
        return models

    # ------------------------------------------------------------------ state
    def _slot(self, key: str, model: str) -> _Slot:
        return self._slots.setdefault((_key_id(key), model), _Slot())

    def _load_persisted(self) -> None:
        if self._loaded_persisted:
            return
        self._loaded_persisted = True
        try:
            from app import kv
            from app.db import session_scope

            with session_scope() as db:
                data = kv.get(db, EXHAUSTED_KV, {}) or {}
            now = time.time()
            for slot_id, until in data.items():
                if until > now and ":" in slot_id:
                    kid, model = slot_id.split(":", 1)
                    self._slots.setdefault((kid, model), _Slot()).cool_until = until
                    self._slots[(kid, model)].reason = "daily quota (persisted)"
        except Exception as exc:  # noqa: BLE001 - DB not ready is fine
            log.debug("could not load persisted Gemini cooldowns: %s", exc)

    def _persist_daily(self, key: str, model: str, until: float) -> None:
        try:
            from app import kv
            from app.db import session_scope

            with session_scope() as db:
                data = {k: v for k, v in (kv.get(db, EXHAUSTED_KV, {}) or {}).items() if v > time.time()}
                data[f"{_key_id(key)}:{model}"] = until
                kv.put(db, EXHAUSTED_KV, data)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not persist Gemini cooldown: %s", exc)

    def _cool(self, key: str, model: str, seconds: float, reason: str) -> None:
        with self._lock:
            slot = self._slot(key, model)
            slot.cool_until = max(slot.cool_until, time.time() + seconds)
            slot.reason = reason
            slot.failed += 1
        log.info("Gemini %s on key %s cooling %.0fs: %s", model, _key_id(key), seconds, reason)

    def _reserve(self, key: str, model: str) -> bool:
        """Local RPM limiter. Waits a few seconds if that frees a slot; otherwise says 'try the next one'."""
        s = get_settings()
        rpm = s.gemini_gemma_rpm if is_gemma(model) else s.gemini_rpm
        while True:
            with self._lock:
                slot = self._slot(key, model)
                now = time.time()
                if slot.cool_until > now:
                    return False
                while slot.calls and now - slot.calls[0] > 60:
                    slot.calls.popleft()
                if len(slot.calls) < rpm:
                    slot.calls.append(now)
                    return True
                wait = 60 - (now - slot.calls[0]) + 0.5
            if wait > s.gemini_max_wait_seconds:
                return False
            time.sleep(wait)

    def status(self) -> dict:
        now = time.time()
        out = {}
        for key in self.keys:
            kid = _key_id(key)
            models = self._models.get(kid, (0, self._pinned or []))[1]
            out[kid] = {
                "key_cooldown_s": max(0, round(self._key_cool.get(kid, 0) - now)),
                "models": {
                    m: {
                        "cooldown_s": max(0, round(self._slots.get((kid, m), _Slot()).cool_until - now)),
                        "reason": self._slots.get((kid, m), _Slot()).reason,
                        "ok": self._slots.get((kid, m), _Slot()).ok,
                        "failed": self._slots.get((kid, m), _Slot()).failed,
                    }
                    for m in models
                },
            }
        return out

    # ------------------------------------------------------------------ requests
    def _body(self, model: str, system: str, prompt: str, schema: dict | None, max_tokens: int) -> dict:
        gen: dict[str, Any] = {"temperature": 0.1, "maxOutputTokens": max_tokens}
        contents_text = prompt
        body: dict[str, Any] = {}
        if is_gemma(model):
            # Gemma on the Gemini API: no system instruction / schema support -> put everything in the prompt.
            contents_text = f"{system}\n\n{prompt}"
            if schema is not None:
                contents_text += ("\n\nRespond with ONLY a JSON object (no markdown) matching this schema:\n"
                                  + json.dumps(schema))
        else:
            body["systemInstruction"] = {"parts": [{"text": system}]}
            if schema is not None:
                gen["responseMimeType"] = "application/json"
                gen["responseSchema"] = schema
            if model not in self._no_thinking:
                # Thinking burns tokens + latency and isn't needed for extraction/classification.
                if "2.5" in model:
                    gen["thinkingConfig"] = {"thinkingBudget": 0}
                elif re.search(r"gemini-[3-9]", model):
                    gen["thinkingConfig"] = {"thinkingLevel": "low"}
        body["contents"] = [{"role": "user", "parts": [{"text": contents_text}]}]
        body["generationConfig"] = gen
        return body

    def _handle_error(self, key: str, model: str, resp: httpx.Response) -> str:
        """Returns 'retry_same', 'next_model' or 'next_key'."""
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        msg = str(err.get("message", resp.text))[:300]
        code = resp.status_code
        if code == 429:
            quota_ids, retry = [], 60.0
            for d in err.get("details", []) or []:
                t = d.get("@type", "")
                if t.endswith("QuotaFailure"):
                    quota_ids += [v.get("quotaId", "") for v in d.get("violations", [])]
                elif t.endswith("RetryInfo"):
                    m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
                    if m:
                        retry = float(m.group(1)) + 1
            if "limit: 0" in msg:
                self._cool(key, model, 24 * 3600, "model has no free-tier quota")
            elif any("PerDay" in q or "Daily" in q for q in quota_ids):
                until = _next_pacific_midnight(time.time())
                self._cool(key, model, until - time.time(), "daily quota exhausted")
                self._persist_daily(key, model, until)
            else:
                self._cool(key, model, min(retry, 120), "per-minute quota")
            return "next_model"
        if code in (401, 403) or "API_KEY_INVALID" in msg or "API key not valid" in msg:
            with self._lock:
                self._key_cool[_key_id(key)] = time.time() + 3600
            log.error("Gemini key %s rejected (%s): %s", _key_id(key), code, msg)
            return "next_key"
        if code == 400 and "thinking" in msg.lower() and model not in self._no_thinking:
            self._no_thinking.add(model)
            return "retry_same"
        if code == 404 or (code == 400 and re.search(r"not (found|supported)|is not available|unsupported", msg, re.I)):
            self._cool(key, model, 24 * 3600, f"model unavailable: {msg[:80]}")
            return "next_model"
        if code >= 500:
            self._cool(key, model, 30, f"server error {code}")
            return "next_model"
        self._cool(key, model, 300, f"HTTP {code}: {msg[:120]}")
        return "next_model"

    def _generate(self, system: str, prompt: str, schema: dict | None, max_tokens: int) -> tuple[str, str, str]:
        if not self.enabled:
            raise LLMUnavailable("no GEMINI_API_KEYS configured")
        self._load_persisted()
        for key in self.keys:
            if self._key_cool.get(_key_id(key), 0) > time.time():
                continue
            for model in self.models_for(key):
                attempts = 0
                while attempts < 2 and self._reserve(key, model):
                    attempts += 1
                    try:
                        resp = httpx.post(
                            f"{BASE}/models/{model}:generateContent",
                            headers={"x-goog-api-key": key},
                            json=self._body(model, system, prompt, schema, max_tokens),
                            timeout=60,
                        )
                    except httpx.HTTPError as exc:
                        self._cool(key, model, 30, f"network: {exc}")
                        break
                    if resp.status_code != 200:
                        if self._handle_error(key, model, resp) == "retry_same":
                            continue
                        break  # next model (or next key, if this key was rejected)
                    try:
                        cand = resp.json()["candidates"][0]
                        text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
                    except (KeyError, IndexError, TypeError, ValueError):
                        self._cool(key, model, 5, "empty/blocked response")
                        break
                    with self._lock:
                        self._slot(key, model).ok += 1
                    return text, model, key
                if self._key_cool.get(_key_id(key), 0) > time.time():
                    break  # key rejected: skip its remaining models
        raise LLMUnavailable("all Gemini keys/models are rate-limited or failing")

    def generate_json(self, system: str, prompt: str, schema: dict, max_tokens: int = 2048) -> dict:
        last: Exception | None = None
        for _ in range(3):  # a model returning malformed JSON -> another attempt (usually the next model)
            text, model, key = self._generate(system, prompt, schema, max_tokens)
            try:
                return extract_json(text)
            except (GeminiError, json.JSONDecodeError) as exc:
                last = exc
                self._cool(key, model, 60, "malformed JSON")
        raise LLMUnavailable(f"models kept returning malformed JSON: {last}")

    def generate_text(self, system: str, prompt: str, max_tokens: int = 1024) -> str:
        return self._generate(system, prompt, None, max_tokens)[0].strip()


_pool: GeminiPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> GeminiPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = GeminiPool()
        return _pool
