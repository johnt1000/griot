"""`griot stats` draws its trends as terminal charts (debt 6, resolved
2026-10-06): spend per day and the quality trend as small column charts,
the source breakdown as horizontal bars. Plain unicode, no dependency, and
only on a terminal that can show them: piped output, NO_COLOR, a narrow
terminal and --json keep the plain text.

These tests render to a string at a FIXED width and assert structure
(proportions, the zero, the largest value filling the space, labels intact),
never the exact art.
"""

import io
import json
import os
import re

import pytest

from griot import common, stats

_INDEX_STATUS = {"points_count": 10, "embed_profile": "jina-code"}
_FULL = stats._BAR_FULL


def _run(spend, timestamp):
    return {"timestamp": timestamp, "indexed": 1, "skipped": 0, "failed": 0, "spend_today_usd": spend}


def _query(sources, timestamp="2026-08-20T10:00:00+00:00"):
    return {"timestamp": timestamp, "duration_seconds": 1.0, "sources": sources, "num_sources": len(sources)}


def _recent_query(sources):
    """A query `griot stats` itself would count: inside its window (local
    days up to now) and in the active collection (its default scope)."""
    from datetime import datetime, timezone
    return {**_query(sources, datetime.now(timezone.utc).isoformat()), "collection": stats.common.COLLECTION_NAME}


class _Stream(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def _columns(monkeypatch, n):
    monkeypatch.setattr(stats.shutil, "get_terminal_size", lambda *a, **k: os.terminal_size((n, 40)))


def _chart_rows(text, marker):
    """The rows of the column chart whose header line contains `marker`:
    every line from the header up to the axis line (inclusive)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    end = next(i for i in range(start, len(lines)) if "└" in lines[i])
    return lines[start + 1:end], lines[end]


def _column_cells(rows, axis):
    """Per-column vertical slices, read at the axis' column positions."""
    first = axis.index("└") + 1
    count = len(axis) - first
    return [[row[first + i] if first + i < len(row) else " " for row in rows] for i in range(count)]


# --- when to draw --------------------------------------------------------


def test_no_chart_when_stdout_is_not_a_terminal(monkeypatch):
    _columns(monkeypatch, 120)
    assert stats._chart_width(_Stream(tty=False), {}) is None


def test_no_chart_for_a_stream_that_cannot_say(monkeypatch):
    """A replaced stdout without isatty(), or a closed one, is not a terminal."""
    _columns(monkeypatch, 120)
    closed = io.StringIO()
    closed.close()  # isatty() now raises ValueError

    assert stats._chart_width(object(), {}) is None
    assert stats._chart_width(closed, {}) is None


def test_no_chart_when_no_color_is_set(monkeypatch):
    _columns(monkeypatch, 120)
    assert stats._chart_width(_Stream(tty=True), {"NO_COLOR": "1"}) is None


def test_an_empty_no_color_does_not_count(monkeypatch):
    """no-color.org: the variable counts when present AND not empty."""
    _columns(monkeypatch, 80)
    assert stats._chart_width(_Stream(tty=True), {"NO_COLOR": ""}) == 80


def test_no_chart_on_a_narrow_terminal(monkeypatch):
    _columns(monkeypatch, stats._MIN_CHART_WIDTH - 1)
    assert stats._chart_width(_Stream(tty=True), {}) is None


def test_a_terminal_at_the_minimum_width_gets_charts(monkeypatch):
    _columns(monkeypatch, stats._MIN_CHART_WIDTH)
    assert stats._chart_width(_Stream(tty=True), {}) == stats._MIN_CHART_WIDTH


def test_a_very_wide_terminal_is_capped(monkeypatch):
    _columns(monkeypatch, 400)
    assert stats._chart_width(_Stream(tty=True), {}) == stats._MAX_CHART_WIDTH


def test_the_columns_variable_decides_the_width(monkeypatch):
    """shutil.get_terminal_size() honours COLUMNS before asking the
    terminal: a user who sets it gets the width they asked for, and one
    narrower than a chart gets the plain text."""
    monkeypatch.setenv("COLUMNS", "72")
    assert stats._chart_width(_Stream(tty=True), {}) == 72
    monkeypatch.setenv("COLUMNS", str(stats._MIN_CHART_WIDTH - 1))
    assert stats._chart_width(_Stream(tty=True), {}) is None


# --- plain text stays what it was ---------------------------------------


def _rich_result():
    runs = [
        _run(0.10, "2026-08-19T10:00:00+00:00"),
        _run(0.40, "2026-08-21T10:00:00+00:00"),
    ]
    queries = [_query(["repo/a.py", "repo/b.py", "commit abc", "tag v1"])]
    quality = [
        {"timestamp": "2026-08-19T10:00:00+00:00", "collection": "c", "pass_rate": 1.0},
        {"timestamp": "2026-08-20T10:00:00+00:00", "collection": "c", "pass_rate": 0.5},
    ]
    return stats.compute_stats(runs, queries, _INDEX_STATUS, quality_checks=quality)


def test_without_a_width_the_report_is_the_plain_text():
    text = stats.format_stats(_rich_result(), days=30)

    assert "└" not in text and "┤" not in text
    assert "spend trend, 2 days" in text
    assert "most used sources: code 50%" in text


def test_every_chart_line_fits_the_width():
    """The charts size themselves to the width. Prose lines are not wrapped
    (they never were); this is about what the charts draw."""
    glyphs = set(stats._EIGHTHS.strip() + stats._BAR_EMPTY + "│┤└→")
    for width in (stats._MIN_CHART_WIDTH, 80, stats._MAX_CHART_WIDTH):
        text = stats.format_stats(_rich_result(), days=30, width=width)
        chart_lines = [line for line in text.splitlines() if glyphs & set(line)]
        assert len(chart_lines) > 10
        assert max(len(line) for line in chart_lines) <= width, width


# --- spend per day --------------------------------------------------------


def test_spend_is_one_column_per_calendar_day_with_the_gap_as_zero(monkeypatch):
    """A day with no recorded spend is a real day at zero, not a skipped
    one: collapsing it would draw two days apart as neighbours."""
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    runs = [
        _run(0.001, "2026-08-19T12:00:00+00:00"),  # under one eighth of a row
        _run(0.40, "2026-08-21T12:00:00+00:00"),
    ]
    result = stats.compute_stats(runs, [], _INDEX_STATUS)
    dates = [p["date"] for p in result["spend_by_date"]]

    text = stats.format_stats(result, days=30, width=80)
    rows, axis = _chart_rows(text, "spend per day")
    cells = _column_cells(rows, axis)

    assert len(cells) == 3
    small, gap, largest = cells
    assert all(c == _FULL for c in largest)  # the largest value fills the height
    assert all(c == " " for c in gap)  # zero: nothing above the axis
    assert small[-1] != " "  # a tiny spend is still visibly not zero
    assert all(c == " " for c in small[:-1])
    assert dates[0] in text and dates[-1] in text
    assert "$0.4000" in text  # the scale is labelled


def test_spend_chart_needs_more_than_one_day():
    result = stats.compute_stats([_run(0.10, "2026-08-19T12:00:00+00:00")], [], _INDEX_STATUS)
    assert "spend per day" not in stats.format_stats(result, days=30, width=80)


def test_spend_chart_keeps_the_most_recent_days_that_fit_and_says_so():
    runs = [_run(0.01 * (i + 1), f"2026-{5 + i // 28:02d}-{1 + i % 28:02d}T12:00:00+00:00") for i in range(100)]
    result = stats.compute_stats(runs, [], _INDEX_STATUS)
    total_days = len(stats._fill_days(result["spend_by_date"]))
    last = result["spend_by_date"][-1]["date"]

    text = stats.format_stats(result, days=365, width=stats._MIN_CHART_WIDTH)
    rows, axis = _chart_rows(text, "spend per day")
    shown = len(_column_cells(rows, axis))

    assert shown < total_days
    assert max(len(line) for line in rows + [axis]) <= stats._MIN_CHART_WIDTH
    assert f"last {shown} of {total_days} days" in text
    assert last in text
    assert all(c == _FULL for c in _column_cells(rows, axis)[-1])  # the last day is the largest


def test_spend_dates_are_iso_calendar_days():
    """The spend chart reads spend_by_date as YYYY-MM-DD to lay out a time
    axis. Pinned here so a change to how compute_stats() writes the date
    fails in this file, not as a crash on someone's terminal."""
    result = stats.compute_stats([_run(0.10, "2026-08-19T12:00:00+00:00"),
                                  _run(0.20, "2026-08-21T12:00:00+00:00")], [], _INDEX_STATUS)
    for point in result["spend_by_date"]:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", point["date"]), point


@pytest.mark.parametrize("bad", ["19/08/2026", "2026-08-19T12:00", "", None])
def test_a_date_the_chart_cannot_place_falls_back_to_the_sparkline(bad):
    """Charts are a nicety: a date in a form the time axis cannot read must
    not crash `griot stats` on a terminal when the plain text and --json
    would have worked. The report keeps the one-line trend instead."""
    result = stats.compute_stats([_run(0.10, "2026-08-19T12:00:00+00:00"),
                                  _run(0.20, "2026-08-21T12:00:00+00:00")], [], _INDEX_STATUS)
    result["spend_by_date"][0]["date"] = bad

    text = stats.format_stats(result, days=30, width=80)

    assert "spend per day" not in text
    assert "spend trend, 2 days" in text


# --- source breakdown ----------------------------------------------------


def _bar_lines(text, labels):
    lines = text.splitlines()
    return {label: next(line for line in lines if line.strip().startswith(label + " ")) for label in labels}


def test_source_breakdown_bars_are_proportional_and_the_largest_fills():
    queries = [_query(["r/a.py"] * 6 + ["commit x"] * 3 + ["tag y"])]
    result = stats.compute_stats([], queries, _INDEX_STATUS)

    text = stats.format_stats(result, days=30, width=80)
    bars = _bar_lines(text, ["code", "commit", "tag"])
    full = {label: line.count(_FULL) for label, line in bars.items()}
    width = len(bars["code"].split()[1])  # the whole bar, full and empty cells

    assert full["code"] == width  # the largest value fills the bar
    assert full["commit"] == round(width * 3 / 6)
    assert full["tag"] == round(width * 1 / 6)
    assert "60%" in bars["code"] and "30%" in bars["commit"] and "10%" in bars["tag"]
    assert "most used sources: code" not in text  # the bars replace the line


def test_source_labels_are_aligned_and_never_truncated():
    queries = [_query(["r/a.py", "MR !1", "MR !2", "release v1"])]
    result = stats.compute_stats([], queries, _INDEX_STATUS)

    text = stats.format_stats(result, days=30, width=stats._MIN_CHART_WIDTH)
    bars = _bar_lines(text, ["code", "merge_request", "release"])

    starts = {line.index(stats._BAR_FULL) for line in bars.values()}
    assert len(starts) == 1  # every bar starts in the same column


def test_a_label_too_long_for_bars_falls_back_to_the_text_line():
    """Labels are never cut into ambiguity: when the longest one leaves no
    room for a readable bar, the breakdown stays a sentence."""
    long_kind = "x" * 60
    result = stats.compute_stats([], [], _INDEX_STATUS)
    result["source_breakdown"] = {long_kind: 2, "code": 1}
    result["num_queries"] = 1

    text = stats.format_stats(result, days=30, width=stats._MIN_CHART_WIDTH)

    assert f"most used sources: {long_kind} 67%" in text


def test_a_bar_that_would_be_a_stub_falls_back_to_the_text_line():
    """Room for a few cells is not room for a proportion: a label that
    leaves a positive but tiny bar still gets the sentence, not a stub."""
    result = stats.compute_stats([], [], _INDEX_STATUS)
    result["num_queries"] = 1
    width = stats._MIN_CHART_WIDTH
    suffix = "  67% (2)"  # the percentage is right-aligned in three columns
    # The longest label that still leaves a bar, but one cell short of readable.
    kind = "y" * (width - len(stats._INDENT) - 2 - 1 - len(suffix) - (stats._MIN_BAR_WIDTH - 1))
    result["source_breakdown"] = {kind: 2, "code": 1}

    text = stats.format_stats(result, days=30, width=width)
    assert f"most used sources: {kind} 67%" in text

    # One character shorter and the bar is exactly readable: it is drawn.
    result["source_breakdown"] = {kind[:-1]: 2, "code": 1}
    assert stats._BAR_FULL * stats._MIN_BAR_WIDTH in stats.format_stats(result, days=30, width=width)


# --- quality trend -------------------------------------------------------


def test_quality_columns_use_a_fixed_zero_to_hundred_scale():
    """0.95 next to 0.96 is not a cliff: the columns read against 0-100%,
    unlike the sparkline, which scales to its own range."""
    quality = [
        {"timestamp": "2026-08-19T10:00:00+00:00", "collection": "c", "pass_rate": 1.0},
        {"timestamp": "2026-08-20T10:00:00+00:00", "collection": "c", "pass_rate": 0.5},
        {"timestamp": "2026-08-21T10:00:00+00:00", "collection": "c", "pass_rate": 0.0},
    ]
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=quality)

    text = stats.format_stats(result, days=30, width=80)
    rows, axis = _chart_rows(text, "pass rate per check")
    full, half, zero = _column_cells(rows, axis)

    height = len(rows)
    assert all(c == _FULL for c in full)
    assert half.count(_FULL) == height // 2 and all(c == " " for c in half[: height // 2])
    assert all(c == " " for c in zero)
    assert "100%" in text
    assert "trend down" in text  # the interpretation line stays


def test_quality_columns_do_not_stretch_a_low_best_to_the_top():
    quality = [
        {"timestamp": "2026-08-19T10:00:00+00:00", "collection": "c", "pass_rate": 0.5},
        {"timestamp": "2026-08-20T10:00:00+00:00", "collection": "c", "pass_rate": 0.25},
    ]
    result = stats.compute_stats([], [], _INDEX_STATUS, quality_checks=quality)

    rows, axis = _chart_rows(stats.format_stats(result, days=30, width=80), "pass rate per check")
    best, _ = _column_cells(rows, axis)

    assert best.count(_FULL) == len(rows) // 2


# --- main() ---------------------------------------------------------------


def test_main_draws_charts_only_when_the_terminal_allows(monkeypatch, capsys):
    monkeypatch.setattr(stats.common, "get_index_status", lambda: _INDEX_STATUS)
    monkeypatch.setattr(stats, "load_window", lambda days, *_: ([], [_recent_query(["r/a.py", "commit x"])]))

    monkeypatch.setattr(stats, "_chart_width", lambda *a, **k: 80)
    stats.main([])
    charted = capsys.readouterr().out

    monkeypatch.setattr(stats, "_chart_width", lambda *a, **k: None)
    stats.main([])
    plain = capsys.readouterr().out

    assert _FULL in charted and "most used sources: code 50%" not in charted
    assert "most used sources: code 50%" in plain


def test_main_reads_the_real_stdout_and_environment(monkeypatch, capsys):
    """capsys' stdout is not a terminal, so the real decision gives text."""
    monkeypatch.setattr(stats.common, "get_index_status", lambda: _INDEX_STATUS)
    monkeypatch.setattr(stats, "load_window", lambda days, *_: ([], [_recent_query(["r/a.py", "commit x"])]))
    _columns(monkeypatch, 120)

    stats.main([])

    assert "most used sources: code 50%" in capsys.readouterr().out


def test_json_output_ignores_the_terminal(monkeypatch, capsys):
    monkeypatch.setattr(stats.common, "get_index_status", lambda: _INDEX_STATUS)
    monkeypatch.setattr(stats, "load_window", lambda days, *_: ([], [_recent_query(["r/a.py"])]))
    monkeypatch.setattr(stats, "_chart_width", lambda *a, **k: 80)

    stats.main(["--json"])

    parsed = json.loads(capsys.readouterr().out)
    assert parsed["source_breakdown"] == {"code": 1}
