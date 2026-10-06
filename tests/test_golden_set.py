"""Tests for `griot golden-set` — manages quality_golden_set.json (curated
level 2, from `griot quality-check`) without hand-editing JSON.

`suggest` derives candidates FOR FREE from the git log, reusing
retrieval_eval.build_qrels_from_git() (no logic duplication), with
case-by-case human approval (mocked via input()). `add` runs a REAL search
(mocked here) and lets the human choose which results form the must_include.
"""

import json

import pytest

from griot import common, golden_set


class FakeHit:
    def __init__(self, score, payload, id="00000000-0000-0000-0000-000000000001"):
        self.score = score
        self.payload = payload
        self.id = id


def _write_golden_set(cases):
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text(json.dumps(cases))


# --- suggest -----------------------------------------------------------


def test_cmd_suggest_creates_candidates_from_git_log_with_approval(git_repo, monkeypatch):
    git_repo.commit("Fix login bug", body="Details.", filename="auth.py")
    git_repo.commit("Add search feature", filename="search.py")

    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    rc = golden_set.cmd_suggest(str(git_repo.path))

    assert rc == 0
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 2
    queries = [c["query"] for c in cases]
    assert any("Fix login bug" in q for q in queries)
    assert any("Add search feature" in q for q in queries)
    auth_case = next(c for c in cases if "Fix login bug" in c["query"])
    assert auth_case["must_include"] == [{"repo": git_repo.path.name, "source_type": "code", "file_path": "auth.py"}]


def test_cmd_suggest_rejecting_a_candidate_does_not_add_it(git_repo, monkeypatch):
    git_repo.commit("Commit A", filename="a.py")
    git_repo.commit("Commit B", filename="b.py")

    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    golden_set.cmd_suggest(str(git_repo.path))

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 1


def test_cmd_suggest_quit_stops_early(git_repo, monkeypatch):
    git_repo.commit("Commit A", filename="a.py")
    git_repo.commit("Commit B", filename="b.py")
    git_repo.commit("Commit C", filename="c.py")

    answers = iter(["y", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    golden_set.cmd_suggest(str(git_repo.path))

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 1


def test_cmd_suggest_respects_limit(git_repo, monkeypatch):
    for i in range(5):
        git_repo.commit(f"Commit {i}", filename=f"f{i}.py")

    seen = {"n": 0}

    def fake_input(prompt):
        seen["n"] += 1
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    golden_set.cmd_suggest(str(git_repo.path), limit=2)

    assert seen["n"] == 2
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 2


def test_cmd_suggest_rejects_nonexistent_path(tmp_path, capsys):
    rc = golden_set.cmd_suggest(str(tmp_path / "does-not-exist"))
    assert rc != 0
    assert "does not exist" in capsys.readouterr().err.lower()


def test_cmd_suggest_no_candidates_prints_clean_message(git_repo, capsys):
    """Repo with no commit touching a file (still present in the working
    tree) -> clear message, no crash or empty approval loop."""
    rc = golden_set.cmd_suggest(str(git_repo.path))
    assert rc == 0
    assert "no candidates" in capsys.readouterr().out.lower()


def test_cmd_suggest_appends_to_existing_cases(git_repo, monkeypatch):
    _write_golden_set([{"query": "already curated case", "must_include": [{"repo": "x", "source_type": "code"}]}])
    git_repo.commit("New commit", filename="new.py")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    golden_set.cmd_suggest(str(git_repo.path))

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 2
    assert cases[0]["query"] == "already curated case"


# --- add -----------------------------------------------------------------


def test_cmd_add_appends_case_from_chosen_results(monkeypatch):
    hits = [
        FakeHit(0.91, {"repo": "repo-x", "source_type": "code", "file_path": "src/a.py", "content": "..."}),
        FakeHit(0.80, {"repo": "repo-x", "source_type": "commit", "commit_hash": "abc12345", "content": "..."}),
        FakeHit(0.60, {"repo": "repo-y", "source_type": "code", "file_path": "b.py", "content": "..."}),
    ]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "1,2")

    rc = golden_set.cmd_add("how does login work")

    assert rc == 0
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 1
    assert cases[0]["query"] == "how does login work"
    assert cases[0]["must_include"] == [
        {"repo": "repo-x", "source_type": "code", "file_path": "src/a.py"},
        {"repo": "repo-x", "source_type": "commit", "commit_hash": "abc12345"},
    ]


def test_cmd_add_empty_input_cancels_without_writing(monkeypatch):
    hits = [FakeHit(0.9, {"repo": "x", "source_type": "code", "file_path": "a.py"})]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    rc = golden_set.cmd_add("some question")

    assert rc == 0
    assert not common.GOLDEN_SET_PATH.exists()


def test_cmd_add_no_search_results_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: [])

    rc = golden_set.cmd_add("question with no result")

    assert rc != 0
    assert "no results" in capsys.readouterr().err.lower()


def test_cmd_add_invalid_number_format_is_an_error(monkeypatch, capsys):
    hits = [FakeHit(0.9, {"repo": "x", "source_type": "code", "file_path": "a.py"})]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "abc")

    rc = golden_set.cmd_add("question")

    assert rc != 0
    assert "invalid" in capsys.readouterr().err.lower()


def test_cmd_add_out_of_range_index_is_an_error(monkeypatch, capsys):
    hits = [FakeHit(0.9, {"repo": "x", "source_type": "code", "file_path": "a.py"})]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "5")

    rc = golden_set.cmd_add("question")

    assert rc != 0
    assert "out of range" in capsys.readouterr().err.lower()


def test_cmd_add_appends_to_existing_cases(monkeypatch):
    _write_golden_set([{"query": "already there", "must_include": []}])
    hits = [FakeHit(0.9, {"repo": "x", "source_type": "code", "file_path": "a.py"})]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "1")

    golden_set.cmd_add("new question")

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 2


# --- _must_include_entry (mapping by source_type) -----------------------


def test_must_include_entry_code_uses_file_path():
    entry = golden_set._must_include_entry({"repo": "r", "source_type": "code", "file_path": "a.py", "chunk_index": 3})
    assert entry == {"repo": "r", "source_type": "code", "file_path": "a.py"}


def test_must_include_entry_commit_uses_commit_hash():
    entry = golden_set._must_include_entry({"repo": "r", "source_type": "commit", "commit_hash": "deadbeef", "author": "x"})
    assert entry == {"repo": "r", "source_type": "commit", "commit_hash": "deadbeef"}


def test_must_include_entry_merge_request_uses_mr_iid():
    entry = golden_set._must_include_entry({"repo": "r", "source_type": "merge_request", "mr_iid": 42, "state": "opened"})
    assert entry == {"repo": "r", "source_type": "merge_request", "mr_iid": 42}


def test_must_include_entry_unknown_source_type_falls_back_to_repo_only():
    entry = golden_set._must_include_entry({"repo": "r", "source_type": "new_thing"})
    assert entry == {"repo": "r", "source_type": "new_thing"}


# --- list / remove -----------------------------------------------------


def test_cmd_list_on_fresh_install_shows_empty(capsys):
    rc = golden_set.cmd_list()
    assert rc == 0
    assert "no cases" in capsys.readouterr().out.lower()


def test_cmd_list_shows_each_case(capsys):
    _write_golden_set([
        {"query": "question one", "must_include": [{"repo": "x"}]},
        {"query": "question two", "must_include": []},
    ])
    rc = golden_set.cmd_list()
    out = capsys.readouterr().out
    assert rc == 0
    assert "question one" in out
    assert "question two" in out


def test_cmd_remove_deletes_by_index():
    _write_golden_set([
        {"query": "first", "must_include": []},
        {"query": "second", "must_include": []},
    ])
    rc = golden_set.cmd_remove(1)
    assert rc == 0
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 1
    assert cases[0]["query"] == "second"


def test_cmd_remove_out_of_range_is_an_error(capsys):
    _write_golden_set([{"query": "only one", "must_include": []}])
    rc = golden_set.cmd_remove(5)
    assert rc != 0
    assert "out of range" in capsys.readouterr().err.lower()


# --- list_cases / add_case / remove_case (pure data functions, no I/O) --


def test_list_cases_on_fresh_install_returns_empty():
    assert golden_set.list_cases() == []


def test_list_cases_returns_loaded_cases():
    _write_golden_set([{"query": "q", "must_include": []}])
    assert golden_set.list_cases() == [{"query": "q", "must_include": []}]


def test_add_case_appends_and_returns_created_case():
    must_include = [{"repo": "repo-x", "source_type": "code", "file_path": "a.py"}]

    case = golden_set.add_case("how does login work", must_include, limit=7)

    assert case == {"query": "how does login work", "limit": 7, "must_include": must_include}
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert cases == [case]


def test_add_case_appends_to_existing_cases():
    _write_golden_set([{"query": "already there", "must_include": []}])

    golden_set.add_case("new question", [{"repo": "x", "source_type": "code"}])

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert len(cases) == 2
    assert cases[0]["query"] == "already there"


def test_add_case_empty_query_raises_value_error():
    with pytest.raises(ValueError, match="query"):
        golden_set.add_case("   ", [{"repo": "x", "source_type": "code"}])
    assert not common.GOLDEN_SET_PATH.exists()


def test_add_case_empty_must_include_raises_value_error():
    with pytest.raises(ValueError, match="must_include"):
        golden_set.add_case("a question", [])
    assert not common.GOLDEN_SET_PATH.exists()


def test_add_case_rejects_an_entry_with_no_constraints():
    """quality_check._matches() is `all(payload.get(k) == v for k, v in
    expected.items())`, and all() over an empty dict is True — an empty
    must_include entry matches EVERY result, so the case passes no matter
    what search returns. The golden set is what tells the user their index
    still works; a case that can never fail makes it report success it did
    not measure.

    cmd_add() can't produce this (_must_include_entry always sets repo and
    source_type), but griot_golden_set_add takes the entries from an agent,
    so the caller is no longer trusted to build them correctly."""
    with pytest.raises(ValueError, match="must_include"):
        golden_set.add_case("a question", [{}])
    assert not common.GOLDEN_SET_PATH.exists()


def test_remove_case_deletes_by_index_and_returns_removed():
    _write_golden_set([
        {"query": "first", "must_include": []},
        {"query": "second", "must_include": []},
    ])

    removed = golden_set.remove_case(1)

    assert removed == {"query": "first", "must_include": []}
    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert cases == [{"query": "second", "must_include": []}]


def test_remove_case_out_of_range_raises_value_error():
    _write_golden_set([{"query": "only one", "must_include": []}])
    with pytest.raises(ValueError, match="out of range"):
        golden_set.remove_case(5)


def test_remove_case_empty_set_raises_value_error():
    with pytest.raises(ValueError, match="out of range"):
        golden_set.remove_case(1)


# --- main() dispatch --------------------------------------------------------


def test_main_dispatches_suggest_add_list_remove(git_repo, monkeypatch):
    git_repo.commit("Single commit", filename="x.py")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert golden_set.main(["suggest", str(git_repo.path)]) == 0
    assert golden_set.main(["list"]) == 0

    hits = [FakeHit(0.9, {"repo": "x", "source_type": "code", "file_path": "a.py"})]
    monkeypatch.setattr(common, "search", lambda query, limit=5, mode=None: hits)
    monkeypatch.setattr("builtins.input", lambda prompt: "1")
    assert golden_set.main(["add", "question"]) == 0

    assert golden_set.main(["remove", "--yes", "1"]) == 0


def test_add_case_rejects_an_entry_whose_values_are_all_none():
    """[review finding] The empty-dict guard closed the degenerate case, not
    the class. _matches() is `payload.get(k) == v`, and .get() returns None
    for a key that isn't there — so ANY entry whose values are all None
    matches every result, exactly like {}. {"bogus": None} is a non-empty
    dict and sailed through the first guard while producing the same
    always-passes case.

    A key with a real value that no payload carries is the opposite and is
    fine: it never matches, so it fails loudly and honestly."""
    with pytest.raises(ValueError, match="must_include"):
        golden_set.add_case("a question", [{"bogus": None}])
    assert not common.GOLDEN_SET_PATH.exists()


def test_add_case_accepts_an_entry_that_merely_cannot_match():
    """The mirror of the test above: an unsatisfiable case is a curation
    mistake the user will see as a failure, not a silent pass. Rejecting it
    here would mean deciding which payload fields exist, which this function
    has no business knowing."""
    case = golden_set.add_case("a question", [{"bogus": "some-value"}])

    assert case["must_include"] == [{"bogus": "some-value"}]


def test_must_include_entry_omits_a_field_the_payload_lacks():
    """Sibling defect, same None-versus-absent confusion, reachable from the
    CLI: _must_include_entry() wrote `{"repo": payload.get("repo")}`
    unconditionally, so a payload without `repo` produced {"repo": None} —
    which matches only results that HAVE no repo, i.e. never. A case that can
    never pass is as useless as one that can never fail, just noisy instead
    of silent."""
    entry = golden_set._must_include_entry({"source_type": "code", "file_path": "a.py"})

    assert "repo" not in entry
    assert entry == {"source_type": "code", "file_path": "a.py"}
