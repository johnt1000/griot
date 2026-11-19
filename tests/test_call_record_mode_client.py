"""What a tool-call row says about WHO asked and WHAT was asked.

Real use (2026-10): five griot_search calls failed with SearchModeUnavailable
and the log could not say which mode the agent had asked for (a failed search
is not in the queries table, and tool_calls kept no arguments). Two calls were
logged at ~2600 s because the machine slept. `failure_reasons` in the stats is
about indexing, so the failed searches did not show anywhere, and nothing said
which agent client made a call."""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from mcp.client.client import Client
from mcp.types import Implementation

from griot import common, logdb, mcp_server, stats

_INDEX_STATUS = {"points_count": 0, "last_indexed_at": None, "running": False, "orphan_lock": False}


def _call(tool="griot_search", ok=True, error=None, client=None, mode=None):
    return {"timestamp": "2026-08-21T10:00:00+00:00", "tool": tool, "ok": ok, "error": error,
            "client": client, "mode": mode}


# --- logdb: the two columns ---------------------------------------------------------------


def test_mode_and_client_round_trip_for_ok_and_failed_calls(tmp_path):
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, mode="hybrid", client="claude-code 2.1")
    logdb.write_tool_call(tmp_path, "griot_search", ok=False, error="SearchModeUnavailable: x",
                          mode="keyword", client="claude-code 2.1")
    logdb.write_tool_call(tmp_path, "griot_stats", ok=True)

    calls = logdb.read_tool_calls(tmp_path, days=1)

    assert [(c["ok"], c["mode"], c["client"]) for c in calls] == [
        (True, "hybrid", "claude-code 2.1"), (False, "keyword", "claude-code 2.1"), (True, None, None)]


def test_a_logs_db_made_before_the_columns_is_migrated_and_its_rows_read_as_null(tmp_path):
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db = sqlite3.connect(tmp_path / "logs.db")
    db.execute("CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
               "tool TEXT NOT NULL, ok INTEGER NOT NULL, duration_seconds REAL, error TEXT, project TEXT)")
    db.execute("INSERT INTO tool_calls (timestamp, tool, ok) VALUES (?, 'griot_search', 1)", (earlier,))
    db.commit()
    db.close()

    logdb.write_tool_call(tmp_path, "griot_search", ok=True, mode="vector", client="opencode 1.0")
    calls = logdb.read_tool_calls(tmp_path, days=36500)

    assert [(c["mode"], c["client"]) for c in calls] == [(None, None), ("vector", "opencode 1.0")]


def _old_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
                 "tool TEXT NOT NULL, ok INTEGER NOT NULL, duration_seconds REAL, error TEXT, project TEXT)")
    conn.execute("INSERT INTO tool_calls (timestamp, tool, ok) VALUES ('2999-01-01T00:00:00+00:00', 'griot_search', 1)")
    conn.commit()
    return conn


def test_a_database_that_cannot_be_written_is_still_readable_without_the_new_columns():
    conn = _old_conn()
    conn.execute("PRAGMA query_only = ON")

    assert logdb._ensure_tool_calls_column(conn, "mode") is False
    rows = logdb._tool_call_rows(conn, datetime(2000, 1, 1, tzinfo=timezone.utc))

    assert [(r["tool"], r["mode"], r["client"]) for r in rows] == [("griot_search", None, None)]


def test_asking_for_a_column_twice_is_harmless():
    conn = _old_conn()

    assert logdb._ensure_tool_calls_column(conn, "client") is True
    assert logdb._ensure_tool_calls_column(conn, "client") is True


def test_a_column_name_that_is_not_ours_is_refused():
    with pytest.raises(ValueError):
        logdb._ensure_tool_calls_column(_old_conn(), "x; DROP TABLE tool_calls")


# --- the MCP wrapper, through a real client ------------------------------------------------


@pytest.fixture
def cannot_run_keyword(monkeypatch):
    """A collection indexed before keyword vectors: an explicit keyword/hybrid
    search is refused, the default falls back to vector."""
    monkeypatch.setattr(common, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(common, "has_keyword_vectors", lambda *a, **k: False)


def _rows():
    return logdb.read_tool_calls(common.LOG_DIR, days=1)


@pytest.mark.anyio
async def test_a_failed_search_records_the_mode_it_asked_for_and_the_client(cannot_run_keyword):
    async with Client(mcp_server.mcp, client_info=Implementation(name="claude-code", version="2.1.0")) as client:
        result = await client.call_tool("griot_search", {"query": "secret question text", "mode": "keyword"})

    assert result.is_error is True
    row = _rows()[-1]
    assert (row["tool"], row["ok"], row["mode"], row["client"]) == ("griot_search", False, "keyword", "claude-code 2.1.0")
    assert row["error"].startswith("SearchModeUnavailable")
    assert "secret question text" not in json.dumps(_rows()), "the query text is never logged here"


@pytest.mark.anyio
async def test_a_search_without_a_mode_records_that_none_was_asked(cannot_run_keyword):
    async with Client(mcp_server.mcp, client_info=Implementation(name="claude-code", version="2.1.0")) as client:
        await client.call_tool("griot_search", {"query": "q"})  # fails later (no real index): still recorded

    row = _rows()[-1]
    assert row["tool"] == "griot_search" and row["mode"] == mcp_server.MODE_NOT_ASKED


def test_a_mode_that_is_not_one_of_the_modes_is_recorded_as_invalid_never_as_written(cannot_run_keyword):
    """Over the protocol the schema refuses such a mode before the tool runs;
    a direct call reaches the wrapper. Caller-chosen text is never written,
    and a literal "default" does not read as "no mode asked"."""
    assert mcp_server.MODE_NOT_ASKED not in common.SEARCH_MODES
    for bad in ("default", "x" * 500 + "\x1b[31m"):
        with pytest.raises(common.SearchFilterError):
            mcp_server.griot_search("q", mode=bad)

    rows = _rows()
    assert [r["mode"] for r in rows] == [mcp_server.MODE_INVALID] * 2
    assert mcp_server.MODE_INVALID != mcp_server.MODE_NOT_ASKED


@pytest.mark.anyio
async def test_two_overlapping_searches_each_record_their_own_mode(cannot_run_keyword, monkeypatch):
    """The asked mode travels in a contextvar: two calls in flight at once
    (the second starts before the first ends) must not read each other's."""
    import anyio
    import threading
    both_started = threading.Barrier(2, timeout=10)
    real = common.search_mode_for

    def meet_then_decide(query, mode):
        both_started.wait()  # neither call may finish before both have set their mode
        return real(query, mode)

    monkeypatch.setattr(common, "search_mode_for", meet_then_decide)
    async with Client(mcp_server.mcp, client_info=Implementation(name="claude-code", version="2.1.0")) as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: client.call_tool("griot_search", {"query": "q", "mode": "keyword"}))
            tg.start_soon(lambda: client.call_tool("griot_search", {"query": "q", "mode": "vector"}))

    by_mode = {r["mode"]: r for r in _rows()}
    assert set(by_mode) == {"keyword", "vector"}
    assert by_mode["keyword"]["error"].startswith("SearchModeUnavailable")
    assert not (by_mode["vector"]["error"] or "").startswith("SearchModeUnavailable")


@pytest.mark.anyio
async def test_only_search_records_a_mode_and_the_mode_does_not_leak_to_the_next_call(cannot_run_keyword):
    async with Client(mcp_server.mcp, client_info=Implementation(name="claude-code", version="2.1.0")) as client:
        await client.call_tool("griot_search", {"query": "q", "mode": "vector"})
        await client.call_tool("griot_spend_status", {})

    rows = _rows()
    assert rows[-2]["mode"] == "vector"
    assert rows[-1]["tool"] == "griot_spend_status" and rows[-1]["mode"] is None
    assert rows[-1]["client"] == "claude-code 2.1.0", "every tool records the client, not only search"


@pytest.mark.anyio
async def test_the_wrapper_adds_nothing_to_a_tools_input_schema():
    async with Client(mcp_server.mcp) as client:
        tools = (await client.list_tools()).tools

    for tool in tools:
        names = set(tool.input_schema.get("properties", {}))
        assert "ctx" not in names and not any(n.startswith("_griot") for n in names), tool.name
    search = next(t for t in tools if t.name == "griot_search")
    assert set(search.input_schema["properties"]) == {
        "query", "limit", "group_by_document", "repos", "source_types", "mode"}


@pytest.mark.anyio
async def test_a_tool_that_already_takes_a_context_still_gets_it_and_still_records_the_client():
    async with Client(mcp_server.mcp, client_info=Implementation(name="opencode", version="1.2")) as client:
        await client.call_tool("griot_repos_remove", {"path": "/Users/you/nothing"})  # asks, refuses: has a ctx

    row = _rows()[-1]
    assert row["tool"] == "griot_repos_remove" and row["client"] == "opencode 1.2"


@pytest.mark.anyio
async def test_the_client_label_is_printable_and_bounded():
    hostile = Implementation(name="evil\x1b[31m\nclient", version="9" * 500)
    async with Client(mcp_server.mcp, client_info=hostile) as client:
        await client.call_tool("griot_spend_status", {})

    label = _rows()[-1]["client"]
    assert label.isprintable() and len(label) <= 80


@pytest.mark.anyio
async def test_a_client_name_that_looks_like_a_credential_is_replaced():
    token = "".join(("ghp_", "Z9y8X7w6V5u4T3s2R1q0", "P9o8N7m6L5k4J3i2"))
    async with Client(mcp_server.mcp, client_info=Implementation(name=token, version="1")) as client:
        await client.call_tool("griot_spend_status", {})

    assert token not in _rows()[-1]["client"]


def test_a_mode_asked_by_one_direct_call_is_not_the_next_calls(cannot_run_keyword):
    with pytest.raises(common.SearchModeUnavailable):
        mcp_server.griot_search("q", mode="keyword")
    mcp_server.griot_spend_status()

    rows = _rows()
    assert rows[-2]["mode"] == "keyword" and rows[-1]["mode"] is None


def test_calling_a_tool_directly_has_no_client_and_works():
    mcp_server.griot_spend_status()

    assert _rows()[-1]["client"] is None


# --- durations ------------------------------------------------------------------------------


def test_a_jump_of_the_wall_clock_is_not_a_slow_call(monkeypatch):
    """A sleeping machine moves time.time() forward and not time.monotonic()
    (on macOS it stops during sleep): the call must measure the latter."""
    real = time.time
    state = {"n": 0}

    def jumping():
        state["n"] += 1
        return real() + (2600.0 if state["n"] > 1 else 0.0)

    monkeypatch.setattr(time, "time", jumping)

    mcp_server.griot_spend_status()

    assert 0 <= _rows()[-1]["duration_seconds"] < 5


@pytest.mark.anyio
async def test_an_async_call_ignores_a_wall_clock_jump_too(monkeypatch):
    real = time.time
    state = {"n": 0}

    def jumping():
        state["n"] += 1
        return real() + (2600.0 if state["n"] > 1 else 0.0)

    monkeypatch.setattr(time, "time", jumping)

    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_repos_remove", {"path": "/Users/you/nothing"})  # an async tool

    assert 0 <= _rows()[-1]["duration_seconds"] < 5


# --- griot stats ----------------------------------------------------------------------------


def test_stats_groups_failed_tool_calls_by_error_class_not_message():
    calls = [
        _call(ok=False, error="SearchModeUnavailable: Keyword search is not built yet for collection 'a'"),
        _call(ok=False, error="SearchModeUnavailable: Keyword search is not built yet for collection 'b'"),
        _call(ok=False, error="ValueError: days must be positive"),
        _call(ok=False, error="no colon at all"),
        _call(ok=False, error=None),
        _call(ok=True),
    ]

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls)

    assert result["tool_failure_classes"] == {
        "SearchModeUnavailable": 2, "ValueError": 1, "unknown": 2}  # no colon, and no error text at all
    assert result["failure_reasons"] == {}, "that field is about documents that failed to index"


def test_stats_counts_calls_by_client_with_null_rows_as_unknown():
    calls = [_call(client="claude-code 2.1"), _call(client="claude-code 2.1"), _call(client="opencode 1"),
             _call(client=None), {"timestamp": "2026-08-21T10:00:00+00:00", "tool": "griot_stats", "ok": True}]

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls)

    assert result["calls_by_client"] == {"claude-code 2.1": 2, "opencode 1": 1, "unknown": 2}


def test_resource_reads_are_in_neither_the_class_nor_the_client_counts():
    calls = [_call(client="claude-code 2.1"),
             _call(tool="griot://result-fields", ok=False, error="Boom: x", client="other 1")]

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls)

    assert result["calls_by_client"] == {"claude-code 2.1": 1}
    assert result["tool_failure_classes"] == {}
    assert result["resource_reads"] == {"griot://result-fields": 1}


def test_stats_counts_searches_by_asked_mode():
    calls = [_call(mode="keyword", ok=False), _call(mode="keyword", ok=False), _call(mode="default"),
             _call(mode=None), _call(tool="griot_stats", mode=None)]

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls)

    assert result["searches_by_asked_mode"] == {"keyword": 2, "default": 1, "unknown": 1}


def test_the_failures_and_clients_are_in_the_text_report():
    calls = [_call(ok=False, error="SearchModeUnavailable: nope", client="claude-code 2.1", mode="keyword"),
             _call(client="claude-code 2.1")]

    text = stats.format_stats(stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls), days=30)

    assert "1× SearchModeUnavailable" in text
    assert "claude-code 2.1" in text and "2 calls" in text


def test_a_report_with_no_tool_calls_prints_neither_section():
    text = stats.format_stats(stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[]), days=30)

    assert "by client" not in text and "failed calls by class" not in text


def test_error_text_in_the_report_is_only_the_class_so_a_message_cannot_forge_lines():
    calls = [_call(ok=False, error="Evil\x1b[2J: forged\nline")]

    text = stats.format_stats(stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls), days=30)

    assert "\x1b" not in text and "forged" not in text


@pytest.mark.anyio
async def test_griot_stats_over_the_protocol_carries_the_new_fields(cannot_run_keyword):
    """The StatsOutput TypedDict rejects a key it does not declare: a new
    field must survive the real tool, or the whole tool dies."""
    async with Client(mcp_server.mcp, client_info=Implementation(name="claude-code", version="2.1.0")) as client:
        await client.call_tool("griot_search", {"query": "q", "mode": "keyword"})
        result = await client.call_tool("griot_stats", {})

    assert result.is_error is False
    out = result.structured_content
    assert out["tool_failure_classes"] == {"SearchModeUnavailable": 1}
    assert out["calls_by_client"]["claude-code 2.1.0"] >= 1
    assert out["searches_by_asked_mode"] == {"keyword": 1}
    assert out["failure_reasons"] == {}


def test_the_cli_json_report_has_the_fields_for_old_rows_too(tmp_path):
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=False, error="SearchModeUnavailable: x")

    report = json.loads(json.dumps(stats.report(30)))

    assert report["tool_failure_classes"] == {"SearchModeUnavailable": 1}
    assert report["calls_by_client"] == {"unknown": 1}
