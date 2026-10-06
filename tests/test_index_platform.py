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

    assert index_platform.main([]) == 1

    out = capsys.readouterr().err  # an error: stderr and a failing exit status
    assert "not found" in out


# --- a fetch the platform refuses is a failure of the run --------------------
#
# [real bug, 2026-10-05] An expired token answered 401 to every fetch: the run
# said "No platform items to index.", exited 0 and recorded nothing, so
# `griot index all`, `griot stats` and the freshness report saw nothing wrong.
# When only some fetches failed, the run was recorded with failed=0.

from pathlib import Path

import requests


def _refused(status, message="401 Client Error for url: https://api.example.com/x?token=hunter2"):
    response = requests.Response()
    response.status_code = status

    def fetch(platform, project_id, host=None):
        raise requests.HTTPError(message, response=response)
    return fetch


@pytest.fixture
def platform_run(git_repo, monkeypatch):
    """index_platform wired to a fake GitHub remote; returns what was
    recorded and embedded."""
    seen = {"runs": [], "documents": None}
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "git@github.com:group/project.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: ("github", "group/project", None))
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.common, "credential_hint", lambda env_var: "")

    def fake_index_documents(documents, desc="Indexing"):
        seen["documents"] = documents
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_platform.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_platform.common, "last_run_failures", lambda: [])
    monkeypatch.setattr(index_platform.common, "log_run_summary", lambda **fields: seen["runs"].append(fields))
    seen["path"] = str(git_repo.path)
    return seen


def test_every_fetch_refused_fails_the_run_and_records_it(platform_run, monkeypatch, capsys):
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, _refused(401))

    rc = index_platform.main(["--path", platform_run["path"]])

    assert rc == 1
    assert "refused" in capsys.readouterr().err
    (run,) = platform_run["runs"]
    assert run["script"] == "index_platform.py"
    assert run["indexed"] == 0 and run["failed"] == 3
    assert {f["reason"] for f in run["failures"]} == {"HTTP 401"}
    assert len({f["id"] for f in run["failures"]}) == 3
    # An error makes it a run that did not do its job for everything that
    # reads runs: the freshness report does not count it as indexing the
    # source, `griot doctor` and `griot stats` say it.
    assert "refused" in run["error"] and "HTTP 401" in run["error"]


def test_every_fetch_refused_fails_a_dry_run_too(platform_run, monkeypatch):
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, _refused(403))

    assert index_platform.main(["--path", platform_run["path"], "--dry-run"]) == 1
    # A dry run writes no run: one would shadow the last real run in
    # griot_index_status and read as a failed indexing in `griot stats`.
    assert platform_run["runs"] == []


def test_a_dry_run_with_some_fetches_refused_records_nothing(platform_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", _refused(401))

    assert not index_platform.main(["--path", platform_run["path"], "--dry-run"])
    assert platform_run["runs"] == []


def test_a_refused_fetch_among_good_ones_is_counted_as_failed(platform_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests",
                        lambda platform, project_id, host=None: [{"title": "a PR", "iid": 1, "state": "open"}])
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", _refused(403))

    rc = index_platform.main(["--path", platform_run["path"]])

    assert not rc
    assert platform_run["documents"]
    (run,) = platform_run["runs"]
    assert run["failed"] == 1 and run["indexed"] == len(platform_run["documents"])
    (failure,) = run["failures"]
    assert failure["reason"] == "HTTP 403" and "issues" in failure["id"]
    assert not run.get("error")


def test_some_refused_and_nothing_else_to_index_is_still_recorded(platform_run, monkeypatch):
    """Releases came back empty, issues were refused: nothing to embed, but
    the refusal is on record and the run did fetch something."""
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", _refused(401))

    rc = index_platform.main(["--path", platform_run["path"]])

    assert not rc
    (run,) = platform_run["runs"]
    assert run["indexed"] == 0 and run["failed"] == 1


def test_the_recorded_reason_never_carries_the_error_text(platform_run, monkeypatch):
    """The exception's text can hold the request URL, and a URL can hold a
    token: the run record (logs.db) gets the status, not the message."""
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, _refused(401))

    index_platform.main(["--path", platform_run["path"]])

    assert "hunter2" not in repr(platform_run["runs"])


def test_a_failure_with_no_status_is_named_by_its_kind(platform_run, monkeypatch):
    def broken(platform, project_id, host=None):
        raise requests.ConnectionError("connection refused by api.example.com?token=hunter2")
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, broken)

    assert index_platform.main(["--path", platform_run["path"]]) == 1
    (run,) = platform_run["runs"]
    assert {f["reason"] for f in run["failures"]} == {"ConnectionError"}
    assert "hunter2" not in repr(run)


def test_nothing_on_the_platform_is_still_not_a_failure(platform_run):
    rc = index_platform.main(["--path", platform_run["path"]])

    assert not rc and platform_run["runs"] == []


def test_a_remote_of_no_known_platform_is_not_a_failure(platform_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: None)

    assert not index_platform.main(["--path", platform_run["path"]])
    assert platform_run["runs"] == []


def test_a_run_the_platform_refused_entirely_does_not_count_as_indexing_it(platform_run, monkeypatch):
    """Through the freshness report, with the run as it is recorded."""
    from griot import freshness
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, _refused(401))
    index_platform.main(["--path", platform_run["path"]])
    (run,) = platform_run["runs"]
    name = Path(platform_run["path"]).name
    recorded = {**run, "timestamp": "2026-10-05T00:00:00", "heads": {name: "a" * 40}}

    (report,) = freshness.assess([recorded], [{"name": name, "path": platform_run["path"], "head": "a" * 40, "refs": {}}])

    assert "platform" in report["missing_sources"]
