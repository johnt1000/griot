"""The metadata fields a search result can carry, listed for agents.

griot_search's description is held to 1600 characters, so a field added to
the payload (GitLab's `url`, a release's `author`) used to be documented only
in docs/indexing-model.md, which no agent reads. The griot://result-fields
resource lists them per source type, from common.RESULT_FIELDS, and every
indexer builds its payload through common.result_metadata(), which refuses a
field that list does not hold: the list cannot fall behind what is stored.

These tests build real documents from every indexer (git repositories made
for the test, platform responses mocked) and compare what they write with
what the resource says, through a real MCP client.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import (common, index_branches, index_code, index_commits, index_platform, index_tags,
                   logdb, mcp_server, platforms)

URI = "griot://result-fields"

# Beside `metadata` in a result rather than inside it (mcp_server._NOT_METADATA).
_TOP_LEVEL = {"repo", "source_type"}


def _platform_documents(monkeypatch) -> list[dict]:
    """A merge request, a release and an issue, each with every optional
    field a platform can give (a web page, a release author)."""
    monkeypatch.setattr(platforms, "fetch_pull_requests", lambda *a, **k: [{
        "iid": 7, "title": "Retry the upload", "description": "why", "state": "merged",
        "author": "ana", "created_at": "2026-09-01T00:00:00Z", "source_branch": "fix",
        "target_branch": "main", "url": "https://gitlab.example/g/p/-/merge_requests/7"}])
    monkeypatch.setattr(platforms, "fetch_releases", lambda *a, **k: [{
        "tag_name": "v1.0", "name": "v1.0", "description": "notes", "released_at": "2026-09-02T00:00:00Z",
        "author": "bruno", "url": "https://gitlab.example/g/p/-/releases/v1.0"}])
    monkeypatch.setattr(platforms, "fetch_issues", lambda *a, **k: [{
        "iid": 3, "title": "Upload fails", "description": "how", "state": "opened",
        "author": "carla", "created_at": "2026-08-30T00:00:00Z",
        "url": "https://gitlab.example/g/p/-/issues/3"}])
    return (index_platform.build_mr_documents("p", "gitlab", "g/p", None)
            + index_platform.build_release_documents("p", "gitlab", "g/p", None)
            + index_platform.build_issue_documents("p", "gitlab", "g/p", None))


def _git_documents(git_repo) -> list[dict]:
    """Code, a commit, a tag and a branch other than the default."""
    git_repo.commit("first", filename="app.py", content="print('hello')\n")
    git_repo.tag("v0.1", message="the first release")
    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "-b", "feature-x"], check=True)
    git_repo.commit("work on the feature", filename="feature.py", content="x = 1\n")
    git_repo.set_remote_head("main")
    return (index_code.process_repository(git_repo.path) + index_commits.build_documents(git_repo.path)
            + index_tags.build_documents(git_repo.path) + index_branches.build_documents(git_repo.path))


def _written_fields(documents) -> dict[str, set[str]]:
    written: dict[str, set[str]] = {}
    for doc in documents:
        metadata = doc["metadata"]
        written.setdefault(metadata["source_type"], set()).update(set(metadata) - _TOP_LEVEL)
    return written


def test_every_indexer_writes_exactly_the_fields_the_list_holds(git_repo, monkeypatch):
    """Both ways: a field written and not listed is one an agent is never
    told about; a field listed and never written sends it after nothing."""
    written = _written_fields(_git_documents(git_repo) + _platform_documents(monkeypatch))
    assert set(written) == set(common.SOURCE_TYPES), "every kind of source was built"
    listed = {kind: set(fields) for kind, fields in common.RESULT_FIELDS.items()}
    assert written == listed


def test_the_kinds_of_source_are_the_kinds_the_list_describes():
    assert tuple(common.RESULT_FIELDS) == common.SOURCE_TYPES


def test_every_listed_field_is_described():
    for kind, fields in common.RESULT_FIELDS.items():
        for name, meaning in fields.items():
            assert isinstance(meaning, str) and len(meaning) > 10, (kind, name)


@pytest.mark.parametrize("metadata", [
    {"source_type": "code", "repo": "r", "file_path": "a.py", "chunk_index": 0, "web_url": "x"},
    # Listed for another kind is not listed for this one.
    {"source_type": "code", "repo": "r", "file_path": "a.py", "chunk_index": 0, "commit_hash": "abc"},
])
def test_a_payload_with_a_field_the_list_does_not_hold_is_refused(metadata):
    extra = (set(metadata) - _TOP_LEVEL - set(common.RESULT_FIELDS["code"])).pop()
    with pytest.raises(ValueError, match=extra):
        common.result_metadata(metadata)


def test_a_payload_of_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="wiki"):
        common.result_metadata({"source_type": "wiki", "repo": "r"})


def test_a_listed_payload_passes_unchanged():
    metadata = {"source_type": "release", "repo": "r", "tag_name": "v1", "released_at": None, "chunk_index": 0}
    assert common.result_metadata(metadata) is metadata


def test_a_builder_that_adds_a_field_fails(monkeypatch):
    """The check is in the path every platform document takes, not only in
    this test file: a new optional field added the way `url` was fails."""
    monkeypatch.setattr(index_platform, "_url_field", lambda item: {"web_url": "https://example/1"})
    with pytest.raises(ValueError, match="web_url"):
        _platform_documents(monkeypatch)


def test_each_git_indexer_builds_its_payload_through_the_check(git_repo, monkeypatch):
    """Every payload the four git indexers write passed the check: one call
    per document, of each document's own kind."""
    seen = []
    real = common.result_metadata

    def checked(metadata):
        seen.append(metadata["source_type"])
        return real(metadata)

    monkeypatch.setattr(common, "result_metadata", checked)
    documents = _git_documents(git_repo)
    assert set(seen) == {"code", "commit", "tag", "branch"}
    assert seen == [doc["metadata"]["source_type"] for doc in documents]


# --- through the protocol --------------------------------------------------------------


async def _read():
    async with Client(mcp_server.mcp) as client:
        listed = {str(r.uri): r for r in (await client.list_resources()).resources}
        result = await client.read_resource(URI)
    return listed, result


@pytest.mark.anyio
async def test_the_resource_is_listed_with_a_description_and_json_type():
    listed, _ = await _read()
    assert URI in listed
    assert listed[URI].mime_type == "application/json"
    assert "griot_search" in listed[URI].description and "metadata" in listed[URI].description


@pytest.mark.anyio
async def test_the_resource_lists_each_kinds_fields_with_their_meaning():
    _, result = await _read()
    assert len(result.contents) == 1 and result.contents[0].mime_type == "application/json"
    read = json.loads(result.contents[0].text)
    assert read["metadata"] == common.RESULT_FIELDS
    # The two fields this resource exists for, where a platform gives them.
    for kind in ("merge_request", "release", "issue"):
        assert "url" in read["metadata"][kind], kind
    for kind in ("commit", "merge_request", "release", "issue"):
        assert "author" in read["metadata"][kind], kind
    assert set(read["beside_metadata"]) == _TOP_LEVEL


@pytest.mark.anyio
async def test_a_read_of_the_resource_is_recorded_under_its_uri():
    """resource_reads is how anyone learns whether agents read it at all."""
    await _read()
    calls = [(c["tool"], c["ok"]) for c in logdb.read_tool_calls(common.LOG_DIR, 1)]
    assert calls == [(URI, True)]


@pytest.mark.anyio
async def test_the_search_description_points_to_the_resource():
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]
    assert URI in tool.description


# --- the human-facing copy -----------------------------------------------------------


def test_the_payload_table_in_the_indexing_model_names_the_same_fields():
    """docs/indexing-model.md keeps a table for people; the resource is the
    authoritative list, and this keeps the copy from falling behind it."""
    doc = (Path(__file__).resolve().parent.parent / "docs" / "indexing-model.md").read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `([a-z_]+)` \| (.+) \|$", doc, flags=re.MULTILINE))
    rows.pop("source_type")  # the header row
    assert set(rows) == set(common.RESULT_FIELDS)
    for kind, cell in rows.items():
        # Only the field names that open each entry, not the ones its notes mention.
        named = set(re.findall(r"(?:^|, )`([a-z_]+)`", cell))
        assert named == set(common.RESULT_FIELDS[kind]) | {"repo"}, kind
    assert URI in doc
