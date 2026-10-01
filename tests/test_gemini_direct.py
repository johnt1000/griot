import requests

from griot import common


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def _direct_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "direct", "model": "gemini-embedding-2", "dim": 4, "price_per_1m_tokens": 0.20})
    monkeypatch.setattr(common, "EMBED_DIM", 4)


def test_embed_texts_direct_calls_batch_embed_contents_and_records_spend(monkeypatch, fake_gemini_token):
    _direct_profile(monkeypatch)
    captured = {}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(200, {
            "embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}, {"values": [0.5, 0.6, 0.7, 0.8]}],
            "usageMetadata": {"promptTokenCount": 100},
        })

    monkeypatch.setattr(common, "_http_post", fake_post)
    vectors = common.embed_texts(["text one", "text two"])

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert "gemini-embedding-2:batchEmbedContents" in captured["url"]
    assert len(captured["json"]["requests"]) == 2
    assert captured["json"]["requests"][0]["outputDimensionality"] == 4
    # 100 tokens * $0.20/1M = $0.00002
    assert common.get_spend_today() == 100 / 1_000_000 * 0.20


def test_embed_texts_direct_returns_none_list_on_http_error(monkeypatch, fake_gemini_token):
    _direct_profile(monkeypatch)

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        return FakeResponse(500, {})

    monkeypatch.setattr(common, "_http_post", fake_post)
    result = common.embed_texts(["a", "b", "c"])
    assert result == [None, None, None]


def test_embed_texts_direct_retries_on_429_then_succeeds(monkeypatch, fake_gemini_token):
    _direct_profile(monkeypatch)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(429, {})
        return FakeResponse(200, {"embeddings": [{"values": [1.0, 1.0, 1.0, 1.0]}], "usageMetadata": {"promptTokenCount": 5}})

    monkeypatch.setattr(common, "_http_post", fake_post)
    result = common.embed_texts(["text"])
    assert result == [[1.0, 1.0, 1.0, 1.0]]
    assert calls["n"] == 3


def test_embed_texts_direct_gives_up_after_max_rate_limit_retries(monkeypatch, fake_gemini_token):
    _direct_profile(monkeypatch)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        return FakeResponse(429, {})

    monkeypatch.setattr(common, "_http_post", fake_post)
    result = common.embed_texts(["a", "b"])
    assert result == [None, None]


def test_embed_texts_direct_requires_gemini_token(monkeypatch):
    _direct_profile(monkeypatch)
    monkeypatch.setattr(common, "GEMINI_TOKEN", None)
    try:
        common.embed_texts(["text"])
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "GEMINI_TOKEN" in str(e)


def test_chat_completion_returns_text_and_records_spend(monkeypatch, fake_gemini_token):
    captured = {}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(200, {
            "candidates": [{"content": {"parts": [{"text": "Paris."}]}}],
            "usageMetadata": {"totalTokenCount": 50},
        })

    monkeypatch.setattr(common, "_http_post", fake_post)
    answer = common.chat_completion("what is the capital of france?")

    assert answer == "Paris."
    assert "generateContent" in captured["url"]
    assert captured["json"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
    assert common.get_spend_today() == 50 / 1_000_000 * common.CHAT_PRICE_PER_1M_TOKENS


def test_chat_completion_uses_explicit_model_override(monkeypatch, fake_gemini_token):
    captured = {}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        captured["url"] = url
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(common, "_http_post", fake_post)
    common.chat_completion("question", model="gemini-flash-lite-latest")
    assert "gemini-flash-lite-latest" in captured["url"]


def test_chat_completion_raises_runtime_error_when_gemini_unavailable(monkeypatch, fake_gemini_token):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        return FakeResponse(500, {})

    monkeypatch.setattr(common, "_http_post", fake_post)
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert "Gemini" in str(e)


def test_chat_completion_respects_spend_ceiling(monkeypatch, fake_gemini_token):
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 0.0)  # already "blown"
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError from the circuit breaker"
    except RuntimeError as e:
        assert "Local circuit breaker" in str(e)
