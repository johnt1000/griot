"""Secret-leak regression (finding H1 from the security audit, 2026-08-19):
the Gemini key was sent as a query param (?key=...) — the str() of
requests' HTTPError/ConnectionError includes the full URL, so the key
leaked verbatim into logs/griot.log (0644), stdout, and tracebacks via
the interpolated messages in _gemini_post_with_retry and its callers.

Contract covered here:
1. The key goes in a header (x-goog-api-key), never in the URL/params.
2. Even if the underlying requests exception contains the key in its text
   (defense in depth — another provider/future regression), the
   GeminiUnavailable/RuntimeError/log message never repeats it.
"""

import requests

from griot import common

TOKEN = "fake-token-for-tests"  # value set by the fake_gemini_token fixture


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, url="https://fake/api"):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.url = url

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            # simulates real requests behavior: the HTTPError message
            # includes the full request URL (with query string)
            raise requests.HTTPError(
                f"{self.status_code} Server Error: Boom for url: {self.url}", response=self
            )


def _direct_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "direct", "model": "gemini-embedding-2", "dim": 4, "price_per_1m_tokens": 0.20})
    monkeypatch.setattr(common, "EMBED_DIM", 4)


def test_gemini_token_goes_in_header_not_url(monkeypatch, fake_gemini_token):
    captured = {}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        captured.update(url=url, params=params, headers=headers)
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {}})

    monkeypatch.setattr(requests, "post", fake_post)
    common.chat_completion("question")

    assert TOKEN not in captured["url"]
    assert not captured["params"] or TOKEN not in str(captured["params"])
    assert captured["headers"] is not None
    assert captured["headers"].get("x-goog-api-key") == TOKEN


def test_gemini_http_error_message_never_contains_token(monkeypatch, fake_gemini_token):
    """Even if the requests exception carries the key in its text (URL with
    ?key=...), the propagated GeminiUnavailable must not repeat it."""
    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        return FakeResponse(500, url=f"https://generativelanguage.googleapis.com/x?key={TOKEN}")

    monkeypatch.setattr(requests, "post", fake_post)
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert TOKEN not in str(e)
        assert TOKEN not in str(e.__cause__)


def test_gemini_connection_error_message_never_contains_token(monkeypatch, fake_gemini_token):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        raise requests.ConnectionError(f"Max retries exceeded with url: /x?key={TOKEN}")

    monkeypatch.setattr(requests, "post", fake_post)
    try:
        common.chat_completion("question")
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert TOKEN not in str(e)
        assert TOKEN not in str(e.__cause__)


def test_embed_error_log_line_never_contains_token(monkeypatch, fake_gemini_token, caplog):
    """embed_texts logs the error (log_and_print → logs/griot.log) instead of
    propagating it — the log line must not contain the key either."""
    import logging

    _direct_profile(monkeypatch)

    def fake_post(url, params=None, json=None, timeout=None, headers=None, allow_redirects=True):
        return FakeResponse(500, url=f"https://generativelanguage.googleapis.com/x?key={TOKEN}")

    monkeypatch.setattr(requests, "post", fake_post)
    with caplog.at_level(logging.WARNING, logger="griot"):
        result = common.embed_texts(["a"])

    assert result == [None]
    assert TOKEN not in caplog.text
