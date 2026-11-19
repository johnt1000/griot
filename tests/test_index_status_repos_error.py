"""`griot_index_status` says when repos.json cannot be read instead of
answering that no repository is registered.

freshness.repository_freshness() answers an empty list when repos.json
cannot be read, on purpose: a search must not fail because of it. But the
status tool is where an agent asks which repositories there are, and an
empty list there reads as "nothing registered", silently. The status now
carries the one sentence every other surface uses for that file."""

import json

import pytest
from mcp.client.client import Client

from griot import common, mcp_server


async def _status():
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {})
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["[not json", json.dumps({"repos": "not a list"})])
async def test_an_unreadable_repos_json_is_said_not_read_as_no_repositories(content):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(content)

    status = await _status()

    assert status["repositories"] == []
    assert status["repos_error"]
    assert str(common.REPOS_JSON_PATH) in status["repos_error"]
    assert status["repos_error"].rstrip().endswith("griot repos add <path>")


@pytest.mark.anyio
async def test_no_repos_json_yet_is_nothing_registered_not_an_error():
    if common.REPOS_JSON_PATH.exists():
        common.REPOS_JSON_PATH.unlink()

    status = await _status()

    assert status["repos_error"] is None


@pytest.mark.anyio
async def test_a_readable_repos_json_has_no_error():
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text("[]")

    status = await _status()

    assert status["repos_error"] is None
