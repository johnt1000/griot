"""Tests for `griot stats` — aggregation over logs that
already exist (logs/logs.db, see logdb.py), zero new collection. Focus on
compute_stats() (pure logic, testable without I/O) and robustness to
missing/corrupted logs.
"""

import json
from datetime import datetime, timezone

import pytest

from griot import common, logdb, stats


def _run(indexed=0, skipped=0, failed=0, spend_today_usd=None, timestamp="2026-08-13T10:00:00+00:00"):
    r = {"timestamp": timestamp, "indexed": indexed, "skipped": skipped, "failed": failed}
    if spend_today_usd is not None:
        r["spend_today_usd"] = spend_today_usd
    return r


def _query(duration_seconds=1.0, sources=None, spend_today_usd=None, timestamp="2026-08-13T10:00:00+00:00"):
    q = {"timestamp": timestamp, "duration_seconds": duration_seconds, "sources": sources or [], "num_sources": len(sources or [])}
    if spend_today_usd is not None:
        q["spend_today_usd"] = spend_today_usd
    return q


# --- compute_stats -----------------------------------------------------


def test_compute_stats_aggregates_indexed_skipped_failed():
    runs = [_run(indexed=10, skipped=5), _run(indexed=3, skipped=27)]
    result = stats.compute_stats(runs, [], {"points_count": 100, "embed_profile": "jina-code"})
    assert result["total_indexed"] == 13
    assert result["total_skipped"] == 32
    assert result["num_runs"] == 2


def test_compute_stats_reuse_rate():
    runs = [_run(indexed=1, skipped=9)]
    result = stats.compute_stats(runs, [], {"points_count": 10, "embed_profile": "jina-code"})
    assert result["reuse_rate"] == 0.9


def test_compute_stats_reuse_rate_none_when_no_runs():
    result = stats.compute_stats([], [], {"points_count": 0, "embed_profile": "jina-code"})
    assert result["reuse_rate"] is None
    assert result["num_runs"] == 0


def test_compute_stats_spend_takes_max_per_day_not_sum():
    """spend_today_usd is CUMULATIVE within the day (a snapshot at the
    moment of the run) — summing every run from the same day would count
    the same spend multiple times. The right measure is the maximum
    observed per day, summed across distinct days."""
    runs = [
        _run(spend_today_usd=0.01, timestamp="2026-08-10T09:00:00+00:00"),
        _run(spend_today_usd=0.05, timestamp="2026-08-10T15:00:00+00:00"),  # same day, higher
        _run(spend_today_usd=0.02, timestamp="2026-08-11T09:00:00+00:00"),
    ]
    result = stats.compute_stats(runs, [], {"points_count": 0, "embed_profile": "gemini"})
    assert result["total_spend_usd"] == 0.07


def test_compute_stats_exposes_spend_by_date_sorted_chronologically():
    """[user-requested] The per-day breakdown was already
    computed internally (to derive total_spend_usd correctly — see the
    'max per day, not sum' rule above) but discarded — the front end's
    spend-over-time chart needs it exposed, sorted oldest-first so a bar
    chart reads left-to-right chronologically."""
    runs = [
        _run(spend_today_usd=0.02, timestamp="2026-08-11T09:00:00+00:00"),
        _run(spend_today_usd=0.01, timestamp="2026-08-10T09:00:00+00:00"),
        _run(spend_today_usd=0.05, timestamp="2026-08-10T15:00:00+00:00"),  # same day, higher — the max wins
    ]
    result = stats.compute_stats(runs, [], {"points_count": 0, "embed_profile": "gemini"})
    assert result["spend_by_date"] == [
        {"date": "2026-08-10", "amount": 0.05},
        {"date": "2026-08-11", "amount": 0.02},
    ]


def test_compute_stats_ignores_runs_without_spend_field():
    runs = [_run(indexed=1), _run(indexed=1, spend_today_usd=0.5)]
    result = stats.compute_stats(runs, [], {"points_count": 0, "embed_profile": "gemini"})
    assert result["total_spend_usd"] == 0.5


def test_compute_stats_spend_includes_chat_queries_not_only_indexing_runs():
    """Real finding (2026-08-14): griot ask (chat) has written spend_today_usd
    on the query since the multi-provider chat generalization, but
    total_spend_usd only looked at runs — chat spend (which can be the bulk
    of real spend in typical usage: few indexing runs, many questions) never
    showed up in `griot stats`. It's the SAME breaker/.spend_state.json for
    both, so the aggregation by calendar day needs to consider both sources
    together."""
    runs = [_run(spend_today_usd=0.001, timestamp="2026-08-14T09:00:00+00:00")]
    queries = [_query(spend_today_usd=0.05, timestamp="2026-08-14T15:00:00+00:00")]  # higher, later in the day
    result = stats.compute_stats(runs, queries, {"points_count": 0, "embed_profile": "gemini"})
    assert result["total_spend_usd"] == 0.05


def test_compute_stats_spend_max_per_day_across_runs_and_queries_combined():
    runs = [_run(spend_today_usd=0.02, timestamp="2026-08-10T09:00:00+00:00")]
    queries = [_query(spend_today_usd=0.03, timestamp="2026-08-11T09:00:00+00:00")]
    result = stats.compute_stats(runs, queries, {"points_count": 0, "embed_profile": "gemini"})
    assert result["total_spend_usd"] == 0.05  # distinct days, sums the two maxima


def test_compute_stats_query_metrics():
    queries = [_query(duration_seconds=1.0), _query(duration_seconds=3.0)]
    result = stats.compute_stats([], queries, {"points_count": 0, "embed_profile": "jina-code"})
    assert result["num_queries"] == 2
    assert result["avg_query_latency_seconds"] == 2.0


def test_compute_stats_query_metrics_none_when_no_queries():
    result = stats.compute_stats([], [], {"points_count": 0, "embed_profile": "jina-code"})
    assert result["num_queries"] == 0
    assert result["avg_query_latency_seconds"] is None


def test_compute_stats_source_breakdown_classifies_by_label_prefix():
    queries = [
        _query(sources=["repo-x/src/a.py", "commit a1b2c3d4 — repo-x"]),
        _query(sources=["repo-x/src/b.py"]),
    ]
    result = stats.compute_stats([], queries, {"points_count": 0, "embed_profile": "jina-code"})
    assert result["source_breakdown"]["code"] == 2
    assert result["source_breakdown"]["commit"] == 1


def test_compute_stats_passes_through_index_status():
    index_status = {"points_count": 12345, "embed_profile": "nomic-q"}
    result = stats.compute_stats([], [], index_status)
    assert result["points_count"] == 12345
    assert result["embed_profile"] == "nomic-q"


# --- _classify_source_label ---------------------------------------------


def test_classify_source_label_code_has_no_prefix():
    assert stats._classify_source_label("my-project/src/config.ts") == "code"


def test_classify_source_label_recognizes_all_prefixes():
    assert stats._classify_source_label("commit a1b2c3d4 — repo") == "commit"
    assert stats._classify_source_label("tag v1.0 — repo") == "tag"
    assert stats._classify_source_label("branch main — repo") == "branch"
    assert stats._classify_source_label("MR !12 (opened) — repo") == "merge_request"
    assert stats._classify_source_label("release v1.0 — repo") == "release"
    assert stats._classify_source_label("issue #3 (opened) — repo") == "issue"


# --- _filter_by_days -----------------------------------------------------


def test_filter_by_days_excludes_old_records():
    old = _run(timestamp="2020-01-01T00:00:00+00:00")
    recent = _run(timestamp="2026-08-12T00:00:00+00:00")
    filtered = stats._filter_by_days([old, recent], days=30, now_iso="2026-08-13T00:00:00+00:00")
    assert filtered == [recent]


def test_filter_by_days_keeps_records_without_parseable_timestamp_out():
    broken = {"indexed": 1}  # no 'timestamp'
    filtered = stats._filter_by_days([broken], days=30, now_iso="2026-08-13T00:00:00+00:00")
    assert filtered == []


def test_filter_by_days_does_not_crash_on_naive_timestamp(monkeypatch):
    """Finding from review: comparing a naive datetime (no timezone) with an
    aware one raises TypeError, not ValueError — only ValueError was being
    caught, so a timestamp without tz (manual edit, or a future log source
    that doesn't use datetime.now(timezone.utc)) would take down `griot
    stats` entirely, against the robustness contract of section 11.2
    ("never break the report")."""
    naive = _run(timestamp="2026-08-12T00:00:00")  # no +00:00
    filtered = stats._filter_by_days([naive], days=30, now_iso="2026-08-13T00:00:00+00:00")
    assert filtered == []


# --- _read_jsonl (robustness to missing/corrupted file) -----------------


_INDEX_STATUS = {"points_count": 1234, "embed_profile": "jina-code"}


# --- density primitives (user-requested, 2026-08-21) -------------------
# Plain unicode, no rich/textual/blessed — a dependency-free way to make
# numbers readable at a glance, the part of Claude Code's /usage screen
# that actually carries value (density + interpretation, not navigation).


def test_bar_is_empty_at_zero():
    assert stats._bar(0.0, width=10) == "░" * 10


def test_bar_is_full_at_one():
    assert stats._bar(1.0, width=10) == "█" * 10


def test_bar_is_proportional_in_between():
    assert stats._bar(0.5, width=10) == "█" * 5 + "░" * 5


def test_bar_clamps_above_one_instead_of_overflowing():
    """Spend can exceed the ceiling (the breaker stops the NEXT call, it
    can't un-spend the last one) — the bar must stay `width` wide."""
    assert stats._bar(2.5, width=10) == "█" * 10


def test_bar_clamps_negative_to_empty():
    assert stats._bar(-1.0, width=10) == "░" * 10


def test_sparkline_renders_one_char_per_point():
    assert len(stats._sparkline([1, 5, 3, 9])) == 4


def test_sparkline_maps_lowest_and_highest_to_the_extremes():
    line = stats._sparkline([0, 100])
    assert line[0] == "▁" and line[-1] == "█"


def test_sparkline_of_a_flat_series_does_not_divide_by_zero():
    """All-equal values have no range — every point must render at the same
    height rather than raising."""
    line = stats._sparkline([4, 4, 4])
    assert len(line) == 3 and len(set(line)) == 1


def test_sparkline_of_a_single_point_is_one_char():
    assert len(stats._sparkline([7])) == 1


def test_sparkline_of_nothing_is_empty():
    assert stats._sparkline([]) == ""


# --- format_stats (rendering) ------------------------------------------


def test_format_stats_shows_a_spend_bar_against_the_daily_ceiling(monkeypatch):
    """A bare "$0.75 (ceiling: $3.00/day)" makes the reader do the division.
    The bar answers "how close am I?" at a glance — the one thing the
    number alone doesn't."""
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    result = stats.compute_stats([_run(spend_today_usd=0.75)], [], _INDEX_STATUS)

    text = stats.format_stats(result, days=30)

    assert stats._BAR_FULL in text
    assert "25%" in text


def test_format_stats_renders_the_spend_trend_that_was_being_discarded():
    """[real gap, left by the front end's removal] compute_stats() has always computed
    spend_by_date; the only thing that ever rendered it was the web
    removed front end's chart, so since its removal the series was computed on
    every call and thrown away."""
    runs = [
        _run(spend_today_usd=0.10, timestamp="2026-08-19T10:00:00+00:00"),
        _run(spend_today_usd=0.50, timestamp="2026-08-20T10:00:00+00:00"),
        _run(spend_today_usd=0.20, timestamp="2026-08-21T10:00:00+00:00"),
    ]
    result = stats.compute_stats(runs, [], _INDEX_STATUS)

    text = stats.format_stats(result, days=30)

    assert any(level in text for level in stats._SPARK_LEVELS)
    assert "3 days" in text


def test_format_stats_omits_the_spend_trend_with_a_single_day():
    """One day is not a trend — a 1-char sparkline is noise, not signal."""
    result = stats.compute_stats([_run(spend_today_usd=0.10)], [], _INDEX_STATUS)

    assert "spend trend" not in stats.format_stats(result, days=30).lower()


def test_format_stats_interprets_the_reuse_rate():
    """The number that justifies griot's incremental indexing deserves a
    sentence, not just a percentage in parentheses."""
    result = stats.compute_stats([_run(indexed=1, skipped=9)], [], _INDEX_STATUS)

    text = stats.format_stats(result, days=30)

    assert "90.0% reused" in text
    assert "re-embedding" in text.lower()


def test_format_stats_warns_when_reuse_is_poor_across_repeated_runs():
    """Low reuse is the signature of the duplicate-id bug class (decision
    65, where reuse collapsed to 0.29% across 14 real runs) — worth
    flagging rather than leaving the reader to notice."""
    runs = [_run(indexed=9, skipped=1), _run(indexed=9, skipped=1)]
    result = stats.compute_stats(runs, [], _INDEX_STATUS)

    assert "low reuse" in stats.format_stats(result, days=30).lower()


def test_recent_reuse_reflects_the_latest_runs_not_the_whole_window():
    """[real case, 2026-08-22] After the decision-65 fixes were validated in
    production, the window held 14 broken runs (~0% reuse) and 8 healthy
    ones (~100%). The aggregate averaged to 47.7%, which both hid that
    incremental indexing now works AND kept printing a "suspicious" warning
    at the user who had just proved it works. An average across two eras
    describes neither."""
    broken = [_run(indexed=1000, skipped=0) for _ in range(14)]
    healthy = [_run(indexed=0, skipped=1000) for _ in range(8)]
    result = stats.compute_stats(broken + healthy, [], _INDEX_STATUS)

    assert result["reuse_rate"] == pytest.approx(8000 / 22000, abs=1e-3)  # the window average
    assert result["recent_reuse_rate"] == pytest.approx(1.0)  # what is happening NOW


def test_recent_reuse_is_none_without_enough_runs_to_be_recent():
    """One run is the whole window, not a "recent" subset — reporting it as
    a separate number would just repeat the aggregate with a second name."""
    result = stats.compute_stats([_run(indexed=1, skipped=9)], [], _INDEX_STATUS)
    assert result["recent_reuse_rate"] is None


def test_format_stats_shows_recent_reuse_when_it_disagrees_with_the_window():
    broken = [_run(indexed=1000, skipped=0) for _ in range(14)]
    healthy = [_run(indexed=0, skipped=1000) for _ in range(8)]

    text = stats.format_stats(stats.compute_stats(broken + healthy, [], _INDEX_STATUS), days=30)

    assert "recent" in text.lower()
    assert "100%" in text


def test_praise_counts_only_the_runs_it_is_praising():
    """[review finding] When the sentence is driven by the RECENT slice,
    the number in it must come from that slice too. Otherwise it praises
    "saved re-embedding 8500 chunks" while describing 5 runs that account
    for 500 of them — number from one scale, claim from another.

    The production case hid this: its old era had skipped≈0 (the decision-65
    bug), so window and recent totals happened to coincide. This series
    breaks that coincidence deliberately — a mediocre-but-not-broken older
    era, which is the general case."""
    older = [_run(indexed=600, skipped=400) for _ in range(20)]   # ~40% reuse, 8000 skipped
    recent = [_run(indexed=0, skipped=100) for _ in range(5)]      # 100% reuse, 500 skipped

    text = stats.format_stats(stats.compute_stats(older + recent, [], _INDEX_STATUS), days=30)

    assert "500 unchanged chunks" in text
    assert "8500 unchanged chunks" not in text


def test_recent_skipped_is_exposed_for_the_renderer():
    older = [_run(indexed=600, skipped=400) for _ in range(20)]
    recent = [_run(indexed=0, skipped=100) for _ in range(5)]

    result = stats.compute_stats(older + recent, [], _INDEX_STATUS)

    assert result["recent_skipped"] == 500
    assert result["total_skipped"] == 8500


def test_recent_skipped_is_none_when_the_window_is_too_short():
    result = stats.compute_stats([_run(indexed=1, skipped=9)], [], _INDEX_STATUS)
    assert result["recent_skipped"] is None


def test_format_stats_does_not_warn_when_recent_reuse_is_healthy():
    """The warning must follow the CURRENT behavior, not the window's
    history — otherwise it fires at someone whose indexing is working, and
    a warning that cries wolf gets ignored when it finally matters."""
    broken = [_run(indexed=1000, skipped=0) for _ in range(14)]
    healthy = [_run(indexed=0, skipped=1000) for _ in range(8)]

    text = stats.format_stats(stats.compute_stats(broken + healthy, [], _INDEX_STATUS), days=30)

    assert "low reuse" not in text.lower()


def test_format_stats_still_warns_when_recent_reuse_is_genuinely_poor():
    healthy = [_run(indexed=0, skipped=1000) for _ in range(8)]
    broken = [_run(indexed=1000, skipped=0) for _ in range(6)]

    text = stats.format_stats(stats.compute_stats(healthy + broken, [], _INDEX_STATUS), days=30)

    assert "low reuse" in text.lower()


def test_format_stats_does_not_cry_low_reuse_on_a_first_indexing(monkeypatch):
    """[review finding] A first indexing has 0% reuse by definition —
    nothing existed to reuse. Warning there would alarm every new user in
    the most normal situation there is. With a single run in the window
    there is no way to tell "first ever" from anything else, so the honest
    move is to say nothing."""
    result = stats.compute_stats([_run(indexed=500, skipped=0)], [], _INDEX_STATUS)

    assert "low reuse" not in stats.format_stats(result, days=30).lower()


def test_format_stats_shows_the_quality_trend_that_was_orphaned():
    """[real gap, left by the front end's removal] Every quality-check has been recorded
    into logs.db since an earlier decision, but the only thing that ever read that
    history was the removed front end's Quality page. Removing it left the data
    accumulating with nothing surfacing it."""
    quality = [
        {"timestamp": "2026-08-19T00:00:00+00:00", "collection": "c", "pass_rate": 0.9},
        {"timestamp": "2026-08-20T00:00:00+00:00", "collection": "c", "pass_rate": 0.7},
        {"timestamp": "2026-08-21T00:00:00+00:00", "collection": "c", "pass_rate": 0.5},
    ]
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=quality)

    text = stats.format_stats(result, days=30)

    assert "Quality:" in text
    assert "50%" in text  # the latest pass rate
    assert any(level in text for level in stats._SPARK_LEVELS)


def test_format_stats_omits_quality_when_never_checked():
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=[])
    assert "Quality:" not in stats.format_stats(result, days=30)


def test_quality_checks_defaults_to_empty_for_existing_callers():
    """compute_stats() is called by other code paths — the new argument
    must be optional, not a signature break."""
    result = stats.compute_stats([], [], _INDEX_STATUS)
    assert result["quality_trend"] == []


def _quality(*rates):
    return [{"timestamp": f"2026-08-{10 + i}T00:00:00+00:00", "collection": "c", "pass_rate": r}
            for i, r in enumerate(rates)]


def test_format_stats_flags_a_falling_quality_trend():
    """A pass rate dropping across checks is the index-regression signal
    the history exists to expose."""
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=_quality(0.95, 0.60))

    assert "down" in stats.format_stats(result, days=30).lower()


def test_quality_trend_direction_survives_a_bad_first_point(monkeypatch):
    """[review finding] Comparing the latest check against the FIRST one in
    the window mislabels this series as "up": an old problem at the window's
    edge (0.50) makes a fresh regression (0.95 -> 0.60) look like an
    improvement, because 0.60 > 0.50. Comparing against the MEDIAN of the
    preceding checks is robust to both that and to a single noisy point."""
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=_quality(0.50, 0.95, 0.95, 0.60))

    assert "down" in stats.format_stats(result, days=30).lower()


def test_quality_trend_direction_ignores_a_single_noisy_dip():
    """The case the median is there to protect: one bad check between two
    healthy ones must not read as a regression."""
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=_quality(0.90, 0.50, 0.90))

    assert "down" not in stats.format_stats(result, days=30).lower()


def test_format_stats_separates_sections_with_blank_lines():
    """[user-requested] Every section ran together in one dense block, so
    the eye had nothing to anchor on. Each top-level section gets a blank
    line before it — the grouping is what makes a screen like this readable
    at a glance, not the numbers themselves."""
    quality = _quality(0.9, 0.8)
    result = stats.compute_stats([_run(indexed=1, skipped=9, spend_today_usd=0.5)],
                                 [_query(sources=["a.py"])], _INDEX_STATUS, quality_checks=quality)

    text = stats.format_stats(result, days=30)

    for section in ("Indexing:", "API spend:", "Queries:", "Quality:"):
        assert f"\n\n{section}" in text, f"{section} must be preceded by a blank line"


def test_format_stats_has_no_blank_line_before_the_first_section():
    """The header already separates it — a blank line there would just be
    a hole under the rule."""
    result = stats.compute_stats([], [], _INDEX_STATUS)
    assert "═\n\nIndex:" not in stats.format_stats(result, days=30)


def test_format_stats_does_not_end_with_trailing_blank_lines():
    result = stats.compute_stats([], [], _INDEX_STATUS)
    text = stats.format_stats(result, days=30)
    assert text == text.rstrip()


def test_stats_splits_queries_by_surface():
    """[the point of instrumenting griot_search] The first real-agent validation
    asks "is the MCP path being used, and does it work?" — a single query
    count answers neither. Recording `via` without surfacing it here would
    repeat the mistake of storing data nothing reads."""
    queries = [
        {"timestamp": "2026-08-21T10:00:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": []},
        {"timestamp": "2026-08-21T10:01:00+00:00", "via": "mcp", "duration_seconds": 2.0, "sources": []},
        {"timestamp": "2026-08-21T10:02:00+00:00", "via": "cli", "duration_seconds": 3.0, "sources": []},
    ]
    result = stats.compute_stats([], queries, _INDEX_STATUS)

    assert result["queries_by_surface"] == {"mcp": 2, "cli": 1}
    assert "mcp 2" in stats.format_stats(result, days=30)


def test_queries_without_a_surface_are_counted_as_unknown():
    """Records written before this field existed have no `via` — they must
    not vanish from the count, and must not be silently attributed to
    either surface."""
    result = stats.compute_stats([], [_query()], _INDEX_STATUS)
    assert result["queries_by_surface"] == {"unknown": 1}


def test_stats_reports_retrieval_quality_from_top_scores():
    """A count of searches says nothing about whether they FOUND anything.
    The median top score does — and it is the signal the validation needs."""
    queries = [
        {"timestamp": "2026-08-21T10:00:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [], "top_score": 0.9},
        {"timestamp": "2026-08-21T10:01:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [], "top_score": 0.5},
        {"timestamp": "2026-08-21T10:02:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [], "top_score": 0.7},
    ]
    result = stats.compute_stats([], queries, _INDEX_STATUS)

    assert result["median_top_score"] == 0.7
    assert "0.70" in stats.format_stats(result, days=30)


def test_searches_that_found_nothing_are_counted_separately():
    """A search returning zero results is the loudest possible signal that
    the index isn't answering — averaging it into a score would hide it."""
    queries = [
        {"timestamp": "2026-08-21T10:00:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [], "top_score": 0.9},
        {"timestamp": "2026-08-21T10:01:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [], "top_score": None, "num_sources": 0},
    ]
    result = stats.compute_stats([], queries, _INDEX_STATUS)

    assert result["empty_searches"] == 1
    assert "1 found nothing" in stats.format_stats(result, days=30)


def test_median_top_score_is_none_without_any_scored_query():
    result = stats.compute_stats([], [_query()], _INDEX_STATUS)
    assert result["median_top_score"] is None


def test_stats_reports_which_mcp_tools_were_called():
    """[orphan sweep] tool_calls was being written with nothing reading it —
    the same "stored but invisible" mistake the UI removal left behind for
    quality_checks. Recording a thing and surfacing it are two changes, and
    only the second one makes the data useful."""
    for tool in ["griot_search", "griot_search", "griot_index_status"]:
        logdb.write_tool_call(common.LOG_DIR, tool, ok=True, duration_seconds=0.1)

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=stats.load_tool_calls(30))

    assert result["tool_calls"] == {"griot_search": 2, "griot_index_status": 1}
    text = stats.format_stats(result, days=30)
    assert "MCP tools:" in text
    assert "griot_search 2" in text


def test_stats_flags_tools_that_failed():
    """A tool called often but always failing must not read as healthy."""
    logdb.write_tool_call(common.LOG_DIR, "griot_index_status", ok=False,
                          duration_seconds=0.1, error="RuntimeError: locked")
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, duration_seconds=0.1)

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=stats.load_tool_calls(30))

    assert result["failed_tool_calls"] == {"griot_index_status": 1}
    assert "1 failed" in stats.format_stats(result, days=30)


def test_stats_omits_the_tool_section_when_nothing_was_called():
    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[])
    assert "MCP tools:" not in stats.format_stats(result, days=30)


def test_legacy_quality_file_is_imported_by_the_reader():
    """[orphan sweep] logdb.migrate_legacy_json_file() lost its only caller
    when that front end was deleted, so an existing install's
    last_quality_check.json stopped being picked up. The reader is the
    natural place for it — same "reader triggers the one-time import"
    pattern the runs/queries migration already uses."""
    common.secure_mkdir(common.DATA_DIR)
    (common.DATA_DIR / "last_quality_check.json").write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(), "collection": "codebase__legacy",
        "self_check": {"sampled": 4, "passed": 3, "failed": 1, "failures": [], "avg_score": 0.6},
    }))

    trend = stats.load_quality_window(365)

    assert len(trend) == 1
    assert trend[0]["collection"] == "codebase__legacy"
    assert trend[0]["pass_rate"] == 0.75


def test_stats_shows_why_documents_failed():
    """[user-requested] `100 failures in the period` sent the reader to
    grep griot.log. The reasons are recorded now, so the report can say
    what went wrong — grouped, because a systemic failure repeats one
    reason across every document."""
    runs = [
        _run(indexed=1, failed=2),
        _run(indexed=1, failed=1),
    ]
    runs[0]["failures"] = [{"id": "r:commit:a", "reason": "embedding returned no vector"},
                           {"id": "r:commit:b", "reason": "embedding returned no vector"}]
    runs[1]["failures"] = [{"id": "r:code:x.py:0", "reason": "write to the vector store failed: locked"}]

    result = stats.compute_stats(runs, [], _INDEX_STATUS)

    assert result["failure_reasons"] == {
        "embedding returned no vector": 2,
        "write to the vector store failed: locked": 1,
    }
    text = stats.format_stats(result, days=30)
    assert "embedding returned no vector" in text


def test_stats_has_no_failure_reasons_when_nothing_failed():
    result = stats.compute_stats([_run(indexed=5)], [], _INDEX_STATUS)
    assert result["failure_reasons"] == {}
    assert "reason" not in stats.format_stats(result, days=30).lower()


def test_runs_recorded_before_failure_detail_existed_still_work():
    """Records written before `failures` existed have only the count — they
    must not break the report, just contribute nothing to the reasons."""
    result = stats.compute_stats([_run(indexed=1, failed=9)], [], _INDEX_STATUS)
    assert result["failure_reasons"] == {}
    assert "9 failures" in stats.format_stats(result, days=30)


def test_format_stats_includes_key_numbers():
    result = stats.compute_stats(
        [_run(indexed=2, skipped=8, spend_today_usd=0.05)],
        [_query(duration_seconds=1.5)],
        {"points_count": 999, "embed_profile": "jina-code"},
    )
    text = stats.format_stats(result, days=30)
    assert "999" in text
    assert "0.05" in text or "$0.05" in text
    assert "jina-code" in text


def test_format_stats_never_crashes_on_empty_stats():
    result = stats.compute_stats([], [], {"points_count": 0, "embed_profile": "jina-code"})
    text = stats.format_stats(result, days=30)
    assert isinstance(text, str) and len(text) > 0


# --- main() / --json ------------------------------------------------------


def test_main_json_output_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(stats.common, "get_index_status", lambda: {"points_count": 5, "embed_profile": "jina-code"})

    rc = stats.main(["--json"])

    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["points_count"] == 5


def test_main_text_output_runs_without_error(monkeypatch, capsys):
    monkeypatch.setattr(stats.common, "get_index_status", lambda: {"points_count": 0, "embed_profile": "jina-code"})

    rc = stats.main([])

    assert rc == 0
    assert "griot" in capsys.readouterr().out.lower()


# --- load_window(): I/O half of main(), reusable without ------------ #
# --- reaching into this module's private _read_jsonl/_filter_by_days) --- #


def test_load_window_returns_filtered_runs_and_queries():
    common.log_run_summary(script="index_code.py", indexed=1, skipped=0, failed=0)
    common.log_query(question="q", model="m", limit=1, num_sources=0, sources=[])

    runs, queries = stats.load_window(30)

    assert len(runs) == 1 and runs[0]["script"] == "index_code.py"
    assert len(queries) == 1 and queries[0]["question"] == "q"


def test_load_window_excludes_records_outside_the_window():
    common.log_run_summary(script="index_code.py", indexed=1, skipped=0, failed=0)
    old = {"timestamp": "2020-01-01T00:00:00+00:00", "indexed": 1, "skipped": 0, "failed": 0}
    logdb.write_run(common.LOG_DIR, old)

    runs, queries = stats.load_window(30)

    assert len(runs) == 1
    assert queries == []


def test_load_quality_window_reads_what_the_quality_check_actually_wrote():
    """End-to-end over the real store: a record written the way a
    quality-check writes one must come back out shaped for the trend. The
    unit tests above all hand-build the reduced dicts, so nothing else
    proves the two halves agree on the record's actual shape."""
    logdb.write_quality_check(common.LOG_DIR, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "collection": "codebase__jina-code",
        "self_check": {"sampled": 10, "passed": 8, "failed": 2, "failures": [], "avg_score": 0.7},
    })

    entries = stats.load_quality_window(30)

    assert len(entries) == 1
    assert entries[0]["pass_rate"] == 0.8
    assert entries[0]["collection"] == "codebase__jina-code"


def test_load_quality_window_drops_a_check_that_sampled_nothing():
    # A timestamp outside the window would make this pass for the wrong reason.
    logdb.write_quality_check(common.LOG_DIR, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "collection": "c",
        "self_check": {"sampled": 0, "passed": 0, "failed": 0, "failures": [], "avg_score": None},
    })

    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=stats.load_quality_window(30))

    assert result["quality_trend"] == []


def test_load_window_missing_db_returns_empty_lists():
    assert not (common.LOG_DIR / logdb.DB_FILENAME).exists()
    runs, queries = stats.load_window(30)
    assert runs == []
    assert queries == []


def test_compute_stats_survives_a_run_that_died(monkeypatch):
    """[real crash, reported from production] `griot stats` died with
    "unsupported operand type(s) for +: 'int' and 'NoneType'".

    A run that died before finishing with counts set to
    None ON PURPOSE — 0 would read as "it ran and did nothing", which is a
    different fact and distorts the totals. But `r.get("indexed", 0)` only
    yields the default when the KEY IS ABSENT; a key present and null gives
    back None, and the sum blows up. The same None-versus-absent confusion
    that produced the golden-set guard bug, in a third place.

    The recent-window slice below already wrote `or 0` — the
    right pattern existed in this very function and the older lines never
    caught up. A dead run contributes nothing to a total; it must not take
    the whole report down with it."""
    runs = [
        _run(indexed=10, skipped=5),
        {"timestamp": "2026-08-13T11:00:00+00:00", "indexed": None, "skipped": None,
         "failed": None, "error": "KeyboardInterrupt"},
        _run(indexed=3, skipped=27),
    ]

    result = stats.compute_stats(runs, [], {"points_count": 100, "embed_profile": "jina-code"})

    assert result["total_indexed"] == 13
    assert result["total_skipped"] == 32
    assert result["total_failed"] == 0
    # The dead run is still a run — it happened, and hiding it would make the
    # report claim a clean history the user did not have.
    assert result["num_runs"] == 3


def test_compute_stats_reuse_rate_ignores_a_dead_run(monkeypatch):
    """A dead run has no counts, so it cannot move a ratio in either
    direction — reuse must read exactly as it would without it."""
    dead = {"timestamp": "2026-08-13T11:00:00+00:00", "indexed": None,
            "skipped": None, "failed": None, "error": "died"}

    with_dead = stats.compute_stats([_run(indexed=1, skipped=9), dead], [],
                                    {"points_count": 10, "embed_profile": "jina-code"})
    without = stats.compute_stats([_run(indexed=1, skipped=9)], [],
                                  {"points_count": 10, "embed_profile": "jina-code"})

    assert with_dead["reuse_rate"] == without["reuse_rate"] == 0.9


def test_report_says_why_the_point_count_is_missing():
    """[real output, 2026-08-22] The report printed "Index: None points"
    when another process held the collection open — the exact "None indexed,
    None skipped" illusion that decisionremoved from the UI, in the surface
    that replaced it.

    None here is not zero and not an error: it means "could not read while
    something else has it open", which is normal on this machine (the MCP
    server holds the handle for its whole life in single mode). The line has
    to say that, because a reader seeing a number's absence assumes the
    index is empty."""
    s = stats.compute_stats([], [], {"points_count": None, "embed_profile": "openai-small"})

    report = stats.format_stats(s, days=30)

    assert "None points" not in report
    assert "in use" in report or "locked" in report


def test_report_still_prints_a_real_point_count():
    s = stats.compute_stats([], [], {"points_count": 4210, "embed_profile": "jina-code"})

    assert "4210 points" in stats.format_stats(s, days=30)


def _dead(timestamp="2026-08-13T11:00:00+00:00", error="KeyboardInterrupt: "):
    return {"timestamp": timestamp, "indexed": None, "skipped": None,
            "failed": None, "error": error}


def test_compute_stats_counts_dead_runs_separately():
    """[review finding] Counting a dead run in num_runs is right — hiding it
    would let the report claim a clean history the user did not have. But the
    decision stopped there: total_failed is 0 for a dead run, and the failure
    section is gated on total_failed, so a window where every run died prints
    "3 runs · 0 embedded · 0 skipped" and never says anything died.

    The dead-run record exists so a run that starts leaves a trace. It used to crash
    (loud but visible); after my fix it went silent instead. A dead run is
    not a failure — a failure counted failures, a dead run counted nothing —
    so it needs its own number, not a merge into total_failed."""
    runs = [_run(indexed=10, skipped=5), _dead(), _dead(error="MemoryError: ")]

    result = stats.compute_stats(runs, [], {"points_count": 1, "embed_profile": "jina-code"})

    assert result["dead_runs"] == 2
    assert result["total_failed"] == 0
    assert result["num_runs"] == 3
    assert "MemoryError" in result["last_error"]


def test_report_says_runs_died_even_with_no_counted_failures():
    runs = [_dead(), _dead(), _dead()]

    report = stats.format_stats(stats.compute_stats(
        runs, [], {"points_count": 0, "embed_profile": "jina-code"}), days=30)

    assert "died" in report
    assert "3" in report


def test_low_reuse_warning_ignores_dead_runs():
    """[review finding, reproduced] The warning is gated on num_runs > 1
    because a FIRST index reuses nothing by definition. A dead run inflates
    num_runs, so Ctrl-C followed by a genuine first index trips the warning —
    and Ctrl-C during a first index is the most common way a run dies. That
    is the same cry-wolf the recent slice was built to stop, coming back
    through another door."""
    runs = [_dead(), _run(indexed=5000, skipped=0)]

    report = stats.format_stats(stats.compute_stats(
        runs, [], {"points_count": 5000, "embed_profile": "jina-code"}), days=30)

    assert "low reuse" not in report


def test_recent_reuse_ignores_dead_runs_when_slicing():
    """[review finding, reproduced] The trailing slice is sized to "a bit more
    than one full griot index all". Dead runs occupy slots without
    contributing counts, so recent_reuse_rate could be computed from a single
    real run while being presented — by the CLI line and by the stats prompt
    — as the figure to trust."""
    runs = [_run(indexed=1, skipped=9) for _ in range(6)] + [_dead() for _ in range(4)] + \
           [_run(indexed=0, skipped=10)]

    result = stats.compute_stats(runs, [], {"points_count": 1, "embed_profile": "jina-code"})

    # The slice must reach past the dead runs for five runs that actually
    # counted something, not stop at one.
    assert result["recent_skipped"] == 10 + 9 * 4
