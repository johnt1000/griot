"""Tests for griot.platforms — platform detection from the 'origin' remote
and normalized fetching of PRs/MRs, releases, and issues on GitHub/GitLab/
Bitbucket/Azure DevOps/Gitea. No real network calls (requests.get is always
mocked) — see the warning at the top of platforms.py: these 5 platforms
have never been tested against a real account with a token in this session.
"""

import requests

from griot import platforms


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []
        self.headers = headers or {}
        self.links = {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err


# --- platform detection -------------------------------------------------


def test_detect_platform_github_ssh():
    assert platforms.detect_platform("git@github.com:owner/repo.git") == ("github", "owner/repo", None)


def test_detect_platform_github_https():
    assert platforms.detect_platform("https://github.com/owner/repo") == ("github", "owner/repo", None)


def test_detect_platform_gitlab_self_hosted(monkeypatch):
    # self-hosted instance is recognized by the host from GRIOT_GITLAB_API_BASE
    # (hardening M4: detection by exact host, no "gitlab" substring)
    monkeypatch.setenv("GRIOT_GITLAB_API_BASE", "https://gitlab.example.com/api/v4")
    assert platforms.detect_platform("git@gitlab.example.com:group/sub/repo.git") == ("gitlab", "group/sub/repo", None)


def test_detect_platform_bitbucket():
    assert platforms.detect_platform("git@bitbucket.org:workspace/repo.git") == ("bitbucket", "workspace/repo", None)


def test_detect_platform_azure_devops_https():
    result = platforms.detect_platform("https://dev.azure.com/myorg/myproject/_git/myrepo")
    assert result == ("azure_devops", "myorg/myproject/myrepo", None)


def test_detect_platform_azure_devops_ssh():
    result = platforms.detect_platform("git@ssh.dev.azure.com:v3/myorg/myproject/myrepo")
    assert result == ("azure_devops", "myorg/myproject/myrepo", None)


def test_detect_platform_azure_devops_missing_segments_returns_none():
    """URL without the 3 expected segments (org/project/repo) -> not recognized,
    not a crash trying to unpack fewer than 3 parts."""
    assert platforms.detect_platform("https://dev.azure.com/onlyorg") is None


def test_detect_platform_gitea_requires_explicit_host_config(monkeypatch):
    monkeypatch.delenv("GRIOT_GITEA_HOSTS", raising=False)
    assert platforms.detect_platform("git@git.mycompany.com:owner/repo.git") is None


def test_detect_platform_gitea_with_host_configured(monkeypatch):
    monkeypatch.setenv("GRIOT_GITEA_HOSTS", "git.mycompany.com")
    result = platforms.detect_platform("git@git.mycompany.com:owner/repo.git")
    assert result == ("gitea", "owner/repo", "git.mycompany.com")


def test_detect_platform_unknown_host_returns_none(monkeypatch):
    monkeypatch.delenv("GRIOT_GITEA_HOSTS", raising=False)
    assert platforms.detect_platform("git@example.com:owner/repo.git") is None


# --- GitHub ------------------------------------------------------------


def test_github_fetch_pull_requests_maps_fields(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(200, [{
            "number": 42, "title": "Fix bug", "body": "description here",
            "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
            "user": {"login": "john"}, "created_at": "2025-12-01T00:00:00Z",
            "head": {"ref": "fix-bug"}, "base": {"ref": "main"},
        }])

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.github_fetch_pull_requests("owner/repo")

    assert result == [{
        "iid": 42, "title": "Fix bug", "description": "description here",
        "state": "merged", "author": "john", "created_at": "2025-12-01T00:00:00Z",
        "source_branch": "fix-bug", "target_branch": "main",
    }]


def test_github_fetch_pull_requests_open_state_not_overridden(monkeypatch):
    """Field finding: GitHub's 'state' only becomes 'merged' when merged_at
    is present — an open or closed-without-merge PR keeps the original state."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(200, [{"number": 1, "title": "x", "state": "open", "merged_at": None}])

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.github_fetch_pull_requests("owner/repo")

    assert result[0]["state"] == "open"


def test_github_fetch_issues_filters_out_pull_requests(monkeypatch):
    """Real finding (documented in platforms.py): GitHub's /issues returns
    PRs mixed in — an item with a 'pull_request' key is a PR, not an issue."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(200, [
            {"number": 1, "title": "real issue", "state": "open", "user": {"login": "a"}, "created_at": "t"},
            {"number": 2, "title": "this is a PR", "state": "open", "pull_request": {"url": "..."}},
        ])

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.github_fetch_issues("owner/repo")

    assert len(result) == 1
    assert result[0]["iid"] == 1


def test_github_paginated_get_follows_link_header(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if len(calls) == 1:
            resp = FakeResponse(200, [{"number": 1, "title": "a", "state": "open"}])
            resp.links = {"next": {"url": "https://api.github.com/repos/owner/repo/pulls?page=2"}}
            return resp
        return FakeResponse(200, [{"number": 2, "title": "b", "state": "open"}])

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")
    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.github_fetch_pull_requests("owner/repo")

    assert len(calls) == 2
    assert [r["iid"] for r in result] == [1, 2]


def test_github_fetch_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    try:
        platforms.github_fetch_pull_requests("owner/repo")
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "GITHUB_TOKEN" in str(e)


# --- GitLab (migrated from common.py, same behavior) --------------------


def test_gitlab_fetch_pull_requests_maps_fields(monkeypatch):
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(200, [{
            "iid": 5, "title": "test MR", "description": "desc",
            "state": "opened", "author": {"username": "mary"},
            "created_at": "2025-01-01", "source_branch": "feature", "target_branch": "main",
        }], headers={"x-total-pages": "1"})

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.gitlab_fetch_pull_requests("group/project")

    assert result == [{
        "iid": 5, "title": "test MR", "description": "desc", "state": "opened",
        "author": "mary", "created_at": "2025-01-01",
        "source_branch": "feature", "target_branch": "main",
    }]


def test_gitlab_fetch_requires_token(monkeypatch):
    monkeypatch.delenv("GITLAB_PERSONAL_ACCESS_TOKEN", raising=False)
    try:
        platforms.gitlab_fetch_pull_requests("group/project")
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "GITLAB_PERSONAL_ACCESS_TOKEN" in str(e)


# --- Bitbucket ------------------------------------------------------------


def test_bitbucket_fetch_pull_requests_queries_all_four_states(monkeypatch):
    """Design finding (no real API to confirm against): Bitbucket Cloud
    requires querying by state — confirms that all 4 states are queried."""
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "fake-token")
    seen_states = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        seen_states.append(params.get("state"))
        return FakeResponse(200, {"values": [], "next": None})

    monkeypatch.setattr(requests, "get", fake_get)
    platforms.bitbucket_fetch_pull_requests("workspace/repo")

    assert set(seen_states) == {"OPEN", "MERGED", "DECLINED", "SUPERSEDED"}


def test_bitbucket_fetch_pull_requests_maps_fields(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        if params.get("state") != "OPEN":
            return FakeResponse(200, {"values": [], "next": None})
        return FakeResponse(200, {"values": [{
            "id": 7, "title": "bitbucket PR", "description": "desc", "state": "OPEN",
            "author": {"display_name": "Anna"}, "created_on": "2025-01-01",
            "source": {"branch": {"name": "feature"}}, "destination": {"branch": {"name": "main"}},
        }], "next": None})

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.bitbucket_fetch_pull_requests("workspace/repo")

    assert result == [{
        "iid": 7, "title": "bitbucket PR", "description": "desc", "state": "OPEN",
        "author": "Anna", "created_at": "2025-01-01",
        "source_branch": "feature", "target_branch": "main",
    }]


def test_bitbucket_fetch_pull_requests_follows_cursor_pagination(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if "page=2" in url:
            return FakeResponse(200, {"values": [{"id": 2, "title": "b"}], "next": None})
        if params and params.get("state") == "OPEN":
            return FakeResponse(200, {"values": [{"id": 1, "title": "a"}], "next": "https://api.bitbucket.org/2.0/repositories/w/r/pullrequests?page=2"})
        return FakeResponse(200, {"values": [], "next": None})

    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "fake-token")
    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.bitbucket_fetch_pull_requests("w/r")

    ids = [r["iid"] for r in result]
    assert 1 in ids and 2 in ids


def test_bitbucket_fetch_releases_returns_empty():
    """No real API to try against — Bitbucket Cloud has no structured
    release resource (documented in platforms.py)."""
    assert platforms.bitbucket_fetch_releases("workspace/repo") == []


def test_bitbucket_fetch_issues_404_means_no_issue_tracker(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(404, {})

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.bitbucket_fetch_issues("workspace/repo")

    assert result == []


def test_bitbucket_fetch_issues_other_http_error_propagates(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(500, {})

    monkeypatch.setattr(requests, "get", fake_get)
    try:
        platforms.bitbucket_fetch_issues("workspace/repo")
        assert False, "should propagate the 500 error"
    except requests.HTTPError:
        pass


# --- Azure DevOps ------------------------------------------------------------


def test_azure_fetch_pull_requests_maps_fields(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "fake-token")

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        assert auth == ("", "fake-token")
        return FakeResponse(200, {"value": [{
            "pullRequestId": 3, "title": "azure PR", "description": "desc", "status": "active",
            "createdBy": {"displayName": "Peter"}, "creationDate": "2025-01-01",
            "sourceRefName": "refs/heads/feature", "targetRefName": "refs/heads/main",
        }]})

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.azure_fetch_pull_requests("org/project/repo")

    assert result == [{
        "iid": 3, "title": "azure PR", "description": "desc", "state": "active",
        "author": "Peter", "created_at": "2025-01-01",
        "source_branch": "feature", "target_branch": "main",
    }]


def test_azure_fetch_pull_requests_requires_pat(monkeypatch):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    try:
        platforms.azure_fetch_pull_requests("org/project/repo")
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "AZURE_DEVOPS_PAT" in str(e)


def test_azure_fetch_releases_and_issues_return_empty():
    """Out of scope by design (documented in platforms.py) — releases
    live in Azure Pipelines, issues are Azure Boards work items, neither
    is 1:1 with the git repo."""
    assert platforms.azure_fetch_releases("org/project/repo") == []
    assert platforms.azure_fetch_issues("org/project/repo") == []


# --- Gitea/Forgejo ------------------------------------------------------------


def test_gitea_fetch_pull_requests_maps_fields(monkeypatch):
    # [real finding] _gitea_paginated_get only stops on an EMPTY page — a fake
    # that always returns items with no empty follow-up page causes a real
    # infinite loop, consuming unbounded memory (all_items.extend on every
    # iteration). An earlier version of this test didn't simulate the empty
    # follow-up page and froze the machine from real memory pressure, not a
    # test finding.
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        assert "git.mycompany.com" in url
        calls["n"] += 1
        if calls["n"] > 1:
            return FakeResponse(200, [])
        return FakeResponse(200, [{
            "number": 9, "title": "gitea PR", "body": "desc", "merged": True,
            "user": {"login": "carol"}, "created_at": "2025-01-01",
            "head": {"ref": "feature"}, "base": {"ref": "main"},
        }])

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.gitea_fetch_pull_requests("owner/repo", "git.mycompany.com")

    assert result == [{
        "iid": 9, "title": "gitea PR", "description": "desc", "state": "merged",
        "author": "carol", "created_at": "2025-01-01",
        "source_branch": "feature", "target_branch": "main",
    }]


def test_gitea_fetch_issues_filters_via_type_param(monkeypatch):
    """Unlike GitHub: Gitea filters out PRs server-side via type=issues, no
    need to filter the response manually."""
    monkeypatch.setenv("GITEA_TOKEN", "fake-token")
    seen_params = {}

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        seen_params.update(params)
        return FakeResponse(200, [])

    monkeypatch.setattr(requests, "get", fake_get)
    platforms.gitea_fetch_issues("owner/repo", "git.mycompany.com")

    assert seen_params["type"] == "issues"


def test_gitea_fetch_requires_token(monkeypatch):
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    try:
        platforms.gitea_fetch_pull_requests("owner/repo", "git.mycompany.com")
        assert False, "should raise ValueError"
    except ValueError as e:
        assert "GITEA_TOKEN" in str(e)


# --- generic fetch_*() dispatch --------------------------------------------


def test_fetch_pull_requests_dispatches_by_platform(monkeypatch):
    monkeypatch.setattr(platforms, "github_fetch_pull_requests", lambda project_id: [{"marker": "github"}])
    assert platforms.fetch_pull_requests("github", "owner/repo") == [{"marker": "github"}]


def test_fetch_pull_requests_dispatches_gitea_with_host(monkeypatch):
    seen = {}

    def fake_gitea_fetch(project_id, host):
        seen["project_id"] = project_id
        seen["host"] = host
        return []

    monkeypatch.setattr(platforms, "gitea_fetch_pull_requests", fake_gitea_fetch)
    platforms.fetch_pull_requests("gitea", "owner/repo", host="git.mycompany.com")

    assert seen == {"project_id": "owner/repo", "host": "git.mycompany.com"}
