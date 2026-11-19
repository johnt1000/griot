"""A search that cannot finish fails in bounded time, and says so.

[real failure, 2026-10-25] Two parallel griot_search calls took 2537s and 2660s
and returned 0.14s apart, while the surrounding calls took 1 to 2 seconds; the
agent client had given up after about two minutes and nothing was logged until
they returned. The likely cause is the machine sleeping, but the code also had
waits with no limit (get_client() took its lock with timeout=-1). These tests
hold the waits open on purpose and read what the caller gets."""

import threading
import time

import pytest
from mcp.client.client import Client

from griot import common, config, logdb, mcp_server


# --- common.get_client(wait=True) ------------------------------------------------


def test_get_client_gives_up_on_a_lock_held_too_long(monkeypatch):
    monkeypatch.setattr(common, "CLIENT_LOCK_WAIT_SECONDS", 0.2)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with common._client_lock:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(common.CollectionBusyError, match="being opened by another call") as caught:
            common.get_client()
        assert time.monotonic() - started < 5
        assert caught.value.in_this_process is True
    finally:
        release.set()
        holder.join(5)


def test_a_status_read_still_gives_up_after_its_short_patience_not_the_long_limit(monkeypatch):
    monkeypatch.setattr(common, "CLIENT_LOCK_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(common, "_STATUS_READ_PATIENCE", 0.1)
    holding, release = threading.Event(), threading.Event()

    def hold():
        with common._client_lock:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        with pytest.raises(common.CollectionBusyError):
            common.get_client(wait=False)
        assert time.monotonic() - started < 5
    finally:
        release.set()
        holder.join(5)


def test_the_lock_wait_outlasts_the_load_retry_budget():
    """A first open legitimately holds the lock for the whole backoff of
    _load_shard_with_retry plus the load itself; a shorter limit would turn
    that into a spurious failure for every caller waiting behind it."""
    assert common.CLIENT_LOCK_WAIT_SECONDS > sum(common._LOCK_RETRY_DELAYS) * 2


# --- the setting ----------------------------------------------------------------------


def test_the_deadline_setting_is_known_to_the_template_and_the_config_command():
    entry = next(e for e in common.ENV_TEMPLATE_SETTINGS if e[0] == "GRIOT_SEARCH_TIMEOUT_SECONDS")
    assert entry[1] == "60"
    assert config.find("GRIOT_SEARCH_TIMEOUT_SECONDS") is not None
    assert common.SEARCH_TIMEOUT_SECONDS == 60


# --- griot_search ----------------------------------------------------------------------


def _slow_search(release):
    def fake_search(query, limit=5, group_by_document=False, **filters):
        release.wait(10)
        return []
    return fake_search


def test_a_search_that_does_not_finish_raises_in_bounded_time_and_is_recorded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(common, "search", _slow_search(release))
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as caught:
            mcp_server.griot_search(query="x")
        assert time.monotonic() - started < 5
        text = str(caught.value)
        assert "did not finish" in text and "0.3" in text
        assert "asleep" in text
        assert "busy" in text
        assert "retry" in text.lower()
        assert "mode=vector needs no keyword index" in text
        call = logdb.read_tool_calls(common.LOG_DIR, days=1)[0]
        assert call["tool"] == "griot_search" and not call["ok"]
        assert "did not finish" in call["error"]
    finally:
        release.set()


def test_the_in_flight_count_returns_to_zero_once_the_abandoned_search_ends(monkeypatch):
    """The abandoned search still uses the shard, so it keeps its own count
    (the idle reaper must not close the shard under it); the count drops the
    moment that search ends, so nothing holds the collection afterwards."""
    release = threading.Event()
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(common, "search", _slow_search(release))
    before = mcp_server._inflight
    with pytest.raises(RuntimeError, match="did not finish"):
        mcp_server.griot_search(query="x")
    assert mcp_server._inflight == before + 1, "the worker still uses the shard"
    release.set()
    deadline = time.monotonic() + 5
    while mcp_server._inflight != before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mcp_server._inflight == before


def test_a_search_that_fails_inside_the_deadline_raises_its_own_error(monkeypatch):
    def fake_search(query, limit=5, group_by_document=False, **filters):
        raise RuntimeError("Local circuit breaker: spend ceiling")

    monkeypatch.setattr(common, "search", fake_search)
    before = mcp_server._inflight
    with pytest.raises(RuntimeError, match="circuit breaker"):
        mcp_server.griot_search(query="x")
    assert mcp_server._inflight == before


def test_zero_turns_the_deadline_off(monkeypatch):
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0)
    ran_in = []

    def fake_search(query, limit=5, group_by_document=False, **filters):
        ran_in.append(threading.current_thread())
        time.sleep(0.3)
        return []

    monkeypatch.setattr(common, "search", fake_search)
    out = mcp_server.griot_search(query="x")
    assert out["results"] == []
    assert ran_in == [threading.current_thread()]


def test_a_search_inside_the_deadline_returns_normally(monkeypatch):
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [])
    assert mcp_server.griot_search(query="x")["results"] == []


@pytest.mark.anyio
async def test_through_a_real_client_the_timeout_is_an_error_result_and_a_normal_call_still_works(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(common, "search", _slow_search(release))
    try:
        async with Client(mcp_server.mcp) as client:
            slow = await client.call_tool("griot_search", {"query": "x"})
            assert slow.is_error is True
            assert "did not finish" in slow.content[0].text
            release.set()
            monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [])
            ok = await client.call_tool("griot_search", {"query": "x"})
            assert ok.is_error is False
    finally:
        release.set()
