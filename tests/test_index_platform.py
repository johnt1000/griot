import pytest

from conftest import GitRepo
from griot import index_platform


# --- --path (mutually exclusive with --repo, skips repos.json) -------------
#
# index_platform.py talks to platforms.detect_platform()/fetch_*() —
# mocked here so it doesn't depend on network/token, same pattern already
# used by the other --path tests.

def test_main_with_path_indexes_without_reading_repos_json(git_repo, monkeypatch):
    calls = {}

    def fake_index_documents(documents, desc="Indexing"):
        calls["documents"] = documents
        return (len(documents), 0, 0)

    def fail_load_repos():
        raise FileNotFoundError("repos.json should not be read when --path is used")

    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "git@github.com:group/project.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: ("github", "group/project", None))
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda platform, project_id, host=None: (
        [{"title": "test PR", "iid": 1, "state": "open"}] if platform == "github" else []
    ))
    monkeypatch.setattr(index_platform.platforms, "fetch_releases", lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_platform.common, "load_repos", fail_load_repos)

    index_platform.main(["--path", str(git_repo.path)])

    assert calls["documents"]
    assert calls["documents"][0]["metadata"]["repo"] == git_repo.path.name


def test_path_and_repo_together_is_argparse_error(git_repo):
    with pytest.raises(SystemExit):
        index_platform.main(["--path", str(git_repo.path), "--repo", "algum-repo"])


def test_path_disambiguates_ids_for_same_basename_different_location(git_repo, tmp_path, monkeypatch):
    """Natural-id collision guard: same PR iid in two repos with the same final basename
    ('repo'), indexed via --path -> without the disambiguated key the ids
    would collide literally."""
    (tmp_path / "elsewhere").mkdir()
    repo2 = GitRepo(tmp_path / "elsewhere" / "repo")

    calls = []

    def fake_index_documents(documents, desc="Indexing"):
        calls.append(documents)
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "git@github.com:group/project.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: ("github", "group/project", None))
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda platform, project_id, host=None: (
        [{"title": "test PR", "iid": 1, "state": "open"}]
    ))
    monkeypatch.setattr(index_platform.platforms, "fetch_releases", lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.common, "index_documents", fake_index_documents)

    index_platform.main(["--path", str(git_repo.path)])
    index_platform.main(["--path", str(repo2.path)])

    id1 = calls[0][0]["id"]
    id2 = calls[1][0]["id"]
    assert id1 != id2
    assert calls[0][0]["metadata"]["repo"] == "repo"
    assert calls[1][0]["metadata"]["repo"] == "repo"


def test_unrecognized_remote_is_skipped_with_warning(git_repo, monkeypatch, capsys):
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "git@example.com:some/repo.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: None)

    docs = index_platform.build_documents(git_repo.path)

    assert docs == []
    assert "isn't from any recognized platform" in capsys.readouterr().out


def test_no_remote_is_skipped_with_warning(git_repo, monkeypatch, capsys):
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: None)

    docs = index_platform.build_documents(git_repo.path)

    assert docs == []
    assert "'origin' remote" in capsys.readouterr().out


# --- repos.json missing ----------------------------------------

def test_main_without_repos_json_prints_friendly_error(monkeypatch, capsys):
    def fail_load_repos():
        raise FileNotFoundError()

    monkeypatch.setattr(index_platform.common, "load_repos", fail_load_repos)

    index_platform.main([])

    out = capsys.readouterr().out
    assert "not found" in out
