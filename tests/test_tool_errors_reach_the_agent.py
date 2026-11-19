"""An error griot wrote for the agent reaches the agent; a crash does not.

From mcp 2.2 on, the SDK shows a client only the text of a ToolError (a
ResourceError for a resource); any other exception becomes the bare `Error
executing tool <name>`, its text kept in the server's log. griot raised no
ToolError, so every message it wrote for the agent (nothing indexed, keyword
search not built, the collection busy, the spend ceiling, the search deadline)
arrived as `Error executing tool griot_search` (seen in real use, 2026-10-25).

What is anticipated is said by the error's class (griot.GriotError), never by
converting everything: a crash keeps the SDK's masking, which is there so that
internals do not reach a client. Either way the call log keeps the ORIGINAL
class, which griot stats groups failures by.

Every test here goes through a real in-memory client: the conversion happens in
the SDK, and only the protocol shows what an agent receives."""

import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

import griot
from griot import common, logdb, mcp_server, repos


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


async def _search_error(**arguments) -> str:
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "anything", **arguments})
    assert result.is_error is True, result
    return _text(result)


def _recorded_error(tool: str) -> str:
    [call] = [c for c in logdb.read_tool_calls(common.LOG_DIR, days=1) if c["tool"] == tool]
    assert not call["ok"]
    return call["error"]


def _search_raises(monkeypatch, error: Exception) -> None:
    def search(*args, **kwargs):
        raise error
    monkeypatch.setattr(common, "search", search)


# --- anticipated: the agent reads griot's text ----------------------------------------------------


ANTICIPATED = [
    pytest.param(lambda: common.CollectionBusyError(
                     "codebase", common.QDRANT_PATH / "codebase",
                     "The collection is held by another griot process (PID 4242)."),
                 "CollectionBusyError", id="collection-busy"),
    pytest.param(lambda: common.SearchFilterError("No repository named 'gamma' has anything indexed."),
                 "SearchFilterError", id="refused-filter"),
    pytest.param(lambda: common.SearchModeUnavailable("Keyword search is not built yet: run `griot index keywords`."),
                 "SearchModeUnavailable", id="keyword-index-missing"),
    pytest.param(lambda: griot.ConfigurationError("GRIOT_SPEND_CEILING_USD takes a number (got 'lots')."),
                 "ConfigurationError", id="configuration"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("make, cls", ANTICIPATED)
async def test_an_anticipated_error_reaches_the_agent_with_its_text_and_is_logged_by_its_class(
        monkeypatch, make, cls):
    error = make()
    _search_raises(monkeypatch, error)

    text = await _search_error()

    assert str(error) in text, text
    assert _recorded_error("griot_search") == f"{cls}: {error}"


@pytest.mark.anyio
async def test_the_spend_circuit_breaker_reaches_the_agent(monkeypatch):
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 0.0)
    monkeypatch.setattr(common, "search", lambda *a, **k: common.check_spend_ceiling())

    text = await _search_error()

    assert "Local circuit breaker" in text and "GRIOT_SPEND_CEILING_USD" in text, text
    assert "Local circuit breaker" in _recorded_error("griot_search")


def test_every_way_the_breaker_stops_a_call_is_a_griot_error(monkeypatch):
    # The daily ceiling goes through the protocol above; the other two stops
    # raise the same class, so they reach the agent the same way.
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1000.0)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 0.0)
    with pytest.raises(griot.GriotError, match="Local circuit breaker: spend in the last"):
        common.check_spend_ceiling()
    with pytest.raises(griot.GriotError, match="which is not an amount"):
        common.record_spend(float("nan"))


def test_a_missing_gemini_token_is_a_griot_error(monkeypatch):
    # The gemini profile's call, with no token in the environment, the file
    # or the keychain (the suite's keychain holds nothing).
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "direct", "model": "gemini-embedding-001"})
    monkeypatch.setattr(common, "GEMINI_TOKEN", None)
    with pytest.raises(griot.GriotError, match="GEMINI_TOKEN not found"):
        common.embed_texts(["q"])


@pytest.mark.anyio
async def test_a_missing_embedding_credential_reaches_the_agent(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "direct", "request_style": "openai_compatible",
                                                    "api_key_env": "GRIOT_TEST_EMBED_KEY"})
    monkeypatch.setattr(common, "credential", lambda name: None)
    monkeypatch.setattr(common, "search", lambda *a, **k: common.embed_texts(["q"]))

    text = await _search_error()

    assert "GRIOT_TEST_EMBED_KEY not found" in text, text


@pytest.mark.anyio
async def test_a_query_that_could_not_be_embedded_reaches_the_agent(monkeypatch):
    # The real common.search, up to the point where the embedding API gave
    # nothing back (it said why through last_embedding_failure).
    monkeypatch.setattr(common, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(common, "search_filter", lambda *a, **k: None)
    monkeypatch.setattr(common, "embed_texts", lambda texts: [None for _ in texts])
    monkeypatch.setattr(common, "last_embedding_failure", lambda: "HTTP error 503 from the Gemini API")

    text = await _search_error(mode="vector")

    assert "could not be embedded" in text and "HTTP error 503" in text, text


@pytest.mark.anyio
async def test_the_search_deadline_reaches_the_agent(monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(common, "search", lambda *a, **k: release.wait(5) and [])
    try:
        text = await _search_error()
    finally:
        release.set()

    assert "did not finish within" in text and "GRIOT_SEARCH_TIMEOUT_SECONDS" in text, text


@pytest.mark.anyio
async def test_a_guidance_error_written_in_a_tool_reaches_the_agent():
    # griot_audit over an empty index: the deliberate "Index a repository
    # first", which arrived as `Error executing tool griot_audit` on mcp 2.2.
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_audit", {})

    assert result.is_error is True
    assert "Index a repository first" in _text(result), _text(result)
    assert _recorded_error("griot_audit").startswith("NothingIndexed: ")


@pytest.mark.anyio
async def test_an_argument_the_tool_refuses_reaches_the_agent():
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {"days": 0})

    assert result.is_error is True
    assert "days must be at least 1 (got 0)" in _text(result), _text(result)
    assert _recorded_error("griot_stats").startswith("ArgumentRefused: ")


@pytest.mark.anyio
async def test_a_sample_size_below_one_reaches_the_agent():
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {"sample_size": 0})

    assert result.is_error is True
    assert "sample_size must be at least 1 (got 0)" in _text(result), _text(result)


# --- a crash: masked for the agent, whole in the log ----------------------------------------------


CRASHES = [
    pytest.param(KeyError("payload['internal'] at /Users/you/private/path"), id="key-error"),
    # Not griot's text just because its class is one griot also uses.
    pytest.param(RuntimeError("qdrant: shard /Users/you/private/path is corrupted"), id="runtime-error"),
    pytest.param(ValueError("internal: could not convert '/Users/you/private/path'"), id="value-error"),
    pytest.param(OSError("[Errno 5] Input/output error: '/Users/you/private/path'"), id="os-error"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("error", CRASHES)
async def test_a_crash_keeps_the_sdk_masking_and_its_row_holds_the_real_class_and_text(monkeypatch, error):
    _search_raises(monkeypatch, error)

    text = await _search_error()

    assert "/Users/you/private/path" not in text, text
    assert "Error executing tool griot_search" in text, text
    assert _recorded_error("griot_search") == f"{type(error).__name__}: {error}"


@pytest.mark.anyio
async def test_griot_stats_groups_the_failures_by_their_original_class(monkeypatch):
    async with Client(mcp_server.mcp) as client:
        _search_raises(monkeypatch, common.SearchModeUnavailable("Keyword search is not built yet."))
        await client.call_tool("griot_search", {"query": "a"})
        _search_raises(monkeypatch, KeyError("internal"))
        await client.call_tool("griot_search", {"query": "b"})
        stats = await client.call_tool("griot_stats", {})

    assert stats.structured_content["tool_failure_classes"] == {"SearchModeUnavailable": 1, "KeyError": 1}


# --- a griot error that carries another error -----------------------------------------------------


PRIVATE = "qdrant: shard /Users/you/private/path is corrupted"


def _golden_set_that_stops(monkeypatch, error: Exception) -> None:
    """griot_quality_check with the self-check done and the curated half
    stopped by `error`."""
    from griot import quality_check
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda collection, sample_size: {"sampled": 2, "passed": 2, "failed": 0,
                                                         "avg_score": 0.99, "failures": []})
    monkeypatch.setattr(mcp_server, "_curated_cases_to_run",
                        lambda: ([{"query": "q", "must_include": [{"repo": "r"}]}], None))
    monkeypatch.setattr(quality_check, "record_for_trend", lambda *a, **k: None)

    def stops(cases):
        raise error
    monkeypatch.setattr(quality_check, "run_golden_set", stops)


async def _quality_check_error() -> str:
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {})
    assert result.is_error is True, result
    return _text(result)


@pytest.mark.anyio
async def test_a_crash_inside_the_golden_set_run_does_not_reach_the_agent(monkeypatch):
    # CheckIncomplete is griot's text, but what stopped the run was a crash:
    # its text stays masked, only its class is named.
    _golden_set_that_stops(monkeypatch, RuntimeError(PRIVATE))

    text = await _quality_check_error()

    assert "/Users/you/private/path" not in text, text
    assert "The self-check did run and is recorded" in text, text
    assert "RuntimeError" in text, text
    # The crash's text is not lost: the call log keeps it.
    recorded = _recorded_error("griot_quality_check")
    assert recorded.startswith("CheckIncomplete: "), recorded
    assert f"RuntimeError: {PRIVATE}" in recorded, recorded


@pytest.mark.anyio
async def test_a_griot_error_inside_the_golden_set_run_reaches_the_agent(monkeypatch):
    error = common.SpendCircuitBreakerError("Local circuit breaker: the daily ceiling was reached.")
    _golden_set_that_stops(monkeypatch, error)

    text = await _quality_check_error()

    assert str(error) in text, text
    assert "The self-check did run and is recorded" in text, text
    # Said once in the log row too: the cause is already in the text.
    assert _recorded_error("griot_quality_check").count(str(error)) == 1


@pytest.mark.anyio
async def test_a_cause_the_text_already_says_is_not_logged_twice():
    # A corrupt golden-set file: UnreadableFile carries the parse error on
    # purpose (it says where the file breaks), so the row has it once.
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text("{ not json")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_golden_set_list", {})

    assert result.is_error is True
    recorded = _recorded_error("griot_golden_set_list")
    assert recorded.startswith("UnreadableFile: "), recorded
    assert "[cause:" not in recorded, recorded


# --- resources -------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_resource_says_what_griot_wrote_for_its_reader():
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text("{ not json")
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(MCPError) as raised:
            await client.read_resource("griot://repos")

    assert "Could not read repos.json" in raised.value.error.message
    assert _recorded_error("griot://repos").startswith("UnreadableFile: Could not read repos.json")


@pytest.mark.anyio
async def test_a_resource_crash_keeps_the_sdk_masking(monkeypatch):
    def repo_status():
        raise KeyError("internal /Users/you/private/path")
    monkeypatch.setattr(repos, "repo_status", repo_status)
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(MCPError) as raised:
            await client.read_resource("griot://repos")

    message = raised.value.error.message
    assert "/Users/you/private/path" not in message, message
    assert "griot://repos" in message, message
    assert _recorded_error("griot://repos") == "KeyError: 'internal /Users/you/private/path'"
