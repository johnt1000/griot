"""Tests for `griot repos` — manages REPOS_JSON_PATH (the list of repos for
batch indexing, `griot index all` without --path) via the command line,
instead of requiring manual JSON editing. Same spirit as `griot auth` (plan
section 11.1): closing a configuration gap that previously only had a
manual-edit path.
"""

import json

import pytest

from griot import common, repos


def _write_repos_json(paths):
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text(json.dumps(paths))


# --- cmd_add -----------------------------------------------------------


def test_cmd_add_creates_repos_json_when_missing(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    rc = repos.cmd_add(str(repo))

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo.resolve())]


def test_cmd_add_appends_to_existing_list(tmp_path):
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _write_repos_json([str(repo1.resolve())])

    rc = repos.cmd_add(str(repo2))

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo1.resolve()), str(repo2.resolve())]


def test_cmd_add_resolves_relative_path(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)

    rc = repos.cmd_add("my-repo")

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo.resolve())]


def test_cmd_add_rejects_duplicate(tmp_path, capsys):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _write_repos_json([str(repo.resolve())])

    rc = repos.cmd_add(str(repo))

    assert rc != 0
    assert "already" in capsys.readouterr().err.lower()
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo.resolve())]  # did not duplicate


def test_cmd_add_rejects_nonexistent_path(tmp_path, capsys):
    rc = repos.cmd_add(str(tmp_path / "does-not-exist"))

    assert rc != 0
    assert "doesn't exist" in capsys.readouterr().err.lower() or "invalid" in capsys.readouterr().err.lower()
    assert not common.REPOS_JSON_PATH.exists()


def test_cmd_add_rejects_file_not_directory(tmp_path, capsys):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")

    rc = repos.cmd_add(str(a_file))

    assert rc != 0


# --- cmd_list ------------------------------------------------------------


def test_cmd_list_on_fresh_install_shows_empty(capsys):
    assert not common.REPOS_JSON_PATH.exists()

    rc = repos.cmd_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "no repo" in out.lower() or "empty" in out.lower()


def test_cmd_list_marks_missing_directories(tmp_path, capsys):
    """Real finding from the session (repos.json with paths from another
    machine, silently invalid until running the indexer): griot repos list
    needs to warn BEFORE, not just let the indexer discover it later."""
    existing = tmp_path / "exists"
    existing.mkdir()
    _write_repos_json([str(existing.resolve()), "/path/that/does/not/exist"])

    rc = repos.cmd_list()

    assert rc == 0
    out = capsys.readouterr().out
    existing_line = next(l for l in out.splitlines() if str(existing.resolve()) in l)
    missing_line = next(l for l in out.splitlines() if "/path/that/does/not/exist" in l)
    assert "✓" in existing_line or "ok" in existing_line.lower()
    assert "✗" in missing_line or "doesn't exist" in missing_line.lower()


# --- cmd_remove ------------------------------------------------------------


def test_cmd_remove_deletes_matching_path(tmp_path):
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"
    repo1.mkdir()
    repo2.mkdir()
    _write_repos_json([str(repo1.resolve()), str(repo2.resolve())])

    rc = repos.cmd_remove(str(repo1))

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo2.resolve())]


def test_cmd_remove_works_on_stale_nonexistent_path(tmp_path):
    """Removing a path that no longer exists on disk needs to work — this is
    exactly the case of cleaning up stale entries (e.g. a repo that was
    moved/deleted), not just the happy path of a repo that's still present."""
    stale = "/path/that/no/longer/exists"
    _write_repos_json([stale])

    rc = repos.cmd_remove(stale)

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == []


def test_cmd_remove_resolves_relative_path(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    _write_repos_json([str(repo.resolve())])
    monkeypatch.chdir(tmp_path)

    rc = repos.cmd_remove("my-repo")

    assert rc == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == []


def test_cmd_remove_not_found_is_a_clear_error(tmp_path, capsys):
    _write_repos_json([str(tmp_path.resolve())])

    rc = repos.cmd_remove("/some/other/path")

    assert rc != 0
    assert "not found" in capsys.readouterr().err.lower()


def test_cmd_remove_on_missing_repos_json_is_a_clear_error(capsys):
    assert not common.REPOS_JSON_PATH.exists()

    rc = repos.cmd_remove("/anything")

    assert rc != 0


# --- repo_status()/add_repo()/remove_repo(): pure functions ---
# --- extracted from cmd_add/cmd_list/cmd_remove — same pattern the v1 UI  ---
# --- work already used for auth.py's cmd_set/cmd_list/cmd_remove -> ---------
# --- provider_status()/set_provider_key()/remove_provider_key(). ------------


def test_repo_status_reports_exists_and_is_git(tmp_path):
    git_repo = tmp_path / "git-repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()
    plain_dir = tmp_path / "plain-dir"
    plain_dir.mkdir()
    _write_repos_json([str(git_repo.resolve()), str(plain_dir.resolve()), "/path/that/does/not/exist"])

    statuses = {s["path"]: s for s in repos.repo_status()}

    assert statuses[str(git_repo.resolve())] == {"name": git_repo.resolve().name, "path": str(git_repo.resolve()), "exists": True, "is_git": True}
    assert statuses[str(plain_dir.resolve())]["exists"] is True
    assert statuses[str(plain_dir.resolve())]["is_git"] is False
    assert statuses["/path/that/does/not/exist"]["exists"] is False
    assert statuses["/path/that/does/not/exist"]["is_git"] is False


def test_repo_status_is_git_via_dotgit_file_not_just_directory():
    """A git worktree/submodule has a `.git` FILE (a pointer
    to the real gitdir elsewhere), not a directory — is_git must use
    .exists(), not .is_dir(), or every worktree/submodule repo would be
    misreported as not-a-real-repo."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        repo = Path(td) / "worktree-repo"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        _write_repos_json([str(repo.resolve())])

        status = repos.repo_status()[0]
        assert status["is_git"] is True


def test_repo_status_empty_when_no_repos_json():
    assert not common.REPOS_JSON_PATH.exists()
    assert repos.repo_status() == []


def test_add_repo_returns_resolved_path_and_writes_it(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    resolved = repos.add_repo(str(repo))

    assert resolved == str(repo.resolve())
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo.resolve())]


def test_add_repo_rejects_nonexistent_path():
    with pytest.raises(ValueError, match="doesn't exist"):
        repos.add_repo("/does/not/exist")


def test_cmd_add_warns_when_path_is_not_a_git_repo(tmp_path, capsys):
    """[security review finding] index-all walks every registered path for
    the 'code' source regardless of git-ness (jobs.py deliberately skips
    the git check when path=None — the target already IS the full allowed
    list) — registering a non-git directory (e.g. a plain folder, or by
    mistake $HOME) would otherwise get silently mass-indexed with no signal
    to the user at registration time."""
    plain_dir = tmp_path / "plain-dir"
    plain_dir.mkdir()

    rc = repos.cmd_add(str(plain_dir))

    assert rc == 0  # still succeeds — this is a warning, not a rejection
    assert "git" in capsys.readouterr().err.lower()


def test_cmd_add_no_warning_for_a_real_git_repo(tmp_path, capsys):
    git_repo = tmp_path / "git-repo"
    git_repo.mkdir()
    (git_repo / ".git").mkdir()

    rc = repos.cmd_add(str(git_repo))

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_add_repo_rejects_duplicate(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    repos.add_repo(str(repo))

    with pytest.raises(ValueError, match="already"):
        repos.add_repo(str(repo))


def test_remove_repo_returns_resolved_path_and_removes_it(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    repos.add_repo(str(repo))

    resolved = repos.remove_repo(str(repo))

    assert resolved == str(repo.resolve())
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == []


def test_remove_repo_rejects_not_found(tmp_path):
    repos.add_repo(str(tmp_path))
    with pytest.raises(ValueError, match="not found"):
        repos.remove_repo("/some/other/path")


def test_remove_repo_rejects_missing_repos_json():
    assert not common.REPOS_JSON_PATH.exists()
    with pytest.raises(ValueError):
        repos.remove_repo("/anything")


def test_save_is_atomic_no_tmp_file_left_behind(tmp_path):
    """[risk 5.2] repos._save() must go through
    common.secure_write_text_atomic(), same crash-safety as
    _save_spend_state() — a truncated repos.json from a crash mid-write
    is exactly what feeds the fail-open bug this same v2 fixes in
    jobs.index_path_allowed()."""
    repo = tmp_path / "my-repo"
    repo.mkdir()
    repos.add_repo(str(repo))
    leftovers = list(common.REPOS_JSON_PATH.parent.glob("*.tmp"))
    assert leftovers == []


# --- main() dispatch --------------------------------------------------------


def test_main_dispatches_add_list_remove(tmp_path, monkeypatch):
    repo = tmp_path / "my-repo"
    repo.mkdir()
    # `add` is answered at a terminal (it has no --yes); see tests/test_cli_confirm.py.
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert repos.main(["add", str(repo)]) == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == [str(repo.resolve())]
    assert repos.main(["list"]) == 0
    assert repos.main(["remove", "--yes", str(repo)]) == 0
    assert json.loads(common.REPOS_JSON_PATH.read_text()) == []
