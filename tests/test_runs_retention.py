"""Retention for the `runs` table of logs.db: the last GRIOT_RUN_RETENTION
runs of each repository and source are kept, never a day window. The
freshness report, `griot doctor`, griot_index_status and `griot stats` read
the LAST run of every source and repository (and the last that changed the
index), and a repository indexed once a year must not lose it."""

import itertools
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import dotenv_values

from griot import common, config, freshness, logdb, mcp_server, stats

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
_clock = itertools.count()


def _run(log_dir, *, script="index_code.py", repo="all", collection="c", heads=None, **fields) -> dict:
    """One run record, written now-ish and in order (ids grow with each call)."""
    record = {"timestamp": (NOW - timedelta(days=400) + timedelta(minutes=next(_clock))).isoformat(),
              "collection": collection, "script": script, "repo": repo,
              "indexed": 0, "skipped": 3, "failed": 0, **fields}
    if heads is not None:
        record["heads"] = heads
    logdb.write_run(log_dir, record)
    return record


def _dead(log_dir, *, script="index_code.py", collection="c") -> dict:
    """A run that died (cli.py): no repo, no heads, counts null on purpose (decision 70)."""
    record = {"timestamp": (NOW - timedelta(days=400) + timedelta(minutes=next(_clock))).isoformat(),
              "collection": collection, "script": script,
              "indexed": None, "skipped": None, "failed": None, "error": "RuntimeError: boom"}
    logdb.write_run(log_dir, record)
    return record


def _kept(log_dir) -> list[dict]:
    conn = sqlite3.connect(log_dir / logdb.DB_FILENAME)
    try:
        return [json.loads(r[0]) for r in conn.execute("SELECT data FROM runs ORDER BY id")]
    finally:
        conn.close()


# --- what the prune removes, and what it keeps -----------------------------------------------


def test_it_keeps_the_last_n_runs_of_each_repository_and_source(tmp_path):
    a = [_run(tmp_path, repo="alpha", indexed=1) for _ in range(5)]
    b = [_run(tmp_path, repo="beta", script="index_commits.py", indexed=1) for _ in range(4)]
    c = [_run(tmp_path, repo="alpha", script="index_commits.py", indexed=1) for _ in range(2)]

    removed = logdb.prune_runs_beyond(tmp_path, 2)

    assert removed == 3 + 2
    assert _kept(tmp_path) == a[-2:] + b[-2:] + c


def test_runs_of_another_collection_are_counted_apart(tmp_path):
    """A second profile's index has its own history: indexing it a hundred
    times must not delete the first profile's last run."""
    first = _run(tmp_path, repo="alpha", collection="one", indexed=1)
    others = [_run(tmp_path, repo="alpha", collection="two", indexed=1) for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [first, others[-1]]


def test_nothing_is_removed_when_every_group_is_within_the_count(tmp_path):
    runs = [_run(tmp_path, repo=name, indexed=1) for name in ("a", "b", "c")]

    assert logdb.prune_runs_beyond(tmp_path, 1) == 0
    assert _kept(tmp_path) == runs


@pytest.mark.parametrize("keep", [1, 2, 3, 7])
def test_the_newest_run_of_every_repository_and_source_survives_any_count(tmp_path, keep):
    """The freshness report, doctor and griot_index_status read the newest
    run of each source and repository: no count may take it, whatever runs
    came after it in other groups, dead ones included."""
    scripts = ("index_code.py", "index_commits.py", "index_tags.py", "index_branches.py", "index_platform.py")
    newest = {}
    for round_ in range(6):
        for repo in ("alpha", "beta", "gamma", "all"):
            for script in scripts:
                for collection in ("one", "two"):
                    if (round_ + len(repo) + len(script)) % 3 == 0:
                        record = _dead(tmp_path, script=script, collection=collection)
                        newest[(collection, script, None)] = record
                    record = _run(tmp_path, repo=repo, script=script, collection=collection,
                                  indexed=round_ % 2, heads={repo: f"h{round_}"})
                    newest[(collection, script, repo)] = record

    logdb.prune_runs_beyond(tmp_path, keep)

    kept = _kept(tmp_path)
    for record in newest.values():
        assert record in kept
    per_group = {}
    for record in kept:
        key = (record["collection"], record["script"], record.get("repo"))
        per_group[key] = per_group.get(key, 0) + 1
    assert all(count <= keep + 2 for count in per_group.values())


def test_the_newest_run_that_changed_the_index_stays(tmp_path):
    """`griot stats` says when the index last changed from the newest run
    that indexed or pruned something; the runs after it usually change
    nothing (the last source of `griot index all`). Deleting it would turn
    that line into an older date, or into "never"."""
    changed = _run(tmp_path, repo="alpha", indexed=4)
    quiet = [_run(tmp_path, repo="alpha", indexed=0) for _ in range(5)]

    logdb.prune_runs_beyond(tmp_path, 2)

    assert _kept(tmp_path) == [changed] + quiet[-2:]


def test_a_run_that_only_pruned_counts_as_a_change(tmp_path):
    changed = _run(tmp_path, repo="alpha", indexed=0, pruned=3)
    quiet = [_run(tmp_path, repo="alpha", indexed=0) for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [changed, quiet[-1]]


def test_the_last_run_that_did_not_die_stays_after_dead_ones(tmp_path):
    """The freshness report skips a run with an error: when the last N runs
    of a source died, the one before them is still what says how far the
    index got."""
    ok = _run(tmp_path, repo="alpha", heads={"alpha": "h1"})
    dead = [_run(tmp_path, repo="alpha", heads={"alpha": "h2"}, error="RuntimeError: x") for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [ok, dead[-1]]


def test_the_last_run_that_covered_a_repository_stays_whichever_group_it_is_in(tmp_path):
    """`griot index all` records every repository's head in one run of the
    group "all": a later run of one repository alone does not make it the
    newest run of the others."""
    everything = _run(tmp_path, repo="all", heads={"alpha": "a1", "beta": "b1"})
    alpha_only = [_run(tmp_path, repo="alpha", heads={"alpha": "a2"}) for _ in range(3)]
    later_all = [_run(tmp_path, repo="all", heads={"alpha": "a3"}) for _ in range(2)]  # beta not covered

    logdb.prune_runs_beyond(tmp_path, 1)

    kept = _kept(tmp_path)
    assert everything in kept  # the only run that still says where beta's index is
    assert alpha_only[-1] in kept and later_all[-1] in kept
    assert alpha_only[0] not in kept and later_all[0] not in kept


def test_a_head_recorded_as_unknown_does_not_shadow_the_last_known_one(tmp_path):
    """freshness.assess() skips a run whose head for the repository is null
    (it could not be read) for the source, but takes its time as the last
    indexing: both runs say something, so both stay."""
    known = _run(tmp_path, repo="alpha", heads={"alpha": "h1"})
    unknown = [_run(tmp_path, repo="alpha", heads={"alpha": None}) for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [known, unknown[-1]]


def test_the_last_run_that_covered_a_repository_stays_even_with_its_head_unknown(tmp_path):
    """freshness.assess() takes "last indexed" from the newest run that did
    not die and recorded the repository, head known or not: when that run
    is the only one that covered it, it stays however many came after."""
    only = _run(tmp_path, repo="all", heads={"alpha": None, "beta": "b1"})
    later = [_run(tmp_path, repo="all", heads={"beta": "b2"}) for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [only, later[-1]]
    report = freshness.assess(logdb.read_recent(tmp_path, "runs", limit=100),
                              [{"name": "alpha", "path": "/x/alpha", "head": None, "refs": {}}])
    assert report[0]["last_indexed_at"] == only["timestamp"]


def test_the_last_platform_run_that_refused_a_repository_stays(tmp_path):
    """The freshness report says the platform refused a repository from the
    newest platform run that concerned it, refused or indexed."""
    refused = _run(tmp_path, script="index_platform.py", repo="all", refused_repos=["beta"],
                   heads={"alpha": "a1"})
    alpha_only = [_run(tmp_path, script="index_platform.py", repo="all", heads={"alpha": "a2"}) for _ in range(3)]

    logdb.prune_runs_beyond(tmp_path, 1)

    assert _kept(tmp_path) == [refused, alpha_only[-1]]


def test_a_record_that_cannot_be_read_is_kept(tmp_path):
    """logs.db is a file: a row whose JSON is broken cannot be placed in a
    group, and what cannot be read is not known to be old."""
    _run(tmp_path, repo="alpha")
    conn = sqlite3.connect(tmp_path / logdb.DB_FILENAME)
    with conn:
        conn.execute("INSERT INTO runs (timestamp, collection, data) VALUES (?, ?, ?)", ("x", "c", "{not json"))
    conn.close()
    newest = _run(tmp_path, repo="alpha")

    assert logdb.prune_runs_beyond(tmp_path, 1) == 1
    conn = sqlite3.connect(tmp_path / logdb.DB_FILENAME)
    try:
        assert [r[0] for r in conn.execute("SELECT data FROM runs ORDER BY id")] == ["{not json", json.dumps(newest)]
    finally:
        conn.close()


def test_a_count_under_one_is_refused(tmp_path):
    _run(tmp_path, repo="alpha")

    with pytest.raises(ValueError):
        logdb.prune_runs_beyond(tmp_path, 0)
    with pytest.raises(ValueError):
        logdb.count_runs_beyond(tmp_path, 0)
    assert len(_kept(tmp_path)) == 1


def test_nothing_logged_yet_creates_nothing(tmp_path):
    log_dir = tmp_path / "logs"

    assert logdb.prune_runs_beyond(log_dir, 5) == 0
    assert logdb.count_runs_beyond(log_dir, 5) == 0
    assert not log_dir.exists()


def test_a_count_under_one_is_refused_before_anything_is_logged_too(tmp_path):
    """The refusal is about the value, not about the file: a caller with a
    bad count learns it on a fresh install too, not on the first run."""
    with pytest.raises(ValueError):
        logdb.prune_runs_beyond(tmp_path / "logs", 0)
    with pytest.raises(ValueError):
        logdb.count_runs_beyond(tmp_path / "logs", 0)


def test_it_never_touches_the_other_tables(tmp_path):
    for _ in range(3):
        _run(tmp_path, repo="alpha")
        logdb.write_quality_check(tmp_path, {"timestamp": NOW.isoformat(), "collection": "c"})
        logdb.write_query(tmp_path, {"timestamp": NOW.isoformat(), "question": "q"})
        logdb.write_tool_call(tmp_path, "griot_search", ok=True, timestamp=NOW.isoformat())

    logdb.prune_runs_beyond(tmp_path, 1)

    conn = sqlite3.connect(tmp_path / logdb.DB_FILENAME)
    try:
        assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("runs", "quality_checks", "queries", "tool_calls")] == [1, 3, 3, 3]
    finally:
        conn.close()


# --- counting what a prune would remove, without removing it ----------------------------------


def test_the_count_is_what_the_prune_then_removes(tmp_path):
    _run(tmp_path, repo="alpha", indexed=4)
    for _ in range(4):
        _run(tmp_path, repo="alpha")
        _dead(tmp_path)
    for _ in range(3):
        _run(tmp_path, repo="beta", script="index_tags.py", heads={"beta": "b"})

    counted = logdb.count_runs_beyond(tmp_path, 2)

    assert counted == 2 + 2 + 1
    assert logdb.prune_runs_beyond(tmp_path, 2) == counted


def test_counting_changes_nothing_in_the_file(tmp_path):
    for _ in range(4):
        _run(tmp_path, repo="alpha")
    db = tmp_path / logdb.DB_FILENAME
    db.chmod(0o400)
    before = db.read_bytes()

    assert logdb.count_runs_beyond(tmp_path, 1) == 3

    assert db.read_bytes() == before and (db.stat().st_mode & 0o777) == 0o400


# --- what still reads right after a prune ------------------------------------------------------


def test_freshness_reads_the_same_after_a_prune(tmp_path, monkeypatch):
    """The report over every registered repository, before and after, with
    the smallest count: the runs it reads are the ones the prune keeps."""
    monkeypatch.setattr(freshness, "_commits_between", lambda path, old, new: 0 if old == new else 2)
    _run(tmp_path, repo="all", heads={"alpha": "a1", "beta": "b1"}, refs={"alpha": {}, "beta": {}})
    for script in ("index_commits.py", "index_tags.py"):
        _run(tmp_path, repo="all", script=script, heads={"alpha": "a1", "beta": "b1"},
             refs={"alpha": {"tags": "t"}, "beta": {"tags": "t"}})
    for _ in range(4):
        _run(tmp_path, repo="alpha", heads={"alpha": "a2"})
        _dead(tmp_path)
    _run(tmp_path, script="index_platform.py", repo="all", refused_repos=["beta"], heads={"alpha": "a2"})
    repositories = [{"name": "alpha", "path": "/x/alpha", "head": "a2", "refs": {"tags": "t"}},
                    {"name": "beta", "path": "/x/beta", "head": "b1", "refs": {"tags": "t"}}]
    before = freshness.assess(logdb.read_recent(tmp_path, "runs", collection="c", limit=2000), repositories)

    assert logdb.prune_runs_beyond(tmp_path, 1) > 0

    after = freshness.assess(logdb.read_recent(tmp_path, "runs", collection="c", limit=2000), repositories)
    assert after == before


def test_stats_reads_a_pruned_history_with_dead_runs(monkeypatch):
    """The window counts the runs still kept: dead runs (null counts) and
    all, and the last change is still the run that made it."""
    log_dir = common.LOG_DIR
    changed = _run(log_dir, repo="alpha", indexed=5, timestamp=(NOW - timedelta(days=2)).isoformat())
    for i in range(3):
        _run(log_dir, repo="alpha", indexed=0, timestamp=(NOW - timedelta(hours=30 - i)).isoformat())
        logdb.write_run(log_dir, {"timestamp": (NOW - timedelta(hours=20 - i)).isoformat(), "collection": "c",
                                  "script": "index_code.py", "indexed": None, "skipped": None, "failed": None,
                                  "error": "RuntimeError: boom"})
    monkeypatch.setattr(common, "COLLECTION_NAME", "c")

    logdb.prune_runs_beyond(log_dir, 1)

    runs, queries = stats.load_window(7, NOW)
    result = stats.compute_stats(runs, queries, {"points_count": 1}, state=stats.load_state(), now=NOW)
    assert result["num_runs"] == 3  # the change, the last quiet run, the last dead one
    assert result["total_indexed"] == 5
    assert stats.load_state()["last_index_change_at"] == changed["timestamp"]


# --- when it runs ----------------------------------------------------------------------------


def test_an_indexing_run_logged_prunes_the_runs_the_retention_no_longer_keeps(monkeypatch):
    monkeypatch.setattr(common, "RUN_RETENTION", 2)
    for _ in range(4):
        _run(common.LOG_DIR, repo="alpha", collection=common.COLLECTION_NAME, script="index_tags.py")

    common.log_run_summary(script="index_tags.py", repo="alpha", indexed=0, skipped=0, failed=0)

    assert len(_kept(common.LOG_DIR)) == 2


def test_a_search_logged_prunes_runs_too(monkeypatch):
    monkeypatch.setattr(common, "RUN_RETENTION", 1)
    for _ in range(3):
        _run(common.LOG_DIR, repo="alpha")

    common.log_query(question="q")

    assert len(_kept(common.LOG_DIR)) == 1
    assert "2 indexing run" in (common.LOG_DIR / "griot.log").read_text()


def test_a_run_prune_that_fails_never_fails_the_run_record(monkeypatch):
    def broken(log_dir, keep):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(logdb, "prune_runs_beyond", broken)

    common.log_run_summary(script="index_tags.py", repo="alpha", indexed=0, skipped=0, failed=0)

    assert len(_kept(common.LOG_DIR)) == 1
    assert "file is not a database" in (common.LOG_DIR / "griot.log").read_text()


def test_the_run_prune_shares_the_once_a_day_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(logdb, "prune_runs_beyond", lambda log_dir, keep: calls.append(keep) or 0)

    common.log_query(question="a")
    common.log_run_summary(script="index_tags.py", repo="alpha", indexed=0, skipped=0, failed=0)

    assert calls == [common.RUN_RETENTION]


@pytest.mark.anyio
async def test_griot_index_status_through_the_protocol_still_has_the_last_run(monkeypatch):
    from mcp.client.client import Client

    monkeypatch.setattr(common, "RUN_RETENTION", 1)
    for _ in range(3):
        _run(common.LOG_DIR, repo="alpha", collection=common.COLLECTION_NAME, script="index_tags.py")
    last = _run(common.LOG_DIR, repo="beta", collection=common.COLLECTION_NAME, script="index_code.py", indexed=7)
    common.log_query(question="prunes")
    assert len(_kept(common.LOG_DIR)) == 2

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {})

    assert result.is_error is False
    assert result.structured_content["last_indexed"]["timestamp"] == last["timestamp"]
    assert result.structured_content["last_indexed"]["indexed"] == 7


# --- the setting -----------------------------------------------------------------------------


def test_it_is_a_count_setting_with_a_default_of_100():
    setting = config.find("run-retention")

    assert setting is not None and setting.variable == "GRIOT_RUN_RETENTION" and setting.kind == "count"
    assert config.default_of(setting) == "100"
    assert common.RUN_RETENTION == 100


def test_the_env_template_writes_it_out_and_says_quality_checks_are_kept():
    common.ENV_PATH.unlink(missing_ok=True)
    common.ensure_env_template()

    assert dotenv_values(common.ENV_PATH).get("GRIOT_RUN_RETENTION") == "100"
    line = next(entry for entry in common.ENV_TEMPLATE_SETTINGS if entry[0] == "GRIOT_RUN_RETENTION")
    assert "quality checks are kept" in line[2]


def test_griot_does_not_start_on_a_count_under_one(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_RUN_RETENTION="0")
    done = subprocess.run([sys.executable, "-c", "import griot.common"], env=env, capture_output=True, text=True,
                          timeout=120)

    assert done.returncode != 0 and "GRIOT_RUN_RETENTION" in done.stderr


def test_config_set_writes_a_larger_count_and_refuses_zero(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked: {prompt}"))

    assert config.main(["set", "run-retention", "500"]) == 0
    assert dotenv_values(common.ENV_PATH).get("GRIOT_RUN_RETENTION") == "500"
    assert config.main(["set", "run-retention", "0"]) != 0


def test_a_smaller_count_says_what_the_next_prune_deletes_and_asks(monkeypatch):
    for _ in range(5):
        _run(common.LOG_DIR, repo="alpha")
    asked = []
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")

    assert config.main(["set", "run-retention", "2"]) == 0

    assert len(asked) == 1
    question = " ".join(asked[0].split())
    assert "3 indexing runs" in question and "run-retention" in question
    assert dotenv_values(common.ENV_PATH).get("GRIOT_RUN_RETENTION") == "2"
    assert len(_kept(common.LOG_DIR)) == 5  # the set itself deletes nothing


def test_a_smaller_count_without_a_terminal_is_refused(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: False)

    assert config.main(["set", "run-retention", "2"]) == 2
    assert dotenv_values(common.ENV_PATH).get("GRIOT_RUN_RETENTION") != "2"


def test_a_server_environment_cannot_lower_the_count_the_file_keeps():
    """A project's .mcp.json would otherwise delete the run history of every project."""
    setting = config.find("run-retention")

    smaller, _ = config.measured_from_environment(setting, "5")
    larger, value = config.measured_from_environment(setting, "1000")

    assert smaller is not None
    assert larger is None and value == "1000"
