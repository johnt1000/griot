"""Tests for `common.CHAT_PROFILES`/`chat_completion()` for the non-Gemini
chat profiles (openai/deepseek/groq — all via the same generic adapter
`request_style="openai_compatible"` already used for the openai-small
embedding profile, reused here for chat). gemini remains the default and
the only "gemini_native" backend — its existing tests (test_gemini_direct.py)
must not break with this generalization.
"""

import requests

from griot import common


def _openai_chat_profile(monkeypatch, price=0.5):
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "openai")
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", {
        "backend": "openai_compatible_chat",
        "endpoint_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "api_key_env": "GRIOT_OPENAI_API_KEY",
        "price_per_1m_tokens": price,
    })


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_chat_profiles_includes_the_four_providers():
    assert set(common.CHAT_PROFILES) == {"gemini", "openai", "deepseek", "groq"}
    assert common.CHAT_PROFILES["gemini"]["backend"] == "gemini_native"
    for name in ("openai", "deepseek", "groq"):
        assert common.CHAT_PROFILES[name]["backend"] == "openai_compatible_chat"


def test_gemini_is_the_default_active_chat_profile():
    assert common.ACTIVE_CHAT_PROFILE_NAME == "gemini"
    assert common.ACTIVE_CHAT_PROFILE is common.CHAT_PROFILES["gemini"]


def test_chat_completion_openai_compatible_calls_endpoint_and_records_spend(monkeypatch):
    _openai_chat_profile(monkeypatch, price=1.0)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse(200, {
            "choices": [{"message": {"content": "Paris."}}],
            "usage": {"total_tokens": 100},
        })

    monkeypatch.setattr(requests, "post", fake_post)
    answer = common.chat_completion("what is the capital of france?")

    assert answer == "Paris."
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-fake"
    assert captured["json"] == {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "what is the capital of france?"}]}
    assert common.get_spend_today() == 100 / 1_000_000 * 1.0


def test_chat_completion_openai_compatible_model_override(monkeypatch):
    _openai_chat_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        captured["json"] = json
        return FakeResponse(200, {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 1}})

    monkeypatch.setattr(requests, "post", fake_post)
    common.chat_completion("question", model="gpt-4o")

    assert captured["json"]["model"] == "gpt-4o"


def test_chat_completion_openai_compatible_requires_api_key(monkeypatch):
    _openai_chat_profile(monkeypatch)
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    try:
        common.chat_completion("question")
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "GRIOT_OPENAI_API_KEY" in str(e)


def test_chat_completion_openai_compatible_requires_confirmed_price(monkeypatch):
    """Design finding (same discipline as/24 — never guess
    a price): openai/deepseek don't have price_per_1m_tokens hardcoded (None
    by default) — needs explicit confirmation before any real call, so the
    spend circuit breaker never stays silent."""
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "openai")
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", {
        "backend": "openai_compatible_chat",
        "endpoint_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "api_key_env": "GRIOT_OPENAI_API_KEY",
        "price_per_1m_tokens": None,
    })
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")
    try:
        common.chat_completion("question")
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "not confirmed" in str(e).lower()


def test_chat_completion_openai_compatible_raises_runtime_error_on_failure(monkeypatch):
    _openai_chat_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        return FakeResponse(500, {})

    monkeypatch.setattr(requests, "post", fake_post)
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert "openai" in str(e).lower()


def test_chat_completion_openai_compatible_respects_spend_ceiling(monkeypatch):
    _openai_chat_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 0.0)
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError from the circuit breaker"
    except RuntimeError as e:
        assert "Local circuit breaker" in str(e)


def test_groq_profile_defaults_to_free_price():
    """Groq's known free tier (rate-limited) — the only paid chat profile
    with a default price that doesn't require explicit confirmation, same
    spirit as decisions already made in the project (never guess a price,
    but here it's a reasonably well-known fact, not a guess)."""
    assert common.CHAT_PROFILES["groq"]["price_per_1m_tokens"] == 0.0


def test_optional_float_env_returns_none_when_unset(monkeypatch):
    """Helper that resolves the optional price for openai/deepseek — None
    when the env var isn't set is what makes CHAT_PROFILES require
    confirmation by default (without needing to reload the module to test
    this)."""
    monkeypatch.delenv("GRIOT_ALGUMA_COISA_QUE_NAO_EXISTE", raising=False)
    assert common._optional_float_env("GRIOT_ALGUMA_COISA_QUE_NAO_EXISTE") is None


def test_optional_float_env_returns_float_when_set(monkeypatch):
    monkeypatch.setenv("GRIOT_ALGUMA_COISA_QUE_NAO_EXISTE", "1.5")
    assert common._optional_float_env("GRIOT_ALGUMA_COISA_QUE_NAO_EXISTE") == 1.5


def test_openai_and_deepseek_chat_profiles_default_to_unconfirmed_price():
    """Confirms the real state of CHAT_PROFILES in this test process (without
    GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS/GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS
    set in the CI/dev environment) — never guess a price, same discipline as
   /24."""
    assert common.CHAT_PROFILES["openai"]["price_per_1m_tokens"] is None
    assert common.CHAT_PROFILES["deepseek"]["price_per_1m_tokens"] is None
