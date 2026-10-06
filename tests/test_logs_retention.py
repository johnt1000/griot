"""Retention for logs.db: the tables that grow with use (`queries`, one row
per search or question, and `tool_calls`, one row per MCP tool call) are
pruned by age, on a write, at most once a day per process. `runs` and
`quality_checks` are never pruned: the freshness report and doctor need the
last run of every source, and the quality trend needs its history."""

import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import dotenv_values

from griot import common, config, logdb, mcp_server, stats

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _at(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _rows(log_dir, table) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(log_dir / logdb.DB_FILENAME)
    try:
        return [r[0] for r in conn.execute(f"SELECT timestamp FROM {table} ORDER BY id")]
    finally:
        conn.close()


def _fill(log_dir, *ages):
    """One row of every table at each age, in the order given."""
    for age in ages:
        logdb.write_run(log_dir, {"timestamp": _at(age), "collection": "c"})
        logdb.write_quality_check(log_dir, {"timestamp": _at(age), "collection": "c"})
        logdb.write_query(log_dir, {"timestamp": _at(age), "question": "q"})
        logdb.write_tool_call(log_dir, "griot_search", ok=True, timestamp=_at(age))


@pytest.fixture(autouse=True)
def _no_prune_yet(monkeypatch):
    monkeypatch.setattr(common, "_last_log_prune", None)


# --- what the prune removes, and what it keeps -----------------------------------------------


def test_it_removes_searches_and_tool_calls_older_than_the_window_and_keeps_the_rest(tmp_path):
    _fill(tmp_path, 400, 91, 89, 1)

    removed = logdb.prune_older_than(tmp_path, 90, now=NOW)

    assert removed == {"queries": 2, "tool_calls": 2}
    assert _rows(tmp_path, "queries") == [_at(89), _at(1)]
    assert _rows(tmp_path, "tool_calls") == [_at(89), _at(1)]


def test_it_never_touches_runs_or_quality_checks(tmp_path):
    _fill(tmp_path, 400, 1)

    logdb.prune_older_than(tmp_path, 30, now=NOW)

    assert _rows(tmp_path, "runs") == [_at(400), _at(1)]
    assert _rows(tmp_path, "quality_checks") == [_at(400), _at(1)]


def test_the_newest_row_of_each_table_stays_so_the_report_can_say_when_it_last_happened(tmp_path):
    """`griot stats` says "last search 400 days ago" from the newest query:
    pruning it would turn that into "never searched", a different fact."""
    _fill(tmp_path, 500, 400)

    removed = logdb.prune_older_than(tmp_path, 30, now=NOW)

    assert removed == {"queries": 1, "tool_calls": 1}
    assert _rows(tmp_path, "queries") == [_at(400)]
    assert _rows(tmp_path, "tool_calls") == [_at(400)]
    assert logdb.read_latest(tmp_path, "queries")["timestamp"] == _at(400)


def test_a_row_exactly_at_the_cutoff_is_kept(tmp_path):
    _fill(tmp_path, 30, 1)

    assert logdb.prune_older_than(tmp_path, 30, now=NOW) == {"queries": 0, "tool_calls": 0}


def test_a_pruned_question_does_not_stay_readable_in_the_file(tmp_path):
    """Question text is what GRIOT_LOG_QUESTIONS exists for: deleted has to
    mean gone from the file, not only from the table."""
    logdb.write_query(tmp_path, {"timestamp": _at(400), "question": "zebra-unicorn-secret-question"})
    logdb.write_query(tmp_path, {"timestamp": _at(1), "question": "recent"})

    logdb.prune_older_than(tmp_path, 30, now=NOW)

    assert b"zebra-unicorn-secret-question" not in (tmp_path / logdb.DB_FILENAME).read_bytes()


def test_a_window_under_one_day_is_refused(tmp_path):
    _fill(tmp_path, 1)

    with pytest.raises(ValueError):
        logdb.prune_older_than(tmp_path, 0, now=NOW)
    assert _rows(tmp_path, "queries") == [_at(1)]


def test_nothing_logged_yet_creates_nothing(tmp_path):
    log_dir = tmp_path / "logs"

    assert logdb.prune_older_than(log_dir, 30, now=NOW) == {"queries": 0, "tool_calls": 0}
    assert not log_dir.exists()


# --- counting what a prune would remove, without removing it ----------------------------------


def test_the_count_is_what_the_prune_then_removes(tmp_path):
    """`griot config set log-retention-days` states this number before the
    person answers: a count that disagreed with the prune would be a promise
    the next search breaks. Same rows: the cutoff, and the newest row kept."""
    _fill(tmp_path, 500, 400, 91, 30, 1)
    logdb.write_query(tmp_path, {"timestamp": _at(200), "question": "an older one written last"})

    counted = logdb.count_older_than(tmp_path, 30, now=NOW)

    assert counted == {"queries": 3, "tool_calls": 3}
    assert logdb.prune_older_than(tmp_path, 30, now=NOW) == counted


def test_the_count_keeps_the_newest_row_like_the_prune(tmp_path):
    _fill(tmp_path, 500, 400)

    assert logdb.count_older_than(tmp_path, 30, now=NOW) == {"queries": 1, "tool_calls": 1}


def test_counting_changes_nothing_in_the_file(tmp_path):
    _fill(tmp_path, 400, 1)
    before = (tmp_path / logdb.DB_FILENAME).read_bytes()

    logdb.count_older_than(tmp_path, 30, now=NOW)

    assert (tmp_path / logdb.DB_FILENAME).read_bytes() == before
    assert _rows(tmp_path, "queries") == [_at(400), _at(1)]


def test_counting_does_not_migrate_or_repair_the_file(tmp_path):
    """Opening logs.db the way a write does would import a legacy
    queries.jsonl and reset the file's mode: a question asked before
    anything is decided must leave the file exactly as it found it."""
    import json

    _fill(tmp_path, 400, 1)
    (tmp_path / "queries.jsonl").write_text(json.dumps({"timestamp": _at(300), "question": "legacy"}) + "\n")
    db = tmp_path / logdb.DB_FILENAME
    db.chmod(0o400)
    before = db.read_bytes()

    assert logdb.count_older_than(tmp_path, 30, now=NOW) == {"queries": 1, "tool_calls": 1}

    assert db.read_bytes() == before and (db.stat().st_mode & 0o777) == 0o400


def test_counting_with_nothing_logged_creates_nothing(tmp_path):
    log_dir = tmp_path / "logs"

    assert logdb.count_older_than(log_dir, 30, now=NOW) == {"queries": 0, "tool_calls": 0}
    assert not log_dir.exists()


def test_concurrent_writers_lose_nothing_while_the_prune_runs(tmp_path):
    """Several griot processes share logs.db: a prune must neither fail nor
    take a row it should not while others write."""
    _fill(tmp_path, 400)
    errors = []

    def write(n):
        try:
            for i in range(40):
                logdb.write_tool_call(tmp_path, f"t{n}", ok=True, timestamp=_at(0))
                logdb.write_query(tmp_path, {"timestamp": _at(0), "question": f"{n}-{i}"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def prune():
        try:
            for _ in range(20):
                logdb.prune_older_than(tmp_path, 30, now=NOW)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)] + [threading.Thread(target=prune)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert _rows(tmp_path, "queries") == [_at(0)] * 160
    assert _rows(tmp_path, "tool_calls") == [_at(0)] * 160


# --- when it runs ----------------------------------------------------------------------------


def test_a_search_logged_prunes_what_the_retention_no_longer_keeps(monkeypatch):
    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 30)
    logdb.write_query(common.LOG_DIR, {"timestamp": "2020-01-01T00:00:00+00:00", "question": "old"})

    common.log_query(question="new")

    assert [q["question"] for q in logdb.read_since(common.LOG_DIR, "queries", 100000)] == ["new"]


def test_it_runs_at_most_once_a_day_per_process(monkeypatch):
    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 30)
    calls = []
    monkeypatch.setattr(logdb, "prune_older_than", lambda log_dir, days: calls.append(days) or {})

    common.log_query(question="a")
    common.log_query(question="b")
    assert calls == [30]

    # A long-lived MCP server: a day later it prunes again.
    monkeypatch.setattr(common, "_last_log_prune", common._last_log_prune - common._LOG_PRUNE_INTERVAL_SECONDS - 1)
    common.log_query(question="c")
    assert calls == [30, 30]


def test_a_prune_that_fails_is_logged_and_the_search_is_still_recorded(monkeypatch):
    def broken(log_dir, days):
        raise OSError("disk on fire")

    monkeypatch.setattr(logdb, "prune_older_than", broken)

    common.log_query(question="kept")

    assert [q["question"] for q in logdb.read_since(common.LOG_DIR, "queries", 1)] == ["kept"]
    assert "disk on fire" in (common.LOG_DIR / "griot.log").read_text()


def test_what_a_prune_removed_is_written_to_the_log(monkeypatch):
    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 30)
    logdb.write_query(common.LOG_DIR, {"timestamp": "2020-01-01T00:00:00+00:00", "question": "old"})

    common.log_query(question="new")

    log = (common.LOG_DIR / "griot.log").read_text()
    assert "1 search" in log and "30 days" in log


@pytest.mark.anyio
async def test_a_tool_call_through_the_protocol_prunes_old_tool_calls(monkeypatch):
    from mcp.client.client import Client

    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 30)
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, timestamp="2020-01-01T00:00:00+00:00")
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, timestamp="2020-01-02T00:00:00+00:00")

    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_config_list", {})

    assert [c["tool"] for c in logdb.read_tool_calls(common.LOG_DIR, 100000)] == ["griot_config_list"]


# --- the setting -----------------------------------------------------------------------------


def test_it_is_a_setting_with_a_generous_default():
    setting = config.find("log-retention-days")

    assert setting is not None and setting.variable == "GRIOT_LOG_RETENTION_DAYS" and setting.kind == "count"
    assert config.default_of(setting) == "365"
    assert common.LOG_RETENTION_DAYS == 365


def test_the_env_template_writes_it_out():
    common.ENV_PATH.unlink(missing_ok=True)
    common.ensure_env_template()

    assert dotenv_values(common.ENV_PATH).get("GRIOT_LOG_RETENTION_DAYS") == "365"


def test_config_set_writes_a_valid_window_and_refuses_zero(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked: {prompt}"))

    # Longer than the default: a shorter one asks first (tests/test_config_command.py).
    assert config.main(["set", "log-retention-days", "400"]) == 0
    assert dotenv_values(common.ENV_PATH).get("GRIOT_LOG_RETENTION_DAYS") == "400"
    assert config.main(["set", "log-retention-days", "0"]) != 0


def test_griot_does_not_start_on_a_window_under_one_day(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_LOG_RETENTION_DAYS="0")
    done = subprocess.run([sys.executable, "-c", "import griot.common"], env=env, capture_output=True, text=True,
                          timeout=120)

    assert done.returncode != 0 and "GRIOT_LOG_RETENTION_DAYS" in done.stderr


def test_a_server_environment_cannot_shorten_the_retention_the_file_keeps():
    """A project's .mcp.json would otherwise delete the usage history of every project."""
    setting = config.find("log-retention-days")

    shorter, _ = config.measured_from_environment(setting, "7")
    longer, value = config.measured_from_environment(setting, "1000")

    assert shorter is not None
    assert longer is None and value == "1000"


# --- what the report says about it -----------------------------------------------------------


def _text(days):
    result = stats.compute_stats([], [{"timestamp": _at(1), "duration_seconds": 0.1}], {"points_count": 1},
                                 now=NOW)
    return stats.format_stats(result, days)


def test_a_report_window_longer_than_the_retention_says_so(monkeypatch):
    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 30)

    assert "kept for 30 days" in _text(90)
    assert "kept for 30 days" not in _text(30)


@pytest.mark.anyio
async def test_griot_stats_says_how_long_searches_are_kept(monkeypatch):
    from mcp.client.client import Client

    monkeypatch.setattr(common, "LOG_RETENTION_DAYS", 45)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {"days": 7})

    assert result.structured_content["log_retention_days"] == 45
