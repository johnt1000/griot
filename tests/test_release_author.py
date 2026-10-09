"""A release keeps who published it, as a merge request and an issue do.

GitHub, GitLab and Gitea/Forgejo return the release's author; griot's
release model had no field for it, so a release came back from a search
with none while the merge requests beside it had theirs (found against the
real GitLab API, 2026-10-07). The author is stored only when the platform
gave one: Bitbucket and Azure DevOps have no release resource griot reads,
and a release indexed before this change has no such field, so both keep
the payload they always had.

Through the whole path: the platform's response (HTTP mocked, in the shape
of the real API), the document built from it, the point written, and
griot_search through the MCP protocol.
"""

import hashlib
import random

import pytest
import requests
from mcp.client.client import Client

from griot import common, index_platform, mcp_server, platforms

QUERY = "what changed in the runner release"


class FakeResponse:
    def __init__(self, json_body, headers=None):
        self.status_code = 200
        self._json_body = json_body
        self.headers = headers or {}
        self.links = {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        pass


def _release(tag: str, author: dict | None) -> dict:
    """One release as GitLab's /projects/:id/releases returns it."""
    release = {"tag_name": tag, "name": tag, "description": f"{QUERY} {tag}", "released_at": "2026-09-18T00:00:00Z",
               "_links": {"self": f"https://gitlab.com/g/p/-/releases/{tag}"}}
    if author is not None:
        release["author"] = author
    return release


def _gitlab_serves(monkeypatch, releases: list[dict]) -> None:
    monkeypatch.delenv("GITLAB_PERSONAL_ACCESS_TOKEN", raising=False)

    def fake_get(url, headers=None, params=None, auth=None, timeout=None, allow_redirects=True):
        assert url.endswith("/releases"), url
        return FakeResponse(releases, headers={"x-total-pages": "1"})

    monkeypatch.setattr(requests, "get", fake_get)


def _vector(text: str, dim: int) -> list[float]:
    """Every release text is near the query; anything else is noise."""
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    v = [rng.uniform(-0.1, 0.1) for _ in range(dim)]
    v[0] = 1.0 if QUERY in text else 0.0
    return v


@pytest.fixture
def embeds(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])


def _index(monkeypatch, releases: list[dict]) -> list[dict]:
    _gitlab_serves(monkeypatch, releases)
    documents = index_platform.build_release_documents("repo", "gitlab", "g/p", None)
    common.index_documents(documents)
    common.release_lock()
    return documents


async def _search_metadata() -> dict[str, dict]:
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": QUERY, "mode": "vector",
                                                         "source_types": ["release"]})
    assert not result.is_error, result
    return {r["metadata"]["tag_name"]: r["metadata"] for r in result.structured_content["results"]}


def test_a_release_document_has_the_author_only_when_the_platform_gave_one(monkeypatch):
    _gitlab_serves(monkeypatch, [_release("v2", {"id": 1, "username": "maintainer", "name": "A Maintainer"}),
                                 _release("v1", None)])

    documents = index_platform.build_release_documents("repo", "gitlab", "g/p", None)

    by_tag = {d["metadata"]["tag_name"]: d["metadata"] for d in documents}
    assert by_tag["v2"]["author"] == "maintainer"
    # Absent, not null: a point of a platform with no author stays as it was.
    assert "author" not in by_tag["v1"]


@pytest.mark.parametrize("platform", ["bitbucket", "azure_devops"])
def test_platforms_without_a_release_resource_still_give_no_release(platform):
    # Neither API has a release griot reads (platforms.py): no author is
    # invented for them, because there is no release to carry one.
    assert platforms.fetch_releases(platform, "w/p/r" if platform == "azure_devops" else "w/r") == []


@pytest.mark.anyio
async def test_griot_search_shows_a_release_author_and_reads_one_without(embeds, monkeypatch):
    _index(monkeypatch, [_release("v2", {"id": 1, "username": "maintainer", "name": "A Maintainer"}),
                         _release("v1", None)])

    metadata = await _search_metadata()

    assert metadata["v2"]["author"] == "maintainer"
    assert "author" not in metadata["v1"]


@pytest.mark.anyio
async def test_a_release_indexed_before_authors_gets_its_author_on_the_next_run(embeds, monkeypatch):
    """The text is unchanged, so nothing is embedded again: the stored
    details are what the next run writes (common._write_stale_details)."""
    _index(monkeypatch, [_release("v2", None)])
    assert "author" not in (await _search_metadata())["v2"]

    calls = []
    monkeypatch.setattr(common, "embed_texts",
                        lambda texts, **kw: calls.append(texts) or [_vector(t, common.EMBED_DIM) for t in texts])
    _index(monkeypatch, [_release("v2", {"id": 1, "username": "maintainer"})])

    assert calls == []
    assert (await _search_metadata())["v2"]["author"] == "maintainer"


@pytest.mark.anyio
async def test_a_credential_shaped_author_is_shown_replaced(embeds, monkeypatch):
    # A name is the repository's data, as a path or a tag is: shown the same
    # way, with a credential-shaped value replaced.
    token = "glpat-" + "x" * 24
    _index(monkeypatch, [_release("v2", {"id": 1, "username": token})])

    metadata = await _search_metadata()

    assert token not in str(metadata)
    assert metadata["v2"]["author"]
