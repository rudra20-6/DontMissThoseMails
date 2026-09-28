"""Minimal Gemini REST client (no SDK needed).

Used only for language work: turning a long email into structured facts + a short summary,
and (as a fallback) answering a decision when Jev is unavailable.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        s = get_settings()
        self.api_key = api_key if api_key is not None else s.gemini_api_key
        self.model = model or s.gemini_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, body: dict, retries: int = 2) -> dict:
        if not self.enabled:
            raise GeminiError("GEMINI_API_KEY is not configured")
        url = API.format(model=self.model)
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = httpx.post(url, params={"key": self.api_key}, json=body, timeout=60)
                if resp.status_code == 200:
                    return resp.json()
                last_err = GeminiError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code in (400, 401, 403, 404):
                    break
            except httpx.HTTPError as exc:
                last_err = exc
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
        raise GeminiError(str(last_err))

    @staticmethod
    def _text(payload: dict) -> str:
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiError(f"Unexpected Gemini response: {str(payload)[:300]}") from exc

    def generate_json(self, system: str, prompt: str, schema: dict[str, Any]) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        text = self._text(self._post(body))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiError(f"Gemini returned invalid JSON: {text[:300]}") from exc

    def generate_text(self, system: str, prompt: str, temperature: float = 0.4) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        return self._text(self._post(body)).strip()
