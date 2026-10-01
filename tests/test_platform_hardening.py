"""Hardening of the platform adapters (M3/M4/M5 findings from the security
audit, 2026-08-19):

- M3: "next page" URLs come from the SERVER (Link header on GitHub, response
  body on Bitbucket) and were being followed with the token still attached,
  without checking the host — a malicious/compromised response could point
  the loop anywhere, taking the credential along. There was also no page cap
  in any of the 4 paginators.
- M4: the project_id extracted from the remote was interpolated raw into the
  API URL (only GitLab escaped it), and platform detection was a substring
  check on the whole remote ("github.com" in url) — a crafted remote could
  redirect the authenticated request or match the wrong platform.
- M5: requests' default allow_redirects follows cross-host redirects; custom
  headers (PRIVATE-TOKEN) and query params are NOT stripped by requests in
  that case — the credential would travel to the new host.
"""

import pytest
import requests

from griot import platforms


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, links=None, headers=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else []
        self.links = links or {}
        self.headers = headers or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


# --- M4: detection by exact hostname, not substring -------------------------


def test_github_detection_rejects_lookalike_host():
    assert platforms.detect_platform("git@github.com.evil.net:owner/repo.git") is None
    assert platforms.detect_platform("https://notgithub.com/owner/repo") is None


def test_bitbucket_detection_rejects_lookalike_host():
    assert platforms.detect_platform("git@bitbucket.org.evil.net:w/repo.git") is None


def test_gitea_detection_requires_exact_host(monkeypatch):
    monkeypatch.setenv("GRIOT_GITEA_HOSTS", "git.minhaempresa.com")
    assert platforms.detect_platform("git@git.minhaempresa.com.evil.net:o/r.git") is None


def test_gitlab_detection_is_not_generic_substring(monkeypatch):
    """"gitlab" appearing anywhere in the URL can't be enough — only gitlab.com
    or the host configured in GRIOT_GITLAB_API_BASE."""
    monkeypatch.delenv("GRIOT_GITLAB_API_BASE", raising=False)
    assert platforms.detect_platform("git@meugitlabfake.evil.net:o/r.git") is None
    assert platforms.detect_platform("git@gitlab.com:grupo/repo.git") == ("gitlab", "grupo/repo", None)


def test_gitlab_self_hosted_via_api_base_env(monkeypatch):
    monkeypatch.setenv("GRIOT_GITLAB_API_BASE", "https://gitlab.example.com/api/v4")
    assert platforms.detect_platform("git@gitlab.example.com:group/sub/repo.git") == ("gitlab", "group/sub/repo", None)


# --- M4: project_id validated/encoded before becoming a URL path -------------


def _forbid_network(monkeypatch):
    def no_request(*args, **kwargs):
        raise AssertionError("should not have made any request — validation comes first")

    monkeypatch.setattr(requests, "get", no_request)


def test_github_rejects_path_traversal_project_id(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    _forbid_network(monkeypatch)
    with pytest.raises(ValueError):
        platforms.github_fetch_pull_requests("../../malicioso")


def test_github_encodes_special_chars_in_project_id(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    captured = {}

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        captured["url"] = url
        return FakeResponse(200, [])

    monkeypatch.setattr(requests, "get", fake_get)
    platforms.github_fetch_pull_requests("owner/repo name?x=1")
    assert "repo%20name%3Fx%3D1" in captured["url"]


def test_azure_rejects_dotdot_segments(monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "t")
    _forbid_network(monkeypatch)
    with pytest.raises(ValueError):
        platforms.azure_fetch_pull_requests("org/../repo")


# --- M3: next_url from a different host is refused -------------------------------


def test_github_pagination_refuses_cross_host_next_url(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(200, [{"number": 1}], links={"next": {"url": "https://evil.example.com/steal"}})
        return FakeResponse(200, [])

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError):
        platforms.github_fetch_pull_requests("owner/repo")
    assert len(calls) == 1  # never followed to the strange host


def test_bitbucket_pagination_refuses_cross_host_next_url(monkeypatch):
    monkeypatch.setenv("BITBUCKET_ACCESS_TOKEN", "t")
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(200, {"values": [{"id": 1}], "next": "https://evil.example.com/steal"})
        return FakeResponse(200, {"values": []})

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError):
        platforms.bitbucket_fetch_issues("w/repo")
    assert len(calls) == 1


def test_gitea_pagination_has_page_cap(monkeypatch):
    """Without a cap, an API that never returns an empty page (bug/malice)
    runs the loop forever — this already happened for real in a test mock
    (real infinite loop). The fake fails explosively after
    an absurd number of calls so this test's RED is a fast error, not a
    hung test."""
    monkeypatch.setenv("GITEA_TOKEN", "t")
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls["n"] += 1
        if calls["n"] > 1000:
            raise RuntimeError("unbounded pagination: >1000 pages in a row — infinite loop")
        return FakeResponse(200, [{"number": 1}])  # never empty

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.gitea_fetch_issues("o/r", "git.example.com")
    assert calls["n"] <= platforms.MAX_PAGES
    assert len(result) <= platforms.MAX_PAGES * 50  # stopped at the cap, not infinite


# --- M5: redirects are not followed with the credential ----------------------------


def test_get_with_retry_does_not_follow_redirects(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        captured["allow_redirects"] = allow_redirects
        return FakeResponse(200, [])

    monkeypatch.setattr(requests, "get", fake_get)
    platforms._get_with_retry("https://api.github.com/x", {})
    assert captured["allow_redirects"] is False


def test_get_with_retry_raises_clear_error_on_redirect(monkeypatch):
    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        return FakeResponse(301, [], headers={"Location": "https://outro.host/x"})

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(requests.HTTPError):
        platforms._get_with_retry("https://api.github.com/x", {})


# --- code review follow-ups (2026-08-19) --------------------------------


def test_pagination_refuses_scheme_downgrade_same_host(monkeypatch):
    """Same host but http:// — the Bearer would go over plaintext."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if len(calls) == 1:
            return FakeResponse(200, [{"number": 1}], links={"next": {"url": "http://api.github.com/x?page=2"}})
        return FakeResponse(200, [])

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError):
        platforms.github_fetch_pull_requests("owner/repo")
    assert len(calls) == 1


def test_gitlab_pagination_without_total_pages_header_does_not_truncate(monkeypatch):
    """GitLab omits x-total-pages on large/keyset collections — the old
    fallback (default = current page) silently truncated at page 1."""
    monkeypatch.setenv("GITLAB_PERSONAL_ACCESS_TOKEN", "t")
    calls = []

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        calls.append(params["page"])
        if len(calls) <= 3:
            return FakeResponse(200, [{"iid": len(calls)}], headers={})  # NO x-total-pages
        return FakeResponse(200, [], headers={})

    monkeypatch.setattr(requests, "get", fake_get)
    result = platforms.gitlab_fetch_issues("grupo/repo")
    assert len(result) == 3  # followed through to the empty page, didn't stop at 1


def test_azure_legacy_visualstudio_host_org_comes_from_hostname():
    assert platforms.detect_platform("https://minhaorg.visualstudio.com/meuproj/_git/meurepo") == \
        ("azure_devops", "minhaorg/meuproj/meurepo", None)


def test_azure_legacy_visualstudio_default_collection_is_skipped():
    assert platforms.detect_platform("https://minhaorg.visualstudio.com/DefaultCollection/meuproj/_git/meurepo") == \
        ("azure_devops", "minhaorg/meuproj/meurepo", None)


def test_openai_compatible_error_message_never_contains_underlying_text(monkeypatch):
    """Same contract as H1 extended to the generic adapter: requests' str(e)
    may contain the full URL — the typed message never repeats it."""
    from griot import common

    class Poisoned:
        status_code = 500
        links = {}
        headers = {}

        def json(self):
            return {}

        def raise_for_status(self):
            raise requests.HTTPError("500 for url: https://x/?key=SEGREDO-NA-URL", response=self)

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        return Poisoned()

    monkeypatch.setattr(common, "_http_post", fake_post)
    with pytest.raises(common.DirectAPIUnavailable) as exc:
        common._openai_compatible_post_with_retry("https://api.example.com/v1", {}, {})
    assert "SEGREDO-NA-URL" not in str(exc.value)


def test_openai_compatible_redirect_is_typed_error_not_json_crash(monkeypatch):
    from griot import common

    class Redirect:
        status_code = 302
        links = {}
        headers = {"Location": "https://outro/"}

        def json(self):
            raise AssertionError("should not attempt to parse a redirect body")

        def raise_for_status(self):
            pass  # 3xx is not an error for raise_for_status

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        return Redirect()

    monkeypatch.setattr(common, "_http_post", fake_post)
    with pytest.raises(common.DirectAPIUnavailable):
        common._openai_compatible_post_with_retry("https://api.example.com/v1", {}, {})
