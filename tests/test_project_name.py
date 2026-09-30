"""The project a griot call came from, recorded in the query log and the tool-call
log. Before this, nothing said WHO used griot, so usage per project could only be
guessed by matching timestamps against session transcripts that get deleted."""

import os
import sqlite3
from pathlib import Path

import pytest

from griot import common, logdb, mcp_server, stats


@pytest.fixture(autouse=True)
def _no_ambient_project(monkeypatch):
    monkeypatch.delenv("GRIOT_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


# --- current_project() ----------------------------------------------------------------


def test_the_explicit_setting_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("GRIOT_PROJECT", "project-a")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "somewhere-else"))
    monkeypatch.chdir(tmp_path)

    assert common.current_project() == "project-a"


def test_claude_codes_project_dir_beats_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project-b"))
    monkeypatch.chdir(tmp_path)

    assert common.current_project() == "project-b"


def test_without_settings_it_is_the_name_of_the_working_directory(monkeypatch, tmp_path):
    project = tmp_path / "my project"
    project.mkdir()
    monkeypatch.chdir(project)

    assert common.current_project() == "my project"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_setting_counts_as_unset(monkeypatch, tmp_path, blank):
    monkeypatch.setenv("GRIOT_PROJECT", blank)
    project = tmp_path / "real-name"
    project.mkdir()
    monkeypatch.chdir(project)

    assert common.current_project() == "real-name"


def test_the_home_directory_is_not_a_project(monkeypatch, tmp_path):
    # Running the CLI from ~ would otherwise write the account's user name into the log.
    home = tmp_path / "someone"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(home)

    assert common.current_project() is None


def test_the_filesystem_root_is_not_a_project(monkeypatch):
    monkeypatch.chdir("/")

    assert common.current_project() is None


def test_a_deleted_working_directory_is_unknown_not_a_crash(monkeypatch, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()

    assert common.current_project() is None


def test_control_characters_and_length_are_cleaned_before_they_reach_a_log(monkeypatch):
    monkeypatch.setenv("GRIOT_PROJECT", "a\nb\tc" + "x" * 300)

    name = common.current_project()

    assert "\n" not in name and "\t" not in name
    assert name.startswith("abc") and len(name) <= 100


def test_a_directory_name_with_a_control_character_is_cleaned_too():
    # An environment variable cannot hold a NUL, a directory name can hold a newline: the cleaner is shared.
    assert common._clean_project("a\x00b\x1bc\n") == "abc"
    assert common._clean_project("\x00\n") is None


# --- the query log -----------------------------------------------------------------------


def test_a_logged_query_carries_the_project(monkeypatch):
    monkeypatch.setenv("GRIOT_PROJECT", "project-a")

    common.log_query(question="q", via="mcp")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[-1]["project"] == "project-a"


def test_a_caller_can_still_set_the_project_itself(monkeypatch):
    monkeypatch.setenv("GRIOT_PROJECT", "from-env")

    common.log_query(question="q", project="explicit")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[-1]["project"] == "explicit"


def test_an_unknown_project_is_recorded_as_null_so_the_shape_never_varies(monkeypatch):
    monkeypatch.chdir("/")

    common.log_query(question="q")

    record = logdb.read_since(common.LOG_DIR, "queries", days=1)[-1]
    assert "project" in record and record["project"] is None


# --- the tool-call log --------------------------------------------------------------------


def test_a_tool_call_records_the_project(monkeypatch):
    monkeypatch.setenv("GRIOT_PROJECT", "project-b")

    mcp_server.griot_spend_status()

    assert logdb.read_tool_calls(common.LOG_DIR, days=1)[-1]["project"] == "project-b"


def test_a_failing_tool_call_still_records_the_project(monkeypatch):
    monkeypatch.setenv("GRIOT_PROJECT", "project-a")

    with pytest.raises(ValueError):
        mcp_server.griot_stats(days=0)  # rejects a nonsensical window: a real tool failure

    last = logdb.read_tool_calls(common.LOG_DIR, days=1)[-1]
    assert last["ok"] is False and last["project"] == "project-a"


def test_write_and_read_tool_call_round_trip_the_project(tmp_path):
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, project="project-a")
    logdb.write_tool_call(tmp_path, "griot_search", ok=True)

    calls = logdb.read_tool_calls(tmp_path, days=1)

    assert [c["project"] for c in calls] == ["project-a", None]


def test_a_logs_db_created_before_the_project_column_is_migrated_in_place(tmp_path):
    # The real logs.db of an existing install: the tool_calls table WITHOUT the column, with history.
    from datetime import datetime, timedelta, timezone

    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db = sqlite3.connect(tmp_path / "logs.db")
    db.executescript("""
        CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            tool TEXT NOT NULL, ok INTEGER NOT NULL, duration_seconds REAL, error TEXT);
    """)
    db.execute("INSERT INTO tool_calls (timestamp, tool, ok, duration_seconds, error) VALUES (?, 'griot_search', 1, 0.5, NULL)",
               (earlier,))
    db.commit()
    db.close()

    logdb.write_tool_call(tmp_path, "griot_index_status", ok=True, project="project-a")
    calls = logdb.read_tool_calls(tmp_path, days=36500)

    assert [(c["tool"], c["project"]) for c in calls] == [("griot_search", None), ("griot_index_status", "project-a")]


def test_migrating_twice_is_harmless(tmp_path):
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, project="a")
    logdb.write_tool_call(tmp_path, "griot_search", ok=True, project="b")

    assert [c["project"] for c in logdb.read_tool_calls(tmp_path, days=1)] == ["a", "b"]


# --- griot stats ---------------------------------------------------------------------------

_STATUS = {"points_count": 1, "embed_profile": "jina-code", "collection": "c", "running": False, "pid": None,
           "path": None, "last_indexed": None, "spend_ceiling_exceeded": False}


def _q(project, **extra):
    return {"timestamp": "2026-08-21T10:00:00+00:00", "via": "mcp", "duration_seconds": 1.0, "sources": [],
            **({"project": project} if project != "<missing>" else {}), **extra}


def test_stats_splits_queries_by_project():
    result = stats.compute_stats([], [_q("project-a"), _q("project-a"), _q("project-b")], _STATUS)

    assert result["queries_by_project"] == {"project-a": 2, "project-b": 1}
    text = stats.format_stats(result, days=30)
    assert "by project: project-a 2 · project-b 1" in text


def test_queries_without_a_project_are_counted_as_unknown_not_dropped():
    # Old records have no `project`; a null one means it could not be worked out. Neither may vanish.
    result = stats.compute_stats([], [_q("<missing>"), _q(None), _q("project-a")], _STATUS)

    assert result["queries_by_project"] == {"unknown": 2, "project-a": 1}


def test_the_by_project_line_is_left_out_when_no_project_is_known():
    result = stats.compute_stats([], [_q("<missing>"), _q(None)], _STATUS)

    assert "by project" not in stats.format_stats(result, days=30)


@pytest.mark.anyio
async def test_the_mcp_stats_output_declares_the_by_project_field():
    # A field missing from StatsOutput is silently dropped for the one surface it was built for.
    from mcp.client.client import Client

    common.log_query(question="q", via="mcp", project="project-a")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {})

    assert result.structured_content["queries_by_project"] == {"project-a": 1}


# --- edge cases found in review ---------------------------------------------------------------


def test_a_project_set_by_hand_is_logged_exactly_as_set(monkeypatch):
    # The explicit setting is the operator's own choice, so it is not cut down to a folder name.
    monkeypatch.setenv("GRIOT_PROJECT", "org/app")

    assert common.current_project() == "org/app"


def test_a_symlink_to_the_home_directory_is_still_the_home_directory(monkeypatch, tmp_path):
    home = tmp_path / "someone"
    home.mkdir()
    link = tmp_path / "shortcut"
    link.symlink_to(home)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(link))

    assert common.current_project() is None


def _fs_ignores_case(tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").exists()


def test_the_home_guard_holds_when_the_path_differs_from_home_only_in_case(monkeypatch, tmp_path):
    # macOS and Windows filesystems ignore case, and resolve() does not normalise it.
    if not _fs_ignores_case(tmp_path):
        pytest.skip("this filesystem is case-sensitive: a differently-cased path is a different directory")
    home = tmp_path / "Someone"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "someone"))

    assert common.current_project() is None


def _old_schema_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
                 "tool TEXT NOT NULL, ok INTEGER NOT NULL, duration_seconds REAL, error TEXT)")
    conn.execute("INSERT INTO tool_calls (timestamp, tool, ok) VALUES ('2999-01-01T00:00:00+00:00', 'griot_search', 1)")
    conn.commit()
    return conn


def test_a_database_that_cannot_be_written_is_still_readable_without_the_column():
    # A read-only file system: the ALTER is refused, and `griot stats` used to read such a database fine.
    from datetime import datetime, timezone

    conn = _old_schema_conn()
    conn.execute("PRAGMA query_only = ON")

    assert logdb._ensure_tool_calls_project_column(conn) is False
    rows = logdb._tool_call_rows(conn, datetime(2000, 1, 1, tzinfo=timezone.utc))

    assert [(r["tool"], r["project"]) for r in rows] == [("griot_search", None)]


def test_the_column_is_reported_present_after_a_successful_migration():
    conn = _old_schema_conn()

    assert logdb._ensure_tool_calls_project_column(conn) is True
    assert logdb._ensure_tool_calls_project_column(conn) is True  # and asking again is harmless


def test_any_other_migration_error_is_not_swallowed():
    conn = _old_schema_conn()
    conn.execute("DROP TABLE tool_calls")  # no table at all: ALTER fails for a reason that is not "read-only"

    with pytest.raises(sqlite3.OperationalError):
        logdb._ensure_tool_calls_project_column(conn)
