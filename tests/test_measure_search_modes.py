"""scripts/measure-search-modes.py: the measurement that made hybrid the
default search mode (MRR@10 and recall@10 per mode and per kind of query),
committed so that the gate can be run again when the default profile or the
fusion changes. These tests cover what decides its numbers (the metrics, what
counts as one document, which queries it builds) and what keeps it from
touching the person's real index (it refuses to run on anything but
throwaway directories). The loop that scores every query in every mode,
measure(), runs here on a tiny real index with a stand-in embedding whose
ranks are known in advance; the measurement proper indexes real
repositories with a real model and is run by hand."""

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
    # Several relevant documents found: the FIRST of them decides.
    assert m.reciprocal_rank(docs, {("code", "r", "b.py"), ("code", "r", "c.py")}) == 0.5


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


# --- the measurement itself, on a tiny real index ------------------------------
#
# A real Qdrant Edge index in the test's own directories, with a stand-in
# embedding whose vectors are written down below, so that every rank in every
# mode is known before the search runs: vector search ranks by the cosines of
# these vectors, keyword search by which texts hold the query's words, and
# hybrid by reciprocal rank fusion of the two (1 / (60 + rank) per ranking).
# Free and fast: nothing is downloaded and nothing is paid for.

TOY_HASH = "0c1d2e3f4a5b6c7d8e9f00112233445566778899"

# text -> {axis: weight}, for queries and documents alike. An unknown text
# raises: a test that embeds something it did not plan for says so.
_TOY_AXES = {
    # documents
    "open the configuration file from disk, then give back its values": {0: 1.0},
    "settings settings: the settings object with its defaults": {0: 1.0, 1: 1.0, 2: 0.1},
    "def zorblax_quux(x):\n    return x * 2": {2: 0.5, 3: 1.0},
    "print a greeting to the console": {2: 1.0},
    "draw a chart of monthly revenue": {5: 1.0},
    "fix: round revenue to cents": {5: 0.5, 6: 1.0},
    # queries
    "how settings are loaded": {0: 1.0},
    "zorblax_quux": {2: 1.0},
    TOY_HASH[:7]: {5: 1.0},
    "of the": {0: 1.0},
}


def _toy_doc(path, text):
    return {"id": f"toy:code:{path}:0", "content": text,
            "metadata": {"source_type": "code", "repo": "toy", "file_path": path, "chunk_index": 0}}


TOY_DOCS = [
    _toy_doc("src/a.py", "open the configuration file from disk, then give back its values"),
    _toy_doc("src/b.py", "settings settings: the settings object with its defaults"),
    _toy_doc("src/c.py", "def zorblax_quux(x):\n    return x * 2"),
    _toy_doc("src/d.py", "print a greeting to the console"),
    _toy_doc("src/e.py", "draw a chart of monthly revenue"),
    {"id": f"toy:commit:{TOY_HASH}", "content": "fix: round revenue to cents",
     "metadata": {"source_type": "commit", "repo": "toy", "commit_hash": TOY_HASH,
                  "author": "someone", "date": "2026-10-01"}},
]


def _toy_file(path):
    return ("code", "toy", path)


# (kind, query, relevant documents). The comments give the rank of the first
# relevant document in each mode, at limit 3.
TOY_CASES = [
    # Vector: a.py is the query's own direction (1), b.py next. Keyword: only
    # b.py holds a word of the query (a.py not found). Hybrid: b.py is in both
    # rankings and a.py in one, so a.py comes second.
    ("descriptive", "how settings are loaded", {_toy_file("src/a.py")}),
    # Only stopwords: keyword (and so hybrid) search cannot run it at all,
    # which measure() scores as finding nothing. One of the two relevant files
    # is not in the index, so vector search recalls half.
    ("descriptive", "of the", {_toy_file("src/a.py"), _toy_file("src/missing.py")}),
    # Vector: d.py is closer than c.py (2). Keyword: c.py alone (1). Hybrid:
    # c.py in both rankings beats d.py in one (1).
    ("identifier", "zorblax_quux", {_toy_file("src/c.py")}),
    # Vector: e.py is closer than the commit (2). Keyword: the hash is stored
    # with the commit (1). Hybrid: the commit is in both (1).
    ("commit hash", TOY_HASH[:7], {("commit", "toy", TOY_HASH)}),
]

TOY_RANKS = {  # (query, mode) -> the rank measure() reports for it
    ("how settings are loaded", "vector"): 1, ("how settings are loaded", "keyword"): None,
    ("how settings are loaded", "hybrid"): 2,
    ("of the", "vector"): 1, ("of the", "keyword"): None, ("of the", "hybrid"): None,
    ("zorblax_quux", "vector"): 2, ("zorblax_quux", "keyword"): 1, ("zorblax_quux", "hybrid"): 1,
    (TOY_HASH[:7], "vector"): 2, (TOY_HASH[:7], "keyword"): 1, (TOY_HASH[:7], "hybrid"): 1,
}


def _toy_vector(text, dim):
    vector = [0.0] * dim
    for axis, weight in _TOY_AXES[text].items():
        vector[axis] = weight
    return vector


@pytest.fixture
def toy_index(monkeypatch):
    from griot import common

    monkeypatch.setattr(common, "embed_texts",
                        lambda texts, **kw: [_toy_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(TOY_DOCS)
    common.release_lock()


def test_measure_reports_each_querys_rank_in_every_mode(m, toy_index):
    _, per_query, modes = m.measure(TOY_CASES, limit=3)
    assert tuple(modes) == ("vector", "keyword", "hybrid")
    assert {(row["query"], row["mode"]): row["rank"] for row in per_query} == TOY_RANKS
    # One row per query and mode, each carrying the kind of its query.
    assert len(per_query) == len(TOY_CASES) * 3
    kinds = {query: kind for kind, query, _ in TOY_CASES}
    assert all(row["kind"] == kinds[row["query"]] for row in per_query)


def test_measure_sums_reciprocal_ranks_and_recall_per_kind_and_mode(m, toy_index):
    totals, _, _ = m.measure(TOY_CASES, limit=3)
    expected = {  # (sum of reciprocal ranks, sum of recalls, number of queries)
        # vector 1 + 1, recall 1 + 1/2; keyword nothing; hybrid 1/2 + 0,
        # recall 1 + 0.
        ("descriptive", "vector"): (2.0, 1.5, 2),
        ("descriptive", "keyword"): (0.0, 0.0, 2),
        ("descriptive", "hybrid"): (0.5, 1.0, 2),
        ("identifier", "vector"): (0.5, 1.0, 1),
        ("identifier", "keyword"): (1.0, 1.0, 1),
        ("identifier", "hybrid"): (1.0, 1.0, 1),
        ("commit hash", "vector"): (0.5, 1.0, 1),
        ("commit hash", "keyword"): (1.0, 1.0, 1),
        ("commit hash", "hybrid"): (1.0, 1.0, 1),
    }
    assert set(totals) == set(expected)
    for key, (sum_rr, sum_recall, n) in expected.items():
        assert totals[key][0] == pytest.approx(sum_rr), key
        assert totals[key][1] == pytest.approx(sum_recall), key
        assert totals[key][2] == n, key


def test_measure_counts_only_the_top_limit(m, toy_index):
    """The @k is the limit asked: at limit 1, a relevant document second in
    a ranking is not found at all."""
    totals, per_query, _ = m.measure([TOY_CASES[2]], limit=1)
    assert {row["mode"]: row["rank"] for row in per_query} == {"vector": None, "keyword": 1, "hybrid": 1}
    assert totals[("identifier", "vector")] == [0.0, 0.0, 1]


def test_measure_refuses_to_run_outside_throwaway_directories(m, toy_index, monkeypatch):
    monkeypatch.delenv("GRIOT_DATA_DIR")
    with pytest.raises(SystemExit, match="GRIOT_DATA_DIR"):
        m.measure(TOY_CASES, limit=3)


# --- where a name is defined, in every language griot indexes ----------------

def _tracked_repo(tmp_path, files, name="poly"):
    """A git repository holding `files` (path -> text), all tracked."""
    root = tmp_path / name
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=root, check=True)
    return root


# (language, file, text that DEFINES `target`). Every text names `target`
# once, where it is declared.
DEFINITIONS = [
    ("python def", "a.py", "def target(x):\n    return x\n"),
    ("python async def", "a.py", "async def target():\n    pass\n"),
    ("python class", "a.py", "class target:\n    pass\n"),
    ("python module-level assignment", "a.py", "target = 3\n"),
    ("python annotated assignment", "a.py", "target: int = 3\n"),
    ("typescript function", "a.ts", "function target(a: number): number {\n  return a\n}\n"),
    ("typescript exported async function", "a.ts", "export async function target() {}\n"),
    ("typescript default class", "a.tsx", "export default class target extends Component {}\n"),
    ("typescript abstract class", "a.ts", "export abstract class target {}\n"),
    ("typescript const", "a.ts", "export const target = 1;\n"),
    ("typescript typed const", "a.tsx", "const target: React.FC<Props> = (props) => null;\n"),
    ("typescript let", "a.ts", "let target = 0;\n"),
    ("typescript interface", "a.ts", "export interface target {\n  id: string\n}\n"),
    ("typescript type alias", "a.ts", "export type target = { id: string };\n"),
    ("typescript enum", "a.ts", "export enum target { A, B }\n"),
    ("typescript method", "a.ts", "class A {\n  private async target(a: string): Promise<void> {\n  }\n}\n"),
    ("typescript arrow class field", "a.ts", "class A {\n  target = () => {\n  }\n}\n"),
    ("javascript var", "a.js", "var target = require('x');\n"),
    ("javascript generator", "a.mjs", "function* target() {}\n"),
    ("javascript method", "a.jsx", "class A {\n  target() {\n    return 1\n  }\n}\n"),
    ("shell function", "a.sh", "target() {\n  echo hi\n}\n"),
    ("shell function, brace on the next line", "a.sh", "target()\n{\n  echo hi\n}\n"),
    ("shell function keyword", "a.sh", "function target {\n  echo hi\n}\n"),
    ("shell variable", "a.sh", "target=/usr/local\n"),
    ("shell exported variable", "a.sh", "export target=1\n"),
    ("go function", "a.go", "func target(a int) int {\n\treturn a\n}\n"),
    ("go method", "a.go", "func (s *Server) target() error {\n\treturn nil\n}\n"),
    ("go type", "a.go", "type target struct {\n}\n"),
    ("rust function", "a.rs", "pub(crate) fn target() {}\n"),
    ("rust struct", "a.rs", "pub struct target {}\n"),
    ("ruby method", "a.rb", "def self.target\nend\n"),
    ("terraform resource", "main.tf", 'resource "aws_s3_bucket" "target" {\n}\n'),
    ("terraform variable", "variables.tf", 'variable "target" {\n  type = string\n}\n'),
    ("terraform module", "main.tf", 'module "target" {\n  source = "./m"\n}\n'),
    ("sql table", "a.sql", "CREATE TABLE IF NOT EXISTS public.target (\n  id int\n);\n"),
    ("sql function", "a.sql", "create or replace function target() returns int as $$ select 1 $$;\n"),
    ("yaml top-level key", "a.yml", "target:\n  image: x\n"),
]


@pytest.mark.parametrize("language, path, text", DEFINITIONS, ids=[d[0] for d in DEFINITIONS])
def test_a_name_is_found_where_it_is_defined_in_every_language_griot_indexes(m, tmp_path, language, path, text):
    root = _tracked_repo(tmp_path, {path: text})
    assert m.defining_files(root, "target") == {path}, language


# (language, file, text that only USES `target`, or mentions it in passing).
USES = [
    ("python call", "a.py", "from m import target\ntarget(1)\n"),
    ("python call at the top level", "a.py", "target()\n"),
    ("python comparison", "a.py", "target == 3\n"),
    ("python attribute", "a.py", "self.target = 3\n"),
    ("typescript call", "a.ts", "const x = target(1);\nawait target();\n"),
    ("typescript import", "a.ts", "import { target } from './t';\n"),
    ("typescript property", "a.ts", "obj.target = 1;\n"),
    ("javascript call statement", "a.js", "target();\n"),
    ("shell call", "a.sh", "target\ntarget \"$@\"\n"),
    ("shell call with a subshell", "a.sh", "echo $(target)\n"),
    ("terraform reference", "main.tf", 'output "x" {\n  value = aws_s3_bucket.target.arn\n}\n'),
    ("terraform resource type", "main.tf", 'resource "target" "logs" {\n}\n'),
    ("sql query", "a.sql", "select * from target;\n"),
    ("a longer name", "a.ts", "function target_two() {}\nconst targetThree = 1;\n"),
    ("a comment", "a.py", "# def target is elsewhere\n"),
]


@pytest.mark.parametrize("language, path, text", USES, ids=[u[0] for u in USES])
def test_a_name_is_not_defined_where_it_is_only_used(m, tmp_path, language, path, text):
    root = _tracked_repo(tmp_path, {path: text})
    assert m.defining_files(root, "target") == set(), language


def test_only_files_griot_would_index_can_be_relevant(m, tmp_path):
    """A definition in a file griot never indexes cannot be found by any
    mode: counting it relevant would lower every score alike. Which files
    those are is read from griot's own indexer, not repeated here."""
    root = _tracked_repo(tmp_path, {
        "src/a.ts": "export function target() {}\n",
        "src/a.kt": "fun target() {}\nclass target\n",       # an extension griot does not index
        "notes.txt": "def target():\n",                       # nor this one
        "node_modules/lib/x.js": "function target() {}\n",    # a directory the indexer skips
        "public/app.min.js": "function target(){}\n",         # a minified bundle
        "pnpm-lock.yaml": "target:\n  version: 1\n",          # a generated file the indexer skips
    })
    (root / "src" / "link.ts").symlink_to("a.ts")             # never followed by the indexer
    subprocess.run(["git", "add", "-A", "-f"], cwd=root, check=True)
    assert m.defining_files(root, "target") == {"src/a.ts"}


def test_the_indexed_file_rules_are_griots_own(m):
    from griot import index_code
    assert m.indexed_file_rules() == (index_code.SUPPORTED_EXTENSIONS, index_code.IGNORE_DIRS,
                                      index_code.IGNORE_FILES, index_code.MAX_FILE_BYTES)


def test_a_file_git_does_not_ignore_counts_even_before_it_is_added(m, tmp_path):
    """The indexer reads new files git would track as well (index_code's
    `--others --exclude-standard`), and nothing git ignores."""
    root = _tracked_repo(tmp_path, {".gitignore": "secret.ts\n"})
    (root / "new.ts").write_text("export function target() {}\n")
    (root / "secret.ts").write_text("export function target() {}\n")
    (root / "huge.ts").write_text("export function target() {}\n" + "x" * m.indexed_file_rules()[3])
    assert m.defining_files(root, "target") == {"new.ts"}


def test_an_identifier_query_on_a_typescript_repository_is_judged(m, tmp_path):
    """The case that sent this measurement back: on a repository with no
    .py file, every identifier query was refused for want of a relevant
    document."""
    root = _tracked_repo(tmp_path, {"src/api.ts": "export const fetchUser = async (id: string) => {}\n",
                                    "src/page.tsx": "import { fetchUser } from './api';\nfetchUser('1');\n",
                                    "deploy.sh": "deploy_app() {\n  echo\n}\ndeploy_app\n"}, name="web")
    cases = m.query_cases({"repo": "web", "identifier": ["fetchUser", "deploy_app"]}, {"web": root},
                          commit_hashes=False)
    assert cases == [("identifier", "fetchUser", {("code", "web", "src/api.ts")}),
                     ("identifier", "deploy_app", {("code", "web", "deploy.sh")})]


# --- an indexing child that crashes after it finished --------------------------

def _child_that(record: str, then: str) -> str:
    """Python source for a stand-in indexing child: it records a run the way
    griot's indexers do (or not), then ends as `then` says."""
    return (
        "import os, sys\n"
        "from griot import common\n"
        "source, root = sys.argv[1], sys.argv[2]\n"
        f"{record}\n"
        f"{then}\n"
    )


_COMPLETE = ("common.log_run_summary(script=f'index_{source}.py', repo=root, indexed=4, skipped=1, failed=0, "
             "duration_seconds=0.1)")
_WITH_FAILURES = ("common.log_run_summary(script=f'index_{source}.py', repo=root, indexed=4, skipped=1, "
                  "failed=2, duration_seconds=0.1)")
_DEAD = ("common.log_run_summary(script=f'index_{source}.py', repo=root, indexed=None, skipped=None, failed=None, "
         "duration_seconds=0.1, error='KeyboardInterrupt')")
_ABORT = "sys.stdout.flush(); os.abort()"


def _stand_in(monkeypatch, m, code):
    monkeypatch.setattr(m, "_index_command",
                        lambda source, root: [sys.executable, "-c", code, source, str(root)])


def test_a_child_that_aborts_after_recording_a_complete_run_does_not_end_the_measurement(
        m, tmp_path, monkeypatch, capsys):
    """libc++abi aborted an indexing child at exit, after it had printed
    that it indexed everything with 0 failed: the run was complete, and a
    45-minute measurement died with it."""
    _stand_in(monkeypatch, m, _child_that(_COMPLETE, _ABORT))
    m._index(_repo(tmp_path))
    err = capsys.readouterr().err
    for source in m.SOURCES_INDEXED:
        assert f"indexing {source} of app" in err
    assert "after recording a complete run" in err and "4 indexed, 1 unchanged, 0 failed" in err


@pytest.mark.parametrize("record, then", [
    ("pass", _ABORT),                    # died before it recorded anything
    (_WITH_FAILURES, "sys.exit(1)"),     # finished, but did not index everything
    (_DEAD, "sys.exit(1)"),              # recorded as a run that died
    ("pass", "sys.exit(3)"),             # a plain failure
    # Its last word was that it died: a complete run written earlier by the
    # same child does not outweigh it.
    (_COMPLETE + "\n" + _DEAD, "sys.exit(1)"),
])
def test_a_child_that_failed_ends_the_measurement(m, tmp_path, monkeypatch, record, then):
    _stand_in(monkeypatch, m, _child_that(record, then))
    with pytest.raises(SystemExit, match="Indexing code of app failed"):
        m._index(_repo(tmp_path))


def test_a_complete_run_recorded_before_the_child_started_does_not_vouch_for_it(m, tmp_path, monkeypatch):
    """The run log holds earlier runs too: only one the failing child wrote
    itself says that IT finished."""
    from griot import common
    root = _repo(tmp_path)
    common.log_run_summary(script="index_code.py", repo=str(root), indexed=4, skipped=1, failed=0,
                           duration_seconds=0.1)
    _stand_in(monkeypatch, m, _child_that("pass", _ABORT))
    with pytest.raises(SystemExit, match="Indexing code of app failed"):
        m._index(root)


@pytest.mark.parametrize("other", [
    "common.log_run_summary(script='index_code.py', repo=root + '-other', indexed=4, skipped=0, failed=0, "
    "duration_seconds=0.1)",
    "common.log_run_summary(script='index_commits.py', repo=root, indexed=4, skipped=0, failed=0, "
    "duration_seconds=0.1)",
], ids=["another repository", "another source"])
def test_a_complete_run_of_another_repository_or_source_does_not_vouch_for_it(m, tmp_path, monkeypatch, other):
    root = _repo(tmp_path)
    _stand_in(monkeypatch, m, _child_that(other, _ABORT))
    with pytest.raises(SystemExit, match="Indexing code of app failed"):
        m._index(root)


# --- nothing written outside the throwaway directory ---------------------------

HF_VARIABLES = ("HF_HOME", "HF_XET_CACHE", "XDG_CACHE_HOME")


def test_children_keep_the_model_download_and_its_logs_inside_the_throwaway_directory(m, tmp_path, monkeypatch):
    """The model download wrote log files to ~/.cache/huggingface/xet/logs:
    huggingface_hub and hf_xet place their cache and logs under HF_HOME
    (hf_xet: HF_XET_CACHE first, then HF_HOME/xet, then
    XDG_CACHE_HOME/huggingface/xet), none of which the script set."""
    for name in HF_VARIABLES:
        monkeypatch.setenv(name, f"/Users/you/before-{name}")
    root = _repo(tmp_path)
    seen = []

    def fake_run(command, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    with m.throwaway_dirs() as base:
        m._index(root)
    assert len(seen) == len(m.SOURCES_INDEXED)
    for env in seen:
        for name in HF_VARIABLES:
            assert Path(env[name]).is_relative_to(base), name
        assert Path(env["GRIOT_DATA_DIR"]).is_relative_to(base)
    for name in HF_VARIABLES:
        assert m.os.environ[name] == f"/Users/you/before-{name}"


def test_the_huggingface_variables_are_removed_after_when_they_were_not_set(m, monkeypatch):
    for name in HF_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    with m.throwaway_dirs() as base:
        assert all(Path(m.os.environ[name]).is_relative_to(base) for name in HF_VARIABLES)
    assert not any(name in m.os.environ for name in HF_VARIABLES)
