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


# --- one repository refused entirely, among others that answered ------------
#
# [debt 17] The run failed only when EVERY fetch of the whole run was refused:
# with several repositories, one whose platform refused all of its fetches
# while the others answered only added failures to a run that exited 0, so
# `griot index all` went on as if that repository had been indexed.

@pytest.fixture
def two_repo_run(tmp_path, monkeypatch):
    """Two registered repositories, `good` and `bad`, each with its own
    project on a fake GitHub; returns what was recorded and embedded."""
    return _two_repos(tmp_path, monkeypatch)


def _two_repos(tmp_path, monkeypatch, bad_name="bad"):
    good, bad = GitRepo(tmp_path / "good"), GitRepo(tmp_path / bad_name)
    seen = {"runs": [], "documents": None, "good": str(good.path), "bad": str(bad.path)}
    monkeypatch.setattr(index_platform.common, "load_repos", lambda: [seen["good"], seen["bad"]])
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: f"git@github.com:group/{repo_path.name}.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform",
                        lambda url: ("github", url.split(":", 1)[1].removesuffix(".git"), None))

    def prs(platform, project_id, host=None):
        return [{"title": f"a PR of {project_id}", "iid": 1, "state": "open"}]
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", prs)
    for name in ("fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, lambda platform, project_id, host=None: [])
    monkeypatch.setattr(index_platform.common, "credential_hint", lambda env_var: "")

    def fake_index_documents(documents, desc="Indexing"):
        seen["documents"] = documents
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_platform.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_platform.common, "last_run_failures", lambda: [])
    monkeypatch.setattr(index_platform.common, "log_run_summary", lambda **fields: seen["runs"].append(fields))
    return seen


def _refuse_for(project_suffix, fetch, status=401):
    """`fetch` for every project but the one ending in `project_suffix`,
    which the platform refuses with `status`."""
    refused = _refused(status)

    def wrapped(platform, project_id, host=None):
        if project_id.endswith(project_suffix):
            return refused(platform, project_id, host)
        return fetch(platform, project_id, host)
    return wrapped


def _refuse_everything_of(monkeypatch, project_suffix, status=401):
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name,
                            _refuse_for(project_suffix, getattr(index_platform.platforms, name), status))


def test_one_repository_refused_entirely_fails_the_run(two_repo_run, monkeypatch, capsys):
    _refuse_everything_of(monkeypatch, "/bad")

    rc = index_platform.main([])

    assert rc == 1
    err = capsys.readouterr().err
    assert "bad" in err and "good" not in err


def test_the_repositories_that_answered_are_still_indexed(two_repo_run, monkeypatch):
    _refuse_everything_of(monkeypatch, "/bad")

    index_platform.main([])

    assert {d["metadata"]["repo"] for d in two_repo_run["documents"]} == {"good"}
    (run,) = two_repo_run["runs"]
    assert run["indexed"] == len(two_repo_run["documents"]) and run["failed"] == 3
    assert {f["id"].split(":platform:")[0] for f in run["failures"]} == {"bad"}
    assert {f["reason"] for f in run["failures"]} == {"HTTP 401"}


def test_one_repository_refused_does_not_mark_the_whole_run_failed(two_repo_run, monkeypatch):
    """The freshness report skips a run with `error` for EVERY repository it
    covered: setting it here would make `good`, which was indexed, read as
    not indexed."""
    _refuse_everything_of(monkeypatch, "/bad")

    index_platform.main([])

    (run,) = two_repo_run["runs"]
    assert not run.get("error")


def test_the_run_covers_only_the_repositories_that_answered(two_repo_run, monkeypatch):
    _refuse_everything_of(monkeypatch, "/bad")

    index_platform.main([])

    (run,) = two_repo_run["runs"]
    assert run["repo_paths"] == [two_repo_run["good"]]


def test_freshness_counts_the_run_for_the_repository_that_answered_only(two_repo_run, monkeypatch):
    """Through the freshness report, with heads as log_run_summary records
    them from the run's repo_paths."""
    from griot import freshness
    _refuse_everything_of(monkeypatch, "/bad")
    index_platform.main([])
    (run,) = two_repo_run["runs"]
    recorded = {**run, "timestamp": "2026-10-06T00:00:00",
                "heads": {Path(p).name: "a" * 40 for p in run["repo_paths"]}}
    repositories = [{"name": name, "path": two_repo_run[name], "head": "a" * 40, "refs": {}} for name in ("good", "bad")]

    good, bad = freshness.assess([recorded], repositories)

    assert "platform" not in good["missing_sources"]
    assert "platform" in bad["missing_sources"]


def test_one_repository_refused_and_nothing_to_index_elsewhere_still_fails(two_repo_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda platform, project_id, host=None: [])
    _refuse_everything_of(monkeypatch, "/bad")

    rc = index_platform.main([])

    assert rc == 1
    (run,) = two_repo_run["runs"]
    assert run["failed"] == 3 and not run.get("error")
    assert run["repo_paths"] == [two_repo_run["good"]]


def test_one_repository_refused_fails_a_dry_run_and_records_nothing(two_repo_run, monkeypatch):
    _refuse_everything_of(monkeypatch, "/bad")
    monkeypatch.setattr(index_platform.common, "dry_run", lambda documents, **kwargs: None)

    assert index_platform.main(["--dry-run"]) == 1
    assert two_repo_run["runs"] == []


def test_a_repository_refused_in_part_does_not_fail_the_run(two_repo_run, monkeypatch):
    """Some of `bad`'s fetches answered: its refusals are failures of the
    run, not a repository the run could not index."""
    monkeypatch.setattr(index_platform.platforms, "fetch_issues",
                        _refuse_for("/bad", index_platform.platforms.fetch_issues))

    assert not index_platform.main([])
    (run,) = two_repo_run["runs"]
    assert run["failed"] == 1
    assert sorted(run["repo_paths"]) == sorted([two_repo_run["good"], two_repo_run["bad"]])


def test_a_repository_on_no_known_platform_is_not_one_refused(two_repo_run, monkeypatch):
    """No fetch was attempted for it: nothing was refused."""
    monkeypatch.setattr(index_platform.platforms, "detect_platform",
                        lambda url: None if "/bad" in url else ("github", "group/good", None))

    assert not index_platform.main([])


# --- the refused repository stays visible after the run -----------------------
#
# [debt 17 follow-up] The run above exits 1 and says so on stderr, and that
# is all: the record held the refused repository only as failure ids, and
# `griot doctor` and `griot stats` said nothing of it.

def test_the_run_records_which_repository_the_platform_refused(two_repo_run, monkeypatch):
    """By name, explicitly: deriving it from the failure ids would break
    once more than MAX_RECORDED_FAILURES fetches were refused, and under
    --path the ids carry another key than the repository's name."""
    _refuse_everything_of(monkeypatch, "/bad")

    index_platform.main([])

    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["bad"]


def test_the_refused_repository_is_recorded_when_nothing_else_was_indexed_either(two_repo_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda platform, project_id, host=None: [])
    _refuse_everything_of(monkeypatch, "/bad")

    index_platform.main([])

    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["bad"]


# [debt 19] When the platform refused every fetch of the whole run, the run
# was recorded with `error` and without `refused_repos`: `griot doctor` and
# `griot stats` named the refused repositories only while that run was the
# newest of all, and a later code or commits run hid it.

def test_a_run_the_platform_refused_entirely_records_every_refused_repository(two_repo_run, monkeypatch):
    _refuse_everything_of(monkeypatch, "/bad")
    _refuse_everything_of(monkeypatch, "/good")

    assert index_platform.main([]) == 1

    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["good", "bad"]
    # Still a run that did not do its job: freshness does not count it.
    assert "refused every fetch" in run["error"]


def test_a_run_refused_entirely_does_not_record_a_repository_it_asked_nothing_of(two_repo_run, monkeypatch):
    """`good` is on no known platform: no fetch was attempted for it, so
    nothing of it was refused."""
    monkeypatch.setattr(index_platform.platforms, "detect_platform",
                        lambda url: None if "/good" in url else ("github", "group/bad", None))
    _refuse_everything_of(monkeypatch, "/bad")

    assert index_platform.main([]) == 1

    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["bad"] and run["error"]


def test_a_run_refused_entirely_under_path_records_the_repository_by_its_name(platform_run, monkeypatch):
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, _refused(401))

    index_platform.main(["--path", platform_run["path"]])

    (run,) = platform_run["runs"]
    assert run["refused_repos"] == [Path(platform_run["path"]).name]


def test_a_repository_refused_in_part_is_not_recorded_as_refused(two_repo_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_issues",
                        _refuse_for("/bad", index_platform.platforms.fetch_issues))

    index_platform.main([])

    (run,) = two_repo_run["runs"]
    assert not run.get("refused_repos")


def test_every_repository_name_printed_is_made_printable(tmp_path, monkeypatch, capsys):
    """A directory name can hold an escape sequence, which would drive the
    terminal the run prints to."""
    seen = _two_repos(tmp_path, monkeypatch, bad_name="bad\x1b[2Jx")
    monkeypatch.setattr(index_platform.common, "load_repos", lambda: [seen["good"], seen["bad"], str(tmp_path / "gone\x1b[2J")])
    _refuse_everything_of(monkeypatch, "Jx")

    index_platform.main([])

    out, err = capsys.readouterr()
    assert "\x1b" not in out and "\x1b" not in err
    assert "bad?[2Jx" in err and "bad?[2Jx" in out and "gone?[2J" in out


def test_a_repository_name_asked_for_and_not_found_is_printed_printable(two_repo_run, monkeypatch, capsys):
    assert index_platform.main(["--repo", "x\x1b[2J"]) == 1

    err = capsys.readouterr().err
    assert "\x1b" not in err and "x?[2J" in err


def test_the_lines_of_a_repository_that_answered_are_printable(tmp_path, monkeypatch, capsys):
    _two_repos(tmp_path, monkeypatch, bad_name="bad\x1b[2Jx")

    index_platform.main([])

    out = capsys.readouterr().out
    assert "\x1b" not in out and "  bad?[2Jx [github:group/bad?[2Jx]: " in out


def test_paths_that_are_not_directories_are_printed_printable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(index_platform.common, "load_repos", lambda: [str(tmp_path / "gone\x1b[2J")])

    assert index_platform.main([]) == 1

    err = capsys.readouterr().err
    assert "\x1b" not in err and "gone?[2J" in err


@pytest.mark.parametrize("no_remote", [True, False], ids=["no remote", "unrecognized remote"])
def test_a_repository_skipped_for_its_remote_is_named_printable(tmp_path, monkeypatch, capsys, no_remote):
    _two_repos(tmp_path, monkeypatch, bad_name="bad\x1b[2Jx")
    if no_remote:
        monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: None)
    else:
        monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: None)

    index_platform.main([])

    out = capsys.readouterr().out
    assert "\x1b" not in out and "bad?[2Jx" in out


# --- GitLab read without a token (public projects) -------------------------


@pytest.fixture
def gitlab_run(platform_run, monkeypatch):
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: "https://gitlab.com/group/project.git")
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: ("gitlab", "group/project", None))
    monkeypatch.delenv("GITLAB_PERSONAL_ACCESS_TOKEN", raising=False)
    return platform_run


def test_a_gitlab_run_without_a_token_says_it_read_public_data_only(gitlab_run, capsys):
    index_platform.main(["--path", gitlab_run["path"]])

    out = capsys.readouterr().out
    assert "without a token" in out and "public" in out


def test_a_gitlab_run_with_a_token_says_nothing_about_reading_without_one(gitlab_run, monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")

    index_platform.main(["--path", gitlab_run["path"]])

    assert "without a token" not in capsys.readouterr().out


@pytest.mark.parametrize("status", [401, 404])
def test_a_private_gitlab_project_without_a_token_is_a_recorded_refusal(gitlab_run, monkeypatch, capsys, status):
    """GitLab hides a private project from an anonymous caller (404, or 401):
    the run fails and is recorded like any other refusal, and the screen says
    which variable to set and how, without a traceback."""
    response = requests.Response()
    response.status_code = status

    def hidden(platform, project_id, host=None):
        raise index_platform.platforms.TokenNeeded(
            f"GitLab answered HTTP {status} to a request made without a token; "
            "set GITLAB_PERSONAL_ACCESS_TOKEN with `griot auth set gitlab`.",
            response=response)
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, hidden)
    monkeypatch.setattr(index_platform.common, "credential_hint", lambda env_var: "HINT-LINE")

    rc = index_platform.main(["--path", gitlab_run["path"]])

    captured = capsys.readouterr()
    assert rc == 1
    assert "griot auth set gitlab" in captured.out
    # The error already says what to do; the generic hint would repeat it.
    assert "HINT-LINE" not in captured.out
    (run,) = gitlab_run["runs"]
    assert {f["reason"] for f in run["failures"]} == {f"HTTP {status}"}
    assert "refused" in run["error"]
    assert run["refused_repos"]


def test_a_platform_item_url_is_stored_when_the_platform_gives_one(gitlab_run, monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda platform, project_id, host=None: [
        {"iid": 1, "title": "with", "url": "https://gitlab.com/group/project/-/merge_requests/1"},
        {"iid": 2, "title": "without", "url": None},
    ])
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", lambda platform, project_id, host=None: [
        {"iid": 3, "title": "issue", "url": "https://gitlab.com/group/project/-/work_items/3"}])
    monkeypatch.setattr(index_platform.platforms, "fetch_releases", lambda platform, project_id, host=None: [
        {"tag_name": "v1", "name": "v1", "url": "https://gitlab.com/group/project/-/releases/v1"}])

    index_platform.main(["--path", gitlab_run["path"]])

    by_title = {d["content"].split("\n")[0]: d["metadata"] for d in gitlab_run["documents"]}
    assert by_title["with"]["url"] == "https://gitlab.com/group/project/-/merge_requests/1"
    # Absent, not null: the other platforms' points stay as they were.
    assert "url" not in by_title["without"]
    assert by_title["issue"]["url"].endswith("/work_items/3")
    assert by_title["v1"]["url"].endswith("/releases/v1")


# --- what the closing message blames ------------------------------------------
#
# [debt 67] With a valid GitLab token, a project that does not exist or that
# the token cannot see answers HTTP 404 to every fetch, and the run ended
# with "Check the token (`griot auth list`)": the wrong cause, seen for real
# on 2026-10-08. The closing message now follows what was refused: a 404
# under a token names the project and the remote it came from; a 401/403
# keeps pointing at the token; a mix says which is which. The rule is the
# platform's answer, not GitLab's: GitHub, Bitbucket, Azure DevOps and Gitea
# answer 404 too for a repository the token cannot see.

# Taken at import, before any fixture replaces them on the module.
_REAL_PLATFORM_FUNCTIONS = {name: getattr(index_platform.platforms, name) for name in (
    "detect_platform", "fetch_pull_requests", "fetch_releases", "fetch_issues")}


def _refuse_all(monkeypatch, status=None, by_label=None, error=None):
    """Every fetch refused: with `status`, or per fetch function name in
    `by_label`, or by raising `error` (an exception, not an HTTP answer)."""
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        if error is not None:
            def fetch(platform, project_id, host=None, _e=error):
                raise _e
        else:
            fetch = _refused((by_label or {}).get(name, status), message="404 Client Error")
        monkeypatch.setattr(index_platform.platforms, name, fetch)


@pytest.mark.parametrize("platform,env_var,remote", [
    ("gitlab", "GITLAB_PERSONAL_ACCESS_TOKEN", "https://gitlab.com/group/project.git"),
    ("github", "GITHUB_TOKEN", "git@github.com:group/project.git"),
    ("bitbucket", "BITBUCKET_ACCESS_TOKEN", "git@bitbucket.org:group/project.git"),
])
def test_every_fetch_404_under_a_token_names_the_project_not_the_token(platform_run, monkeypatch, capsys,
                                                                     platform, env_var, remote):
    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: remote)
    monkeypatch.setattr(index_platform.platforms, "detect_platform", lambda url: (platform, "group/project", None))
    monkeypatch.setenv(env_var, "fake-token")
    _refuse_all(monkeypatch, status=404)

    rc = index_platform.main(["--path", platform_run["path"]])

    err = capsys.readouterr().err
    assert rc == 1
    assert "group/project" in err and remote in err
    assert "not found" in err and "not visible to the token" in err
    assert "Check the token" not in err
    (run,) = platform_run["runs"]
    name = Path(platform_run["path"]).name
    assert run["not_found_repos"] == [name] and run["refused_repos"] == [name]
    assert "not found" in run["error"] and "HTTP 404" in run["error"]


@pytest.mark.parametrize("status", [401, 403])
def test_every_fetch_refused_with_401_or_403_still_points_at_the_token(gitlab_run, monkeypatch, capsys, status):
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")
    _refuse_all(monkeypatch, status=status)

    assert index_platform.main(["--path", gitlab_run["path"]]) == 1

    err = capsys.readouterr().err
    assert "Check the token (`griot auth list`)" in err
    assert "not found" not in err
    (run,) = gitlab_run["runs"]
    assert "not_found_repos" not in run
    assert run["error"] == f"the platform refused every fetch (HTTP {status})"


def test_a_404_without_a_token_in_the_environment_is_not_called_not_found(platform_run, monkeypatch, capsys):
    """Only a token that was sent can be one the project is invisible to:
    with none set, the 404 says nothing about the project."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _refuse_all(monkeypatch, status=404)

    index_platform.main(["--path", platform_run["path"]])

    err = capsys.readouterr().err
    assert "not visible to the token" not in err and "Check the token" in err
    assert "not_found_repos" not in platform_run["runs"][0]


def test_a_gitlab_token_needed_404_is_not_called_not_found(gitlab_run, monkeypatch, capsys):
    """Without a token GitLab's 404 is a TokenNeeded: the token is the fix."""
    response = requests.Response()
    response.status_code = 404

    def hidden(platform, project_id, host=None):
        raise index_platform.platforms.TokenNeeded("set GITLAB_PERSONAL_ACCESS_TOKEN", response=response)
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, hidden)

    index_platform.main(["--path", gitlab_run["path"]])

    err = capsys.readouterr().err
    assert "not visible to the token" not in err and "Check the token" in err


def test_a_token_needed_is_the_token_s_fault_whatever_the_environment_says(gitlab_run, monkeypatch, capsys):
    """TokenNeeded is the adapter's own word that the request went without
    a token, so its 404 is never read as a project the token cannot see."""
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")
    response = requests.Response()
    response.status_code = 404

    def hidden(platform, project_id, host=None):
        raise index_platform.platforms.TokenNeeded("set GITLAB_PERSONAL_ACCESS_TOKEN", response=response)
    for name in ("fetch_pull_requests", "fetch_releases", "fetch_issues"):
        monkeypatch.setattr(index_platform.platforms, name, hidden)

    index_platform.main(["--path", gitlab_run["path"]])

    err = capsys.readouterr().err
    assert "not visible to the token" not in err and "Check the token" in err


def test_a_missing_token_that_stops_before_any_request_points_at_the_token(platform_run, monkeypatch, capsys):
    """The adapters that require a token raise before asking anything: no
    HTTP status, and the token is what to fix."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _refuse_all(monkeypatch, error=ValueError("GITHUB_TOKEN not found in the environment"))

    assert index_platform.main(["--path", platform_run["path"]]) == 1

    err = capsys.readouterr().err
    assert "Check the token" in err and "ValueError" in err


def test_a_mix_of_404_and_403_says_which_fetch_got_which(gitlab_run, monkeypatch, capsys):
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")
    _refuse_all(monkeypatch, by_label={"fetch_pull_requests": 404, "fetch_releases": 404, "fetch_issues": 403})

    assert index_platform.main(["--path", gitlab_run["path"]]) == 1

    err = capsys.readouterr().err
    assert "group/project" in err
    not_found_line = next(line for line in err.splitlines() if "not visible to the token" in line)
    token_line = next(line for line in err.splitlines() if "Check the token" in line)
    assert "merge/pull requests" in not_found_line and "releases" in not_found_line and "issues" not in not_found_line
    assert "issues" in token_line and "HTTP 403" in token_line
    (run,) = gitlab_run["runs"]
    # Not all of it a 404: the token is part of the cause, not ruled out.
    assert "not_found_repos" not in run


def test_a_refusal_that_is_no_http_answer_does_not_blame_the_token(platform_run, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    _refuse_all(monkeypatch, error=requests.ConnectionError("no route"))

    assert index_platform.main(["--path", platform_run["path"]]) == 1

    err = capsys.readouterr().err
    assert "Check the token" not in err and "not found" not in err
    assert "ConnectionError" in err and "warnings above" in err


def test_the_remote_is_named_without_the_credentials_written_in_it(gitlab_run, monkeypatch, capsys):
    monkeypatch.setattr(index_platform, "_remote_url",
                        lambda repo_path: "https://oauth2:hunter2@gitlab.com/group/project.git")
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")
    _refuse_all(monkeypatch, status=404)

    index_platform.main(["--path", gitlab_run["path"]])

    err = capsys.readouterr().err
    assert "https://gitlab.com/group/project.git" in err and "hunter2" not in err


def test_one_repository_404_among_others_names_its_project(two_repo_run, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    _refuse_everything_of(monkeypatch, "/bad", status=404)

    assert index_platform.main([]) == 1

    err = capsys.readouterr().err
    assert "refused every fetch for bad" in err
    assert "group/bad" in err and "git@github.com:group/bad.git" in err and "not visible to the token" in err
    assert "Check the token" not in err
    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["bad"] and run["not_found_repos"] == ["bad"]


@pytest.mark.parametrize("platform,env_var,remote,status,blamed", [
    ("gitlab", "GITLAB_PERSONAL_ACCESS_TOKEN", "https://gitlab.com/group/project.git", 404, "not visible to the token"),
    ("gitlab", "GITLAB_PERSONAL_ACCESS_TOKEN", "https://gitlab.com/group/project.git", 401, "Check the token"),
    ("github", "GITHUB_TOKEN", "git@github.com:group/project.git", 404, "not visible to the token"),
    ("github", "GITHUB_TOKEN", "git@github.com:group/project.git", 403, "Check the token"),
])
def test_the_real_adapters_answer_over_http_lead_to_the_right_cause(platform_run, monkeypatch, capsys,
                                                                   platform, env_var, remote, status, blamed):
    """Through the real adapters, with only HTTP faked: each fetch the
    platform answers with `status`, under a token."""
    from griot import platforms

    monkeypatch.setattr(index_platform, "_remote_url", lambda repo_path: remote)
    for name, function in _REAL_PLATFORM_FUNCTIONS.items():
        monkeypatch.setattr(platforms, name, function)
    monkeypatch.setenv(env_var, "fake-token")
    monkeypatch.delenv("GRIOT_GITLAB_API_BASE", raising=False)

    def answer(url, **kwargs):
        response = requests.Response()
        response.status_code = status
        response.url = url
        return response
    monkeypatch.setattr(platforms.requests, "get", answer)

    assert index_platform.main(["--path", platform_run["path"]]) == 1

    err = capsys.readouterr().err
    assert blamed in err and "group/project" in err


def test_only_the_repositories_all_404_are_recorded_as_not_found(two_repo_run, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    _refuse_everything_of(monkeypatch, "/bad", status=404)
    _refuse_everything_of(monkeypatch, "/good", status=401)

    assert index_platform.main([]) == 1

    err = capsys.readouterr().err
    assert "group/bad" in err and "Check the token" in err
    (run,) = two_repo_run["runs"]
    assert run["refused_repos"] == ["good", "bad"] and run["not_found_repos"] == ["bad"]
    # A mixed run's error names both causes.
    assert "HTTP 401" in run["error"] and "HTTP 404" in run["error"]
