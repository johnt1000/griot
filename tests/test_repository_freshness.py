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
    assert "not in this history" in stats._behind_phrase({"repo": "one", "commits_behind": None, "behind_sources": ["code"]})


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
