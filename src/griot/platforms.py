"""Detection and lookup of PRs/MRs, releases, and issues across code-hosting
platforms (GitHub, GitLab, Bitbucket, Azure DevOps, Gitea/Forgejo) based on
a local repo's 'origin' remote — a generalization of what used to be just
`common.gitlab_request()`/`common.gitlab_project_path()` (migrated here, not
duplicated: `griot index platform`, formerly `griot index gitlab`, is the
only consumer).

Each adapter normalizes its response to the SAME format that
index_platform.py consumes (a natural key shared across platforms:
iid/title/description/state/author/created_at, plus source_branch/
target_branch only for PRs/MRs) — this way build_mr_documents()/
build_release_documents()/build_issue_documents() don't need to know which
platform answered, same as it already worked with GitLab alone.

**Uneven support per platform — documented, not a bug**:
- GitHub and Gitea/Forgejo: native PRs + releases + issues, format very
  close to GitLab's (Gitea deliberately mirrors the GitHub API).
- Bitbucket Cloud: no structured "release" concept (use tags instead via
  `griot index tags`, which already covers this locally with no API needed)
  — `fetch_releases()` always returns `[]`. Issues only exist if the repo's
  issue tracker is enabled (404 treated as "no issues", not an error).
- Azure DevOps (Azure Repos): only PRs are supported. Releases in that world
  live in Azure Pipelines (a separate subsystem, not about the repo's
  history) and issues are Azure Boards "work items" (a much heavier
  concept, not 1:1 with a repo) — out of scope, `fetch_releases()`/
  `fetch_issues()` always return `[]`.

**Gitea/Forgejo needs explicit configuration** (`GRIOT_GITEA_HOSTS`,
comma-separated list) — unlike the other four, it has no fixed hostname to
detect (it's always self-hosted). Without this env var, a remote pointing
to a Gitea instance isn't recognized by any platform (the same "no known
remote" behavior that already existed for any non-GitLab host before this
change).

**Only GitHub has been exercised against a real account and token** (2026-08-25: pull requests and releases indexed from four private repositories). GitLab has run against gitlab.com on both paths: a public project read without a token (2026-10-07: its merge requests, releases and issues), and with a token (2026-10-08: the token accepted, a public project indexed, a missing project refused with HTTP 404); self-hosted instances have not. The other adapters are covered by tests that mock `requests.get`/`requests.post` with responses shaped after each provider's publicly documented API — useful, but not the same confidence as a live smoke test, which needs an account, a token and a repository on each platform.
"""

from __future__ import annotations

import os
import time
from urllib.parse import quote, urlsplit

import requests

from griot import common

# [finding M3, 2026-08-19 audit] Cap on pages per listing: without it, an
# API that never returns an empty page (bug, malice, or a wrong test mock —
# a REAL infinite loop happened once) runs forever
# accumulating memory. 200 pages × 50-100 items = 10k-20k items per
# resource — well above any reasonable repo; hitting the cap produces an
# explicit warning (silent truncation would mask incomplete coverage).
MAX_PAGES = 200


def _strip_git_suffix(path: str) -> str:
    return path.removesuffix(".git")


def _path_after_host(url: str) -> str:
    """Extracts what comes after the host in a git remote (SSH or HTTPS) —
    e.g. 'git@github.com:owner/repo.git' -> 'owner/repo', 'https://github.com/
    owner/repo' -> 'owner/repo'. Same logic that already existed for GitLab
    only (doesn't validate the host — deciding whether the host matches the
    platform is each adapter's `_matches_host()` job, called BEFORE this)."""
    if url.startswith("git@") or (":" in url and "://" not in url):
        path = url.split(":", 1)[-1]
    else:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
    return _strip_git_suffix(path)


def _remote_host(url: str) -> str:
    """[finding M4] EXACT host of the remote (SSH or HTTPS), for equality
    comparison — detection used to be a substring match on the whole URL
    ('github.com' in url), so 'github.com.evil.net' (or a repo name
    containing the term) matched the wrong platform and the request went
    out authenticated to the wrong adapter."""
    if "://" in url:
        return (urlsplit(url).hostname or "").lower()
    if ":" in url:  # SSH scp-like form: [user@]host:path
        return url.split(":", 1)[0].split("@", 1)[-1].lower()
    return ""


def _safe_project_path(project_id: str, expected_segments: int) -> str:
    """[finding M4] Validates and encodes the project_id extracted from the
    remote before interpolating it into an API URL path — only GitLab used
    to quote(); the others accepted raw '..'/special characters, allowing a
    crafted remote to redirect the authenticated request to a different
    endpoint on the same host."""
    parts = project_id.split("/")
    if len(parts) != expected_segments or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid project identifier for this platform: {project_id!r}")
    return "/".join(quote(p, safe="") for p in parts)


def _require_same_host(next_url: str, base_url: str) -> None:
    """[finding M3] 'Next page' URLs come from the SERVER (Link header,
    response body) and used to be followed with the token still attached —
    a compromised/spoofed response could point the loop at any host,
    taking the credential along. Scheme too [review]: the same host over
    http:// (downgrade) would send the Bearer in cleartext."""
    next_parts = urlsplit(next_url)
    if next_parts.hostname != urlsplit(base_url).hostname or next_parts.scheme != "https":
        raise ValueError(
            f"pagination pointed outside the original API ({next_parts.scheme}://{next_parts.hostname}) — refused, the credential doesn't leave the original host/scheme."
        )


RETRY_ATTEMPTS = 5


def _retry_after_seconds(resp: requests.Response) -> int | None:
    """The delay a 429 asks for, when it gives one in seconds (what GitLab
    sends); None for an absent value or the HTTP-date form."""
    value = (resp.headers or {}).get("Retry-After")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _get_with_retry(url: str, headers: dict, params: dict | None = None, auth: tuple | None = None,
                    retry_after_cap: int | None = None) -> requests.Response:
    """GET with retry on 429 (exponential backoff, 5 attempts) — same
    philosophy as `common._gemini_post_with_retry()`/`_openai_compatible_post_with_retry()`,
    generalized here across the 5 platforms instead of duplicating per adapter.

    `retry_after_cap`, when given, waits what the server's Retry-After asks
    (bounded by the cap) instead of the fixed backoff, and does not wait
    after the last attempt. Only GitLab passes it: its unauthenticated limit
    is per minute, so the fixed 1-16s backoff can spend every attempt inside
    one throttled window. The other adapters keep their behaviour.

    [finding M5] allow_redirects=False: requests only strips the
    Authorization header on a cross-host redirect — PRIVATE-TOKEN (GitLab)
    and similar headers would travel to the new host. A redirect becomes an
    explicit error, never followed with a credential."""
    for attempt in range(RETRY_ATTEMPTS):
        resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=30, allow_redirects=False)
        if resp.status_code == 429:
            wait = 2 ** attempt
            if retry_after_cap is not None:
                if attempt == RETRY_ATTEMPTS - 1:
                    break
                wait = min(_retry_after_seconds(resp) or wait, retry_after_cap)
            common.log_and_print(f"429 on {url}, waiting {wait}s...")
            time.sleep(wait)
            continue
        if 300 <= resp.status_code < 400:
            raise requests.HTTPError(
                f"redirect {resp.status_code} not followed (policy: credential doesn't follow a redirect) on {url}",
                response=resp,
            )
        return resp
    return resp


# The variable each platform's token is read from: what an error about a
# refused token names (common.credential_hint).
# The provider names in common.credential_env_vars() are the platform names.
TOKEN_ENV = {platform: common.credential_env_vars()[platform]
             for platform in ("github", "gitlab", "bitbucket", "azure_devops", "gitea")}


def _require_token(env_var: str) -> str:
    token = common.credential(env_var)
    if not token:
        raise ValueError(f"{env_var} not found in the environment")
    return token


# ─────────────────────────── GitHub ───────────────────────────

def _github_matches(url: str) -> bool:
    return _remote_host(url) == "github.com"


def _github_project_id(url: str) -> str:
    return _path_after_host(url)


def _github_headers() -> dict:
    token = _require_token("GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _github_paginated_get(url: str, params: dict | None = None) -> list[dict]:
    """Pagination via Link header (RFC 5988, rel="next") — GitHub's standard,
    different from GitLab's page/total-pages or Bitbucket's cursor."""
    headers = _github_headers()
    all_items: list[dict] = []
    request_params = {**(params or {}), "per_page": 100}
    next_url = url
    pages = 0
    while next_url:
        pages += 1
        if pages > MAX_PAGES:
            common.log_and_print(f"WARNING: cap of {MAX_PAGES} pages reached on {url} — listing truncated.", level="warning")
            break
        resp = _get_with_retry(next_url, headers, request_params)
        resp.raise_for_status()
        all_items.extend(resp.json())
        next_url = resp.links.get("next", {}).get("url")
        if next_url:
            _require_same_host(next_url, url)  # [M3] the server doesn't get to choose where the token goes
        request_params = None  # already embedded in next_url
    return all_items


def github_fetch_pull_requests(project_id: str) -> list[dict]:
    prs = _github_paginated_get(f"https://api.github.com/repos/{_safe_project_path(project_id, 2)}/pulls", {"state": "all"})
    return [
        {
            "iid": pr.get("number"),
            "title": pr.get("title", ""),
            "description": pr.get("body"),
            # GitHub has no "merged" state separate from "closed" in the
            # 'state' field — just open/closed; merged_at != null is the real signal.
            "state": "merged" if pr.get("merged_at") else pr.get("state"),
            "author": (pr.get("user") or {}).get("login"),
            "created_at": pr.get("created_at"),
            "source_branch": (pr.get("head") or {}).get("ref"),
            "target_branch": (pr.get("base") or {}).get("ref"),
        }
        for pr in prs
    ]


def github_fetch_releases(project_id: str) -> list[dict]:
    releases = _github_paginated_get(f"https://api.github.com/repos/{_safe_project_path(project_id, 2)}/releases")
    return [
        {
            "name": r.get("name"),
            "tag_name": r.get("tag_name"),
            "description": r.get("body"),
            "released_at": r.get("published_at"),
            # Null for a release whose account was deleted.
            "author": (r.get("author") or {}).get("login"),
        }
        for r in releases
    ]


def github_fetch_issues(project_id: str) -> list[dict]:
    # [real finding] GitHub's /issues endpoint returns PRs mixed in (every
    # PR is also an issue in their API) — an item with the 'pull_request'
    # key present is a PR, not a real issue. Without filtering this out,
    # every PR would get indexed twice (once as merge_request, once as
    # issue) under incompatible metadata.
    items = _github_paginated_get(f"https://api.github.com/repos/{_safe_project_path(project_id, 2)}/issues", {"state": "all"})
    issues = [item for item in items if "pull_request" not in item]
    return [
        {
            "iid": i.get("number"),
            "title": i.get("title", ""),
            "description": i.get("body"),
            "state": i.get("state"),
            "author": (i.get("user") or {}).get("login"),
            "created_at": i.get("created_at"),
        }
        for i in issues
    ]


# ─────────────────────────── GitLab ───────────────────────────
# Migrated from common.py (gitlab_request()/gitlab_project_path()). The API
# base is configurable (GRIOT_GITLAB_API_BASE, default gitlab.com) to cover
# self-hosted instances — unlike the other fixed-host platforms, but
# without hardcoding any private host in the code.


def _gitlab_api_base() -> str:
    return os.getenv("GRIOT_GITLAB_API_BASE", "https://gitlab.com/api/v4")


def _gitlab_matches(url: str) -> bool:
    # [M4] exact host: gitlab.com, or the configured self-hosted instance's
    # host — "gitlab" as a substring matched anything.
    host = _remote_host(url)
    return host == "gitlab.com" or (host != "" and host == (urlsplit(_gitlab_api_base()).hostname or "").lower())


def _gitlab_project_id(url: str) -> str:
    return _path_after_host(url)


class TokenNeeded(requests.HTTPError):
    """GitLab refused a request made without a token in a way a token could
    change (401, or the 404 it gives an anonymous caller for a private
    project). Still an HTTPError carrying the response, so a run records it
    by its status like any other platform refusal."""


# GitLab.com's unauthenticated API limit is counted per minute: a longer
# Retry-After is not waited in full, so a throttled run ends in minutes as a
# recorded refusal instead of holding the terminal.
GITLAB_RETRY_AFTER_CAP = 60


def _gitlab_token() -> str | None:
    # An empty value is no token: sent as an empty PRIVATE-TOKEN header,
    # GitLab answers 401 even for a public project.
    return common.credential(TOKEN_ENV["gitlab"]) or None


def anonymous_read_note(platform: str) -> str | None:
    """What a run says when it reads a platform without a token, or None.
    Only GitLab reads without one; the other adapters require their token."""
    if platform == "gitlab" and not _gitlab_token():
        return f"{TOKEN_ENV['gitlab']} is not set: reading GitLab without a token, public data only."
    return None


def _gitlab_request(project_id: str, endpoint: str, params: dict | None = None) -> list[dict]:
    token = _gitlab_token()
    encoded_path = quote(project_id, safe="")
    url = f"{_gitlab_api_base()}/projects/{encoded_path}/{endpoint}"
    # Without a token, no header at all: public projects answer anonymously.
    # The token, when there is one, still goes only to the configured API host.
    headers = {"PRIVATE-TOKEN": token} if token else {}

    all_items: list[dict] = []
    page = 1
    while True:
        request_params = {**(params or {}), "per_page": 100, "page": page}
        resp = _get_with_retry(url, headers, request_params, retry_after_cap=GITLAB_RETRY_AFTER_CAP)
        if not token and resp.status_code in (401, 404):
            # Not the URL: the message is printed and could be logged.
            raise TokenNeeded(
                f"GitLab answered HTTP {resp.status_code} to a request made without a token: the project is "
                f"private, or does not exist. To read a private project, set {TOKEN_ENV['gitlab']} with "
                f"`griot auth set gitlab`.",
                response=resp,
            )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        all_items.extend(items)

        # [review] GitLab OMITS x-total-pages for collections >10k/keyset —
        # the old default (current page) silently truncated at page 1
        # exactly on the large repos. Without the header: loop until an
        # empty page (the `not items` break above), bounded by MAX_PAGES.
        total_pages_header = resp.headers.get("x-total-pages")
        if total_pages_header is not None and page >= int(total_pages_header):
            break
        if page >= MAX_PAGES:
            common.log_and_print(f"WARNING: cap of {MAX_PAGES} pages reached on {endpoint} — listing truncated.", level="warning")
            break
        page += 1
    return all_items


def gitlab_fetch_pull_requests(project_id: str) -> list[dict]:
    mrs = _gitlab_request(project_id, "merge_requests", {"state": "all"})
    return [
        {
            "iid": mr.get("iid"),
            "title": mr.get("title", ""),
            "description": mr.get("description"),
            "state": mr.get("state"),
            "author": (mr.get("author") or {}).get("username"),
            "created_at": mr.get("created_at"),
            "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"),
            "url": mr.get("web_url"),
        }
        for mr in mrs
    ]


def gitlab_fetch_releases(project_id: str) -> list[dict]:
    releases = _gitlab_request(project_id, "releases")
    return [
        {
            "name": r.get("name"),
            "tag_name": r.get("tag_name"),
            "description": r.get("description"),
            "released_at": r.get("released_at"),
            # The account, as for its merge requests: `name` is a display
            # name anyone can change.
            "author": (r.get("author") or {}).get("username"),
            # A release has no web_url; its page is _links.self.
            "url": (r.get("_links") or {}).get("self"),
        }
        for r in releases
    ]


def gitlab_fetch_issues(project_id: str) -> list[dict]:
    issues = _gitlab_request(project_id, "issues", {"state": "all"})
    return [
        {
            "iid": i.get("iid"),
            "title": i.get("title", ""),
            "description": i.get("description"),
            "state": i.get("state"),
            "author": (i.get("author") or {}).get("username"),
            "created_at": i.get("created_at"),
            "url": i.get("web_url"),
        }
        for i in issues
    ]


# ─────────────────────────── Bitbucket ───────────────────────────

def _bitbucket_matches(url: str) -> bool:
    return _remote_host(url) == "bitbucket.org"


def _bitbucket_project_id(url: str) -> str:
    return _path_after_host(url)


def _bitbucket_headers() -> dict:
    token = _require_token("BITBUCKET_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"}


def _bitbucket_paginated_get(url: str, params: dict | None = None) -> list[dict]:
    """Cursor-based pagination: each response carries 'next' with the full
    URL of the next page (already embedding all query params) — different
    from GitLab's page/total-pages or GitHub's Link header. The 'next'
    comes from the response BODY (not a header) — even more
    server-controlled, same host check as GitHub's ([M3])."""
    headers = _bitbucket_headers()
    all_items: list[dict] = []
    next_url = url
    request_params = params
    pages = 0
    while next_url:
        pages += 1
        if pages > MAX_PAGES:
            common.log_and_print(f"WARNING: cap of {MAX_PAGES} pages reached on {url} — listing truncated.", level="warning")
            break
        resp = _get_with_retry(next_url, headers, request_params)
        resp.raise_for_status()
        body = resp.json()
        all_items.extend(body.get("values", []))
        next_url = body.get("next")
        if next_url:
            _require_same_host(next_url, url)
        request_params = None  # already embedded in body["next"]
    return all_items


def bitbucket_fetch_pull_requests(project_id: str) -> list[dict]:
    # [design, no real API to confirm against] Bitbucket Cloud's API has
    # historically not accepted a single "state=ALL" — each state needs its
    # own query. Queries the 4 states that exist (OPEN/MERGED/DECLINED/
    # SUPERSEDED) and merges them — more calls than ideal, but works with
    # any API version.
    safe_path = _safe_project_path(project_id, 2)
    all_prs: list[dict] = []
    for state in ("OPEN", "MERGED", "DECLINED", "SUPERSEDED"):
        prs = _bitbucket_paginated_get(
            f"https://api.bitbucket.org/2.0/repositories/{safe_path}/pullrequests",
            {"state": state, "pagelen": 50},
        )
        all_prs.extend(prs)
    return [
        {
            "iid": pr.get("id"),
            "title": pr.get("title", ""),
            "description": pr.get("description"),
            "state": pr.get("state"),
            "author": (pr.get("author") or {}).get("display_name"),
            "created_at": pr.get("created_on"),
            "source_branch": ((pr.get("source") or {}).get("branch") or {}).get("name"),
            "target_branch": ((pr.get("destination") or {}).get("branch") or {}).get("name"),
        }
        for pr in all_prs
    ]


def bitbucket_fetch_releases(project_id: str) -> list[dict]:
    # Bitbucket Cloud has no structured "release" resource equivalent to
    # GitHub/GitLab/Gitea's (it has "downloads", which is just loose file
    # uploads, with no title/description/tag natively attached) — griot
    # index tags already covers local tags without needing an API for it.
    return []


def bitbucket_fetch_issues(project_id: str) -> list[dict]:
    try:
        issues = _bitbucket_paginated_get(
            f"https://api.bitbucket.org/2.0/repositories/{_safe_project_path(project_id, 2)}/issues",
            {"pagelen": 50},
        )
    except requests.HTTPError as e:
        # issue tracker disabled on the repo -> 404, not a real error, just
        # "no issues at all here" (same spirit as fetch_releases() returning
        # [] for platforms without the resource).
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    return [
        {
            "iid": i.get("id"),
            "title": i.get("title", ""),
            "description": (i.get("content") or {}).get("raw"),
            "state": i.get("state"),
            "author": (i.get("reporter") or {}).get("display_name"),
            "created_at": i.get("created_on"),
        }
        for i in issues
    ]


# ─────────────────────────── Azure DevOps ───────────────────────────

def _azure_matches(url: str) -> bool:
    host = _remote_host(url)
    # exact host for the two official domains; dotted suffix for the
    # legacy {org}.visualstudio.com format (the dot rules out evilvisualstudio.com)
    return host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith(".visualstudio.com")


def _azure_project_id(url: str) -> str | None:
    """Remote format quite different from the other 4 platforms — needs 3
    segments (org/project/repo), not 2 (owner/repo):
      https://dev.azure.com/{org}/{project}/_git/{repo}
      git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
      https://{org}.visualstudio.com/{project}/_git/{repo}   (legacy)
    Returns 'org/project/repo' — the API assembles the real URL from these
    3 parts separately, see azure_fetch_pull_requests().

    [review] In the legacy visualstudio.com format, the org lives in the
    HOSTNAME, not the path — without this branch, the format used to go
    silently unrecognized (or worse, with DefaultCollection in the path,
    produced the wrong org)."""
    host = _remote_host(url)
    path = _path_after_host(url)
    parts = [p for p in path.replace("v3/", "").replace("_git/", "").split("/") if p]
    if host.endswith(".visualstudio.com"):
        org = host.split(".", 1)[0]
        if parts and parts[0] == "DefaultCollection":
            parts = parts[1:]
        if len(parts) < 2:
            return None
        return f"{org}/{parts[0]}/{parts[-1]}"
    if len(parts) < 3:
        return None
    org, project, repo = parts[0], parts[1], parts[-1]
    return f"{org}/{project}/{repo}"


def _azure_auth() -> tuple:
    # Azure DevOps PAT goes as the password in Basic Auth, empty username —
    # not a Bearer or a PRIVATE-TOKEN header like the other platforms.
    token = _require_token("AZURE_DEVOPS_PAT")
    return ("", token)


def azure_fetch_pull_requests(project_id: str) -> list[dict]:
    org, project, repo = _safe_project_path(project_id, 3).split("/", 2)
    url = f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}/pullrequests"
    all_prs: list[dict] = []
    skip = 0
    while True:
        resp = _get_with_retry(
            url, headers={}, auth=_azure_auth(),
            params={"searchCriteria.status": "all", "api-version": "7.1", "$top": 100, "$skip": skip},
        )
        resp.raise_for_status()
        page = resp.json().get("value", [])
        if not page:
            break
        all_prs.extend(page)
        if len(page) < 100:
            break
        if skip >= (MAX_PAGES - 1) * 100:
            common.log_and_print(f"WARNING: cap of {MAX_PAGES} pages reached on {url} — listing truncated.", level="warning")
            break
        skip += 100
    return [
        {
            "iid": pr.get("pullRequestId"),
            "title": pr.get("title", ""),
            "description": pr.get("description"),
            "state": pr.get("status"),
            "author": (pr.get("createdBy") or {}).get("displayName"),
            "created_at": pr.get("creationDate"),
            "source_branch": (pr.get("sourceRefName") or "").removeprefix("refs/heads/"),
            "target_branch": (pr.get("targetRefName") or "").removeprefix("refs/heads/"),
        }
        for pr in all_prs
    ]


def azure_fetch_releases(project_id: str) -> list[dict]:
    # Releases in the Azure DevOps world live in Azure Pipelines (a CI/CD
    # subsystem, an API completely separate from Azure Repos) — not "about
    # the git repo's history" the same way a GitHub/GitLab release is. Out
    # of scope here, griot index tags covers local tags.
    return []


def azure_fetch_issues(project_id: str) -> list[dict]:
    # "Issues" in Azure DevOps are Azure Boards work items — a much
    # heavier concept (customizable types, its own workflow) and not 1:1
    # with a specific git repo (a work item may not reference any repo at
    # all). Out of scope.
    return []


# ─────────────────────────── Gitea / Forgejo ───────────────────────────
# Self-hosted by nature — no fixed hostname to detect (unlike the other 4),
# needs explicit configuration.

def _gitea_hosts() -> set[str]:
    raw = os.getenv("GRIOT_GITEA_HOSTS", "")
    return {h.strip() for h in raw.split(",") if h.strip()}


def _gitea_matches(url: str) -> bool:
    # [M4] exact equality with the remote's host, not a substring match on the URL
    return _remote_host(url) in _gitea_hosts()


def _gitea_project_id(url: str) -> str:
    return _path_after_host(url)


def _gitea_host_from_url(url: str) -> str:
    host = _remote_host(url)
    if host in _gitea_hosts():
        return host
    raise ValueError("no host from GRIOT_GITEA_HOSTS matches this URL")


def _gitea_headers() -> dict:
    token = _require_token("GITEA_TOKEN")
    return {"Authorization": f"token {token}"}


def _gitea_paginated_get(url: str, params: dict | None = None) -> list[dict]:
    """Pagination by page/limit, same as GitLab, but with no total header —
    stops when a page comes back empty (Gitea/Forgejo's API mirrors GitHub
    on this point: no x-total-pages)."""
    headers = _gitea_headers()
    all_items: list[dict] = []
    page = 1
    while True:
        resp = _get_with_retry(url, headers, {**(params or {}), "limit": 50, "page": page})
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        all_items.extend(items)
        if page >= MAX_PAGES:
            common.log_and_print(f"WARNING: cap of {MAX_PAGES} pages reached on {url} — listing truncated.", level="warning")
            break
        page += 1
    return all_items


def gitea_fetch_pull_requests(project_id: str, host: str) -> list[dict]:
    prs = _gitea_paginated_get(f"https://{host}/api/v1/repos/{_safe_project_path(project_id, 2)}/pulls", {"state": "all"})
    return [
        {
            "iid": pr.get("number"),
            "title": pr.get("title", ""),
            "description": pr.get("body"),
            "state": "merged" if pr.get("merged") else pr.get("state"),
            "author": (pr.get("user") or {}).get("login"),
            "created_at": pr.get("created_at"),
            "source_branch": (pr.get("head") or {}).get("ref"),
            "target_branch": (pr.get("base") or {}).get("ref"),
        }
        for pr in prs
    ]


def gitea_fetch_releases(project_id: str, host: str) -> list[dict]:
    releases = _gitea_paginated_get(f"https://{host}/api/v1/repos/{_safe_project_path(project_id, 2)}/releases")
    return [
        {
            "name": r.get("name"),
            "tag_name": r.get("tag_name"),
            "description": r.get("body"),
            "released_at": r.get("created_at"),
            "author": (r.get("author") or {}).get("login"),
        }
        for r in releases
    ]


def gitea_fetch_issues(project_id: str, host: str) -> list[dict]:
    # type=issues filters out PRs server-side — unlike GitHub, which
    # returns everything mixed together and requires filtering after
    # receiving the response.
    issues = _gitea_paginated_get(
        f"https://{host}/api/v1/repos/{_safe_project_path(project_id, 2)}/issues", {"state": "all", "type": "issues"},
    )
    return [
        {
            "iid": i.get("number"),
            "title": i.get("title", ""),
            "description": i.get("body"),
            "state": i.get("state"),
            "author": (i.get("user") or {}).get("login"),
            "created_at": i.get("created_at"),
        }
        for i in issues
    ]


# ─────────────────────────── Registration/detection ───────────────────────────

# Since the M4 hardening (2026-08-19) all matchers compare the remote's
# EXACT host (not a substring on the URL), so there's no longer any real
# ambiguity between them — the order is kept for stability, not necessity.
_PLATFORM_MATCHERS = [
    ("github", _github_matches, _github_project_id),
    ("bitbucket", _bitbucket_matches, _bitbucket_project_id),
    ("azure_devops", _azure_matches, _azure_project_id),
    ("gitlab", _gitlab_matches, _gitlab_project_id),
    ("gitea", _gitea_matches, _gitea_project_id),
]


def detect_platform(remote_url: str) -> tuple[str, str, str | None] | None:
    """(platform_name, project_id, host) from the 'origin' remote, or None
    if no platform recognizes the host. project_id can be None even with a
    recognized platform (e.g. an Azure DevOps URL without the 3 expected
    segments) — treated as 'unrecognized' by the caller. 'host' is only
    populated for Gitea/Forgejo (self-hosted, no fixed hostname — the other
    4 platforms resolve the API from their fixed name, they don't need the
    remote's host)."""
    for name, matches, extract_id in _PLATFORM_MATCHERS:
        if matches(remote_url):
            project_id = extract_id(remote_url)
            if not project_id:
                return None
            host = _gitea_host_from_url(remote_url) if name == "gitea" else None
            return (name, project_id, host)
    return None


def fetch_pull_requests(platform: str, project_id: str, host: str | None = None) -> list[dict]:
    if platform == "gitea":
        return gitea_fetch_pull_requests(project_id, host)
    return {
        "github": github_fetch_pull_requests,
        "gitlab": gitlab_fetch_pull_requests,
        "bitbucket": bitbucket_fetch_pull_requests,
        "azure_devops": azure_fetch_pull_requests,
    }[platform](project_id)


def fetch_releases(platform: str, project_id: str, host: str | None = None) -> list[dict]:
    if platform == "gitea":
        return gitea_fetch_releases(project_id, host)
    return {
        "github": github_fetch_releases,
        "gitlab": gitlab_fetch_releases,
        "bitbucket": bitbucket_fetch_releases,
        "azure_devops": azure_fetch_releases,
    }[platform](project_id)


def fetch_issues(platform: str, project_id: str, host: str | None = None) -> list[dict]:
    if platform == "gitea":
        return gitea_fetch_issues(project_id, host)
    return {
        "github": github_fetch_issues,
        "gitlab": gitlab_fetch_issues,
        "bitbucket": bitbucket_fetch_issues,
        "azure_devops": azure_fetch_issues,
    }[platform](project_id)
