import httpx
import pytest

from app.clients import gemini as g
from app.clients.gemini import GeminiPool, LLMUnavailable, extract_json, rank_models

LIST = [
    "models/gemini-2.5-flash", "models/gemini-2.5-flash-lite", "models/gemini-3.1-flash-lite-preview",
    "models/gemini-3-flash-preview", "models/gemini-2.5-flash-image", "models/gemini-2.0-flash",
    "models/gemini-2.5-flash-preview-tts", "models/gemini-2.5-pro", "models/gemma-3-27b-it", "models/gemma-3-4b-it",
    "models/gemini-2.5-flash-lite-001", "models/text-embedding-004",
]


def test_rank_models_prefers_lite_newest_and_skips_non_text():
    ranked = rank_models(LIST, max_models=4, use_gemma=True)
    assert ranked == ["gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite", "gemini-3-flash-preview",
                      "gemini-2.5-flash", "gemma-3-27b-it"]


def test_extract_json_handles_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope this helps') == {"a": 2}


def _ok(text='{"x": 1}'):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _429(per_day=False, delay="7s"):
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


@pytest.fixture
def fake(monkeypatch):
    """Scripted responses per (key, model); records every call."""
    script: dict[tuple[str, str], list] = {}
    calls: list[tuple[str, str]] = []

    def post(url, headers, json, timeout):
        model = url.split("/models/")[1].split(":")[0]
        key = headers["x-goog-api-key"]
        calls.append((key, model))
        queue = script.get((key, model), [])
        return queue.pop(0) if queue else _ok()

    monkeypatch.setattr(g.httpx, "post", post)
    return script, calls


def test_rotates_models_then_keys(fake):
    script, calls = fake
    pool = GeminiPool(keys=["k1", "k2"], models=["lite", "flash"])
    script[("k1", "lite")] = [_429()]  # per-minute -> next model
    script[("k1", "flash")] = [_429(per_day=True)]  # per-day -> next key
    assert pool.generate_json("s", "p", {"type": "OBJECT"}) == {"x": 1}
    assert calls == [("k1", "lite"), ("k1", "flash"), ("k2", "lite")]
    st = pool.status()
    assert st[g._key_id("k1")]["models"]["flash"]["reason"] == "daily quota exhausted"
    assert st[g._key_id("k1")]["models"]["flash"]["cooldown_s"] > 60  # parked until midnight Pacific
    # next call skips the cooling slots entirely: k1/lite is cooling ~8s, k1/flash all day -> k2/lite
    calls.clear()
    pool.generate_json("s", "p", {"type": "OBJECT"})
    assert calls == [("k2", "lite")]


def test_all_exhausted_raises(fake):
    script, calls = fake
    pool = GeminiPool(keys=["k1"], models=["lite"])
    script[("k1", "lite")] = [_429(per_day=True)]
    with pytest.raises(LLMUnavailable):
        pool.generate_json("s", "p", {"type": "OBJECT"})
    calls.clear()
    with pytest.raises(LLMUnavailable):  # no HTTP call wasted while parked
        pool.generate_json("s", "p", {"type": "OBJECT"})
    assert calls == []


def test_daily_exhaustion_survives_restart(fake):
    script, calls = fake
    script[("k1", "lite")] = [_429(per_day=True)]
    with pytest.raises(LLMUnavailable):
        GeminiPool(keys=["k1"], models=["lite"]).generate_json("s", "p", {})
    calls.clear()
    with pytest.raises(LLMUnavailable):
        GeminiPool(keys=["k1"], models=["lite"]).generate_json("s", "p", {})  # fresh process, same DB
    assert calls == []


def test_thinking_config_rejected_is_retried_without(fake, monkeypatch):
    script, calls = fake
    bodies = []
    orig = g.httpx.post

    def spy(url, headers, json, timeout):
        bodies.append(json)
        return orig(url=url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(g.httpx, "post", spy)
    script[("k1", "gemini-2.5-flash-lite")] = [httpx.Response(400, json={"error": {"message": "thinking_budget is not supported"}})]
    pool = GeminiPool(keys=["k1"], models=["gemini-2.5-flash-lite"])
    assert pool.generate_json("s", "p", {}) == {"x": 1}
    assert "thinkingConfig" in bodies[0]["generationConfig"] and "thinkingConfig" not in bodies[1]["generationConfig"]


def test_gemma_gets_prompt_only_json(fake, monkeypatch):
    script, calls = fake
    seen = []
    orig = g.httpx.post
    monkeypatch.setattr(g.httpx, "post", lambda url, headers, json, timeout: (seen.append(json), orig(url=url, headers=headers, json=json, timeout=timeout))[1])
    script[("k1", "gemma-3-27b-it")] = [_ok('```json\n{"x": 2}\n```')]
    assert GeminiPool(keys=["k1"], models=["gemma-3-27b-it"]).generate_json("SYS", "P", {"type": "OBJECT"}) == {"x": 2}
    assert "systemInstruction" not in seen[0] and "responseSchema" not in seen[0]["generationConfig"]
    assert seen[0]["contents"][0]["parts"][0]["text"].startswith("SYS")


def test_invalid_key_skips_to_next_key(fake):
    script, calls = fake
    script[("bad", "lite")] = [httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})]
    pool = GeminiPool(keys=["bad", "good"], models=["lite", "flash"])
    pool.generate_json("s", "p", {})
    assert calls == [("bad", "lite"), ("good", "lite")]
