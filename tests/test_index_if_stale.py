"""`griot index all --if-stale`: index only the registered repositories
whose index is behind them, for a scheduler or a git hook to call blindly.

Real use found an index 34 days old with two repositories behind it. The
cheapest step is a command that can run on a schedule: it compares each
registered repository with what the last runs recorded (freshness.py, the
same assessment griot_index_status and `griot stats` report), indexes the
ones behind, and exits 0 when none is behind or when another griot process
holds the collection, because a routine overlap is not a failure a
scheduler should report. A paid run goes through the same spend gate as
`griot index all`: without a terminal, at or above confirm-spend, it
refuses (exit 2) unless --yes is passed.

Most tests run the real CLI in a subprocess (a driver that replaces only
the embedder and records which source ran for which repository), in this
test's own directories, with the keychain backend forced off."""

import json
import os
import pty
import select
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from griot import cli, common, doctor, freshness, logdb, notices

SOURCES = ["code", "commits", "tags", "branches", "platform"]

_DRIVER = """
import json, os, sys
from griot import cli, common
calls = os.environ["IF_STALE_CALLS"]
if os.environ.get("IF_STALE_PAID"):
    common.ACTIVE_PROFILE = {**common.ACTIVE_PROFILE, "backend": "openai_compatible",
                             "price_per_1m_tokens": 3500000.0}
    common.ACTIVE_PROFILE_NAME = "openai-small"
    common.CONFIRM_SPEND_USD = 0.0
    common.SPEND_CEILING_USD = 1e12
def embed(texts, **kw):
    with open(calls, "a") as f:
        f.write(json.dumps({"embedded": len(texts)}) + "\\n")
    return [[0.1] * common.EMBED_DIM for _ in texts]
common.embed_texts = embed
real = cli._run_index_source
def recorded(source, rest):
    with open(calls, "a") as f:
        f.write(json.dumps({"source": source, "rest": rest}) + "\\n")
    return real(source, rest)
cli._run_index_source = recorded
raise SystemExit(cli.main(sys.argv[1:]))
"""


def _git(path, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "M", "GIT_AUTHOR_EMAIL": "m@example.com",
           "GIT_COMMITTER_NAME": "M", "GIT_COMMITTER_EMAIL": "m@example.com"}
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(tmp_path, name):
    path = tmp_path / "repos" / name
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "main.py").write_text(f"def {name.replace('-', '_')}():\n    return 1\n")
    _git(path, "add", "main.py")
    _git(path, "commit", "-q", "-m", "first")
    return path


def _advance(path, n=1):
    for i in range(n):
        (path / f"later{i}.py").write_text(f"VALUE = {i}\n")
        _git(path, "add", f"later{i}.py")
        _git(path, "commit", "-q", "-m", f"later {i}")


def _register(*paths):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(p) for p in paths]))


def _indexed(*paths, sources=SOURCES):
    """What a run of each source records for these repositories, as the real
    indexers record it (common.log_run_summary)."""
    for source in sources:
        common.log_run_summary(script=f"index_{source}.py", repo="all", repo_paths=[str(p) for p in paths],
                               indexed=1, skipped=0, failed=0)


def _run(tmp_path, *argv, paid=False, extra_env=None):
    """The real CLI in a subprocess, in this test's directories, never the
    user's, with no terminal. Returns (completed process, calls)."""
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    calls = tmp_path / f"calls-{len(list(tmp_path.glob('calls-*')))}.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
               IF_STALE_CALLS=str(calls))
    if paid:
        env["IF_STALE_PAID"] = "1"
    env.update(extra_env or {})
    done = subprocess.run([sys.executable, str(driver), *argv], env=env, capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL, start_new_session=True)
    rows = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return done, rows


def _ran(rows):
    """[(source, repository)] in the order the indexers were called."""
    out = []
    for row in rows:
        if "source" not in row:
            continue
        repo = next((arg.split("=", 1)[1] for arg in row["rest"] if arg.startswith("--repo=")), None)
        if repo is None and "--repo" in row["rest"]:
            repo = row["rest"][row["rest"].index("--repo") + 1]
        out.append((row["source"], repo))
    return out


def _embedded(rows):
    return sum(row.get("embedded", 0) for row in rows)


def _no_traceback(done):
    assert "Traceback" not in done.stderr, done.stderr


@pytest.fixture
def two(tmp_path):
    one, other = _repo(tmp_path, "one"), _repo(tmp_path, "other")
    _register(one, other)
    _indexed(one, other)
    return one, other


# --- nothing behind ---------------------------------------------------------------------------------


NONE_BEHIND = ("Nothing to index: none of the 2 registered repositories is behind (HEAD, tags and branches; "
               "pull requests and issues are not compared).")


def test_nothing_behind_embeds_nothing_and_says_so_on_stderr(tmp_path, two):
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    _no_traceback(done)
    assert _ran(rows) == [] and _embedded(rows) == 0
    assert done.stdout == ""
    assert done.stderr.rstrip().splitlines()[-1] == NONE_BEHIND


def test_nothing_behind_is_silent_with_quiet(tmp_path, two):
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--quiet")
    assert done.returncode == 0, done.stderr
    assert (done.stdout, done.stderr) == ("", "")
    assert _ran(rows) == []


def test_a_source_that_never_ran_beside_code_is_not_behind_on_any_surface(tmp_path, two):
    """--if-stale acts on the conditions doctor, stats and griot_index_status
    report (commits behind, a rewritten history, changed refs, code never
    indexed) and on no other: a platform that never ran is not one of them,
    so doctor saying nothing is behind and --if-stale doing nothing agree."""
    partial = _repo(tmp_path, "partial")
    _register(*two, partial)
    _indexed(partial, sources=SOURCES[:-1])  # platform never ran
    check = doctor.check_repositories(common)
    assert check == {**check, "status": "ok", "detail": f"3 registered, all present; {notices.none_behind(3)}"}
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == []
    assert done.stderr.rstrip().splitlines()[-1] == f"Nothing to index: {notices.none_behind(3)}."


def test_code_never_indexed_is_behind_as_doctor_says_it(tmp_path, two):
    partial = _repo(tmp_path, "partial")
    _register(*two, partial)
    _indexed(partial, sources=SOURCES[1:])  # code never ran
    check = doctor.check_repositories(common)
    assert check["status"] == "warn" and "code never indexed in partial" in check["detail"], check
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == [(source, "partial") for source in SOURCES]
    assert "the ones behind: partial (code never indexed)." in done.stderr


def test_the_nothing_behind_line_is_one_phrase_shared_with_doctor():
    assert notices.none_behind(2) == ("none of the 2 registered repositories is behind (HEAD, tags and branches; "
                                      "pull requests and issues are not compared)")
    assert notices.none_behind(1) == ("the 1 registered repository is not behind (HEAD, tags and branches; "
                                      "pull requests and issues are not compared)")
    assert notices.repo_not_behind("one") == "one is not behind (HEAD, tags and branches)"


def test_doctor_says_nothing_behind_in_the_same_words(tmp_path, two):
    check = doctor.check_repositories(common)
    assert check["status"] == "ok", check
    assert check["detail"] == f"2 registered, all present; {notices.none_behind(2)}"


def test_a_missing_path_is_named_and_not_counted(tmp_path, two):
    gone = tmp_path / "repos" / "gone"
    _register(*two, gone)
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == []
    assert f"Warning: {gone} is not a directory, skipped." in done.stderr
    assert done.stderr.rstrip().splitlines()[-1] == (
        "Nothing to index: none of the 2 registered repositories on disk is behind (HEAD, tags and branches; "
        "pull requests and issues are not compared).")


# --- only what is behind ----------------------------------------------------------------------------


def test_only_the_repository_behind_is_indexed(tmp_path, two):
    one, other = two
    _advance(other, 3)
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    _no_traceback(done)
    assert _ran(rows) == [(source, "other") for source in SOURCES]
    assert _embedded(rows) > 0
    lines = done.stderr.splitlines()
    assert "Indexing 1 of 2 registered repositories, the ones behind: other (3 commits)." in lines
    assert "=== griot index code: other ===" in lines
    assert "=== griot index all --if-stale: other indexed (1 of 2 registered) ===" in lines
    # The run recorded the new HEAD: the next call finds nothing behind.
    again, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert again.returncode == 0 and _ran(rows) == [], again.stderr
    assert again.stderr.rstrip().splitlines()[-1] == NONE_BEHIND


def test_a_rewritten_history_counts_as_behind(tmp_path, two):
    one, _ = two
    (one / "main.py").write_text("def one():\n    return 2\n")
    _git(one, "commit", "-q", "-a", "--amend", "-m", "first, rewritten")
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == [(source, "one") for source in SOURCES]
    assert ("Indexing 1 of 2 registered repositories, the ones behind: one (the indexed commit is not in this "
            "history).") in done.stderr.splitlines()


def test_a_repository_never_indexed_is_behind_in_the_shared_phrase(tmp_path, two):
    fresh = _repo(tmp_path, "fresh")
    _register(*two, fresh)
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == [(source, "fresh") for source in SOURCES]
    assert "Indexing 1 of 3 registered repositories, the ones behind: fresh (never indexed)." in done.stderr


def test_a_source_that_never_ran_is_named_in_the_phrase_doctor_uses():
    assert notices.never_indexed(["code"]) == "code never indexed"
    assert notices.never_indexed(["tags", "platform"]) == "tags and platform never indexed"
    assert notices.never_indexed(list(freshness.SOURCES)) == "never indexed"
    assert tuple(notices.SOURCES) == freshness.SOURCES, "the copy follows the sources"


def test_only_the_sources_asked_for_count(tmp_path, two):
    one, _ = two
    _git(one, "tag", "v1")  # only the tags source is behind
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--sources", "code,commits")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == []
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--sources", "tags")
    assert _ran(rows) == [("tags", "one")], done.stderr
    assert "the ones behind: one (tags changed)." in done.stderr


def test_a_platform_that_refused_every_fetch_is_not_retried_on_a_schedule(tmp_path, two):
    """Doctor and stats report the refusal with its fix; retrying it every
    day would fail every day, and a scheduler would see a failure no one
    can fix by waiting."""
    refused = _repo(tmp_path, "refused")
    _register(*two, refused)
    _indexed(refused, sources=SOURCES[:-1])
    # Its platform never indexed it: the newest platform run refused it.
    common.log_run_summary(script="index_platform.py", repo="all", repo_paths=[str(two[0])], indexed=0,
                           skipped=0, failed=1, refused_repos=["refused"])
    report = next(r for r in freshness.repository_freshness() if r["repo"] == "refused")
    assert report["platform_refused"] and report["missing_sources"] == ["platform"]
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == []


def test_the_selection_counts_a_missing_source_only_when_it_is_code():
    report = {"repo": "one", "head": "a" * 40, "behind": False, "behind_sources": [], "commits_behind": 0,
              "missing_sources": ["platform"], "platform_refused": False}
    assert freshness.stale_reason(report, SOURCES) is None
    assert freshness.stale_reason({**report, "missing_sources": ["tags", "branches"]}, SOURCES) is None
    assert freshness.stale_reason({**report, "missing_sources": ["code", "platform"]}, SOURCES) == "code never indexed"
    never = {**report, "behind": None, "commits_behind": None, "missing_sources": list(SOURCES)}
    assert freshness.stale_reason(never, SOURCES) == "never indexed"
    assert freshness.stale_reason({**report, "missing_sources": []}, SOURCES) is None
    assert freshness.stale_reason({**report, "head": None, "behind": None}, SOURCES) is None
    # A source left out by --sources is not a reason either.
    assert freshness.stale_reason({**report, "missing_sources": ["code"]}, ["commits"]) is None


# --- a real run settles: no fixture writes the run log ----------------------------------------------


def _runs_of(script):
    return [run for run in logdb.read_recent(common.LOG_DIR, "runs", collection=common.COLLECTION_NAME, limit=500)
            if run.get("script") == script]


def test_after_a_real_run_with_nothing_for_some_sources_the_next_run_does_nothing(tmp_path):
    """Repositories with no tags, no remote branches and no remote: the
    tags, branches and platform sources find nothing. Their runs are still
    recorded, so the next call is a no-op instead of indexing every source
    of every repository again, every day, as never indexed."""
    one, other = _repo(tmp_path, "one"), _repo(tmp_path, "other")
    _register(one, other)
    first, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert first.returncode == 0, first.stderr
    assert sorted(_ran(rows)) == sorted((source, repo) for repo in ("one", "other") for source in SOURCES)
    for source in ("tags", "branches", "platform"):
        assert _runs_of(f"index_{source}.py"), f"the empty {source} run was not recorded"
    reports = freshness.repository_freshness()
    assert [(r["repo"], r["behind"], r["missing_sources"]) for r in reports] == [
        ("one", False, []), ("other", False, [])]
    again, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert again.returncode == 0, again.stderr
    assert _ran(rows) == [] and _embedded(rows) == 0
    assert again.stderr.rstrip().splitlines()[-1] == NONE_BEHIND
    assert doctor.check_repositories(common)["detail"] == f"2 registered, all present; {notices.none_behind(2)}"


def test_a_new_tag_after_an_empty_tags_run_is_behind(tmp_path):
    one = _repo(tmp_path, "one")
    _register(one)
    first, _ = _run(tmp_path, "index", "all", "--if-stale")
    assert first.returncode == 0, first.stderr
    _git(one, "tag", "v1")
    done, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert done.returncode == 0, done.stderr
    assert "the ones behind: one (tags changed)." in done.stderr
    assert ("tags", "one") in _ran(rows)


def test_a_repository_with_no_file_to_index_has_its_code_run_recorded(tmp_path):
    """Doctor would otherwise say "code never indexed" right after the
    code run, and --if-stale would run every source of it again."""
    blank = tmp_path / "repos" / "blank"
    blank.mkdir(parents=True)
    _git(blank, "init", "-q", "-b", "main")
    (blank / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    _git(blank, "add", "logo.png")
    _git(blank, "commit", "-q", "-m", "first")
    _register(blank)
    first, _ = _run(tmp_path, "index", "all", "--if-stale")
    assert first.returncode == 0, first.stderr
    assert "No documents to index." in first.stderr + first.stdout
    assert _runs_of("index_code.py")
    again, rows = _run(tmp_path, "index", "all", "--if-stale")
    assert again.returncode == 0 and _ran(rows) == [], again.stderr


@pytest.mark.parametrize("source", ["tags", "branches", "platform"])
def test_an_empty_dry_run_records_nothing(tmp_path, source):
    """A dry run writes no run (it would shadow the last real one in
    griot_index_status), an empty one included."""
    one = _repo(tmp_path, "one")
    _register(one)
    done, _ = _run(tmp_path, "index", source, "--dry-run")
    assert done.returncode == 0, done.stderr
    assert _runs_of(f"index_{source}.py") == []


def test_the_flag_is_never_forwarded_to_a_source():
    assert cli._take_if_stale(["--if-stale", "--repo", "one"]) == (["--repo", "one"], True)
    assert cli._take_if_stale(["--if-st", "--dry-run"]) == (["--dry-run"], True)
    assert cli._take_if_stale(["--repo", "one", "--", "--if-stale"]) == (["--repo", "one", "--", "--if-stale"], False)


# --- --repo and --path ------------------------------------------------------------------------------


def test_repo_narrows_to_one_repository(tmp_path, two):
    one, other = two
    _advance(one)
    _advance(other)
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--repo", "one")
    assert done.returncode == 0, done.stderr
    assert {repo for _, repo in _ran(rows)} == {"one"}
    assert "Indexing 1 of 2 registered repositories, the ones behind: one (1 commit)." not in done.stderr
    assert "Indexing one, which is behind (1 commit)." in done.stderr.splitlines()


def test_repo_that_is_not_behind_is_named(tmp_path, two):
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--repo", "one")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == []
    assert done.stderr.rstrip().splitlines()[-1] == "Nothing to index: one is not behind (HEAD, tags and branches)."


def test_repo_that_is_not_registered_is_incorrect_use(tmp_path, two):
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--repo", "nope")
    assert done.returncode == 2, done.stderr
    assert done.stderr.rstrip().splitlines() == ["Error: no repository named 'nope' is registered.",
                                                 "  griot repos list"]
    assert _ran(rows) == []


def test_path_cannot_be_combined(tmp_path, two):
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--path", str(two[0]))
    assert done.returncode == 2, done.stderr
    assert done.stderr.rstrip().splitlines() == [
        "Error: --if-stale compares the registered repositories, and --path indexes one that is not registered: "
        "use one or the other.",
        "  griot index all --if-stale --repo NAME"]
    assert _ran(rows) == []


def test_without_if_stale_index_all_indexes_every_repository(tmp_path, two):
    """The filter belongs to the flag."""
    done, rows = _run(tmp_path, "index", "all", "--sources", "code")
    assert done.returncode == 0, done.stderr
    assert _ran(rows) == [("code", None)]


# --- a held collection is a routine overlap ---------------------------------------------------------


def _hold_the_lock():
    """The lock as a live index run leaves it: this test process."""
    common.secure_mkdir(common.LOCK_PATH.parent)
    me = psutil.Process(os.getpid())
    common.LOCK_PATH.write_text(json.dumps({"pid": me.pid, "start_time": me.create_time(), "label": None}))
    return me.pid


@pytest.mark.parametrize("quiet", [False, True])
def test_a_held_lock_skips_with_exit_0_and_a_line_quiet_does_not_hide(tmp_path, two, quiet):
    _advance(two[0])
    pid = _hold_the_lock()
    try:
        done, rows = _run(tmp_path, "index", "all", "--if-stale", *(["--quiet"] if quiet else []))
    finally:
        common.LOCK_PATH.unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    _no_traceback(done)
    assert _embedded(rows) == 0
    assert done.stdout == ""
    lines = done.stderr.rstrip().splitlines()
    assert lines[-2].startswith(f"Skipped: another griot index run holds griot's lock (PID {pid}, started ")
    assert lines[-2].endswith("). Nothing was indexed; the next run tries again."), lines
    assert lines[-1] == "  griot index all --if-stale"
    assert not any(line.startswith("Error:") for line in lines), lines


def test_without_if_stale_a_held_lock_still_fails(tmp_path, two):
    """Other runs keep their exit codes: only --if-stale treats it as routine."""
    _hold_the_lock()
    try:
        done, _ = _run(tmp_path, "index", "all", "--sources", "code")
    finally:
        common.LOCK_PATH.unlink(missing_ok=True)
    assert done.returncode == 1, done.stderr


def test_the_lock_error_is_typed_and_keeps_its_message():
    pid = _hold_the_lock()
    try:
        with pytest.raises(common.IndexLockHeldError) as caught:
            common.acquire_lock()
    finally:
        common.LOCK_PATH.unlink(missing_ok=True)
    assert isinstance(caught.value, RuntimeError), "every existing `except RuntimeError` still catches it"
    assert caught.value.pid == pid
    assert str(caught.value).startswith(f"A griot process is already running (PID {pid}).")


def test_the_race_between_two_runs_is_typed_too(monkeypatch):
    real_open = os.open

    def raced(path, *args, **kwargs):
        if Path(path) == common.LOCK_PATH:
            raise FileExistsError(path)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", raced)
    with pytest.raises(common.IndexLockHeldError) as caught:
        common.acquire_lock()
    assert caught.value.pid is None


def _in_process(monkeypatch, behaviour):
    """cli.main with each source replaced by `behaviour(source, rest)`."""
    calls = []

    def fake(source, rest):
        calls.append((source, rest))
        return behaviour(source, rest)
    monkeypatch.setattr(cli, "_run_index_source", fake)
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [
        {"pid": 4242, "command": "griot mcp", "started": None, "role": "mcp"}])
    return calls


def _busy():
    return common.CollectionBusyError(common.COLLECTION_NAME, common.QDRANT_PATH, "held")


def test_a_held_collection_at_the_first_source_skips(tmp_path, two, monkeypatch, capsys):
    _advance(two[0])

    def behaviour(source, rest):
        raise _busy()
    _in_process(monkeypatch, behaviour)
    monkeypatch.setenv("GRIOT_QUIET", "1")
    assert cli.main(["index", "all", "--if-stale"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err.rstrip().splitlines() == [
        f"Skipped: another griot process holds the collection '{common.COLLECTION_NAME}'. Holder: PID 4242 "
        f"(griot mcp). Nothing was indexed; the next run tries again.",
        "  griot index all --if-stale"]


def test_a_collection_held_after_some_sources_ran_says_what_was_indexed(tmp_path, two, monkeypatch, capsys):
    _advance(two[0])

    def behaviour(source, rest):
        if source == "commits":
            raise _busy()
        return 0
    calls = _in_process(monkeypatch, behaviour)
    assert cli.main(["index", "all", "--if-stale", "--repo", "one"]) == 0
    assert [source for source, _ in calls] == ["code", "commits"]
    err = capsys.readouterr().err
    assert err.rstrip().splitlines()[-2:] == [
        f"Stopped at commits of one: another griot process holds the collection '{common.COLLECTION_NAME}'. "
        f"Holder: PID 4242 (griot mcp). code of one was indexed; the rest is tried on the next run.",
        "  griot index all --if-stale --repo one"]


def test_a_failed_source_keeps_its_exit_code_and_names_the_repository(tmp_path, two, monkeypatch, capsys):
    _advance(two[0])
    _in_process(monkeypatch, lambda source, rest: 3 if source == "platform" else 0)
    assert cli.main(["index", "all", "--if-stale"]) == 3
    err = capsys.readouterr().err
    assert err.rstrip().splitlines()[-2:] == [
        "Error: 'griot index platform' failed for one (exit 3); the pipeline stopped there.", "  griot doctor"]


def test_any_other_error_is_still_a_failure(tmp_path, two, monkeypatch, capsys):
    _advance(two[0])

    def behaviour(source, rest):
        raise RuntimeError("the disk is gone")
    _in_process(monkeypatch, behaviour)
    assert cli.main(["index", "all", "--if-stale"]) == 1
    err = capsys.readouterr().err
    assert err.rstrip().splitlines()[-2:] == [
        "Error: 'griot index code' failed for one: the disk is gone", "  griot doctor"]


# --- the spend gate is the same ---------------------------------------------------------------------


def test_a_paid_run_without_a_terminal_above_the_threshold_spends_nothing(tmp_path, two):
    _advance(two[1])
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--quiet", paid=True)
    assert done.returncode == 2, done.stderr
    assert _embedded(rows) == 0 and _ran(rows) == []
    assert done.stdout == ""
    lines = done.stderr.rstrip().splitlines()
    assert lines[-1] == "  griot index all --if-stale --quiet --yes", lines
    assert " from other," in lines[0] or " from other " in lines[0], lines
    assert "registered repositories" not in done.stderr


def test_a_paid_run_estimates_only_the_repositories_behind(tmp_path, two):
    _advance(two[1])
    done, rows = _run(tmp_path, "index", "all", "--if-stale", paid=True)
    assert done.returncode == 2, done.stderr
    lines = done.stderr.splitlines()
    assert lines[:2] == ["Indexing 1 of 2 registered repositories, the ones behind: other (1 commit).",
                         "Estimating the cost of code, commits, tags, branches, platform from other (behind) with "
                         "paid profile 'openai-small'..."], lines
    assert any(line.startswith("Error: embedding ") and " from other, " in line or " from other " in line
               for line in lines), lines


def _paid_in_process(monkeypatch):
    monkeypatch.setattr(common, "profile_is_paid", lambda profile=None: True)
    monkeypatch.setattr(common, "refuse_missing_credential", lambda: 0)


def test_the_estimate_reads_only_the_repositories_behind(tmp_path, two, monkeypatch, capsys):
    _advance(two[1])
    _paid_in_process(monkeypatch)
    estimated = []

    def pending(sources, rest):
        estimated.append(rest)
        return [], 0
    monkeypatch.setattr(cli, "_pending_by_source", pending)
    _in_process(monkeypatch, lambda source, rest: 0)
    assert cli.main(["index", "all", "--if-stale"]) == 0
    assert estimated == [["--repo=other"]]


def test_a_collection_held_during_the_estimate_skips(tmp_path, two, monkeypatch, capsys):
    _advance(two[1])
    _paid_in_process(monkeypatch)

    def pending(sources, rest):
        raise _busy()
    monkeypatch.setattr(cli, "_pending_by_source", pending)
    calls = _in_process(monkeypatch, lambda source, rest: 0)
    assert cli.main(["index", "all", "--if-stale"]) == 0
    assert calls == []
    assert capsys.readouterr().err.rstrip().splitlines()[-2:] == [
        f"Skipped: another griot process holds the collection '{common.COLLECTION_NAME}'. Holder: PID 4242 "
        f"(griot mcp). Nothing was indexed; the next run tries again.",
        "  griot index all --if-stale"]


def test_a_paid_run_with_yes_runs(tmp_path, two):
    _advance(two[1])
    done, rows = _run(tmp_path, "index", "all", "--if-stale", "--yes", paid=True)
    assert done.returncode == 0, done.stderr
    assert _embedded(rows) > 0
    assert {repo for _, repo in _ran(rows)} == {"other"}


def _in_a_terminal(tmp_path, argv, keys: bytes):
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    calls = tmp_path / "calls-pty.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
               IF_STALE_CALLS=str(calls), IF_STALE_PAID="1")
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, str(driver), *argv], stdin=slave, stdout=slave, stderr=slave,
                                env=env, close_fds=True)
        os.close(slave)
        os.write(master, keys)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 120)
            if not ready:
                proc.kill()
                pytest.fail(f"timed out; so far: {out!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=60)
    finally:
        os.close(master)
    rows = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return code, out.decode(errors="replace"), rows


@pytest.mark.parametrize("keys,code", [(b"y\n", 0), (b"n\n", 1)])
def test_the_question_at_a_real_terminal_names_the_repository_behind(tmp_path, two, keys, code):
    _advance(two[1])
    got, out, rows = _in_a_terminal(tmp_path, ["index", "all", "--if-stale"], keys)
    assert got == code, out
    assert ("(code, commits) from other with paid profile 'openai-small'?" in out
            or "from other with paid profile 'openai-small'?" in out), out
    assert (_embedded(rows) > 0) is (code == 0)


# --- help and docs ----------------------------------------------------------------------------------


def test_the_help_names_the_flag_and_what_it_does_not_compare(capsys):
    with pytest.raises(SystemExit):
        cli.main(["index", "all", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--if-stale" in text
    assert "Pull requests and issues are not compared." in text
    assert "--path cannot be combined with it" in text


def test_the_docs_give_both_recipes_and_say_griot_installs_neither():
    text = (Path(__file__).resolve().parent.parent / "docs" / "getting-started.md").read_text()
    assert "griot index all --if-stale --quiet" in text
    assert "launchd" in text and "post-merge" in text
    assert "installs neither" in text
    roadmap = (Path(__file__).resolve().parent.parent / "ROADMAP.md").read_text()
    assert "griot index all --if-stale" in roadmap and "resident watcher" in roadmap
