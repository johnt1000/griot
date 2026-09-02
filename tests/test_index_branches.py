import subprocess
from pathlib import Path

import pytest

from conftest import GitRepo
from griot import index_branches


def _checkout_new_branch(repo_path, name, base="main"):
    subprocess.run(["git", "-C", str(repo_path), "checkout", "-q", "-b", name, base], check=True)


def test_default_branch_reads_symbolic_ref(git_repo):
    git_repo.commit("c1")
    git_repo.set_remote_head("main")
    assert index_branches.default_branch(git_repo.path) == "origin/main"


def test_default_branch_without_remote_returns_none(git_repo):
    git_repo.commit("c1")
    assert index_branches.default_branch(git_repo.path) is None


def test_remote_branches_excludes_head_pointer(git_repo):
    git_repo.commit("c1")
    _checkout_new_branch(git_repo.path, "feature-x")
    git_repo.commit("c2", filename="f.txt")
    git_repo.set_remote_head("main")

    branches = index_branches.remote_branches(git_repo.path)
    assert "origin/main" in branches
    assert "origin/feature-x" in branches
    assert not any("->" in b for b in branches)  # "origin/HEAD -> origin/main" should never appear


def test_build_documents_skips_default_branch_and_lists_others(git_repo):
    git_repo.commit("c1 on main")
    _checkout_new_branch(git_repo.path, "feature-x")
    git_repo.commit("c2 on feature-x", filename="f.txt")
    git_repo.set_remote_head("main")

    docs = index_branches.build_documents(git_repo.path)
    names = {d["metadata"]["branch_name"] for d in docs}
    assert names == {"origin/feature-x"}  # origin/main (default) is left out, covered by other indexers


def test_build_documents_includes_ahead_commits_in_content(git_repo):
    git_repo.commit("c1 on main")
    _checkout_new_branch(git_repo.path, "feature-x")
    git_repo.commit("commit exclusive to the feature", filename="f.txt")
    git_repo.set_remote_head("main")

    docs = index_branches.build_documents(git_repo.path)
    assert len(docs) == 1
    assert "commit exclusive to the feature" in docs[0]["content"]
    assert docs[0]["id"] == f"{git_repo.path.name}:branch:origin/feature-x"


def test_build_documents_empty_when_only_default_branch_exists(git_repo):
    git_repo.commit("c1")
    git_repo.set_remote_head("main")
    assert index_branches.build_documents(git_repo.path) == []


# --- --path (mutually exclusive with --repo, skips repos.json) -------------

def test_main_with_path_indexes_without_reading_repos_json(git_repo, monkeypatch):
    git_repo.commit("c1 on main")
    _checkout_new_branch(git_repo.path, "feature-x")
    git_repo.commit("c2 on feature-x", filename="f.txt")
    git_repo.set_remote_head("main")
    calls = {}

    def fake_index_documents(documents, desc="Indexing"):
        calls["documents"] = documents
        return (len(documents), 0, 0)

    def fail_load_repos():
        raise FileNotFoundError("repos.json should not be read when --path is used")

    monkeypatch.setattr(index_branches.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_branches.common, "load_repos", fail_load_repos)

    index_branches.main(["--path", str(git_repo.path)])

    assert calls["documents"]
    assert calls["documents"][0]["metadata"]["repo"] == git_repo.path.name


def test_path_and_repo_together_is_argparse_error(git_repo):
    with pytest.raises(SystemExit):
        index_branches.main(["--path", str(git_repo.path), "--repo", "some-repo"])


def test_path_disambiguates_ids_for_same_basename_different_location(git_repo, tmp_path, monkeypatch):
    """Natural-id collision guard: the same non-default branch ('origin/feature-x') in two
    repos with the same final basename ('repo'), indexed via --path -> without
    the disambiguated key the ids would literally collide."""
    def _setup(repo):
        repo.commit("c1 on main")
        _checkout_new_branch(repo.path, "feature-x")
        repo.commit("c2 on feature-x", filename="f.txt")
        repo.set_remote_head("main")

    (tmp_path / "elsewhere").mkdir()
    repo2 = GitRepo(tmp_path / "elsewhere" / "repo")
    _setup(git_repo)
    _setup(repo2)

    calls = []

    def fake_index_documents(documents, desc="Indexing"):
        calls.append(documents)
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_branches.common, "index_documents", fake_index_documents)

    index_branches.main(["--path", str(git_repo.path)])
    index_branches.main(["--path", str(repo2.path)])

    id1 = calls[0][0]["id"]
    id2 = calls[1][0]["id"]
    assert id1 != id2
    assert calls[0][0]["metadata"]["repo"] == "repo"
    assert calls[1][0]["metadata"]["repo"] == "repo"


# --- repos.json missing ----------------------------------------

def test_main_without_repos_json_prints_friendly_error(monkeypatch, capsys):
    def fail_load_repos():
        raise FileNotFoundError()

    monkeypatch.setattr(index_branches.common, "load_repos", fail_load_repos)

    index_branches.main([])

    out = capsys.readouterr().out
    assert "not found" in out


# --- git argument injection (finding L1 from the 2026-08-19 audit) ----------

def test_git_ref_arguments_are_terminated_with_end_of_options(monkeypatch):
    """Refs interpolated into `git log` (branch, base..branch) come from
    `git branch -r` — always prefixed with origin/ today, but one refactor
    away from becoming an option injection. --end-of-options (git 2.24+)
    closes that door structurally."""
    calls = []

    def fake_run_git(repo_path, *args):
        calls.append(args)
        return ""

    monkeypatch.setattr(index_branches, "run_git", fake_run_git)

    index_branches.last_commit(Path("/whatever"), "origin/feature")
    index_branches.ahead_commits(Path("/whatever"), "origin/main", "origin/feature")

    for args in calls:
        ref_positions = [i for i, a in enumerate(args) if "origin/" in a]
        eoo = args.index("--end-of-options")
        assert ref_positions, f"call without a ref? {args}"
        assert all(eoo < i for i in ref_positions), f"ref before --end-of-options: {args}"
