"""Which repository is behind its own HEAD.

`griot stats` says when the index was last written, for the whole
collection. The question an agent has before trusting a result is another
one: is the index of THIS repository behind what the repository holds now?
Each indexing run now records the HEAD of every repository it covered, and
griot compares that with the HEAD of the moment: per repository, how many
commits behind, and which sources have not run since. Said by
griot_index_status, by `griot stats`, and beside the results of a search."""

import json
import os
import subprocess

import pytest

from griot import common, freshness, logdb, stats


# --- repositories to measure against --------------------------------------------------------------


def _git(path, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "M", "GIT_AUTHOR_EMAIL": "m@example.com",
           "GIT_COMMITTER_NAME": "M", "GIT_COMMITTER_EMAIL": "m@example.com"}
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _repo(tmp_path, name, commits=1):
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    for n in range(commits):
        (path / f"f{n}.txt").write_text(f"{n}\n")
        _git(path, "add", f"f{n}.txt")
        _git(path, "commit", "-q", "-m", f"commit {n}")
    return path


def _advance(path, commits):
    for n in range(commits):
        (path / f"more{n}.txt").write_text(f"{n}\n")
        _git(path, "add", f"more{n}.txt")
        _git(path, "commit", "-q", "-m", f"later {n}")
    return _git(path, "rev-parse", "HEAD")


def _run(script, heads, *, at="2026-10-02T10:00:00+00:00", error=None, refs=None):
    record = {"timestamp": at, "script": script, "heads": heads, "indexed": 1, "skipped": 0, "failed": 0}
    if refs is not None:
        record["refs"] = refs
    if error:
        record.update(error=error, indexed=None, skipped=None, failed=None)
    return record


def _snapshot(path):
    """What a run records for one repository, as log_run_summary does."""
    return freshness.heads_of([str(path)])[path.name], freshness.refs_of([str(path)])[path.name]


# --- the heads a run records ----------------------------------------------------------------------


def test_the_heads_of_the_repositories_a_run_covered(tmp_path):
    one, two = _repo(tmp_path, "one"), _repo(tmp_path, "two")
    plain = tmp_path / "plain"
    plain.mkdir()

    heads = freshness.heads_of([str(one), str(two), str(plain), str(tmp_path / "gone")])

    assert heads == {"one": _git(one, "rev-parse", "HEAD"), "two": _git(two, "rev-parse", "HEAD"), "plain": None, "gone": None}


def test_a_run_records_the_heads_of_what_it_covered(tmp_path, monkeypatch):
    one = _repo(tmp_path, "one")
    _git(one, "tag", "v1")
    written = []
    monkeypatch.setattr(logdb, "write_run", lambda log_dir, record: written.append(record))

    common.log_run_summary(script="index_code.py", repo="one", repo_paths=[str(one)], indexed=1, skipped=0, failed=0)

    assert written[0]["heads"] == {"one": _git(one, "rev-parse", "HEAD")}
    assert "repo_paths" not in written[0], "the paths are not what is kept; the heads are"
    # And what the tags and branches sources index, as one fingerprint each.
    assert set(written[0]["refs"]["one"]) == {"tags", "branches"}
    assert written[0]["refs"]["one"]["tags"] == freshness.refs_of([str(one)])["one"]["tags"]


def test_the_fingerprint_of_the_refs_changes_with_them(tmp_path):
    one = _repo(tmp_path, "one")
    before = freshness.refs_of([str(one)])["one"]
    _git(one, "tag", "v1")
    after_a_tag = freshness.refs_of([str(one)])["one"]
    _git(one, "update-ref", "refs/remotes/origin/feature", "HEAD")
    after_a_branch = freshness.refs_of([str(one)])["one"]

    assert before["tags"] != after_a_tag["tags"] and before["branches"] == after_a_tag["branches"]
    assert after_a_tag["branches"] != after_a_branch["branches"] and after_a_tag["tags"] == after_a_branch["tags"]
    assert freshness.refs_of([str(tmp_path / "gone")]) == {"gone": None}


def test_a_run_that_names_no_paths_records_no_heads(monkeypatch):
    """A run that died before it knew what it covered (cli.py)."""
    written = []
    monkeypatch.setattr(logdb, "write_run", lambda log_dir, record: written.append(record))

    common.log_run_summary(script="index_code.py", indexed=None, skipped=None, failed=None, error="died")

    assert "heads" not in written[0]


@pytest.mark.parametrize("module", ["index_code", "index_commits", "index_tags", "index_branches", "index_platform"])
def test_every_indexer_records_the_heads(module):
    """Read from the source: an indexer that forgets would make its source
    invisible to the freshness report, with nothing failing."""
    import inspect

    import importlib
    text = inspect.getsource(importlib.import_module(f"griot.{module}"))
    call = text[text.index("common.log_run_summary("):]
    call = call[:call.index("\n    )") + 1]
    assert "repo_paths=" in call, f"{module} does not pass repo_paths to log_run_summary"


# --- the measure ----------------------------------------------------------------------------------


def _now(*paths):
    """The repositories as repository_freshness() hands them to assess()."""
    return [{"name": p.name, "path": str(p), "head": _git(p, "rev-parse", "HEAD"),
             "refs": freshness.refs_of([str(p)])[p.name]} for p in paths]


def test_a_repository_never_indexed_is_not_behind_and_not_fresh_either(tmp_path):
    one = _repo(tmp_path, "one")

    [report] = freshness.assess([], _now(one))

    assert report["repo"] == "one" and report["path"] == str(one)
    assert report["behind"] is None and report["commits_behind"] is None
    assert report["last_indexed_at"] is None and report["sources"] == {} and report["behind_sources"] == []


def test_a_repository_indexed_at_its_current_head_is_fresh(tmp_path):
    one = _repo(tmp_path, "one")
    head = _git(one, "rev-parse", "HEAD")

    [report] = freshness.assess([_run("index_code.py", {"one": head})], _now(one))

    assert report["behind"] is False and report["commits_behind"] == 0
    assert report["sources"] == {"code": {"indexed_head": head, "at": "2026-10-02T10:00:00+00:00", "commits_behind": 0}}
    assert report["last_indexed_at"] == "2026-10-02T10:00:00+00:00"


def test_commits_made_since_the_run_put_the_repository_behind(tmp_path):
    one = _repo(tmp_path, "one")
    then = _git(one, "rev-parse", "HEAD")
    _advance(one, 3)

    [report] = freshness.assess([_run("index_code.py", {"one": then})], _now(one))

    assert report["behind"] is True and report["commits_behind"] == 3
    assert report["sources"]["code"]["commits_behind"] == 3


def test_each_source_is_measured_by_its_own_last_run(tmp_path):
    """The code was indexed, then two commits came, then only the commits
    source ran: the commits are fresh, the code is three... two behind."""
    one = _repo(tmp_path, "one")
    first = _git(one, "rev-parse", "HEAD")
    later = _advance(one, 2)
    runs = [_run("index_commits.py", {"one": later}, at="2026-10-02T12:00:00+00:00"),  # newest first, as logdb reads
            _run("index_code.py", {"one": first})]

    [report] = freshness.assess(runs, _now(one))

    assert report["behind"] is True and report["commits_behind"] == 2, "the worst of the sources"
    assert report["sources"]["commits"]["commits_behind"] == 0
    assert report["sources"]["code"]["commits_behind"] == 2
    assert report["last_indexed_at"] == "2026-10-02T12:00:00+00:00"


def test_the_newest_run_of_a_source_is_the_one_that_counts(tmp_path):
    one = _repo(tmp_path, "one")
    first = _git(one, "rev-parse", "HEAD")
    later = _advance(one, 1)
    runs = [_run("index_code.py", {"one": later}, at="2026-10-02T12:00:00+00:00"),
            _run("index_code.py", {"one": first})]

    [report] = freshness.assess(runs, _now(one))

    assert report["behind"] is False and report["sources"]["code"]["indexed_head"] == later


def test_a_run_that_died_indexed_nothing(tmp_path):
    one = _repo(tmp_path, "one")
    first = _git(one, "rev-parse", "HEAD")
    later = _advance(one, 1)
    runs = [_run("index_code.py", {"one": later}, at="2026-10-02T12:00:00+00:00", error="RuntimeError: x"),
            _run("index_code.py", {"one": first})]

    [report] = freshness.assess(runs, _now(one))

    assert report["behind"] is True and report["sources"]["code"]["indexed_head"] == first


def test_a_run_covers_only_the_repositories_whose_heads_it_recorded(tmp_path):
    one, two = _repo(tmp_path, "one"), _repo(tmp_path, "two")
    runs = [_run("index_code.py", {"one": _git(one, "rev-parse", "HEAD")})]

    one_report, two_report = freshness.assess(runs, _now(one, two))

    assert one_report["behind"] is False and two_report["behind"] is None


def test_a_run_from_before_the_heads_were_recorded_says_nothing_about_freshness(tmp_path):
    one = _repo(tmp_path, "one")
    old_style = {"timestamp": "2026-09-01T00:00:00+00:00", "script": "index_code.py", "repo": "one", "indexed": 3}

    [report] = freshness.assess([old_style], _now(one))

    assert report["behind"] is None and report["sources"] == {}


def test_a_head_the_repository_no_longer_has_is_behind_by_an_unknown_number(tmp_path):
    """History rewritten since the run: the indexed commit is gone."""
    one = _repo(tmp_path, "one")

    [report] = freshness.assess([_run("index_code.py", {"one": "0" * 40})], _now(one))

    assert report["behind"] is True and report["commits_behind"] is None
    assert report["sources"]["code"]["commits_behind"] is None


@pytest.mark.parametrize("rewrite", [["commit", "--amend", "-q", "--no-edit", "-m", "reworded"],
                                     ["reset", "-q", "--hard", "HEAD~2"]])
def test_a_history_rewritten_since_the_run_is_behind_by_an_unknown_number_too(tmp_path, rewrite):
    """The indexed commit is still in the object store for a while, so git
    counts "commits in HEAD that are not in it" and gives a number: 1 after
    an amend, 0 after a reset. Both read as fresh or nearly so, for an index
    of a commit that is not in the history any more."""
    one = _repo(tmp_path, "one", commits=3)
    then = _git(one, "rev-parse", "HEAD")
    _git(one, *rewrite)

    [report] = freshness.assess([_run("index_code.py", {"one": then})], _now(one))

    assert report["behind"] is True and report["commits_behind"] is None
    assert report["behind_sources"] == ["code"]


# --- each source is measured by what it indexes --------------------------------------------------


def test_a_new_tag_puts_the_tags_source_behind_and_nothing_else(tmp_path):
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)
    _git(one, "tag", "v1")

    [report] = freshness.assess([_run("index_code.py", {"one": head}, refs={"one": refs}),
                                 _run("index_tags.py", {"one": head}, refs={"one": refs})], _now(one))

    assert report["sources"]["tags"] == {"at": "2026-10-02T10:00:00+00:00", "changed": True}
    assert report["sources"]["code"]["commits_behind"] == 0
    assert report["behind"] is True and report["behind_sources"] == ["tags"] and report["commits_behind"] == 0


def test_a_commit_does_not_put_the_tags_source_behind(tmp_path):
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)
    _advance(one, 2)

    [report] = freshness.assess([_run("index_code.py", {"one": head}, refs={"one": refs}),
                                 _run("index_tags.py", {"one": head}, refs={"one": refs})], _now(one))

    assert report["sources"]["tags"]["changed"] is False
    assert report["behind_sources"] == ["code"] and report["commits_behind"] == 2


def test_a_new_remote_branch_puts_the_branches_source_behind(tmp_path):
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)
    _git(one, "update-ref", "refs/remotes/origin/feature", "HEAD")

    [report] = freshness.assess([_run("index_branches.py", {"one": head}, refs={"one": refs})], _now(one))

    assert report["sources"]["branches"] == {"at": "2026-10-02T10:00:00+00:00", "changed": True}
    assert report["behind"] is True and report["behind_sources"] == ["branches"]


def test_the_platform_source_is_never_behind_by_anything_local(tmp_path):
    """Pull requests and issues live on the platform: nothing here can say."""
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)
    _advance(one, 1)
    _git(one, "tag", "v1")

    [report] = freshness.assess([_run("index_platform.py", {"one": head}, refs={"one": refs})], _now(one))

    assert report["sources"]["platform"] == {"at": "2026-10-02T10:00:00+00:00"}
    assert report["behind"] is False and report["behind_sources"] == []


def test_a_run_from_before_refs_were_recorded_cannot_say_whether_tags_changed(tmp_path):
    one = _repo(tmp_path, "one")
    head = _git(one, "rev-parse", "HEAD")

    [report] = freshness.assess([_run("index_tags.py", {"one": head})], _now(one))

    assert report["sources"]["tags"]["changed"] is None and report["behind_sources"] == []


def test_the_sources_that_never_ran_are_named(tmp_path):
    """Fresh tags and no code at all is not a repository an agent can
    search: `missing_sources` says what was never indexed."""
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)

    [report] = freshness.assess([_run("index_tags.py", {"one": head}, refs={"one": refs})], _now(one))

    assert report["behind"] is False
    assert report["missing_sources"] == ["code", "commits", "branches", "platform"]


def test_a_repository_never_indexed_names_every_source_as_missing(tmp_path):
    one = _repo(tmp_path, "one")

    [report] = freshness.assess([], _now(one))

    assert report["missing_sources"] == ["code", "commits", "tags", "branches", "platform"]


def test_a_repository_that_is_not_there_or_not_git_has_no_head(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    now = [{"name": "plain", "path": str(plain), "head": None}, {"name": "gone", "path": str(tmp_path / "gone"), "head": None}]

    plain_report, gone_report = freshness.assess([_run("index_code.py", {"plain": "a" * 40, "gone": "b" * 40})], now)

    assert plain_report["head"] is None and plain_report["behind"] is None
    assert gone_report["behind"] is None and gone_report["last_indexed_at"] == "2026-10-02T10:00:00+00:00"
    assert plain_report["missing_sources"] == [] and gone_report["missing_sources"] == [], "nothing could have run"


def test_the_sources_behind_are_named_in_one_order_whatever_the_runs_order(tmp_path):
    one = _repo(tmp_path, "one")
    head, refs = _snapshot(one)
    _advance(one, 1)
    _git(one, "tag", "v1")
    runs = [_run("index_tags.py", {"one": head}, refs={"one": refs}, at="2026-10-02T12:00:00+00:00"),
            _run("index_code.py", {"one": head}, refs={"one": refs})]

    [report] = freshness.assess(runs, _now(one))

    assert report["behind_sources"] == ["code", "tags"]


def test_a_commit_indexed_on_another_branch_is_not_in_this_history(tmp_path):
    """A checkout, not a rewrite: the words have to fit both."""
    one = _repo(tmp_path, "one")
    on_main = _git(one, "rev-parse", "HEAD")
    _git(one, "checkout", "-q", "-b", "feature", "HEAD~0")
    _git(one, "checkout", "-q", "main")
    _advance(one, 1)
    indexed = _git(one, "rev-parse", "HEAD")
    _git(one, "checkout", "-q", "feature")
    assert _git(one, "rev-parse", "HEAD") == on_main

    [report] = freshness.assess([_run("index_code.py", {"one": indexed})], _now(one))

    assert report["behind"] is True and report["commits_behind"] is None
    assert "not in this history" in stats.behind_phrase({"repo": "one", "commits_behind": None, "behind_sources": ["code"]})


def test_the_log_is_read_once_per_search_and_each_repository_asked_once_for_its_head(tmp_path, monkeypatch):
    one = _repo(tmp_path, "one")
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": _git(one, "rev-parse", "HEAD")}),
                                     "collection": common.COLLECTION_NAME})
    reads, git_calls = [], []
    real_read, real_git = logdb.read_recent, common.run_git
    monkeypatch.setattr(logdb, "read_recent", lambda *a, **k: reads.append(1) or real_read(*a, **k))
    monkeypatch.setattr(common, "run_git", lambda path, args, **k: git_calls.append(args[0]) or real_git(path, args, **k))

    freshness.behind_among(["one"])

    assert len(reads) == 1
    assert git_calls.count("rev-parse") == 1 and git_calls.count("for-each-ref") == 2


def test_a_run_asks_each_repository_once_for_its_head(tmp_path, monkeypatch):
    one = _repo(tmp_path, "one")
    git_calls = []
    real_git = common.run_git
    monkeypatch.setattr(common, "run_git", lambda path, args, **k: git_calls.append(args[0]) or real_git(path, args, **k))
    monkeypatch.setattr(logdb, "write_run", lambda log_dir, record: None)

    common.log_run_summary(script="index_code.py", repo="one", repo_paths=[str(one)], indexed=1, skipped=0, failed=0)

    assert git_calls.count("rev-parse") == 1 and git_calls.count("for-each-ref") == 2


def test_the_report_names_each_source_by_what_a_person_calls_it(tmp_path):
    one = _repo(tmp_path, "one")
    head = _git(one, "rev-parse", "HEAD")
    runs = [_run(script, {"one": head}) for script in
            ("index_code.py", "index_commits.py", "index_tags.py", "index_branches.py", "index_platform.py")]

    [report] = freshness.assess(runs, _now(one))

    assert set(report["sources"]) == {"code", "commits", "tags", "branches", "platform"}


# --- wired to the registered repositories and the log ---------------------------------------------


def test_the_registered_repositories_against_the_runs_of_this_collection(tmp_path, monkeypatch):
    one = _repo(tmp_path, "one")
    then = _git(one, "rev-parse", "HEAD")
    _advance(one, 2)
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": then}), "collection": common.COLLECTION_NAME})
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": "f" * 40}, at="2026-10-02T13:00:00+00:00"),
                                     "collection": "another-collection"})

    [report] = freshness.repository_freshness()

    assert report["repo"] == "one" and report["commits_behind"] == 2


def test_no_registered_repository_means_an_empty_report(monkeypatch):
    monkeypatch.setattr(common, "load_repos", lambda: [])

    assert freshness.repository_freshness() == []


def test_a_repository_registered_under_another_name_than_its_directory_is_matched_by_name(tmp_path, monkeypatch):
    """Everything indexed is keyed by the directory name (repos.py): so is this."""
    one = _repo(tmp_path, "one")
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": _git(one, "rev-parse", "HEAD")}),
                                     "collection": common.COLLECTION_NAME})

    [report] = freshness.repository_freshness()

    assert report["behind"] is False


# --- where it is said -----------------------------------------------------------------------------


def test_the_index_status_carries_it(tmp_path, monkeypatch):
    one = _repo(tmp_path, "one")
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])

    status = common.get_index_status(reuse_active_handle=False)

    assert [r["repo"] for r in status["repositories"]] == ["one"]


@pytest.mark.anyio
async def test_the_status_tool_says_which_repositories_are_behind(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    one = _repo(tmp_path, "one")
    then = _git(one, "rev-parse", "HEAD")
    _advance(one, 4)
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_commits.py", {"one": then}), "collection": common.COLLECTION_NAME})

    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_index_status", {})).structured_content

    [report] = out["repositories"]
    assert report["behind"] is True and report["commits_behind"] == 4 and report["sources"]["commits"]["commits_behind"] == 4


@pytest.mark.anyio
async def test_a_search_says_when_a_repository_in_its_results_is_behind(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    one, two = _repo(tmp_path, "one"), _repo(tmp_path, "two")
    then = _git(one, "rev-parse", "HEAD")
    _advance(one, 2)
    monkeypatch.setattr(common, "load_repos", lambda: [str(one), str(two)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": then, "two": _git(two, "rev-parse", "HEAD")}),
                                     "collection": common.COLLECTION_NAME})

    class Hit:
        def __init__(self, repo):
            self.id, self.score = f"{repo}:code:f.py:0", 0.5
            self.payload = {"repo": repo, "source_type": "code", "file_path": "f.py", "content": "x", "chunk_index": 0}

    monkeypatch.setattr(common, "search", lambda *a, **k: [Hit("one"), Hit("two")])
    monkeypatch.setattr(mcp_server, "_log_search", lambda *a, **k: None)

    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_search", {"query": "x"})).structured_content

    assert out["behind"] == {"one": 2}, "only what is behind, among the repositories in the results"
    assert "behind" in out["note"] and "one" in out["note"]


@pytest.mark.anyio
async def test_a_search_whose_repositories_are_fresh_says_nothing_of_it(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    one = _repo(tmp_path, "one")
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_run("index_code.py", {"one": _git(one, "rev-parse", "HEAD")}),
                                     "collection": common.COLLECTION_NAME})

    class Hit:
        id, score = "one:code:f.py:0", 0.5
        payload = {"repo": "one", "source_type": "code", "file_path": "f.py", "content": "x", "chunk_index": 0}

    monkeypatch.setattr(common, "search", lambda *a, **k: [Hit()])
    monkeypatch.setattr(mcp_server, "_log_search", lambda *a, **k: None)

    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_search", {"query": "x"})).structured_content

    assert out["behind"] == {} and out["note"] == mcp_server.SEARCH_RESULT_NOTE


def test_a_search_asks_git_nothing_when_no_run_ever_recorded_a_head(tmp_path, monkeypatch):
    """Before the first run of this version there is nothing to compare
    with, and a search must not pay for the comparison."""
    one = _repo(tmp_path, "one")
    monkeypatch.setattr(common, "load_repos", lambda: [str(one)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {"timestamp": "2026-09-01T00:00:00+00:00", "script": "index_code.py",
                                     "collection": common.COLLECTION_NAME, "indexed": 1})
    asked = []
    monkeypatch.setattr(freshness, "head_of", lambda path: asked.append(path))

    assert freshness.behind_among(["one"]) == {} and asked == []


@pytest.mark.anyio
async def test_a_repository_name_cannot_carry_a_line_break_into_the_note(monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    class Hit:
        id, score = "x:code:f.py:0", 0.5
        payload = {"repo": "bad\nname", "source_type": "code", "file_path": "f.py", "content": "x", "chunk_index": 0}

    monkeypatch.setattr(common, "search", lambda *a, **k: [Hit()])
    monkeypatch.setattr(mcp_server, "_log_search", lambda *a, **k: None)
    monkeypatch.setattr(freshness, "behind_among", lambda names, collection=None: {"bad\nname": 2})

    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_search", {"query": "x"})).structured_content

    assert "\n" not in out["note"].replace(mcp_server.SEARCH_RESULT_NOTE, "") and "badname" in out["note"]


def test_the_server_tells_the_agent_what_behind_means():
    from griot import mcp_server

    assert "behind" in mcp_server.SERVER_INSTRUCTIONS and "for the whole index" not in mcp_server.SERVER_INSTRUCTIONS


def test_stats_names_the_repositories_behind_and_asks_for_attention():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [
                  {"repo": "one", "path": "/x/one", "head": "a" * 40, "behind": True, "commits_behind": 3,
                   "behind_sources": ["code", "tags"], "missing_sources": [],
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}},
                  {"repo": "two", "path": "/x/two", "head": "b" * 40, "behind": False, "commits_behind": 0,
                   "behind_sources": [], "missing_sources": ["code"],
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}},
                  {"repo": "three", "path": "/x/three", "head": "c" * 40, "behind": True, "commits_behind": None,
                   "behind_sources": ["code"], "missing_sources": [],
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}},
                  {"repo": "four", "path": "/x/four", "head": "d" * 40, "behind": True, "commits_behind": 0,
                   "behind_sources": ["branches"], "missing_sources": [],
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}}]}

    s = stats.compute_stats([], [], status)
    text = stats.format_stats(s, 30)

    assert s["repositories_behind"] == [
        {"repo": "one", "commits_behind": 3, "behind_sources": ["code", "tags"]},
        {"repo": "four", "commits_behind": 0, "behind_sources": ["branches"]},
        {"repo": "three", "commits_behind": None, "behind_sources": ["code"]}]
    assert s["repositories_without_code"] == ["two"]
    behind_line = next(line for line in s["attention"] if "behind" in line)
    assert "one (3 commits; tags changed)" in behind_line and "four (branches changed)" in behind_line
    assert "three (the indexed commit is not in this history)" in behind_line
    assert any("code never indexed" in line and "two" in line for line in s["attention"])
    assert "index behind in one (3 commits; tags changed)" in text, "in the state lines, not only under attention"


def test_stats_says_nothing_when_every_repository_is_fresh():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [{"repo": "one", "path": "/x/one", "head": "a" * 40, "behind": False, "commits_behind": 0,
                                "behind_sources": [], "missing_sources": ["platform"],
                                "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}}]}

    s = stats.compute_stats([], [], status)

    assert s["repositories_behind"] == [] and s["repositories_without_code"] == []
    assert not any("behind" in line or "never indexed" in line for line in s["attention"])
    assert "behind" not in stats.format_stats(s, 30)


def test_stats_from_a_status_that_knows_nothing_of_repositories():
    """An older status dict, or a caller that did not look."""
    s = stats.compute_stats([], [], {"points_count": 0, "points_error": None, "last_indexed": None,
                                     "spend_ceiling_exceeded": False})

    assert s["repositories_behind"] == [] and s["repositories_without_code"] == []


# --- a repository the platform refused entirely ----------------------------------------------------
#
# [debt 17 follow-up] index_platform.py records, under `refused_repos`, the
# repositories whose platform refused every fetch while others answered.
# The newest platform run that concerned a repository (covered it, or
# refused it) says whether its platform is refusing it now.


def _platform_run(heads, refused=None, **kwargs):
    record = _run("index_platform.py", heads, **kwargs)
    if refused is not None:
        record["refused_repos"] = refused
    return record


def test_a_repository_the_last_platform_run_refused_is_said_to_be(tmp_path):
    good, bad = _repo(tmp_path, "good"), _repo(tmp_path, "bad")
    runs = [_platform_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["bad"])]

    good_report, bad_report = freshness.assess(runs, _now(good, bad))

    assert bad_report["platform_refused"] is True
    assert good_report["platform_refused"] is False


def test_a_later_platform_run_that_indexed_it_clears_the_refusal(tmp_path):
    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    runs = [_platform_run({"bad": head}, at="2026-10-03T10:00:00+00:00"),
            _platform_run({}, refused=["bad"], at="2026-10-02T10:00:00+00:00")]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is False


def test_a_later_platform_run_of_another_repository_leaves_the_refusal(tmp_path):
    good, bad = _repo(tmp_path, "good"), _repo(tmp_path, "bad")
    runs = [_platform_run({"good": _git(good, "rev-parse", "HEAD")}, at="2026-10-03T10:00:00+00:00"),
            _platform_run({}, refused=["bad"], at="2026-10-02T10:00:00+00:00")]

    _, report = freshness.assess(runs, _now(good, bad))

    assert report["platform_refused"] is True


def test_a_refusal_newer_than_the_last_platform_run_that_indexed_it_stands(tmp_path):
    bad = _repo(tmp_path, "bad")
    runs = [_platform_run({}, refused=["bad"], at="2026-10-03T10:00:00+00:00"),
            _platform_run({"bad": _git(bad, "rev-parse", "HEAD")}, at="2026-10-02T10:00:00+00:00")]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is True


def test_only_platform_runs_say_whether_the_platform_refused(tmp_path):
    """A code run covering the repository after the refusal says nothing
    about the platform."""
    bad = _repo(tmp_path, "bad")
    runs = [_run("index_code.py", {"bad": _git(bad, "rev-parse", "HEAD")}, at="2026-10-03T10:00:00+00:00"),
            _platform_run({}, refused=["bad"], at="2026-10-02T10:00:00+00:00")]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is True


def test_a_platform_run_that_died_says_nothing_about_a_refusal(tmp_path):
    """A run that died fetched nothing: it neither clears nor makes one."""
    bad = _repo(tmp_path, "bad")
    runs = [_platform_run({"bad": _git(bad, "rev-parse", "HEAD")}, error="killed", at="2026-10-03T10:00:00+00:00"),
            _platform_run({}, refused=["bad"], at="2026-10-02T10:00:00+00:00")]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is True


def test_a_refused_list_edited_into_something_else_is_ignored(tmp_path):
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_platform_run({}, refused="bad")], _now(bad))

    assert report["platform_refused"] is False


def test_stats_asks_for_attention_to_a_repository_the_platform_refused():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [
                  {"repo": "bad\x1b[2J", "path": "/x/bad", "head": "a" * 40, "behind": None, "commits_behind": None,
                   "behind_sources": [], "missing_sources": [], "platform_refused": True,
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}},
                  {"repo": "good", "path": "/x/good", "head": "b" * 40, "behind": False, "commits_behind": 0,
                   "behind_sources": [], "missing_sources": [], "platform_refused": False,
                   "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}}]}

    s = stats.compute_stats([], [], status)

    [line] = [line for line in s["attention"] if "refused" in line]
    assert "bad?[2J" in line and "good" not in line and "griot auth list" in line


@pytest.mark.anyio
async def test_the_status_and_stats_tools_say_which_repository_the_platform_refused(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    good, bad = _repo(tmp_path, "good"), _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(good), str(bad)])
    common.secure_mkdir(common.LOG_DIR)
    logdb.write_run(common.LOG_DIR, {**_platform_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["bad"]),
                                     "collection": common.COLLECTION_NAME})

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert {r["repo"]: r["platform_refused"] for r in status["repositories"]} == {"good": False, "bad": True}
    assert any("refused" in line and "bad" in line for line in out["attention"])


# --- a run the platform refused entirely ------------------------------------------------------------
#
# [debt 19] A run whose every fetch was refused is recorded with `error`
# (it did not do its job) and, now, with `refused_repos`. The refusal is
# found in the newest PLATFORM run, not only while that run is the newest
# run of all: a later code or commits run used to hide it.


def _all_refused_run(heads, refused, at="2026-10-02T10:00:00+00:00"):
    """As index_platform.py records it: counts, an error, every head of
    the run, and the repositories refused."""
    record = _platform_run(heads, refused=refused, at=at)
    record.update(indexed=0, skipped=0, failed=3, error="the platform refused every fetch (HTTP 401)")
    return record


def test_a_run_refused_entirely_is_still_named_after_a_later_code_run(tmp_path):
    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    runs = [_run("index_code.py", {"bad": head}, at="2026-10-03T10:00:00+00:00"),
            _all_refused_run({"bad": head}, refused=["bad"])]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is True


def test_a_run_refused_entirely_still_does_not_count_as_indexing_the_platform(tmp_path):
    bad = _repo(tmp_path, "bad")
    runs = [_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"])]

    [report] = freshness.assess(runs, _now(bad))

    assert "platform" not in report["sources"] and "platform" in report["missing_sources"]
    assert report["last_indexed_at"] is None


def test_a_run_refused_entirely_clears_no_older_refusal_of_a_repository_it_did_not_refuse(tmp_path):
    """Its heads hold every repository of the run; only those it names as
    refused say anything about the platform."""
    good, bad = _repo(tmp_path, "good"), _repo(tmp_path, "bad")
    heads = {"good": _git(good, "rev-parse", "HEAD"), "bad": _git(bad, "rev-parse", "HEAD")}
    runs = [_all_refused_run(heads, refused=["good"], at="2026-10-03T10:00:00+00:00"),
            _platform_run({"good": heads["good"]}, refused=["bad"], at="2026-10-02T10:00:00+00:00")]

    good_report, bad_report = freshness.assess(runs, _now(good, bad))

    assert good_report["platform_refused"] is True and bad_report["platform_refused"] is True


def test_a_later_platform_run_that_indexed_it_clears_a_refusal_of_the_whole_run(tmp_path):
    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    runs = [_platform_run({"bad": head}, at="2026-10-03T10:00:00+00:00"),
            _all_refused_run({"bad": head}, refused=["bad"])]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is False


def _write_runs(*records):
    common.secure_mkdir(common.LOG_DIR)
    for record in records:  # oldest first, as they happened
        logdb.write_run(common.LOG_DIR, {**record, "collection": common.COLLECTION_NAME})


def test_doctor_names_a_repository_refused_by_a_whole_run_after_a_later_code_run(tmp_path, monkeypatch):
    from griot import doctor

    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": head}, refused=["bad"]),
                _run("index_code.py", {"bad": head}, at="2026-10-03T10:00:00+00:00"))

    check = doctor.check_repositories(common)

    assert check["status"] == "warn" and "refused" in check["detail"] and "bad" in check["detail"]


@pytest.mark.anyio
async def test_the_tools_name_a_repository_refused_by_a_whole_run_after_a_later_code_run(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": head}, refused=["bad"]),
                _run("index_code.py", {"bad": head}, at="2026-10-03T10:00:00+00:00"))

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert [r["platform_refused"] for r in status["repositories"]] == [True]
    # The newest run of all is the code run, which did its job: only the
    # newest platform run can still say the platform refused.
    assert not (status.get("last_indexed") or {}).get("error")
    assert any("refused" in line and "bad" in line for line in out["attention"])



# --- a run refused entirely, said once -------------------------------------------------------------
#
# [debt 33] While a platform run refused entirely was the newest run of all,
# `griot doctor` and `griot stats` said it twice: "the last indexing run
# failed: the platform refused every fetch (...)" and the line naming the
# refused repository. The line naming it says more, so it is the one kept;
# the generic one stays for every run it does not cover.


def _refusal_lines(lines):
    return [line for line in lines if "refused" in line]


def test_doctor_says_once_that_the_newest_run_was_refused_entirely(tmp_path, monkeypatch):
    from griot import doctor

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"]))

    index, repositories = doctor.check_index(common), doctor.check_repositories(common)

    [line] = _refusal_lines([index["detail"], repositories["detail"]])
    assert "bad" in line and "HTTP 401" not in line
    assert "last indexed" not in index["detail"], "a run refused entirely indexed nothing"


def test_stats_says_once_that_the_newest_run_was_refused_entirely(tmp_path, monkeypatch):
    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"]))

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    [line] = _refusal_lines(s["attention"])
    assert "bad" in line and "griot auth list" in line
    # The run is still the last attempt, and still a failed one.
    assert s["last_indexed_error"] == "the platform refused every fetch (HTTP 401)"


def test_a_refused_repository_no_longer_registered_keeps_the_generic_line(tmp_path, monkeypatch):
    """Nothing else names `gone` any more: the generic line is the only
    trace of its refusal, so it stays."""
    from griot import doctor

    bad, gone = _repo(tmp_path, "bad"), _repo(tmp_path, "gone")
    heads = {"bad": _git(bad, "rev-parse", "HEAD"), "gone": _git(gone, "rev-parse", "HEAD")}
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run(heads, refused=["bad", "gone"]))

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))
    index = doctor.check_index(common)

    assert any("last indexing run failed" in line and "HTTP 401" in line for line in s["attention"])
    assert "last indexing run failed" in index["detail"] and index["status"] == "warn"


def test_a_refused_run_that_names_no_repository_keeps_the_generic_line(tmp_path, monkeypatch):
    """A run recorded before `refused_repos` existed names nothing: only
    the generic line says it failed."""
    from griot import doctor

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    record = _all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"])
    del record["refused_repos"]
    _write_runs(record)

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    assert any("last indexing run failed" in line for line in s["attention"])
    assert "last indexing run failed" in doctor.check_index(common)["detail"]


def test_a_run_that_died_after_a_refusal_keeps_the_generic_line(tmp_path, monkeypatch):
    bad = _repo(tmp_path, "bad")
    head = _git(bad, "rev-parse", "HEAD")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": head}, refused=["bad"]),
                _run("index_code.py", {"bad": head}, at="2026-10-03T10:00:00+00:00", error="collection busy"))

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    assert any("did not finish: collection busy" in line for line in s["attention"])
    assert any("refused" in line and "bad" in line for line in s["attention"])


@pytest.mark.anyio
async def test_the_stats_tool_says_once_that_the_newest_run_was_refused_entirely(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"]))

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content
        out = (await client.call_tool("griot_stats", {})).structured_content

    [line] = _refusal_lines(out["attention"])
    assert "bad" in line
    # The status keeps saying both, as data: the run failed, and which
    # repository its platform refused.
    assert status["last_indexed"]["error"] == "the platform refused every fetch (HTTP 401)"
    assert [r["platform_refused"] for r in status["repositories"]] == [True]


def _refused_report(name):
    return {"repo": name, "path": f"/x/{name}", "head": "a" * 40, "behind": False, "commits_behind": 0,
            "behind_sources": [], "missing_sources": [], "platform_refused": True, "sources": {}}


@pytest.mark.parametrize("last,reports,expected", [
    # A name with an escape sequence is compared as it is printed.
    ({"error": "refused", "refused_repos": ["bad\x1b[2J"]}, [_refused_report("bad\x1b[2J")], True),
    # No error: nothing generic to leave out.
    ({"error": None, "refused_repos": ["bad"]}, [_refused_report("bad")], False),
    # A repository not named by the refusal line keeps the generic line.
    ({"error": "refused", "refused_repos": ["bad", "other"]}, [_refused_report("bad")], False),
    ({"error": "refused", "refused_repos": []}, [], False),
    ({"error": "refused", "refused_repos": None}, [_refused_report("bad")], False),
    ({"error": "refused", "refused_repos": "bad"}, [_refused_report("bad")], False),
    ({"error": "refused", "refused_repos": [None]}, [_refused_report("bad")], False),
    # A number is not a repository's name, even one spelled like it.
    ({"error": "refused", "refused_repos": [1]}, [_refused_report("1")], False),
])
def test_which_last_runs_the_refusal_line_already_names(last, reports, expected):
    assert stats.refusal_names_last_run(last, reports) is expected


def test_doctor_calls_a_refused_last_run_an_attempt_beside_the_points_count(monkeypatch):
    """With the refusal said by the repositories check, the index check
    reports the count, and the run as what it was: an attempt."""
    from griot import doctor

    monkeypatch.setattr(common, "get_index_status", lambda **k: {
        "points_count": 12, "points_error": None, "collection": "c", "keyword_search": None,
        "last_indexed": {"timestamp": "2026-10-02T10:00:00+00:00", "indexed": 0, "error": "the platform refused every fetch (HTTP 401)",
                         "refused_repos": ["bad"]},
        "repositories": [_refused_report("bad")]})

    check = doctor.check_index(common)

    assert check["status"] == "ok" and "12 points" in check["detail"]
    assert "last indexing attempt 2026-10-02" in check["detail"] and "refused" not in check["detail"]


@pytest.mark.anyio
async def test_the_status_tool_names_the_repositories_a_run_refused(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=["bad"]))

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content

    assert status["last_indexed"]["refused_repos"] == ["bad"]


@pytest.mark.anyio
@pytest.mark.parametrize("edited", ["bad", [1], {"bad": True}])
async def test_a_refused_list_edited_into_something_else_does_not_break_the_tools(tmp_path, monkeypatch, edited):
    """griot_index_status declares a list of names: any other shape is
    read as no names, and the generic line says the run failed."""
    from mcp.client.client import Client

    from griot import mcp_server

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    _write_runs(_all_refused_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=edited))

    async with Client(mcp_server.mcp) as client:
        status = await client.call_tool("griot_index_status", {})
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert not status.is_error and status.structured_content["last_indexed"]["refused_repos"] is None
    assert any("last indexing run failed" in line for line in out["attention"])


# --- a repository the platform does not find -------------------------------------------------------
#
# [debt 67] A 404 to every fetch under a token means the project in the
# remote does not exist or the token cannot see it: index_platform.py
# records those repositories under `not_found_repos` too, and `griot stats`,
# `griot doctor` and griot_index_status point at the project, not at the
# token, while that refusal is the newest platform run's word on them.


def _not_found_run(heads, refused, not_found, at="2026-10-02T10:00:00+00:00"):
    record = _platform_run(heads, refused=refused, at=at)
    record["not_found_repos"] = not_found
    return record


def test_a_repository_the_platform_did_not_find_is_said_to_be(tmp_path):
    good, bad, gone = _repo(tmp_path, "good"), _repo(tmp_path, "bad"), _repo(tmp_path, "gone")
    runs = [_not_found_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["bad", "gone"], not_found=["gone"])]

    good_report, bad_report, gone_report = freshness.assess(runs, _now(good, bad, gone))

    assert gone_report["platform_refused"] is True and gone_report["platform_not_found"] is True
    assert bad_report["platform_refused"] is True and bad_report["platform_not_found"] is False
    assert good_report["platform_not_found"] is False


def test_a_newer_refusal_for_the_token_replaces_a_not_found(tmp_path):
    bad = _repo(tmp_path, "bad")
    runs = [_platform_run({}, refused=["bad"], at="2026-10-03T10:00:00+00:00"),
            _not_found_run({}, refused=["bad"], not_found=["bad"])]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refused"] is True and report["platform_not_found"] is False


def test_a_later_platform_run_that_indexed_it_clears_a_not_found(tmp_path):
    bad = _repo(tmp_path, "bad")
    runs = [_platform_run({"bad": _git(bad, "rev-parse", "HEAD")}, at="2026-10-03T10:00:00+00:00"),
            _not_found_run({}, refused=["bad"], not_found=["bad"])]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_not_found"] is False


@pytest.mark.parametrize("edited", ["bad", None, {"bad": True}])
def test_a_not_found_list_edited_into_something_else_is_ignored(tmp_path, edited):
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_not_found_run({}, refused=["bad"], not_found=edited)], _now(bad))

    assert report["platform_refused"] is True and report["platform_not_found"] is False


def test_a_not_found_name_the_run_did_not_refuse_is_not_believed(tmp_path):
    """not_found_repos is a subset of refused_repos as recorded; a record
    edited to name a repository only there says nothing."""
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_not_found_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=[],
                                                not_found=["bad"])], _now(bad))

    assert report["platform_refused"] is False and report["platform_not_found"] is False


def _reported(name, refused, not_found):
    return {"repo": name, "path": f"/x/{name}", "head": "a" * 40, "behind": False, "commits_behind": 0,
            "behind_sources": [], "missing_sources": [], "platform_refused": refused,
            "platform_not_found": not_found, "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}}


def test_stats_points_a_repository_not_found_at_its_project_not_at_the_token():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported("gone\x1b[2J", True, True), _reported("bad", True, False),
                               _reported("good", False, False)]}

    s = stats.compute_stats([], [], status)

    [not_found] = [line for line in s["attention"] if "not visible to the token" in line]
    [token] = [line for line in s["attention"] if "check the platform's token" in line]
    assert "gone?[2J" in not_found and "bad" not in not_found and "remote" in not_found
    assert "bad" in token and "gone" not in token and "good" not in token + not_found


def test_a_not_found_without_a_refusal_is_not_named():
    """platform_not_found qualifies a refusal; a report that has one without
    the other (built by hand) names nothing."""
    assert stats.platform_not_found_names([_reported("gone", False, True)]) == []
    assert stats.platform_not_found_names([_reported("gone", True, True)]) == ["gone"]


def test_stats_says_nothing_about_the_token_when_every_refusal_is_a_not_found():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported("gone", True, True)]}

    s = stats.compute_stats([], [], status)

    assert not any("check the platform's token" in line for line in s["attention"])
    assert any("gone" in line and "not visible to the token" in line for line in s["attention"])


def test_doctor_points_a_repository_not_found_at_its_project(tmp_path, monkeypatch):
    from griot import doctor

    gone = _repo(tmp_path, "gone")
    monkeypatch.setattr(common, "load_repos", lambda: [str(gone)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [_reported("gone", True, True)])

    check = doctor.check_repositories(common)

    assert check["status"] == "warn" and "gone" in check["detail"] and "not visible to the token" in check["detail"]
    assert "check the platform's token" not in check["fix"] and "remote" in check["fix"]


def test_doctor_with_both_kinds_says_both(tmp_path, monkeypatch):
    from griot import doctor

    gone, bad = _repo(tmp_path, "gone"), _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(gone), str(bad)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [
        _reported("gone", True, True), _reported("bad", True, False)])

    check = doctor.check_repositories(common)

    assert "not visible to the token" in check["detail"] and "refused every fetch for bad" in check["detail"]
    assert "check the platform's token" in check["fix"] and "remote" in check["fix"]


def test_a_run_not_found_entirely_is_said_once_by_stats(tmp_path, monkeypatch):
    """The generic "last indexing run failed" line stays out when the
    not-found line names every repository the run refused."""
    gone = _repo(tmp_path, "gone")
    monkeypatch.setattr(common, "load_repos", lambda: [str(gone)])
    record = _all_refused_run({"gone": _git(gone, "rev-parse", "HEAD")}, refused=["gone"])
    record.update(not_found_repos=["gone"], error="the platform refused every fetch (HTTP 404: not found, or not visible to the token)")
    _write_runs(record)

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    said = [line for line in s["attention"] if "404" in line or "refused" in line or "failed" in line]
    [line] = said
    assert "gone" in line and "not visible to the token" in line


@pytest.mark.anyio
async def test_the_status_and_stats_tools_say_which_repository_was_not_found(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    good, gone = _repo(tmp_path, "good"), _repo(tmp_path, "gone")
    monkeypatch.setattr(common, "load_repos", lambda: [str(good), str(gone)])
    _write_runs(_not_found_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["gone"], not_found=["gone"]))

    async with Client(mcp_server.mcp) as client:
        status = await client.call_tool("griot_index_status", {})
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert not status.is_error
    assert {r["repo"]: (r["platform_refused"], r["platform_not_found"])
            for r in status.structured_content["repositories"]} == {"good": (False, False), "gone": (True, True)}
    assert any("gone" in line and "not visible to the token" in line for line in out["attention"])
    assert not any("check the platform's token" in line for line in out["attention"])


# --- a repository refused for more than one reason ---------------------------------------------------
#
# [debt 70] A repository whose fetches got some 404s and some 401/403 was
# left out of not_found_repos and so got the token advice alone. The run
# records the causes per refused repository (`refusal_causes`), the report
# carries them as `platform_refusal_causes`, and `griot stats`, `griot
# doctor` and griot_index_status name such a repository as refused for
# mixed reasons, saying which fetch got which. A record from before the
# field reads exactly as before.

_MIXED = {"not_found": ["merge/pull requests", "releases"], "token": ["issues"]}


def _causes_run(heads, refused, causes, not_found=None, at="2026-10-02T10:00:00+00:00"):
    record = _platform_run(heads, refused=refused, at=at)
    record["refusal_causes"] = causes
    if not_found is not None:
        record["not_found_repos"] = not_found
    return record


def test_the_report_carries_the_causes_of_the_newest_refusal(tmp_path):
    bad, gone = _repo(tmp_path, "bad"), _repo(tmp_path, "gone")
    runs = [_causes_run({}, refused=["bad", "gone"], not_found=["gone"],
                        causes={"bad": _MIXED, "gone": {"not_found": ["issues"]}})]

    bad_report, gone_report = freshness.assess(runs, _now(bad, gone))

    assert bad_report["platform_refusal_causes"] == _MIXED and bad_report["platform_not_found"] is False
    assert gone_report["platform_refusal_causes"] == {"not_found": ["issues"]}


def test_a_record_without_causes_reports_none(tmp_path):
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_platform_run({}, refused=["bad"])], _now(bad))

    assert report["platform_refused"] is True and report["platform_refusal_causes"] is None


def test_a_repository_not_refused_has_no_causes_even_if_the_record_says_some(tmp_path):
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_causes_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=[],
                                             causes={"bad": _MIXED})], _now(bad))

    assert report["platform_refused"] is False and report["platform_refusal_causes"] is None


def test_the_causes_of_an_older_refusal_do_not_outlive_a_newer_one(tmp_path):
    bad = _repo(tmp_path, "bad")
    runs = [_platform_run({}, refused=["bad"], at="2026-10-03T10:00:00+00:00"),
            _causes_run({}, refused=["bad"], causes={"bad": _MIXED})]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_refusal_causes"] is None


@pytest.mark.parametrize("edited", [
    "bad", None, ["not_found"], {"bad": "token"}, {"bad": {"token": "issues"}}, {"bad": {"bogus": ["issues"]}},
    {"bad": {"token": [1]}}, {"bad": {}}, {"bad": {"token": []}}])
def test_causes_edited_into_something_else_are_ignored(tmp_path, edited):
    bad = _repo(tmp_path, "bad")

    [report] = freshness.assess([_causes_run({}, refused=["bad"], causes=edited)], _now(bad))

    assert report["platform_refused"] is True and report["platform_refusal_causes"] is None


def _reported_with(name, causes, not_found=False):
    return {**_reported(name, True, not_found), "platform_refusal_causes": causes}


def test_stats_names_a_mixed_refusal_and_says_which_fetch_got_which():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_with("both", _MIXED), _reported("bad", True, False),
                               _reported("gone", True, True)]}

    s = stats.compute_stats([], [], status)

    [both] = [line for line in s["attention"] if "mixed reasons" in line]
    assert "for mixed reasons" in both
    assert "merge/pull requests, releases: not found, or not visible to the token" in both
    assert "issues: refused for the token" in both
    assert "check the platform's token" in both and "project path" in both
    [token] = [line for line in s["attention"] if line.startswith("the platform refused every fetch for bad")]
    assert "both" not in token
    [gone] = [line for line in s["attention"] if "404 to every fetch" in line]
    assert "both" not in gone


def test_stats_gives_no_token_advice_to_a_mix_without_a_token_refusal():
    causes = {"not_found": ["issues"], "other": ["releases", "merge/pull requests"]}
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_with("both", causes)]}

    s = stats.compute_stats([], [], status)

    [line] = [line for line in s["attention"] if "mixed reasons" in line]
    assert "releases, merge/pull requests: the platform did not answer or failed" in line
    assert "token (`griot auth list`)" not in line and "check the platform's token" not in line
    assert "project path" in line
    # [debt 72] The fetches that failed get their own advice: not the token.
    assert "try again later" in line


def test_a_mix_of_the_token_and_no_answer_gets_no_project_advice():
    causes = {"token": ["issues"], "other": ["releases"]}
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_with("both", causes)]}

    [line] = [line for line in stats.compute_stats([], [], status)["attention"] if "mixed reasons" in line]

    assert "check the platform's token" in line and "project path" not in line


def test_a_refusal_with_a_single_recorded_cause_reads_as_before():
    """Causes that name one kind are what the old fields already say."""
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_with("bad", {"token": ["issues"]}),
                               _reported_with("gone", {"not_found": ["issues"]}, not_found=True)]}

    s = stats.compute_stats([], [], status)

    assert not any("mixed reasons" in line for line in s["attention"])
    assert any(line.startswith("the platform refused every fetch for bad") for line in s["attention"])
    assert any("gone" in line and "404 to every fetch" in line for line in s["attention"])


def test_a_mix_recorded_with_not_found_set_is_still_the_not_found():
    """platform_not_found says every fetch was a 404; when a hand-edited
    record says both, the older field wins, as it did before the causes."""
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_with("gone", _MIXED, not_found=True)]}

    s = stats.compute_stats([], [], status)

    assert not any("mixed reasons" in line for line in s["attention"])
    assert stats.platform_mixed_refusals([_reported_with("gone", _MIXED, not_found=True)]) == []


def test_a_mix_without_a_refusal_is_not_named():
    assert stats.platform_mixed_refusals([{**_reported("x", False, False), "platform_refusal_causes": _MIXED}]) == []
    assert stats.platform_mixed_refusals([_reported_with("x\x1b[2J", _MIXED)]) == [("x?[2J", _MIXED)]


def test_doctor_names_a_mixed_refusal_with_both_fixes(tmp_path, monkeypatch):
    from griot import doctor

    both = _repo(tmp_path, "both")
    monkeypatch.setattr(common, "load_repos", lambda: [str(both)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [_reported_with("both", _MIXED)])

    check = doctor.check_repositories(common)

    assert check["status"] == "warn" and "both" in check["detail"] and "for mixed reasons" in check["detail"]
    assert "issues: refused for the token" in check["detail"]
    assert "the platform's token" in check["fix"] and "project path" in check["fix"]


def test_doctor_gives_no_token_fix_to_a_mix_without_a_token_refusal(tmp_path, monkeypatch):
    from griot import doctor

    both = _repo(tmp_path, "both")
    monkeypatch.setattr(common, "load_repos", lambda: [str(both)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [
        _reported_with("both", {"not_found": ["issues"], "other": ["releases"]})])

    check = doctor.check_repositories(common)

    assert "the platform's token" not in check["fix"] and "project path" in check["fix"]


def test_doctor_reads_a_record_without_causes_as_before(tmp_path, monkeypatch):
    from griot import doctor

    bad = _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(bad)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [_reported("bad", True, False)])

    check = doctor.check_repositories(common)

    assert "refused every fetch for bad" in check["detail"] and "mixed reasons" not in check["detail"]
    assert "the platform's token" in check["fix"] and "project path" not in check["fix"]


def test_a_mixed_run_is_said_once_by_stats(tmp_path, monkeypatch):
    both = _repo(tmp_path, "both")
    monkeypatch.setattr(common, "load_repos", lambda: [str(both)])
    record = _all_refused_run({"both": _git(both, "rev-parse", "HEAD")}, refused=["both"])
    record["refusal_causes"] = {"both": _MIXED}
    _write_runs(record)

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    said = [line for line in s["attention"] if "refused" in line or "failed" in line]
    [line] = said
    assert "mixed reasons" in line and "both" in line and "issues: refused for the token" in line


@pytest.mark.anyio
async def test_the_status_and_stats_tools_say_which_fetch_of_a_mixed_refusal_got_which(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    good, both, old = _repo(tmp_path, "good"), _repo(tmp_path, "both"), _repo(tmp_path, "old")
    monkeypatch.setattr(common, "load_repos", lambda: [str(good), str(both), str(old)])
    _write_runs(_causes_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["both", "old"],
                            causes={"both": _MIXED}))

    async with Client(mcp_server.mcp) as client:
        status = await client.call_tool("griot_index_status", {})
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert not status.is_error
    by_repo = {r["repo"]: r for r in status.structured_content["repositories"]}
    assert by_repo["both"]["platform_refusal_causes"] == _MIXED
    assert by_repo["both"]["platform_not_found"] is False
    # A refusal recorded without causes (as before the field) reads as before.
    assert by_repo["old"]["platform_refused"] is True and by_repo["old"]["platform_refusal_causes"] is None
    assert by_repo["good"]["platform_refusal_causes"] is None
    assert any("mixed reasons" in line and "issues: refused for the token" in line for line in out["attention"])
    [token] = [line for line in out["attention"] if line.startswith("the platform refused every fetch for old")]
    assert "both" not in token


# --- a platform that did not answer, or failed, under a token -----------------------
#
# [debt 72] A repository whose every fetch failed for another cause (no
# answer, a timeout, a 5xx) has the single cause "other", so it was neither
# not found nor mixed and got the token advice. The readers now say the
# platform did not answer or failed, name the status or kind of error the
# run recorded (`other_reasons`, carried as `platform_other_reasons`), and
# leave the token out of it. A record without causes still reads as before.

_FAILED = {"other": ["merge/pull requests", "releases", "issues"]}


def _reported_failed(name, reasons=None, causes=_FAILED):
    return {**_reported_with(name, causes), "platform_other_reasons": reasons}


def test_the_report_carries_the_other_reasons_of_the_newest_refusal(tmp_path):
    bad, gone = _repo(tmp_path, "bad"), _repo(tmp_path, "gone")
    record = _causes_run({}, refused=["bad", "gone"], causes={"bad": _FAILED, "gone": {"token": ["issues"]}})
    record["other_reasons"] = {"bad": ["HTTP 502", "ReadTimeout"]}

    bad_report, gone_report = freshness.assess([record], _now(bad, gone))

    assert bad_report["platform_other_reasons"] == ["HTTP 502", "ReadTimeout"]
    assert gone_report["platform_other_reasons"] is None


def test_other_reasons_of_a_repository_not_refused_are_not_reported(tmp_path):
    bad = _repo(tmp_path, "bad")
    record = _causes_run({"bad": _git(bad, "rev-parse", "HEAD")}, refused=[], causes={"bad": _FAILED})
    record["other_reasons"] = {"bad": ["HTTP 500"]}

    [report] = freshness.assess([record], _now(bad))

    assert report["platform_other_reasons"] is None


def test_other_reasons_of_an_older_refusal_do_not_outlive_a_newer_one(tmp_path):
    bad = _repo(tmp_path, "bad")
    older = _causes_run({}, refused=["bad"], causes={"bad": _FAILED})
    older["other_reasons"] = {"bad": ["HTTP 500"]}
    runs = [_platform_run({}, refused=["bad"], at="2026-10-03T10:00:00+00:00"), older]

    [report] = freshness.assess(runs, _now(bad))

    assert report["platform_other_reasons"] is None


@pytest.mark.parametrize("edited", ["bad", None, ["HTTP 500"], {"bad": "HTTP 500"}, {"bad": [500]}, {"bad": []}])
def test_other_reasons_edited_into_something_else_are_ignored(tmp_path, edited):
    bad = _repo(tmp_path, "bad")
    record = _causes_run({}, refused=["bad"], causes={"bad": _FAILED})
    record["other_reasons"] = edited

    [report] = freshness.assess([record], _now(bad))

    assert report["platform_refused"] is True and report["platform_other_reasons"] is None


def test_stats_says_the_platform_failed_and_does_not_blame_the_token():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_failed("down", ["HTTP 502", "ReadTimeout"]), _reported("bad", True, False)]}

    s = stats.compute_stats([], [], status)

    [line] = [line for line in s["attention"] if "down" in line]
    assert "did not answer or failed every fetch for down (HTTP 502, ReadTimeout)" in line
    assert "not the token" in line and "try again later" in line and "network" in line
    assert "check the platform's token" not in line and "project path" not in line
    [token] = [line for line in s["attention"] if line.startswith("the platform refused every fetch for bad")]
    assert "down" not in token and "check the platform's token" in token


def test_stats_names_a_failure_without_recorded_reasons_all_the_same():
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_failed("down\x1b[2J")]}

    [line] = [line for line in stats.compute_stats([], [], status)["attention"] if "down" in line]

    assert "did not answer or failed every fetch for down?[2J in the last platform run" in line
    assert "check the platform's token" not in line


def test_a_mix_names_the_reasons_of_its_failed_fetches():
    causes = {"token": ["issues"], "other": ["releases"]}
    status = {"points_count": 10, "points_error": None, "last_indexed": None, "spend_ceiling_exceeded": False,
              "repositories": [_reported_failed("both", ["HTTP 500"], causes=causes)]}

    [line] = [line for line in stats.compute_stats([], [], status)["attention"] if "mixed reasons" in line]

    assert "releases: the platform did not answer or failed (HTTP 500)" in line
    assert "check the platform's token (`griot auth list`) for the fetches it refused" in line
    assert "for the fetches that failed, not the token" in line and "try again later" in line


def test_platform_failures_are_only_refusals_with_the_other_cause_alone():
    assert stats.platform_failures([_reported_failed("down", ["HTTP 500"])]) == [("down", ["HTTP 500"])]
    assert stats.platform_failures([_reported_failed("down", None)]) == [("down", None)]
    assert stats.platform_failures([{**_reported_failed("down"), "platform_refused": False}]) == []
    assert stats.platform_failures([_reported_with("bad", {"token": ["issues"]})]) == []
    assert stats.platform_failures([_reported_with("both", {"other": ["issues"], "token": ["releases"]})]) == []
    # No causes recorded: a refusal from before them, which reads as before.
    assert stats.platform_failures([_reported("bad", True, False)]) == []
    # platform_not_found keeps its word over a record edited by hand.
    assert stats.platform_failures([_reported_with("gone", _FAILED, not_found=True)]) == []


def test_doctor_says_the_platform_failed_without_the_token_fix(tmp_path, monkeypatch):
    from griot import doctor

    down = _repo(tmp_path, "down")
    monkeypatch.setattr(common, "load_repos", lambda: [str(down)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [_reported_failed("down", ["HTTP 503"])])

    check = doctor.check_repositories(common)

    assert check["status"] == "warn" and "did not answer or failed every fetch for down (HTTP 503)" in check["detail"]
    assert "griot auth list" not in check["fix"] and "the platform's token" not in check["fix"]
    assert "not the token" in check["fix"] and "network" in check["fix"] and "later" in check["fix"]


def test_doctor_with_a_failure_and_a_token_refusal_gives_both(tmp_path, monkeypatch):
    from griot import doctor

    down, bad = _repo(tmp_path, "down"), _repo(tmp_path, "bad")
    monkeypatch.setattr(common, "load_repos", lambda: [str(down), str(bad)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [
        _reported_failed("down", ["HTTP 503"]), _reported("bad", True, False)])

    check = doctor.check_repositories(common)

    assert "refused every fetch for bad" in check["detail"] and "failed every fetch for down" in check["detail"]
    assert "the platform's token" in check["fix"] and "network" in check["fix"]


def test_a_failed_run_is_said_once_by_stats(tmp_path, monkeypatch):
    down = _repo(tmp_path, "down")
    monkeypatch.setattr(common, "load_repos", lambda: [str(down)])
    record = _all_refused_run({"down": _git(down, "rev-parse", "HEAD")}, refused=["down"])
    record.update(refusal_causes={"down": _FAILED}, other_reasons={"down": ["HTTP 500"]},
                  error="every fetch to the platform failed (HTTP 500)")
    _write_runs(record)

    s = stats.compute_stats([], [], common.get_index_status(reuse_active_handle=False))

    said = [line for line in s["attention"] if "refused" in line or "failed" in line]
    [line] = said
    assert "down (HTTP 500)" in line and "check the platform's token" not in line


@pytest.mark.anyio
async def test_the_status_and_stats_tools_say_the_platform_failed(tmp_path, monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    good, down, old = _repo(tmp_path, "good"), _repo(tmp_path, "down"), _repo(tmp_path, "old")
    monkeypatch.setattr(common, "load_repos", lambda: [str(good), str(down), str(old)])
    record = _causes_run({"good": _git(good, "rev-parse", "HEAD")}, refused=["down", "old"],
                         causes={"down": _FAILED})
    record["other_reasons"] = {"down": ["ReadTimeout"]}
    _write_runs(record)

    async with Client(mcp_server.mcp) as client:
        status = await client.call_tool("griot_index_status", {})
        out = (await client.call_tool("griot_stats", {})).structured_content

    assert not status.is_error
    by_repo = {r["repo"]: r for r in status.structured_content["repositories"]}
    assert by_repo["down"]["platform_other_reasons"] == ["ReadTimeout"]
    assert by_repo["down"]["platform_refusal_causes"] == _FAILED
    assert by_repo["old"]["platform_other_reasons"] is None and by_repo["good"]["platform_other_reasons"] is None
    [line] = [line for line in out["attention"] if "did not answer" in line]
    assert "did not answer or failed every fetch for down (ReadTimeout)" in line and "not the token" in line
    # A refusal recorded without causes still gets the token advice, alone.
    [token] = [line for line in out["attention"] if line.startswith("the platform refused every fetch for old")]
    assert "down" not in token and "check the platform's token" in token
