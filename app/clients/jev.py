"""Client for the Jev decision API (https://thejevai.com/docs).

Jev takes a `state` plus typed `questions` and returns typed answers:
  - choice: pick one option from a {name: description} criteria object
  - score:  rate on an ordered list of levels (low -> high); result is probability weighted
  - noul:   probability (0..1) that a yes/no statement is true
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class JevError(RuntimeError):
    pass


@dataclass
class Choice:
    instructions: str
    options: dict[str, str | None]

    def to_json(self) -> dict:
        return {"type": "choice", "instructions": self.instructions, "criteria": self.options}


@dataclass
class Score:
    instructions: str
    levels: list[str]  # ordered low -> high

    def to_json(self) -> dict:
        return {"type": "score", "instructions": self.instructions, "criteria": self.levels}


@dataclass
class Noul:
    instructions: str
    yes: str = ""
    no: str = ""

    def to_json(self) -> dict:
        body: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.yes:
            body["yes"] = self.yes
        if self.no:
            body["no"] = self.no
        return body


Question = Choice | Score | Noul


@dataclass
class Answers:
    """Normalised view of a Jev response."""

    raw: dict[str, Any] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)

    def choice(self, qid: str) -> tuple[str, float]:
        """Returns (selected option, its probability or confidence)."""
        ans = self.raw.get(qid) or {}
        selected = ans.get("choice")
        probs = ans.get("probabilities")
        if isinstance(selected, dict):  # tolerate {"name": ..} shapes
            selected = selected.get("name") or selected.get("choice")
        if not selected and isinstance(probs, dict) and probs:
            selected = max(probs, key=lambda k: probs[k] or 0)
        prob = None
        if isinstance(probs, dict) and selected in probs:
            prob = probs[selected]
        if prob is None:
            prob = ans.get("confidence", 1.0)
        q = self.questions.get(qid)
        if isinstance(q, Choice) and selected not in q.options:
            raise JevError(f"Jev returned unknown option {selected!r} for {qid}")
        return str(selected), float(prob or 0.0)

    def noul(self, qid: str) -> float:
        ans = self.raw.get(qid) or {}
        value = ans.get("noul")
        if value is None:
            raise JevError(f"Jev returned no noul value for {qid}")
        return float(value)

    def score(self, qid: str) -> float:
        """Returns the score as a level index on 0..len(levels)-1 (can be fractional)."""
        ans = self.raw.get(qid) or {}
        q = self.questions.get(qid)
        levels = q.levels if isinstance(q, Score) else []
        probs = ans.get("probabilities")
        # Prefer computing the expectation from per-level probabilities; it is unambiguous.
        if isinstance(probs, list) and levels and len(probs) == len(levels):
            total = sum(float(p or 0) for p in probs) or 1.0
            return sum(i * float(p or 0) for i, p in enumerate(probs)) / total
        if isinstance(probs, dict) and levels and set(probs) <= set(levels) | {str(i) for i in range(len(levels))}:
            total = sum(float(p or 0) for p in probs.values()) or 1.0
            acc = 0.0
            for key, p in probs.items():
                idx = levels.index(key) if key in levels else int(key)
                acc += idx * float(p or 0)
            return acc / total
        value = ans.get("score")
        if value is None:
            raise JevError(f"Jev returned no score for {qid}")
        return float(value)


class JevClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        s = get_settings()
        self.api_key = api_key if api_key is not None else s.jev_api_key
        self.base_url = (base_url or s.jev_api_base_url).rstrip("/")
        self.model = model or s.jev_model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def ask(self, state: Any, questions: dict[str, Question], retries: int = 2) -> Answers:
        if not self.enabled:
            raise JevError("JEV_API_KEY is not configured")
        body = {
            "model": self.model,
            "state": state,
            "questions": {qid: q.to_json() for qid, q in questions.items()},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/v1/systemone"
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = httpx.post(url, json=body, headers=headers, timeout=30)
            except httpx.HTTPError as exc:
                last_err = exc
            else:
                if resp.status_code in (401, 402, 400, 403, 422):
                    raise JevError(f"Jev HTTP {resp.status_code}: {resp.text[:300]}")
                if resp.status_code == 200:
                    payload = resp.json()
                    if payload.get("code", 0) != 0:
                        raise JevError(f"Jev error code {payload.get('code')}: {payload.get('message')}")
                    data = payload.get("data") or {}
                    result = data.get("result") or payload.get("result") or {}
                    answers = result.get("answers")
                    if not isinstance(answers, dict):
                        raise JevError("Jev response missing data.result.answers")
                    log.debug("Jev answers: %s (credits %s)", answers, data.get("creditsUsed"))
                    return Answers(raw=answers, questions=questions)
                last_err = JevError(f"Jev HTTP {resp.status_code}: {resp.text[:300]}")
            if attempt < retries:
                time.sleep(2 ** attempt)
        raise JevError(str(last_err))
