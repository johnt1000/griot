"""A message that carries another sentence ends with exactly one period.

The reason an embedding call failed can end its own sentence (an HTTP error
followed by credential_hint(), which closes with a period) or not (a 429
after N attempts). A message that appended "." after it read "..".  Each test
here pins the exact ending, for both kinds of reason."""

import pytest

from griot import common, mcp_server, quality_check


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code, self._payload, self.text = status_code, payload or {}, "error body"
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise common.requests.HTTPError(f"{self.status_code} Client Error", response=self)


@pytest.fixture
def openai_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)


def test_a_refused_credential_ends_the_search_error_with_one_period(openai_profile, monkeypatch):
    """The real path: a 401, whose reason ends with the credential hint."""
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Response(401))
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    hint = common.credential_hint("GRIOT_OPENAI_API_KEY")
    assert hint.endswith(".") and not hint.endswith("..")
    url = common.EMBED_PROFILES["openai-small"]["endpoint_url"]
    assert str(error.value) == (f"The query could not be embedded with profile 'openai-small': "
                                f"HTTP error 401 on {url}. {hint}")


def test_a_refused_gemini_credential_ends_the_search_error_with_one_period(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "gemini")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["gemini"])
    monkeypatch.setattr(common, "GEMINI_TOKEN", "not-a-real-key")
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Response(403))
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert str(error.value) == (f"The query could not be embedded with profile 'gemini': "
                                f"HTTP error 403 from the Gemini API. {common.credential_hint('GEMINI_TOKEN')}")
    assert str(error.value).endswith(".") and not str(error.value).endswith("..")


def test_a_reason_without_a_period_still_gets_one(openai_profile, monkeypatch):
    """The other kind of reason: the rate limit says nothing after it."""
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Response(429))
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    url = common.EMBED_PROFILES["openai-small"]["endpoint_url"]
    assert str(error.value) == (f"The query could not be embedded with profile 'openai-small': "
                                f"persistent 429 after 5 attempts on {url}.")


def test_no_reason_at_all_ends_with_one_period(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts: [None for _ in texts])
    common._note_embedding_failure(None)
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert str(error.value).endswith(": the embedding call returned nothing.")


@pytest.mark.parametrize("text, expected", [
    ("a reason", "a reason."),
    ("a reason.", "a reason."),
    ("a reason. ", "a reason."),
    ("is it?", "is it?"),
    ("stop!", "stop!"),
])
def test_a_sentence_ends_with_exactly_one_mark(text, expected):
    assert common.sentence(text) == expected


# --- the MCP quality check carries the search error inside its own --------------------


SEARCH_ERROR = ("The query could not be embedded with profile 'openai-small': HTTP error 401 on "
                "https://api.example.invalid/v1/embeddings. GRIOT_OPENAI_API_KEY is not set; set it with "
                "`griot auth set openai`.")


def _golden_set_that_stops(monkeypatch, error):
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda collection, sample_size: {"sampled": 2, "passed": 2, "failed": 0,
                                                         "avg_score": 0.99, "failures": []})
    monkeypatch.setattr(mcp_server, "_curated_cases_to_run",
                        lambda: ([{"query": "q", "must_include": [{"repo": "r"}]}], None))
    monkeypatch.setattr(quality_check, "_record_for_trend", lambda *a, **k: None)

    def stops(cases):
        raise RuntimeError(error)

    monkeypatch.setattr(quality_check, "run_golden_set", stops)


@pytest.mark.parametrize("error, carried", [
    (SEARCH_ERROR, SEARCH_ERROR),
    ("spend ceiling reached", "spend ceiling reached."),
])
def test_the_stopped_golden_set_carries_the_error_with_one_period(monkeypatch, error, carried):
    _golden_set_that_stops(monkeypatch, error)
    with pytest.raises(RuntimeError) as raised:
        mcp_server.griot_quality_check()
    assert str(raised.value).startswith(
        f"The curated golden set stopped before it finished: {carried} The self-check did run")
    assert ".." not in str(raised.value)


@pytest.mark.anyio
async def test_the_stopped_golden_set_reads_one_period_through_the_protocol(monkeypatch):
    from mcp.client.client import Client
    _golden_set_that_stops(monkeypatch, SEARCH_ERROR)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {})
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert result.is_error is True
    assert f"stopped before it finished: {SEARCH_ERROR} The self-check did run" in text
    assert ".." not in text
