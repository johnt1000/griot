"""scripts/measure-search-modes.py: the measurement that made hybrid the
default search mode (MRR@10 and recall@10 per mode and per kind of query),
committed so that the gate can be run again when the default profile or the
fusion changes. These tests cover what decides its numbers (the metrics, what
counts as one document, which queries it builds) and what keeps it from
touching the person's real index (it refuses to run on anything but
throwaway directories). The measurement itself indexes real repositories
with a real model and is run by hand."""

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "measure-search-modes.py"
SHIPPED_QUERIES = ROOT / "scripts" / "search-mode-queries" / "griot.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("measure_search_modes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def m():
    return _load_script()


def _code(repo, path):
    return {"source_type": "code", "repo": repo, "file_path": path}


# --- the metrics -------------------------------------------------------------

def test_reciprocal_rank_is_one_over_the_rank_of_the_first_relevant_document(m):
    docs = [("code", "r", "a.py"), ("code", "r", "b.py"), ("code", "r", "c.py")]
    assert m.reciprocal_rank(docs, {("code", "r", "c.py"), ("code", "r", "z.py")}) == pytest.approx(1 / 3)
    assert m.reciprocal_rank(docs, {("code", "r", "a.py")}) == 1.0


def test_reciprocal_rank_is_zero_when_nothing_relevant_was_found(m):
    assert m.reciprocal_rank([("code", "r", "a.py")], {("code", "r", "b.py")}) == 0.0
    assert m.reciprocal_rank([], {("code", "r", "b.py")}) == 0.0


def test_recall_is_the_share_of_relevant_documents_found(m):
    docs = [("code", "r", "a.py"), ("code", "r", "b.py")]
    relevant = {("code", "r", "a.py"), ("code", "r", "x.py"), ("code", "r", "y.py"), ("code", "r", "z.py")}
    assert m.recall(docs, relevant) == pytest.approx(0.25)
    assert m.recall(docs, {("code", "r", "a.py"), ("code", "r", "b.py")}) == 1.0
    assert m.recall([], {("code", "r", "a.py")}) == 0.0


def test_the_rank_counts_documents_not_chunks(m):
    """Two chunks of one file are one document: the second chunk of a.py
    must not push b.py down to rank 3."""
    payloads = [_code("r", "a.py"), _code("r", "a.py"), _code("r", "b.py")]
    docs = m.ranked_documents(payloads)
    assert docs == [("code", "r", "a.py"), ("code", "r", "b.py")]
    assert m.reciprocal_rank(docs, {("code", "r", "b.py")}) == 0.5


def test_a_commit_is_identified_by_its_hash_and_a_file_by_its_repository_and_path(m):
    commit = {"source_type": "commit", "repo": "r", "commit_hash": "abc123", "file_path": "ignored"}
    assert m.doc_of(commit) == ("commit", "r", "abc123")
    assert m.doc_of(_code("r", "a.py")) != m.doc_of(_code("other", "a.py"))
    # A point written before source_type existed is code.
    assert m.doc_of({"repo": "r", "file_path": "a.py"}) == ("code", "r", "a.py")


def test_the_table_averages_each_kind_and_mode(m):
    totals = {
        ("descriptive", "vector"): [1.5, 1.0, 2], ("descriptive", "keyword"): [0.5, 0.5, 2],
        ("descriptive", "hybrid"): [2.0, 2.0, 2],
    }
    table = m.format_table(totals, ("vector", "keyword", "hybrid"))
    lines = table.splitlines()
    assert "vector MRR" in lines[0] and "hybrid R@10" in lines[0]
    # Every column heading stands apart from the next, the longest included.
    assert " keyword R@10 " in lines[0]
    row = next(line for line in lines if line.startswith("descriptive"))
    assert row.split()[1:] == ["2", "0.750", "0.500", "0.250", "0.250", "1.000", "1.000"]


# --- the arguments -----------------------------------------------------------

def test_arguments_take_several_repositories_and_query_files(m, tmp_path):
    args = m.parse_args(["--repo", str(tmp_path / "a"), "--repo", str(tmp_path / "b"),
                         "--queries", str(tmp_path / "q.json")])
    assert args.repos == [tmp_path / "a", tmp_path / "b"]
    assert args.queries == [tmp_path / "q.json"]
    assert args.limit == 10
    assert args.profile is None
    assert args.per_query is None


@pytest.mark.parametrize("argv", [
    [],
    ["--repo", "/Users/you/code/a"],
    ["--queries", "/Users/you/q.json"],
    ["--repo", "/Users/you/code/a", "--queries", "/Users/you/q.json", "--limit", "0"],
])
def test_arguments_without_a_repository_or_queries_are_refused(m, argv, capsys):
    with pytest.raises(SystemExit) as e:
        m.parse_args(argv)
    assert e.value.code == 2


def test_two_repositories_with_the_same_directory_name_are_refused(m, tmp_path):
    """Points name their repository by its directory name: two roots with
    the same name would be one repository in the results."""
    first, second = _repo(tmp_path / "x"), _repo(tmp_path / "y")
    assert m.repository_roots([first]) == {"app": first.resolve()}
    with pytest.raises(SystemExit, match="Two repositories are named 'app'"):
        m.repository_roots([first, second])


def test_a_directory_that_is_not_a_repository_is_refused(m, tmp_path):
    with pytest.raises(SystemExit, match="not the root of a git repository"):
        m.repository_roots([tmp_path])


# --- never the real directories ----------------------------------------------

def test_it_refuses_to_run_without_griot_directories_set(m, monkeypatch):
    environ = {}
    with pytest.raises(SystemExit, match="GRIOT_CONFIG_DIR"):
        m.require_throwaway_dirs(environ)
    environ = {"GRIOT_CONFIG_DIR": tempfile.mkdtemp()}
    with pytest.raises(SystemExit, match="GRIOT_DATA_DIR"):
        m.require_throwaway_dirs(environ)


def test_it_refuses_griot_directories_outside_the_temporary_directory(m, monkeypatch):
    monkeypatch.delitem(sys.modules, "griot.common", raising=False)
    temp = tempfile.mkdtemp()
    for environ in ({"GRIOT_CONFIG_DIR": temp, "GRIOT_DATA_DIR": str(Path.home())},
                    {"GRIOT_CONFIG_DIR": str(Path.home() / ".config"), "GRIOT_DATA_DIR": temp}):
        with pytest.raises(SystemExit, match="temporary"):
            m.require_throwaway_dirs(environ)


def test_it_refuses_when_griot_was_already_loaded_with_other_directories(m, monkeypatch):
    """griot.common reads its directories once, when it is imported: setting
    the variables afterwards changes nothing, so the index would be built
    wherever the earlier import pointed."""
    from griot import common
    monkeypatch.setitem(sys.modules, "griot.common", common)
    config, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    with pytest.raises(SystemExit, match="already"):
        m.require_throwaway_dirs({"GRIOT_CONFIG_DIR": config, "GRIOT_DATA_DIR": data})


def test_throwaway_directories_are_accepted(m, monkeypatch):
    monkeypatch.delitem(sys.modules, "griot.common", raising=False)
    config, data = tempfile.mkdtemp(), tempfile.mkdtemp()
    assert m.require_throwaway_dirs({"GRIOT_CONFIG_DIR": config, "GRIOT_DATA_DIR": data}) == (
        Path(config).resolve(), Path(data).resolve())


def test_the_throwaway_directories_are_set_while_it_runs_and_deleted_after(m, monkeypatch):
    monkeypatch.setenv("GRIOT_CONFIG_DIR", "/Users/you/before-config")
    monkeypatch.setenv("GRIOT_DATA_DIR", "/Users/you/before-data")
    with m.throwaway_dirs() as base:
        config, data = Path(m.os.environ["GRIOT_CONFIG_DIR"]), Path(m.os.environ["GRIOT_DATA_DIR"])
        assert config.is_relative_to(base) and data.is_relative_to(base)
        (data / "griot").mkdir(parents=True)
        (data / "griot" / "index").write_text("x")
    assert not base.exists()
    assert m.os.environ["GRIOT_CONFIG_DIR"] == "/Users/you/before-config"
    assert m.os.environ["GRIOT_DATA_DIR"] == "/Users/you/before-data"


def test_the_throwaway_directories_are_deleted_when_the_run_fails(m):
    with pytest.raises(RuntimeError):
        with m.throwaway_dirs() as base:
            raise RuntimeError("index failed")
    assert not base.exists()


# --- the queries -------------------------------------------------------------

def _repo(tmp_path, name="app"):
    root = tmp_path / name
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text("def parse_thing(x):\n    return x\n\nLIMIT = 3\n\n\nclass Reader:\n    pass\n")
    (root / "pkg" / "other.py").write_text("from pkg.core import parse_thing\nparse_thing(1)\n")
    (root / "README.md").write_text("an app\n")
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(command, cwd=root, check=True)
    for i in range(5):
        (root / "n.txt").write_text(str(i))
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                        "-m", f"change {i}"], cwd=root, check=True)
    return root


def test_descriptive_queries_take_their_relevant_files_from_the_query_file(m, tmp_path):
    root = _repo(tmp_path)
    spec = {"repo": "app", "descriptive": [{"query": "how a thing is parsed", "relevant": ["pkg/core.py"]}]}
    cases = m.query_cases(spec, {"app": root}, commit_hashes=False)
    assert cases == [("descriptive", "how a thing is parsed", {("code", "app", "pkg/core.py")})]


def test_an_identifier_is_relevant_where_it_is_defined_not_where_it_is_used(m, tmp_path):
    root = _repo(tmp_path)
    spec = {"repo": "app", "identifier": ["parse_thing", "LIMIT", "Reader"]}
    cases = m.query_cases(spec, {"app": root}, commit_hashes=False)
    assert cases == [("identifier", "parse_thing", {("code", "app", "pkg/core.py")}),
                     ("identifier", "LIMIT", {("code", "app", "pkg/core.py")}),
                     ("identifier", "Reader", {("code", "app", "pkg/core.py")})]


@pytest.mark.parametrize("spec, complaint", [
    ({"repo": "nope", "descriptive": []}, "nope"),
    ({"repo": "app", "descriptive": [{"query": "q", "relevant": ["missing.py"]}]}, "missing.py"),
    ({"repo": "app", "descriptive": [{"query": "q", "relevant": []}]}, "q"),
    ({"repo": "app", "identifier": ["not_defined_anywhere"]}, "not_defined_anywhere"),
    # A misspelt kind would drop its queries without a word.
    ({"repo": "app", "identifiers": ["parse_thing"]}, "identifiers"),
])
def test_a_query_that_cannot_be_judged_is_refused(m, tmp_path, spec, complaint):
    """A query with no relevant document scores 0 in every mode: it would
    lower all three averages and hide nothing but a typo."""
    root = _repo(tmp_path)
    with pytest.raises(SystemExit, match=complaint):
        m.query_cases(spec, {"app": root}, commit_hashes=False)


def test_commit_hash_queries_are_generated_from_the_repository_history(m, tmp_path):
    root = _repo(tmp_path)
    log = subprocess.run(["git", "log", "--format=%H"], cwd=root, capture_output=True, text=True,
                         check=True).stdout.split()
    spec = {"repo": "app", "commit_hash": {"count": 3, "prefix_lengths": [7, 40]}}
    cases = m.query_cases(spec, {"app": root})
    assert len(cases) == 3
    for kind, query, relevant in cases:
        assert kind == "commit hash"
        (full,) = [h for h in log if h.startswith(query)]
        assert relevant == {("commit", "app", full)}
    assert [len(q) for _, q, _ in cases] == [7, 40, 7]
    assert len({q for _, q, _ in cases}) == 3
    # Spread over the history (5 commits, newest first), not its newest three.
    assert [next(iter(r))[2] for _, _, r in cases] == [log[0], log[1], log[3]]
    # The same history gives the same queries: a re-run measures the same thing.
    assert m.query_cases(spec, {"app": root}) == cases


def test_more_commit_hashes_than_the_history_holds_is_refused(m, tmp_path):
    root = _repo(tmp_path)
    spec = {"repo": "app", "commit_hash": {"count": 50, "prefix_lengths": [7]}}
    with pytest.raises(SystemExit, match="commit"):
        m.query_cases(spec, {"app": root})
    # Left out on request, the history is not read at all (a shallow clone).
    assert m.query_cases(spec, {"app": root}, commit_hashes=False) == []


def test_the_shipped_query_set_still_matches_this_repository(m):
    """The queries committed for this repository name files that exist and
    identifiers that are still defined: a rename shows up here, not as a
    silent drop in a measurement months later. (Commit hashes are left out:
    a shallow clone has no history to draw them from.)"""
    spec = json.loads(SHIPPED_QUERIES.read_text())
    assert spec["repo"] == "griot"
    cases = m.query_cases(spec, {"griot": ROOT}, commit_hashes=False)
    kinds = [kind for kind, _, _ in cases]
    assert kinds.count("descriptive") >= 20 and kinds.count("identifier") >= 20
    assert spec["commit_hash"]["count"] >= 10
