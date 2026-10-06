"""`griot stats` used two clocks and two scopes (debt 8). The window was cut
in UTC while spend was grouped by the local day the circuit breaker counts,
so the first local day of a window came in partial; and the state lines were
about the active profile's collection while the counts added up every
profile. These tests pin one clock (local days, DST included) and one scope
(the active collection by default, every profile on request), with spend and
MCP tool calls global on purpose."""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from griot import common, logdb, mcp_server, stats

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset to fix the local zone")


def _local_zone(name: str):
    """A fixed local zone with DST, restored afterwards: the local day is
    whatever the machine says, so a test that does not pin it proves nothing.
    Restored by hand, not by monkeypatch: tzset() has to run AFTER the
    variable is back, and monkeypatch restores only at its own teardown."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


@pytest.fixture
def new_york():
    yield from _local_zone("America/New_York")


@pytest.fixture
def havana():
    """A zone whose clock changes AT midnight: one day has no 00:00 at all,
    another has it twice."""
    yield from _local_zone("America/Havana")


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _run(ts: datetime, collection: str | None = None, **fields) -> dict:
    record = {"timestamp": ts.isoformat(), "script": "index_code.py", "indexed": 1, "skipped": 0, "failed": 0}
    if collection is not None:
        record["collection"] = collection
    record.update(fields)
    return record


# --- one clock -----------------------------------------------------------------------------


def test_the_window_opens_at_local_midnight_on_a_spring_forward_day(new_york):
    # 2026-03-08: clocks go from 02:00 EST to 03:00 EDT. Midnight was still EST (UTC-5).
    now = _utc(2026, 3, 10, 16)  # 12:00 EDT
    assert stats.window_start(3, now) == _utc(2026, 3, 8, 5)


def test_the_window_opens_at_local_midnight_on_a_fall_back_day(new_york):
    # 2026-11-01: clocks go from 02:00 EDT back to 01:00 EST. Midnight was still EDT (UTC-4).
    now = _utc(2026, 11, 2, 17)  # 12:00 EST
    assert stats.window_start(2, now) == _utc(2026, 11, 1, 4)


def test_a_day_without_a_midnight_opens_when_the_day_does(havana):
    # 2026-03-08: 00:00 CST becomes 01:00 CDT, so the day begins at 01:00 (05:00 UTC).
    assert stats.window_start(1, _utc(2026, 3, 8, 18)) == _utc(2026, 3, 8, 5)


def test_a_midnight_placed_on_the_day_before_walks_forward_to_when_the_day_opens(havana, monkeypatch):
    """Which side of a skipped midnight mktime picks depends on the Python
    (3.10 answers 23:00 of the day before; newer ones 01:00 of the day), so
    the test above passes on a newer Python without the walk ever running.
    Here combine() answers as 3.10 does, and the window must still open at
    the day's first instant, not before it and not past it."""
    real = stats.datetime

    class AsOnPython310(real):
        @classmethod
        def combine(cls, date, time_, tzinfo=None):
            if date == real(2026, 3, 8).date():
                # 23:00 CST on the 7th, as an aware moment: astimezone() keeps it there.
                return real(2026, 3, 8, 4, tzinfo=timezone.utc).astimezone()
            return real.combine(date, time_)

    monkeypatch.setattr(stats, "datetime", AsOnPython310)
    assert stats.window_start(1, _utc(2026, 3, 8, 18)) == _utc(2026, 3, 8, 5)


def test_a_day_with_two_midnights_opens_at_the_first(havana):
    # 2026-11-01: 01:00 CDT goes back to 00:00 CST; the day began at the first 00:00 (04:00 UTC).
    assert stats.window_start(1, _utc(2026, 11, 1, 18)) == _utc(2026, 11, 1, 4)


def test_one_day_is_today_since_local_midnight(new_york):
    now = _utc(2026, 10, 6, 16)  # 12:00 EDT
    assert stats.window_start(1, now) == _utc(2026, 10, 6, 4)


def test_the_window_of_an_evening_already_tomorrow_in_utc_opens_today(new_york):
    now = _utc(2026, 10, 7, 2)  # 22:00 EDT on the 6th: the 7th in UTC
    assert stats.window_start(1, now) == _utc(2026, 10, 6, 4)


def test_the_first_local_day_of_the_window_comes_in_whole_and_the_day_before_does_not(new_york):
    now = _utc(2026, 10, 6, 16)  # 12:00 EDT on the 6th; --days 2 is the 5th and the 6th
    kept = stats._filter_by_days([
        {"timestamp": "2026-10-04T13:00:00-04:00"},  # the 4th, after "now minus 48 hours": the old UTC cut kept it
        {"timestamp": "2026-10-05T00:30:00-04:00"},
        {"timestamp": _utc(2026, 10, 5, 4).isoformat()},  # exactly local midnight
    ], 2, now.isoformat())
    assert [r["timestamp"] for r in kept] == ["2026-10-05T00:30:00-04:00", _utc(2026, 10, 5, 4).isoformat()]


def test_the_window_and_the_spend_days_are_the_same_local_days(new_york):
    """Read from a real logs.db: the spend of the day before the window stays
    out, and every spend day of the report is a whole local day of the window."""
    now = _utc(2025, 10, 6, 16)
    for ts, spend in ((_utc(2025, 10, 4, 18), 0.5),   # the 4th, 14:00 local: outside
                      (_utc(2025, 10, 5, 4, 30), 0.1),  # the 5th, 00:30 local: the first minutes of the window
                      (_utc(2025, 10, 6, 2), 0.3),     # the 5th, 22:00 local (the 6th in UTC)
                      (_utc(2025, 10, 6, 15), 0.2)):   # the 6th
        logdb.write_run(common.LOG_DIR, _run(ts, common.COLLECTION_NAME, spend_today_usd=spend))

    runs, queries = stats.load_window(2, now=now)
    result = stats.compute_stats(runs, queries, {}, now=now)

    assert [d["date"] for d in result["spend_by_date"]] == ["2025-10-05", "2025-10-06"]
    assert result["spend_by_date"][0]["amount"] == 0.3
    assert result["total_spend_usd"] == 0.5
    assert result["num_runs"] == 3


def test_a_fall_back_day_is_twenty_five_hours_long_and_all_of_it_counts(new_york):
    """--days 1 at the end of a 25-hour day: a record from its first half hour
    is more than 24 hours old and still today."""
    now = _utc(2026, 11, 2, 4, 30)  # 23:30 EST on the 1st
    logdb.write_run(common.LOG_DIR, _run(_utc(2026, 11, 1, 4, 15), common.COLLECTION_NAME))  # 00:15 EDT on the 1st
    logdb.write_query(common.LOG_DIR, {"timestamp": _utc(2026, 11, 1, 4, 15).isoformat(),
                                       "collection": common.COLLECTION_NAME, "duration_seconds": 1.0})
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, timestamp=_utc(2026, 11, 1, 4, 15).isoformat())
    logdb.write_quality_check(common.LOG_DIR, {"timestamp": _utc(2026, 11, 1, 4, 15).isoformat(),
                                               "collection": common.COLLECTION_NAME,
                                               "self_check": {"sampled": 2, "passed": 2}})

    runs, queries = stats.load_window(1, now=now)
    assert len(runs) == 1 and len(queries) == 1
    assert len(stats.load_tool_calls(1, now=now)) == 1
    assert len(stats.load_quality_window(1, now=now)) == 1


def test_tool_calls_from_before_local_midnight_stay_out(new_york):
    now = _utc(2025, 10, 6, 16)
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, timestamp=_utc(2025, 10, 6, 3).isoformat())  # 23:00 on the 5th
    logdb.write_tool_call(common.LOG_DIR, "griot_stats", ok=True, timestamp=_utc(2025, 10, 6, 5).isoformat())
    assert [c["tool"] for c in stats.load_tool_calls(1, now=now)] == ["griot_stats"]


def test_quality_checks_from_before_local_midnight_stay_out(new_york):
    now = _utc(2025, 10, 6, 16)
    for hour in (3, 5):
        logdb.write_quality_check(common.LOG_DIR, {"timestamp": _utc(2025, 10, 6, hour).isoformat(),
                                                   "collection": "c", "self_check": {"sampled": 1, "passed": 1}})
    assert [q["timestamp"] for q in stats.load_quality_window(1, now=now)] == [_utc(2025, 10, 6, 5).isoformat()]


def test_the_report_names_the_local_day_its_window_starts(new_york):
    now = _utc(2026, 10, 6, 16)
    result = stats.compute_stats([], [], {}, now=now, since=stats.window_start(7, now))
    assert result["window_start"] == "2026-09-30T00:00:00-04:00"
    assert "since 2026-09-30, local time" in stats.format_stats(result, 7)


def test_help_says_the_window_is_counted_in_local_days(capsys):
    with pytest.raises(SystemExit):
        stats.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "local midnight" in out and "--all-profiles" in out


def test_a_window_of_no_days_is_refused(capsys):
    with pytest.raises(SystemExit):
        stats.main(["--days", "0"])
    assert "--days" in capsys.readouterr().err


# --- one scope -----------------------------------------------------------------------------


def _mixed_history():
    runs = [
        _run(_utc(2026, 10, 6, 10), "mine", indexed=4, skipped=6, failed=1,
             failures=[{"id": "x", "reason": "too long"}], pruned=2, redacted=1, spend_today_usd=0.1),
        _run(_utc(2026, 10, 6, 11), "other", indexed=100, skipped=0, failed=3,
             failures=[{"id": "y", "reason": "bad key"}], pruned=50, spend_today_usd=0.4),
        _run(_utc(2026, 10, 6, 12), "other", indexed=None, skipped=None, failed=None, error="died"),
        _run(_utc(2026, 10, 6, 13), None, indexed=7, skipped=0, failed=0),  # an old record that names no collection
    ]
    queries = [
        {"timestamp": _utc(2026, 10, 6, 10).isoformat(), "collection": "mine", "duration_seconds": 1.0,
         "sources": ["repo/a.py"], "top_score": 0.9, "num_sources": 1, "via": "cli"},
        {"timestamp": _utc(2026, 10, 6, 11).isoformat(), "collection": "other", "duration_seconds": 9.0,
         "sources": ["commit abc"], "top_score": None, "num_sources": 0, "via": "mcp", "spend_today_usd": 0.6},
    ]
    quality = [{"timestamp": _utc(2026, 10, 6, 9).isoformat(), "collection": "other", "pass_rate": 0.2},
               {"timestamp": _utc(2026, 10, 6, 10).isoformat(), "collection": "mine", "pass_rate": 0.9}]
    tools = [{"tool": "griot_search", "ok": True}, {"tool": "griot_stats", "ok": False}]
    return runs, queries, quality, tools


def test_by_default_the_counts_are_those_of_one_collection_and_spend_is_every_profile():
    runs, queries, quality, tools = _mixed_history()
    s = stats.compute_stats(runs, queries, {}, quality_checks=quality, tool_calls=tools, collection="mine")

    assert s["scope"] == "active_profile" and s["scope_collection"] == "mine"
    assert (s["num_runs"], s["total_indexed"], s["total_skipped"], s["total_failed"]) == (1, 4, 6, 1)
    assert (s["total_pruned"], s["total_redacted"], s["dead_runs"]) == (2, 1, 0)
    assert s["failure_reasons"] == {"too long": 1}
    assert s["num_queries"] == 1 and s["source_breakdown"] == {"code": 1}
    assert s["queries_by_surface"] == {"cli": 1} and s["empty_searches"] == 0
    assert [q["pass_rate"] for q in s["quality_trend"]] == [0.9]
    # Money is spent per account: every profile's spend, whatever the scope.
    assert s["total_spend_usd"] == 0.6
    assert s["tool_calls"] == {"griot_search": 1, "griot_stats": 1}
    assert s["records_without_collection"] == 1


def test_every_profile_on_request_counts_every_record():
    runs, queries, quality, tools = _mixed_history()
    s = stats.compute_stats(runs, queries, {}, quality_checks=quality, tool_calls=tools)

    assert s["scope"] == "all_profiles" and s["scope_collection"] is None
    assert (s["num_runs"], s["total_indexed"], s["total_failed"], s["dead_runs"]) == (4, 111, 4, 1)
    assert s["num_queries"] == 2 and len(s["quality_trend"]) == 2
    assert s["total_spend_usd"] == 0.6
    assert s["records_without_collection"] == 0


def test_a_quality_check_that_names_no_collection_is_counted_as_such():
    s = stats.compute_stats([], [], {}, quality_checks=[{"timestamp": "t", "pass_rate": 0.5}], collection="mine")
    assert s["quality_trend"] == [] and s["records_without_collection"] == 1


def test_the_report_says_which_scope_it_shows_and_that_spend_is_global():
    runs, queries, quality, tools = _mixed_history()
    text = stats.format_stats(stats.compute_stats(runs, queries, {"embed_profile": "p"}, quality_checks=quality,
                                                  tool_calls=tools, collection="mine"), 7)
    assert "Scope:       collection mine" in text
    assert "--all-profiles" in text
    assert "1 record in the period names no collection" in text
    spend_line = next(line for line in text.splitlines() if line.startswith("API spend:"))
    assert "every profile" in spend_line

    text = stats.format_stats(stats.compute_stats(runs, queries, {"embed_profile": "p"}), 7)
    assert "Scope:       every profile" in text
    assert "names no collection" not in text


def test_the_cli_shows_the_active_collection_unless_asked_for_every_profile(monkeypatch, capsys):
    monkeypatch.setattr(common, "get_index_status", lambda: {"points_count": 0, "embed_profile": "jina-code"})
    now = datetime.now(timezone.utc)
    logdb.write_run(common.LOG_DIR, _run(now, common.COLLECTION_NAME, indexed=3))
    logdb.write_run(common.LOG_DIR, _run(now, "griot_some_other_profile", indexed=40))

    stats.main(["--json"])
    mine = json.loads(capsys.readouterr().out)
    stats.main(["--json", "--all-profiles"])
    every = json.loads(capsys.readouterr().out)

    assert (mine["total_indexed"], mine["scope_collection"]) == (3, common.COLLECTION_NAME)
    assert (every["total_indexed"], every["scope"]) == (43, "all_profiles")
    assert mine["window_start"] and every["window_start"] == mine["window_start"]


def test_the_last_search_is_the_last_search_of_the_active_collection():
    logdb.write_query(common.LOG_DIR, {"timestamp": "2026-10-01T00:00:00+00:00", "collection": common.COLLECTION_NAME})
    logdb.write_query(common.LOG_DIR, {"timestamp": "2026-10-05T00:00:00+00:00", "collection": "griot_other"})
    assert stats.load_state()["last_query_at"] == "2026-10-01T00:00:00+00:00"


def test_recent_queries_can_be_read_for_one_collection():
    logdb.write_query(common.LOG_DIR, {"timestamp": "2026-10-01T00:00:00+00:00", "collection": "a"})
    logdb.write_query(common.LOG_DIR, {"timestamp": "2026-10-02T00:00:00+00:00", "collection": "b"})
    logdb.write_query(common.LOG_DIR, {"timestamp": "2026-10-03T00:00:00+00:00"})
    assert [q["timestamp"] for q in logdb.read_recent(common.LOG_DIR, "queries", collection="a", limit=5)] \
        == ["2026-10-01T00:00:00+00:00"]


@pytest.mark.anyio
async def test_griot_stats_has_the_same_scope_as_the_cli_through_the_protocol(monkeypatch):
    from mcp.client.client import Client

    monkeypatch.setattr(common, "get_index_status", lambda: {"points_count": 0, "embed_profile": "jina-code"})
    now = datetime.now(timezone.utc)
    logdb.write_run(common.LOG_DIR, _run(now, common.COLLECTION_NAME, indexed=3))
    logdb.write_run(common.LOG_DIR, _run(now, "griot_some_other_profile", indexed=40))
    logdb.write_run(common.LOG_DIR, _run(now, None, indexed=500))

    async with Client(mcp_server.mcp) as client:
        mine = await client.call_tool("griot_stats", {})
        every = await client.call_tool("griot_stats", {"all_profiles": True})

    assert mine.is_error is False and every.is_error is False
    mine, every = mine.structured_content, every.structured_content
    assert (mine["total_indexed"], mine["scope"], mine["scope_collection"]) == (3, "active_profile", common.COLLECTION_NAME)
    assert mine["records_without_collection"] == 1
    assert (every["total_indexed"], every["scope"], every["scope_collection"]) == (543, "all_profiles", None)
    assert mine["window_start"] == stats.window_start(30).isoformat()
