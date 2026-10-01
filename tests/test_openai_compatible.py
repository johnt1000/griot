"""Tests for the generic HTTP adapter `request_style="openai_compatible"`
(plan, section 9.2) — format `{model, input} -> data[].embedding`, reused
today by the `openai-small` profile and, in the future, by Voyage/the
`remote` backend. Same pattern of standing in for `common._http_post` as
test_gemini_direct.py — no real network calls.
"""

import requests

from griot import common


def _openai_small_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {
        "backend": "direct",
        "request_style": "openai_compatible",
        "endpoint_url": "https://api.openai.com/v1/embeddings",
        "model": "text-embedding-3-small",
        "dim": 4,
        "price_per_1m_tokens": 0.02,
        "api_key_env": "GRIOT_OPENAI_API_KEY",
    })
    monkeypatch.setattr(common, "EMBED_DIM", 4)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def test_embed_texts_openai_compatible_calls_endpoint_and_records_spend(monkeypatch):
    _openai_small_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse(200, {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
                {"index": 1, "embedding": [0.5, 0.6, 0.7, 0.8]},
            ],
            "usage": {"total_tokens": 100},
        })

    monkeypatch.setattr(common, "_http_post", fake_post)
    vectors = common.embed_texts(["text one", "text two"])

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer sk-fake-key"
    assert captured["json"] == {"model": "text-embedding-3-small", "input": ["text one", "text two"]}
    assert common.get_spend_today() == 100 / 1_000_000 * 0.02


def test_embed_texts_openai_compatible_respects_data_index_order(monkeypatch):
    """The API can return data[] out of order — matching is done by 'index',
    not by position in the list, unlike Gemini's batchEmbedContents (which
    has no index and depends on response order)."""
    _openai_small_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake-key")

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        return FakeResponse(200, {
            "data": [
                {"index": 1, "embedding": [9.0, 9.0, 9.0, 9.0]},
                {"index": 0, "embedding": [1.0, 1.0, 1.0, 1.0]},
            ],
            "usage": {"total_tokens": 10},
        })

    monkeypatch.setattr(common, "_http_post", fake_post)
    vectors = common.embed_texts(["primeiro", "segundo"])
    assert vectors == [[1.0, 1.0, 1.0, 1.0], [9.0, 9.0, 9.0, 9.0]]


def test_embed_texts_openai_compatible_returns_none_list_on_http_error(monkeypatch):
    _openai_small_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake-key")

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        return FakeResponse(500, {})

    monkeypatch.setattr(common, "_http_post", fake_post)
    result = common.embed_texts(["a", "b", "c"])
    assert result == [None, None, None]


def test_embed_texts_openai_compatible_retries_on_429_then_succeeds(monkeypatch):
    _openai_small_profile(monkeypatch)
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake-key")
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(429, {})
        return FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 1.0, 1.0, 1.0]}], "usage": {"total_tokens": 5}})

    monkeypatch.setattr(common, "_http_post", fake_post)
    result = common.embed_texts(["text"])
    assert result == [[1.0, 1.0, 1.0, 1.0]]
    assert calls["n"] == 3


def test_embed_texts_openai_compatible_requires_api_key_env(monkeypatch):
    _openai_small_profile(monkeypatch)
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    try:
        common.embed_texts(["text"])
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "GRIOT_OPENAI_API_KEY" in str(e)


def test_embed_texts_openai_compatible_passes_extra_params(monkeypatch):
    """extra_params from the profile (e.g. Voyage's input_type) needs to go
    in the request body — openai-small doesn't use it today, but the adapter
    is generic."""
    _openai_small_profile(monkeypatch)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {
        **common.ACTIVE_PROFILE,
        "extra_params": {"input_type": "document"},
    })
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-fake-key")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        captured["json"] = json
        return FakeResponse(200, {"data": [{"index": 0, "embedding": [1.0, 1.0, 1.0, 1.0]}], "usage": {"total_tokens": 5}})

    monkeypatch.setattr(common, "_http_post", fake_post)
    common.embed_texts(["text"])
    assert captured["json"]["input_type"] == "document"
