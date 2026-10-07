"""`griot golden-set suggest` offers only cases that can pass, and writes safely.

A candidate is a commit: its message is the question and the files it
touched are what must come back. Two things made cases nobody could pass.
Every touched file that still existed was required, including files `griot
index code` never reads (an image, a lock file, a file the repository
ignores). And a commit that touched more files than a search returns
required all of them. Approved, such a case failed forever, and a failing
golden set is what tells someone their index is broken.

The file itself was written in place: a write that stopped half-way left a
golden set nobody could read."""

import json
import subprocess

import pytest

from griot import cli, common, golden_set


def _commit_files(repo, message: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(["git", "-C", str(repo.path), "add", "-f", name], check=True)
    subprocess.run(["git", "-C", str(repo.path), "commit", "-q", "-m", message], check=True)


def _approve_everything(monkeypatch) -> list[str]:
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    return asked


def _cases() -> list[dict]:
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


# --- only what the index can hold ---------------------------------------------------------------


def test_a_file_the_index_never_reads_is_not_required(git_repo, monkeypatch):
    _commit_files(git_repo, "Add the login screen", {"auth.py": "x = 1", "logo.png": "not really an image"})
    _approve_everything(monkeypatch)
    assert golden_set.cmd_suggest(str(git_repo.path)) == 0
    (case,) = _cases()
    assert [entry["file_path"] for entry in case["must_include"]] == ["auth.py"]


def test_a_commit_that_touched_nothing_the_index_reads_is_not_offered(git_repo, monkeypatch, capsys):
    _commit_files(git_repo, "Replace the logo", {"logo.png": "not really an image"})
    asked = _approve_everything(monkeypatch)
    assert golden_set.cmd_suggest(str(git_repo.path)) == 0
    assert asked == [] and _cases() == []


def test_a_file_the_repository_ignores_is_not_required(git_repo, monkeypatch):
    """Committed once, ignored and untracked since: it is still on disk, and
    it is exactly what `griot index code` leaves out."""
    _commit_files(git_repo, "Add settings and notes", {"settings.py": "x = 1", "notes.md": "private"})
    (git_repo.path / ".gitignore").write_text("notes.md\n")
    subprocess.run(["git", "-C", str(git_repo.path), "rm", "-q", "--cached", "notes.md"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "Stop tracking the notes"], check=True)
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    required = [entry["file_path"] for case in _cases() for entry in case["must_include"]]
    assert "settings.py" in required and "notes.md" not in required


# --- only what a search can return --------------------------------------------------------------


def test_a_commit_that_touched_more_files_than_a_search_returns_is_not_offered(git_repo, monkeypatch, capsys):
    """Every file is required, and a search for the case returns five
    results: seven files can never all be among them."""
    _commit_files(git_repo, "Rename the module everywhere", {f"mod{n}.py": "x = 1" for n in range(7)})
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    asked = _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    cases = _cases()
    assert len(asked) == 1 and [case["query"] for case in cases] == ["Fix the login bug"]
    out = " ".join(capsys.readouterr().out.split())
    assert "1 commit" in out and "more files than" in out, "what was left out is said, with the reason"


def test_every_case_it_writes_can_pass(git_repo, monkeypatch):
    for n in range(1, 8):
        _commit_files(git_repo, f"Change {n} files", {f"dir{n}/f{i}.py": "x = 1" for i in range(n)})
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    cases = _cases()
    golden_set.check_cases(cases)
    assert cases and all(1 <= len(case["must_include"]) <= case["limit"] for case in cases)
    assert sorted(len(case["must_include"]) for case in cases) == [1, 2, 3, 4, 5]


def test_the_number_offered_counts_candidates_not_commits(git_repo, monkeypatch):
    """`--limit 2` used to take the first two commits and then drop the
    ones it could not use: fewer candidates than asked for, or none."""
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _commit_files(git_repo, "Fix the search bug", {"search.py": "x = 2"})
    _commit_files(git_repo, "Replace the logo", {"logo.png": "new"})
    _commit_files(git_repo, "Rename everything", {f"mod{n}.py": "x = 1" for n in range(7)})
    asked = _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path), limit=2)
    assert len(asked) == 2
    assert sorted(case["query"] for case in _cases()) == ["Fix the login bug", "Fix the search bug"]


# --- the file is never left half-written --------------------------------------------------------


def test_a_write_that_stops_half_way_leaves_the_cases_that_were_there(monkeypatch):
    golden_set.add_case("a question", [{"repo": "alpha", "source_type": "code"}])
    before = common.GOLDEN_SET_PATH.read_text()
    real = common.secure_write_text

    def stops_half_way(path, text):
        real(path, text[: len(text) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(common, "secure_write_text", stops_half_way)
    with pytest.raises(OSError):
        golden_set.add_case("another question", [{"repo": "alpha", "source_type": "code"}])
    assert common.GOLDEN_SET_PATH.read_text() == before
    assert [p.name for p in common.GOLDEN_SET_PATH.parent.iterdir() if ".tmp" in p.name] == []


def test_suggest_writes_the_same_way(git_repo, monkeypatch):
    golden_set.add_case("a question", [{"repo": "alpha", "source_type": "code"}])
    before = common.GOLDEN_SET_PATH.read_text()
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _approve_everything(monkeypatch)
    real = common.secure_write_text

    def stops_half_way(path, text):
        real(path, text[: len(text) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(common, "secure_write_text", stops_half_way)
    with pytest.raises(OSError):
        golden_set.cmd_suggest(str(git_repo.path))
    assert common.GOLDEN_SET_PATH.read_text() == before


def test_the_file_keeps_its_permission(git_repo, monkeypatch):
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    assert common.GOLDEN_SET_PATH.stat().st_mode & 0o777 == 0o600


# --- what the first review found: the rule is "the index holds a chunk of it" -----------------


@pytest.mark.parametrize("content", ["", "   \n\t\n"])
def test_a_file_with_no_text_is_not_required(git_repo, monkeypatch, content):
    """`griot index code` lists it and stores nothing for it: no chunk, so
    nothing a search could ever return."""
    _commit_files(git_repo, "Add the package", {"pkg/__init__.py": content, "pkg/core.py": "x = 1"})
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    (case,) = _cases()
    assert [entry["file_path"] for entry in case["must_include"]] == ["pkg/core.py"]


def test_a_commit_without_a_message_is_not_a_question(git_repo, monkeypatch, capsys):
    (git_repo.path / "c.py").write_text("x = 1")
    subprocess.run(["git", "-C", str(git_repo.path), "add", "c.py"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "--allow-empty-message", "-m", ""], check=True)
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    asked = _approve_everything(monkeypatch)
    assert golden_set.cmd_suggest(str(git_repo.path)) == 0
    assert len(asked) == 1 and [case["query"] for case in _cases()] == ["Fix the login bug"]
    assert "no message" in " ".join(capsys.readouterr().out.split())


def test_a_file_whose_name_git_would_quote_is_still_found(git_repo, monkeypatch):
    """git writes `caf\\303\\251.py` for a name outside ASCII unless told not
    to: the file then "did not exist" and its commit was dropped in silence."""
    _commit_files(git_repo, "Add the menu", {"café menu.py": "x = 1"})
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path))
    (case,) = _cases()
    assert [entry["file_path"] for entry in case["must_include"]] == ["café menu.py"]


def test_a_directory_inside_a_repository_gets_its_own_paths(git_repo, monkeypatch):
    """git names files from the top of the work tree. Given `repo/sub`, a
    commit that touched `sub/b.py` was dropped, and one that touched the
    top-level `a.py` required `sub/a.py`: another file of the same name."""
    _commit_files(git_repo, "Touch the top one only", {"a.py": "x = 1"})
    # Four characters longer than `a.py`, as `sub/` is: a path cut by length
    # instead of checked would turn it into the `a.py` of the directory.
    _commit_files(git_repo, "Touch another top one", {"new_a.py": "x = 1"})
    _commit_files(git_repo, "Add both in sub", {"sub/a.py": "x = 2", "sub/b.py": "x = 3"})
    _approve_everything(monkeypatch)
    golden_set.cmd_suggest(str(git_repo.path / "sub"))
    cases = _cases()
    assert [case["query"] for case in cases] == ["Add both in sub"]
    assert sorted(entry["file_path"] for entry in cases[0]["must_include"]) == ["a.py", "b.py"]
    assert {entry["repo"] for entry in cases[0]["must_include"]} == {"sub"}


def test_when_the_files_cannot_be_listed_it_says_that_and_not_that_nothing_is_readable(git_repo, monkeypatch, capsys):
    from griot import index_code
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})

    def cannot_list(repo_path):
        print("git could not list the files of this repository, so nothing is read from it")
        return []

    monkeypatch.setattr(index_code, "discover_files", cannot_list)
    assert golden_set.cmd_suggest(str(git_repo.path)) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "git could not list the files" in out


@pytest.mark.parametrize("limit", [0, -3])
def test_a_number_of_candidates_below_one_is_an_error(git_repo, monkeypatch, capsys, limit):
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    asked = _approve_everything(monkeypatch)
    assert golden_set.cmd_suggest(str(git_repo.path), limit=limit) == 2
    assert asked == [] and "--limit" in capsys.readouterr().err


def test_a_directory_that_is_not_a_git_work_tree_is_an_error(tmp_path, capsys):
    """It printed the git command line that failed, then "No candidates
    found", and exited 0: to a script, "nothing to suggest"."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1")
    assert golden_set.cmd_suggest(str(plain)) == 1
    out = capsys.readouterr()
    assert "not a git work tree" in out.err and "No candidates" not in out.out and "--no-pager" not in out.out


# --- a candidate add_case refuses ----------------------------------------------------------------


def _refuse_one(monkeypatch, refused_query: str) -> None:
    """add_case refuses the case of `refused_query` (it is given a mode that
    is not a search mode, the way a mode refusal reaches it); every other
    candidate gets the mode suggest gives it."""
    real = golden_set.case_mode_for
    monkeypatch.setattr(golden_set, "case_mode_for",
                        lambda query, mode: ("bogus", None) if query == refused_query else real(query, mode))


def test_a_candidate_add_case_refuses_is_reported_and_the_run_goes_on(git_repo, monkeypatch, capsys):
    """add_case raises ValueError on a case it refuses; uncaught, the
    traceback ended the run, and the candidates after it were never offered."""
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _commit_files(git_repo, "Fix the search bug", {"search.py": "x = 2"})
    _commit_files(git_repo, "Fix the cache bug", {"cache.py": "x = 2"})
    _refuse_one(monkeypatch, "Fix the search bug")
    asked = _approve_everything(monkeypatch)

    assert cli.main(["golden-set", "suggest", str(git_repo.path)]) == 0

    assert len(asked) == 3, "the candidates after the refused one are still offered"
    assert sorted(case["query"] for case in _cases()) == ["Fix the cache bug", "Fix the login bug"]
    out = capsys.readouterr().out
    refused = out.split("Candidate query: Fix the search bug", 1)[1].split("Candidate query:", 1)[0]
    assert "Not added: `mode` must be one of" in refused, "said under the candidate it is about, with the reason"
    summary = " ".join(out.rsplit("Candidate query:", 1)[1].split())
    assert "2 case(s) added" in summary and "1 approved candidate(s) not added" in summary


def test_a_run_where_every_approved_case_was_written_says_nothing_was_left_out(git_repo, monkeypatch, capsys):
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _approve_everything(monkeypatch)
    assert cli.main(["golden-set", "suggest", str(git_repo.path)]) == 0
    assert "not added" not in capsys.readouterr().out.lower()


def test_a_golden_set_that_is_not_json_stops_the_run_at_the_first_write(git_repo, monkeypatch, capsys):
    """Every candidate would fail the same way, so it is said once and no
    one is asked about the rest; the file is left as it was."""
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _commit_files(git_repo, "Fix the search bug", {"search.py": "x = 2"})
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text("{ not json")
    asked = _approve_everything(monkeypatch)

    assert cli.main(["golden-set", "suggest", str(git_repo.path)]) == 1

    assert len(asked) == 1
    assert common.GOLDEN_SET_PATH.read_text() == "{ not json"
    err = capsys.readouterr().err
    assert err.count("Error:") == 1 and str(common.GOLDEN_SET_PATH) in err and "not valid JSON" in err
