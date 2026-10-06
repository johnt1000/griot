"""`griot stats` reported activity inside a window and nothing about the
state of the index: two reports a week apart could print the same lines, the
golden set never appeared, and a run that died yesterday looked like a quiet
week. These tests are about what the report says regardless of the window:
how old things are, what needs attention, and what the numbers add up to."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from griot import common, logdb, quality_check, repos, stats

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _at(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def _status(**over) -> dict:
    status = {"points_count": 100, "embed_profile": "any", "spend_ceiling_exceeded": False,
              "last_indexed": {"timestamp": _at(days=2), "indexed": 5, "skipped": 0, "failed": 0, "error": None}}
    status.update(over)
    return status


def _report(runs=(), queries=(), status=None, days=7, **kwargs) -> tuple[dict, str]:
    result = stats.compute_stats(list(runs), list(queries), status or _status(), now=NOW, **kwargs)
    return result, stats.format_stats(result, days)


# --- how old things are --------------------------------------------------------------------


def test_the_report_says_when_the_index_was_last_written_whatever_the_window():
    result, text = _report(status=_status(last_indexed={"timestamp": _at(days=40), "indexed": 1, "error": None}))
    assert result["last_indexed_at"] == _at(days=40)
    assert "last indexed 40 days ago" in text


def test_the_report_says_when_it_was_last_searched_even_outside_the_window():
    result, text = _report(state={"last_query_at": _at(days=12)})
    assert result["last_query_at"] == _at(days=12)
    assert "last search 12 days ago" in text


def test_an_index_never_written_or_never_searched_says_so():
    _, text = _report(status=_status(last_indexed=None), state={"last_query_at": None})
    assert "never indexed" in text and "never searched" in text


@pytest.mark.parametrize("delta,expected", [
    ({"seconds": 20}, "just now"), ({"minutes": 5}, "5 minutes ago"), ({"minutes": 1}, "1 minute ago"),
    ({"hours": 3}, "3 hours ago"), ({"days": 1}, "1 day ago"), ({"days": 45}, "45 days ago"),
])
def test_an_age_is_written_in_the_unit_a_person_would_use(delta, expected):
    assert stats._ago(_at(**delta), NOW) == expected


@pytest.mark.parametrize("timestamp", [None, "", "not a date", "2026-09-30T10:00:00"])
def test_an_age_that_cannot_be_read_is_not_invented(timestamp):
    """A naive timestamp cannot be compared with an aware one: no age rather
    than a crash or a guess."""
    assert stats._ago(timestamp, NOW) is None


# --- what needs attention ------------------------------------------------------------------


def test_a_healthy_index_has_nothing_to_attend_to():
    result, text = _report()
    assert result["attention"] == [] and "Attention" not in text


def test_a_last_run_that_died_leads_the_report_even_when_it_is_outside_the_window():
    status = _status(last_indexed={"timestamp": _at(days=20), "indexed": None, "error": "collection busy"})
    result, text = _report(status=status)
    assert any("did not finish" in item and "collection busy" in item for item in result["attention"])
    assert text.index("Attention") < text.index("Index:")
    assert "last indexing attempt 20 days ago" in text and "last indexed 20" not in text, "a run that died indexed nothing"


def test_a_last_run_that_counted_and_failed_is_not_said_to_have_died():
    status = _status(last_indexed={"timestamp": _at(days=1), "indexed": 0, "failed": 3,
                                   "error": "the platform refused every fetch (HTTP 401)"})
    result, _ = _report(status=status)
    (item,) = [item for item in result["attention"] if "refused every fetch" in item]
    assert "did not finish" not in item and "failed" in item


def test_a_reached_spend_ceiling_is_said_first():
    result, _ = _report(status=_status(spend_ceiling_exceeded=True))
    assert any("ceiling" in item for item in result["attention"])


def test_a_collection_that_cannot_be_read_is_said_first():
    result, _ = _report(status=_status(points_count=None, points_error="unreadable: bad segment"))
    assert any("could not be read" in item for item in result["attention"])


def test_a_collection_that_is_only_busy_is_not_an_alarm():
    result, _ = _report(status=_status(points_count=None, points_error="busy"))
    assert result["attention"] == []


# --- quality and the golden set ------------------------------------------------------------


def test_quality_never_checked_is_a_fact_in_the_report_not_a_missing_section():
    _, text = _report(state={"last_quality_check": None})
    assert "Quality:" in text and "never checked" in text


def test_quality_checked_before_the_index_last_changed_says_so():
    state = {"last_quality_check": {"timestamp": _at(days=39), "pass_rate": 1.0}, "last_index_change_at": _at(days=2)}
    result, text = _report(state=state)
    assert result["quality_is_older_than_index"] is True
    assert "39 days ago" in text and "before the index last changed" in text


def test_quality_checked_after_the_last_indexing_does_not_say_so():
    state = {"last_quality_check": {"timestamp": _at(hours=1), "pass_rate": 1.0}, "last_index_change_at": _at(days=2)}
    result, text = _report(state=state)
    assert result["quality_is_older_than_index"] is False and "before the index" not in text


def test_an_index_that_never_changed_since_cannot_make_a_check_old():
    state = {"last_quality_check": {"timestamp": _at(days=5), "pass_rate": 1.0}, "last_index_change_at": None}
    result, _ = _report(state=state)
    assert result["quality_is_older_than_index"] is False


def test_the_golden_set_appears_with_its_size_and_its_last_result():
    state = {"golden_set": {"cases": 6, "unregistered_repos": []},
             "last_quality_check": {"timestamp": _at(hours=2), "pass_rate": 1.0},
             "last_golden_check": {"timestamp": _at(days=3), "passed": 4, "total": 6}}
    result, text = _report(state=state)
    assert result["golden_set"] == {"cases": 6, "unregistered_repos": [], "last_passed": 4, "last_total": 6,
                                    "last_run_at": _at(days=3)}
    assert "Golden set:" in text and "6 cases" in text
    assert "4 of 6 passed (3 days ago)" in text, "its own age, not the age of a later check that skipped it"


def test_a_golden_set_that_was_never_run_says_so():
    _, text = _report(state={"golden_set": {"cases": 5, "unregistered_repos": []}, "last_quality_check": None})
    assert "5 cases" in text and "never run" in text


def test_cases_that_expect_a_repository_that_is_not_registered_are_named():
    """They make the whole golden set read as broken. The text says what was
    compared (repos.json), because a repository indexed with --path is not
    in it and its cases pass."""
    state = {"golden_set": {"cases": 6, "unregistered_repos": ["gone", "renamed"]}}
    _, text = _report(state=state)
    assert "not in repos.json" in text and "--path" in text and "gone" in text and "renamed" in text


def test_a_golden_set_file_that_cannot_be_read_needs_attention():
    """Not the same as having none: it is something to fix, and silence
    about it read as "no golden set"."""
    result, text = _report(state={"golden_set": None, "golden_set_error": "JSONDecodeError: Expecting value"})
    assert any("golden set file could not be read" in item and "Expecting value" in item for item in result["attention"])
    assert "Attention" in text


def test_no_golden_set_is_not_mentioned():
    _, text = _report(state={"golden_set": None})
    assert "Golden set" not in text


# --- what the numbers add up to ------------------------------------------------------------


@pytest.fixture
def utc_minus_three():
    """A zone three hours behind UTC for one test, restored by hand: undoing
    a monkeypatch early would also undo the isolation fixtures."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Sao_Paulo"
    time.tzset()
    try:
        if datetime(2026, 9, 10, 23, tzinfo=timezone.utc).astimezone().utcoffset() != timedelta(hours=-3):
            pytest.skip("this machine has no tz database entry for the zone the test needs")
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_spend_is_summed_by_the_local_day_the_breaker_uses(utc_minus_three):
    """The breaker's day is the LOCAL day; the report grouped by the UTC
    date. One local evening that crossed midnight UTC was counted twice."""
    runs = [
        {"timestamp": "2026-09-10T23:00:00+00:00", "indexed": 1, "spend_today_usd": 0.10},  # 20:00 local
        {"timestamp": "2026-09-11T01:00:00+00:00", "indexed": 1, "spend_today_usd": 0.12},  # 22:00, same day
        {"timestamp": "2026-09-11T14:00:00+00:00", "indexed": 1, "spend_today_usd": 0.05},  # next local day
    ]
    result, _ = _report(runs=runs)
    assert result["total_spend_usd"] == pytest.approx(0.17)
    assert result["spend_by_date"] == [{"date": "2026-09-10", "amount": 0.12}, {"date": "2026-09-11", "amount": 0.05}]


def test_a_timestamp_that_cannot_be_read_does_not_take_the_spend_down():
    runs = [{"timestamp": "garbage", "indexed": 1, "spend_today_usd": 0.5},
            {"timestamp": _at(days=1), "indexed": 1, "spend_today_usd": 0.1}]
    result, _ = _report(runs=runs)
    assert result["total_spend_usd"] == pytest.approx(0.1)


def test_what_indexing_removed_and_replaced_is_counted():
    runs = [{"timestamp": _at(days=1), "indexed": 1, "skipped": 0, "failed": 0, "pruned": 12, "redacted": 3},
            {"timestamp": _at(days=1), "indexed": 0, "skipped": 4, "failed": 0, "pruned": None},  # a run from before the fields
            {"timestamp": _at(hours=5), "indexed": 0, "skipped": 4, "failed": 0, "pruned": 1, "redacted": 0}]
    result, text = _report(runs=runs)
    assert result["total_pruned"] == 13 and result["total_redacted"] == 3
    assert "13 stale points removed" in text and "3 credential-looking values replaced" in text


def test_nothing_removed_or_replaced_adds_no_line():
    _, text = _report(runs=[{"timestamp": _at(days=1), "indexed": 1, "skipped": 0, "failed": 0}])
    assert "stale points" not in text and "credential-looking" not in text


def test_latency_is_reported_as_what_is_typical_and_what_is_slow():
    """An average of 0.8 s and one 20 s search says nothing about either."""
    queries = [{"timestamp": _at(hours=1), "duration_seconds": d, "sources": [], "num_sources": 1}
               for d in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 20.0]]
    result, text = _report(queries=queries)
    assert result["query_latency_p50_seconds"] == pytest.approx(0.95, abs=0.06)
    assert result["query_latency_p90_seconds"] >= 1.3
    assert "latency p50" in text and "p90" in text and "avg latency" not in text


def test_one_search_has_a_latency_without_percentile_arithmetic_failing():
    result, _ = _report(queries=[{"timestamp": _at(hours=1), "duration_seconds": 2.0, "sources": [], "num_sources": 1}])
    assert result["query_latency_p50_seconds"] == result["query_latency_p90_seconds"] == 2.0


@pytest.mark.parametrize("duration", [None, "2", True])
def test_a_search_recorded_without_a_duration_does_not_break_the_report(duration):
    queries = [{"timestamp": _at(hours=1), "duration_seconds": duration, "sources": [], "num_sources": 1},
               {"timestamp": _at(hours=1), "duration_seconds": 2.0, "sources": [], "num_sources": 1}]
    result, _ = _report(queries=queries)
    assert result["num_queries"] == 2
    assert result["avg_query_latency_seconds"] == result["query_latency_p50_seconds"] == 2.0


def test_no_searches_means_no_latency():
    result, _ = _report()
    assert result["query_latency_p50_seconds"] is None and result["query_latency_p90_seconds"] is None


def test_callers_that_pass_no_state_still_get_a_report():
    """compute_stats() is called by tests and tools that know nothing of it."""
    result = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"})
    assert result["attention"] == []
    # Absent, not None: "not looked at" must not read as "there is none".
    assert not {"golden_set", "last_query_at", "last_quality_check_at"} & set(result)
    text = stats.format_stats(result, 7)
    assert "never searched" not in text and "never checked" not in text and "Quality:" not in text


# --- read from the real stores -------------------------------------------------------------


SELF = {"sampled": 10, "passed": 9, "failed": 1, "failures": [], "avg_score": 0.9}
EMPTY_STATE = {"last_query_at": None, "last_index_change_at": None, "last_quality_check": None,
               "last_golden_check": None, "golden_set": None, "golden_set_error": None}


def _write_golden_set(text: str) -> None:
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    common.GOLDEN_SET_PATH.write_text(text)


def test_the_state_is_read_from_the_logs_and_the_golden_set_file(tmp_path):
    (tmp_path / "kept").mkdir()
    repos.add_repo(str(tmp_path / "kept"))
    _write_golden_set(json.dumps([
        {"query": "a", "must_include": [{"repo": "kept", "source_type": "code"}]},
        {"query": "b", "must_include": [{"repo": "gone", "source_type": "code"}]},
    ]))
    common.log_query(question="old", via="cli", num_sources=1, duration_seconds=1.0, sources=[])
    quality_check._record_for_trend(common.COLLECTION_NAME, SELF, {"total": 2, "passed": 1, "failed": 1, "cases": []})

    state = stats.load_state()

    assert state["last_query_at"] is not None
    assert state["last_quality_check"]["pass_rate"] == 0.9
    assert (state["last_golden_check"]["passed"], state["last_golden_check"]["total"]) == (1, 2)
    assert state["golden_set"] == {"cases": 2, "unregistered_repos": ["gone"]}
    assert state["golden_set_error"] is None


def test_the_latest_record_is_the_last_one_written():
    common.log_query(question="first", via="cli", num_sources=1, duration_seconds=1.0, sources=[])
    common.log_query(question="second", via="cli", num_sources=1, duration_seconds=1.0, sources=[])
    assert logdb.read_latest(common.LOG_DIR, "queries")["question"] == "second"
    assert [r["question"] for r in logdb.read_recent(common.LOG_DIR, "queries", limit=5)] == ["second", "first"]
    with pytest.raises(ValueError):
        logdb.read_latest(common.LOG_DIR, "queries; DROP TABLE runs")
    # Queries name their collection inside the record: the filter reads it there.
    assert logdb.read_latest(common.LOG_DIR, "queries", collection="any") is None
    assert logdb.read_latest(common.LOG_DIR, "queries", collection=common.COLLECTION_NAME)["question"] == "second"


def test_a_check_without_the_golden_set_does_not_erase_its_last_result():
    """The background check and `--skip-golden-set` record a check with no
    golden part. After one, the report said "never run"."""
    quality_check._record_for_trend(common.COLLECTION_NAME, SELF, {"total": 5, "passed": 4, "failed": 1, "cases": []})
    quality_check._record_for_trend(common.COLLECTION_NAME, dict(SELF, passed=10, failed=0), None)

    state = stats.load_state()

    assert state["last_quality_check"]["pass_rate"] == 1.0, "the newest check"
    assert (state["last_golden_check"]["passed"], state["last_golden_check"]["total"]) == (4, 5), "the newest that ran it"
    assert state["last_golden_check"]["timestamp"] < state["last_quality_check"]["timestamp"]


def test_a_check_of_another_profiles_collection_says_nothing_about_this_one():
    quality_check._record_for_trend("codebase__some-other-profile", SELF, {"total": 5, "passed": 5, "failed": 0, "cases": []})
    state = stats.load_state()
    assert state["last_quality_check"] is None and state["last_golden_check"] is None


def test_the_last_change_is_the_last_run_that_wrote_or_removed_something():
    """Not simply the last run: the last source of `griot index all` usually
    embeds nothing, and a run of another profile is about another index."""
    assert stats.load_state()["last_index_change_at"] is None
    common.log_run_summary(script="index_code", indexed=12, skipped=0, failed=0, pruned=0)
    wrote = stats.load_state()["last_index_change_at"]
    common.log_run_summary(script="index_tags", indexed=0, skipped=3, failed=0, pruned=0)
    common.log_run_summary(script="index_code", indexed=None, skipped=None, failed=None, error="died")
    logdb.write_run(common.LOG_DIR, {"timestamp": datetime.now(timezone.utc).isoformat(),
                                     "collection": "codebase__some-other-profile", "indexed": 99})
    assert wrote is not None and stats.load_state()["last_index_change_at"] == wrote
    common.log_run_summary(script="index_code", indexed=0, skipped=12, failed=0, pruned=2)
    assert stats.load_state()["last_index_change_at"] > wrote, "removing points changes the index too"


def test_a_fresh_install_has_an_empty_state_and_creates_nothing_but_the_log():
    assert stats.load_state() == EMPTY_STATE
    assert not common.GOLDEN_SET_PATH.exists()


@pytest.mark.parametrize("content", ["{not json", "{}", "[1, 2]", '[{"query": "a"}]',
                                     '[{"query": "a", "must_include": null}]',
                                     '[{"query": "a", "must_include": ["x"]}]'])
def test_a_golden_set_file_that_is_there_and_unusable_is_reported_not_taken_for_none(content):
    _write_golden_set(content)
    state = stats.load_state()
    assert state["golden_set"] is None and state["golden_set_error"]
    result = stats.compute_stats([], [], {"points_count": 1, "embed_profile": "any"}, state=state)
    assert any("golden set file could not be read" in item for item in result["attention"])


def test_without_a_registry_nothing_is_claimed_about_registration():
    """No repos.json: there is nothing to compare with, so no case is said
    to expect an unregistered repository."""
    _write_golden_set(json.dumps([{"query": "a", "must_include": [{"repo": "kept", "source_type": "code"}]}]))
    assert not common.REPOS_JSON_PATH.exists()
    assert stats.load_state()["golden_set"] == {"cases": 1, "unregistered_repos": []}


def test_the_command_prints_the_state(capsys):
    common.log_query(question="q", via="cli", num_sources=1, duration_seconds=1.0, sources=[])
    assert stats.main(["--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "never indexed" in out and "last search just now" in out and "never checked" in out


def test_the_json_form_carries_the_same_state(capsys):
    assert stats.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"attention", "last_indexed_at", "last_query_at", "golden_set", "total_pruned",
            "query_latency_p50_seconds"} <= set(payload)


@pytest.mark.anyio
async def test_the_tool_returns_the_state_through_the_protocol():
    from mcp.client.client import Client
    from griot import mcp_server
    common.log_query(question="q", via="mcp", num_sources=1, duration_seconds=1.0, sources=[])
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {"days": 7})
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_stats"]
    assert result.is_error is False, result.content
    out = result.structured_content
    assert out["attention"] == [] and out["last_query_at"] is not None and out["last_indexed_at"] is None
    assert {"attention", "last_indexed_at", "last_query_at", "golden_set"} <= set(tool.output_schema["properties"])


@pytest.mark.anyio
async def test_the_tool_returns_a_golden_set_and_a_dead_last_run_through_the_protocol(tmp_path):
    from mcp.client.client import Client
    from griot import mcp_server
    _write_golden_set(json.dumps([{"query": "a", "must_include": [{"repo": "gone", "source_type": "code"}]}]))
    (tmp_path / "kept").mkdir()
    repos.add_repo(str(tmp_path / "kept"))
    quality_check._record_for_trend(common.COLLECTION_NAME, SELF, {"total": 1, "passed": 0, "failed": 1, "cases": []})
    common.log_run_summary(script="index_code", indexed=None, skipped=None, failed=None, error="died early")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {})
    assert result.is_error is False, result.content
    out = result.structured_content
    assert out["golden_set"]["unregistered_repos"] == ["gone"] and out["golden_set"]["last_total"] == 1
    assert out["golden_set"]["last_run_at"] and out["last_indexed_error"] == "died early"
    assert any("did not finish" in item for item in out["attention"])


@pytest.mark.anyio
async def test_the_health_prompt_points_at_golden_set_fields_that_exist():
    """It tells the agent where the LAST run of the golden set is. The names
    it gives have to be in what griot_stats really returns."""
    import re
    from mcp.client.client import Client
    from griot import mcp_server
    async with Client(mcp_server.mcp) as client:
        text = (await client.get_prompt("health", {})).messages[0].content.text
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_stats"]
    golden = set(tool.output_schema["$defs"]["GoldenSetState"]["properties"])
    assert "golden_set" in tool.output_schema["properties"]
    named = set(re.findall(r"\blast_[a-z_]+\b", text))
    assert named == {"last_passed", "last_total", "last_run_at"} and named <= golden
    assert "past run" in text


@pytest.mark.anyio
async def test_every_field_the_stats_prompt_names_is_one_the_tool_returns():
    import re
    from mcp.client.client import Client
    from griot import mcp_server
    async with Client(mcp_server.mcp) as client:
        text = (await client.get_prompt("stats", {})).messages[0].content.text
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_stats"]
    named = set(re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", text)) - {"griot_stats"}
    assert "attention" in text
    assert named and named <= set(tool.output_schema["properties"]), named - set(tool.output_schema["properties"])


# --- the golden set tells "cannot pass" from "did not pass" --------------------------------


def test_a_case_whose_repository_is_not_indexed_says_so_instead_of_not_found(monkeypatch):
    """Five curated cases expecting repositories that were never indexed read
    as "search quality is 0 of 5". The case is not wrong and search did not
    fail: there is nothing to find."""
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [[0.1] * common.EMBED_DIM for _ in texts])
    common.index_documents([{"id": "here:code:a.py:0", "content": "something",
                             "metadata": {"source_type": "code", "repo": "here", "file_path": "a.py", "chunk_index": 0}}])
    common.release_lock()
    result = quality_check.run_golden_set([
        {"query": "something", "must_include": [{"repo": "here", "source_type": "code"}]},
        {"query": "something", "must_include": [{"repo": "elsewhere", "source_type": "code"}]},
    ])
    ok, cannot = result["cases"]
    assert ok["passed"] is True and "reason" not in ok
    assert cannot["passed"] is False
    assert "nothing is indexed for" in cannot["reason"] and "elsewhere" in cannot["reason"]
    assert result["failed"] == 1, "still a failure: the gate stays closed, the reason is what changes"
