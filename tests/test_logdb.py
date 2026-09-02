"""Tests for src/griot/logdb.py — SQLite-backed storage for run/query
history (logs/logs.db), replacing runs.jsonl/queries.jsonl. Motivation
(user-requested, 2026-08-21): stats.py/get_index_status() had to read and
re-parse the WHOLE unbounded file on every call (no rotation); a
timestamp-indexed table makes day-window reads and "most recent run for
this collection" real indexed lookups instead of linear scans.
"""

import json
import sqlite3
import stat
from datetime import datetime, timezone

import pytest

from griot import logdb


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


# --- write_run / write_query round-trip -------------------------------


def test_write_and_read_run_round_trips_all_fields(tmp_path):
    record = {"timestamp": "2026-08-21T10:00:00+00:00", "profile": "jina-code",
              "collection": "codebase__jina-code", "script": "index_code.py",
              "repo": "r", "indexed": 3, "skipped": 1, "failed": 0,
              "duration_seconds": 1.23, "spend_today_usd": 0.01}
    logdb.write_run(tmp_path, record)

    runs = logdb.read_since(tmp_path, "runs", days=30)

    assert runs == [record]


def test_write_and_read_query_round_trips_all_fields(tmp_path):
    record = {"timestamp": "2026-08-21T10:00:00+00:00", "profile": "jina-code",
              "collection": "codebase__jina-code", "question": "q",
              "model": "m", "chat_profile": "gemini", "limit": 5,
              "num_sources": 2, "duration_seconds": 0.5,
              "sources": ["code:a.py", "commit abc"], "spend_today_usd": 0.02}
    logdb.write_query(tmp_path, record)

    queries = logdb.read_since(tmp_path, "queries", days=30)

    assert queries == [record]


def test_read_since_returns_empty_list_when_nothing_written(tmp_path):
    assert logdb.read_since(tmp_path, "runs", days=30) == []
    assert logdb.read_since(tmp_path, "queries", days=30) == []


def test_read_since_rejects_unknown_table(tmp_path):
    with pytest.raises(ValueError):
        logdb.read_since(tmp_path, "not-a-real-table", days=30)


# --- day-window narrowing ----------------------------------------------


def test_read_since_excludes_records_older_than_the_window(tmp_path):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    old = {"timestamp": "2020-01-01T00:00:00+00:00", "indexed": 1}
    recent = {"timestamp": "2026-08-20T12:00:00+00:00", "indexed": 2}
    logdb.write_run(tmp_path, old)
    logdb.write_run(tmp_path, recent)

    runs = logdb.read_since(tmp_path, "runs", days=30, now=now)

    assert runs == [recent]


def test_read_since_orders_by_timestamp_ascending(tmp_path):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    first = {"timestamp": "2026-08-19T00:00:00+00:00", "indexed": 1}
    second = {"timestamp": "2026-08-20T00:00:00+00:00", "indexed": 2}
    logdb.write_run(tmp_path, second)
    logdb.write_run(tmp_path, first)

    runs = logdb.read_since(tmp_path, "runs", days=30, now=now)

    assert runs == [first, second]


# --- most_recent_run_for_collection() (get_index_status()'s read) ------


def test_most_recent_run_for_collection_returns_none_when_nothing_stored(tmp_path):
    assert logdb.most_recent_run_for_collection(tmp_path, "codebase__x") is None


def test_most_recent_run_for_collection_picks_latest_matching(tmp_path):
    logdb.write_run(tmp_path, {"timestamp": "2026-08-19T00:00:00+00:00", "collection": "codebase__x", "script": "index_code.py"})
    logdb.write_run(tmp_path, {"timestamp": "2026-08-20T00:00:00+00:00", "collection": "codebase__other", "script": "index_code.py"})
    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "collection": "codebase__x", "script": "index_tags.py"})

    record = logdb.most_recent_run_for_collection(tmp_path, "codebase__x")

    assert record["script"] == "index_tags.py"


# --- one-time migration from legacy runs.jsonl/queries.jsonl -----------


def test_migrates_existing_runs_jsonl_on_first_use(tmp_path):
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"timestamp": "2026-08-20T00:00:00+00:00", "indexed": 5}) + "\n"
    )

    runs = logdb.read_since(tmp_path, "runs", days=365)

    assert len(runs) == 1 and runs[0]["indexed"] == 5


def test_migrates_existing_queries_jsonl_on_first_use(tmp_path):
    (tmp_path / "queries.jsonl").write_text(
        json.dumps({"timestamp": "2026-08-20T00:00:00+00:00", "question": "old q"}) + "\n"
    )

    queries = logdb.read_since(tmp_path, "queries", days=365)

    assert len(queries) == 1 and queries[0]["question"] == "old q"


def test_migration_skips_corrupted_lines_without_crashing(tmp_path):
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"timestamp": "2026-08-20T00:00:00+00:00", "indexed": 1}) + "\n"
        "not json\n"
        + json.dumps({"timestamp": "2026-08-20T00:00:01+00:00", "indexed": 2}) + "\n"
    )

    runs = logdb.read_since(tmp_path, "runs", days=365)

    assert [r["indexed"] for r in runs] == [1, 2]


def test_migration_runs_only_once_does_not_duplicate(tmp_path):
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"timestamp": "2026-08-20T00:00:00+00:00", "indexed": 1}) + "\n"
    )
    logdb.read_since(tmp_path, "runs", days=365)  # triggers migration
    logdb.read_since(tmp_path, "runs", days=365)  # must not re-import
    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 2})

    runs = logdb.read_since(tmp_path, "runs", days=365)

    assert len(runs) == 2


def test_migration_preserves_insertion_order_for_most_recent_lookup(tmp_path):
    """[correctness, not just performance] most_recent_run_for_collection()
    relies on higher id == more recent (see get_index_status()'s original
    'append-only, last line wins' invariant) — the one-time legacy import
    must insert historical records BEFORE any live write can happen, or an
    old imported record could end up with a higher id than a genuinely
    newer live write and be picked as 'most recent' incorrectly."""
    (tmp_path / "runs.jsonl").write_text(
        json.dumps({"timestamp": "2020-01-01T00:00:00+00:00", "collection": "c", "script": "legacy"}) + "\n"
    )

    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "collection": "c", "script": "fresh"})

    record = logdb.most_recent_run_for_collection(tmp_path, "c")
    assert record["script"] == "fresh"


def test_missing_jsonl_files_do_not_error(tmp_path):
    assert logdb.read_since(tmp_path, "runs", days=30) == []
    assert logdb.read_since(tmp_path, "queries", days=30) == []


def test_read_from_a_log_dir_that_does_not_exist_yet_does_not_crash(tmp_path):
    """[real bug found via a subprocess integration test] A brand-new
    install (or a fresh env in a subprocess) has no logs/ directory at
    all yet — a read must not assume secure_mkdir() already ran (only the
    write path does that today, in common.py)."""
    fresh_log_dir = tmp_path / "logs"
    assert not fresh_log_dir.exists()

    assert logdb.read_since(fresh_log_dir, "runs", days=30) == []
    assert logdb.most_recent_run_for_collection(fresh_log_dir, "codebase__x") is None


# --- quality_checks table ----------------------------------------------
# [user-requested, 2026-08-21] last_quality_check.json only ever held the
# LAST result — every run overwrote the previous one, so quality history
# was thrown away. A table keeps it, which is what makes a pass-rate trend
# possible at all.


def test_write_and_read_quality_check_round_trips(tmp_path):
    record = {"timestamp": "2026-08-21T10:00:00+00:00", "collection": "codebase__jina-code",
              "self_check": {"passed": 8, "sampled": 10, "avg_score": 0.71, "failures": []}}
    logdb.write_quality_check(tmp_path, record)

    assert logdb.read_since(tmp_path, "quality_checks", days=30) == [record]


def test_read_since_accepts_quality_checks_table(tmp_path):
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
    logdb.write_quality_check(tmp_path, {"timestamp": "2020-01-01T00:00:00+00:00", "collection": "c"})
    logdb.write_quality_check(tmp_path, {"timestamp": "2026-08-20T12:00:00+00:00", "collection": "c"})

    kept = logdb.read_since(tmp_path, "quality_checks", days=30, now=now)

    assert len(kept) == 1 and kept[0]["timestamp"] == "2026-08-20T12:00:00+00:00"




def test_migrates_legacy_single_json_file_once(tmp_path):
    legacy = tmp_path / "last_quality_check.json"
    legacy.write_text(json.dumps({"timestamp": "2026-08-20T00:00:00+00:00", "collection": "c"}))

    logdb.migrate_legacy_json_file(tmp_path, legacy, "quality_checks")
    logdb.migrate_legacy_json_file(tmp_path, legacy, "quality_checks")  # must not re-import

    assert len(logdb.read_since(tmp_path, "quality_checks", days=365)) == 1


def test_migrating_a_missing_legacy_file_is_a_noop(tmp_path):
    logdb.migrate_legacy_json_file(tmp_path, tmp_path / "nope.json", "quality_checks")
    assert logdb.read_since(tmp_path, "quality_checks", days=365) == []


def test_migrating_a_corrupted_legacy_file_does_not_crash(tmp_path):
    legacy = tmp_path / "last_quality_check.json"
    legacy.write_text("{not valid json")

    logdb.migrate_legacy_json_file(tmp_path, legacy, "quality_checks")

    assert logdb.read_since(tmp_path, "quality_checks", days=365) == []


def test_legacy_json_migration_never_deletes_the_original_file(tmp_path):
    """Same discipline the runs.jsonl/queries.jsonl migration follows —
    griot never deletes user data as a side effect of a storage change."""
    legacy = tmp_path / "last_quality_check.json"
    legacy.write_text(json.dumps({"timestamp": "2026-08-20T00:00:00+00:00"}))

    logdb.migrate_legacy_json_file(tmp_path, legacy, "quality_checks")

    assert legacy.exists()


# --- spend state (atomic accumulation) ---------------------------------


def test_write_spend_accumulates_within_the_same_day(tmp_path):
    logdb.write_spend(tmp_path, "2026-08-21", 0.5, now=1000.0, window_seconds=300)
    logdb.write_spend(tmp_path, "2026-08-21", 0.25, now=1001.0, window_seconds=300)

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(0.75)


def test_read_spend_today_ignores_another_days_total(tmp_path):
    logdb.write_spend(tmp_path, "2026-08-20", 5.0, now=1000.0, window_seconds=300)
    assert logdb.read_spend_today(tmp_path, "2026-08-21") == 0.0


def test_write_spend_on_a_new_day_replaces_instead_of_adding(tmp_path):
    logdb.write_spend(tmp_path, "2026-08-20", 5.0, now=1000.0, window_seconds=300)
    logdb.write_spend(tmp_path, "2026-08-21", 0.25, now=2000.0, window_seconds=300)

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(0.25)


def test_read_spend_today_is_zero_when_nothing_recorded(tmp_path):
    assert logdb.read_spend_today(tmp_path, "2026-08-21") == 0.0


def test_velocity_sums_only_events_inside_the_window(tmp_path):
    logdb.write_spend(tmp_path, "2026-08-21", 0.4, now=1000.0, window_seconds=300)
    logdb.write_spend(tmp_path, "2026-08-21", 0.1, now=1200.0, window_seconds=300)

    assert logdb.read_spend_velocity(tmp_path, since=1100.0) == pytest.approx(0.1)
    assert logdb.read_spend_velocity(tmp_path, since=900.0) == pytest.approx(0.5)


def test_velocity_is_zero_when_no_events(tmp_path):
    assert logdb.read_spend_velocity(tmp_path, since=0.0) == 0.0


def test_write_spend_prunes_events_older_than_the_window(tmp_path):
    logdb.write_spend(tmp_path, "2026-08-21", 0.4, now=1000.0, window_seconds=300)
    logdb.write_spend(tmp_path, "2026-08-21", 0.1, now=5000.0, window_seconds=300)

    # the first event is far outside the window of the second write and
    # must have been deleted, so it can never be summed again
    assert logdb.read_spend_velocity(tmp_path, since=0.0) == pytest.approx(0.1)


# --- one-time migration of the legacy .spend_state.json ----------------


def test_legacy_spend_state_is_imported_when_it_is_from_today(tmp_path):
    """Losing a mid-day total on upgrade would reset the ceiling to zero
    and authorize a second full day of paid calls."""
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text(json.dumps({"date": "2026-08-21", "spend_usd": 1.5, "recent_events": [[1000.0, 1.5]]}))

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(1.5)
    assert logdb.read_spend_velocity(tmp_path, since=0.0) == pytest.approx(1.5)


def test_legacy_spend_state_from_another_day_is_not_imported(tmp_path):
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text(json.dumps({"date": "2026-08-01", "spend_usd": 9.99, "recent_events": []}))

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == 0.0


def test_legacy_spend_state_is_imported_only_once(tmp_path):
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text(json.dumps({"date": "2026-08-21", "spend_usd": 1.5, "recent_events": []}))

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")
    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(1.5)
    assert legacy.exists()  # never deleted


def test_legacy_spend_import_stamps_todays_date_on_a_stale_row(tmp_path):
    """[review finding, defense in depth] The import UPSERTs onto the
    single spend_state row. Today that row is always absent when the
    migration runs (it happens before any normal write), so the conflict
    branch is unreachable — but if it ever became reachable against a row
    left from an older day, adding the migrated total WITHOUT stamping the
    new date would hide it behind a stale date, and the next normal write
    would see the date mismatch and silently reset it away. Money must not
    depend on an undocumented call-order assumption."""
    logdb.write_spend(tmp_path, "2026-08-20", 7.0, now=1000.0, window_seconds=300)  # stale row from another day
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text(json.dumps({"date": "2026-08-21", "spend_usd": 4.0, "recent_events": []}))

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(11.0)


def test_corrupted_legacy_spend_state_does_not_crash(tmp_path):
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text("{not valid json")

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == 0.0


def test_legacy_spend_state_with_malformed_events_still_imports_the_total(tmp_path):
    legacy = tmp_path / ".spend_state.json"
    legacy.write_text(json.dumps({"date": "2026-08-21", "spend_usd": 2.0, "recent_events": [["bad"], None, [1000.0, 0.5]]}))

    logdb.migrate_legacy_spend_file(tmp_path, legacy, "2026-08-21")

    assert logdb.read_spend_today(tmp_path, "2026-08-21") == pytest.approx(2.0)
    assert logdb.read_spend_velocity(tmp_path, since=0.0) == pytest.approx(0.5)


# --- tool_calls (which MCP tools an agent actually uses) ---------------
# [user-requested, before the first real-agent validation] griot exposes 7 tools
# and 1 prompt, none of it validated against real agent use. Recording which
# ones actually get called is what turns "do these earn their place?" from
# opinion into data.


def test_write_and_read_a_tool_call(tmp_path):
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, duration_seconds=0.42,
                          timestamp="2026-08-22T10:00:00+00:00")

    calls = logdb.read_tool_calls(tmp_path, days=30, now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc))

    assert len(calls) == 1
    assert calls[0]["tool"] == "griot_search"
    assert calls[0]["ok"] is True
    assert calls[0]["duration_seconds"] == 0.42
    assert calls[0]["error"] is None


def test_a_failed_tool_call_keeps_its_reason(tmp_path):
    """A tool that is called a lot but always fails looks identical to a
    healthy one if only the count is kept."""
    logdb.write_tool_call(tmp_path, "griot_index_status", ok=False, duration_seconds=0.1,
                          error="RuntimeError: locked", timestamp="2026-08-22T10:00:00+00:00")

    call = logdb.read_tool_calls(tmp_path, days=30, now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc))[0]

    assert call["ok"] is False
    assert "locked" in call["error"]


def test_tool_calls_outside_the_window_are_excluded(tmp_path):
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, timestamp="2020-01-01T00:00:00+00:00")
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, timestamp="2026-08-21T00:00:00+00:00")

    calls = logdb.read_tool_calls(tmp_path, days=30, now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc))

    assert len(calls) == 1


def test_reading_tool_calls_from_an_empty_store(tmp_path):
    assert logdb.read_tool_calls(tmp_path, days=30) == []


# --- permissions (same 0600/0700 contract as every other data file) ----


def test_db_file_created_0600(tmp_path):
    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 1})
    assert _mode(tmp_path / logdb.DB_FILENAME) == 0o600


def test_existing_world_readable_db_gets_repaired_on_next_write(tmp_path):
    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 1})
    (tmp_path / logdb.DB_FILENAME).chmod(0o644)

    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:01+00:00", "indexed": 2})

    assert _mode(tmp_path / logdb.DB_FILENAME) == 0o600


# --- schema sanity (guards against a future accidental column typo) ----


def test_db_file_never_briefly_world_readable_even_if_chmod_is_skipped(tmp_path, monkeypatch):
    """[review finding, non-blocking but cheap to close] sqlite3.connect()
    creates the physical file immediately, under the process umask, before
    any CREATE TABLE runs — os.chmod() afterward repairs it, but there was
    a narrow window where the file existed at umask-default permissions.
    Pre-creating the file at 0600 via os.open(O_CREAT) before connect()
    closes that window entirely: even if the chmod call were skipped, the
    file must already be 0600 from creation."""
    monkeypatch.setattr(logdb.os, "chmod", lambda *a, **kw: None)

    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 1})

    assert _mode(tmp_path / logdb.DB_FILENAME) == 0o600


def test_connect_uses_a_generous_busy_timeout(tmp_path, monkeypatch):
    """[review finding, non-blocking] sqlite3's default 5s busy_timeout can
    raise 'database is locked' (OperationalError) under real contention
    between two griot processes (MCP + CLI + UI can all run at once). A
    longer timeout lets SQLite's own internal retry absorb realistic
    contention instead of surfacing as an unhandled exception."""
    captured = {}
    real_connect = logdb.sqlite3.connect

    def _spy_connect(path, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(logdb.sqlite3, "connect", _spy_connect)

    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 1})

    assert captured["timeout"] is not None and captured["timeout"] >= 15.0


def test_schema_creates_expected_tables(tmp_path):
    logdb.write_run(tmp_path, {"timestamp": "2026-08-21T00:00:00+00:00", "indexed": 1})
    conn = sqlite3.connect(tmp_path / logdb.DB_FILENAME)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"runs", "queries", "_jsonl_migrated"} <= names
