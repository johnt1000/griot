"""The read-only data the server also offers as MCP resources.

A tool means "do something"; a resource means "read this", addressed by a URI
the agent (or the person, in clients that let them attach one) fetches when it
wants context. The three resources DUPLICATE three read-only tools rather than
replacing them: tools stay, because they are what agents are known to call.

Two surfaces for the same data is the risk docs/mcp-capability-coverage.md
names: they can come to disagree. So each resource is produced by the same
function its tool runs and serialized by the same output model the SDK
builds for that tool, and these tests compare them through a real client,
which is the only place the serialization the agent sees happens."""

import json
import time

import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from griot import common, logdb, mcp_server, repos

# The URI of each resource, and the tool whose output it must equal.
RESOURCES = {
    "griot://repos": "griot_repos_list",
    "griot://stats": "griot_stats",
    "griot://index-status": "griot_index_status",
}


def _register(tmp_path):
    present = tmp_path / "alpha"
    present.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(present), str(tmp_path / "gone")]))


async def _read(client, uri):
    result = await client.read_resource(uri)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mime_type == "application/json"
    return json.loads(content.text)


@pytest.mark.anyio
async def test_the_three_resources_are_listed_with_a_description_and_json_type():
    async with Client(mcp_server.mcp) as client:
        listed = {str(r.uri): r for r in (await client.list_resources()).resources}

    assert set(RESOURCES) <= set(listed), set(RESOURCES) - set(listed)
    for uri in RESOURCES:
        assert listed[uri].mime_type == "application/json", uri
        # The description is all a client shows before reading: it has to say
        # what the data is, not be left empty.
        assert listed[uri].description and len(listed[uri].description) > 40, uri


@pytest.mark.anyio
async def test_each_resource_names_the_tool_it_duplicates():
    """An agent that finds the resource should know the tool exists (and
    takes arguments the resource cannot), and the other way round is not
    needed: the tools are what agents already use."""
    async with Client(mcp_server.mcp) as client:
        listed = {str(r.uri): r for r in (await client.list_resources()).resources}

    for uri, tool in RESOURCES.items():
        assert tool in listed[uri].description, uri


@pytest.mark.anyio
async def test_the_repos_resource_equals_the_repos_tool(tmp_path):
    _register(tmp_path)
    async with Client(mcp_server.mcp) as client:
        tool = await client.call_tool("griot_repos_list", {})
        read = await _read(client, "griot://repos")

    assert tool.is_error is False
    assert read == tool.structured_content
    assert read["count"] == 2 and [r["exists"] for r in read["repos"]] == [True, False]


@pytest.mark.anyio
async def test_the_index_status_resource_equals_the_index_status_tool():
    async with Client(mcp_server.mcp) as client:
        tool = await client.call_tool("griot_index_status", {})
        read = await _read(client, "griot://index-status")

    assert tool.is_error is False
    assert read == tool.structured_content
    assert read["collection"] == common.COLLECTION_NAME


@pytest.mark.anyio
async def test_the_stats_resource_equals_the_stats_tool_for_the_default_window(monkeypatch):
    # Recording the calls would make the second read count the first; the
    # comparison is about the data, so recording is held still here (it is
    # covered by its own test below).
    monkeypatch.setattr(mcp_server, "_record_call", lambda *a, **k: None)
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, duration_seconds=0.1)

    async with Client(mcp_server.mcp) as client:
        tool = await client.call_tool("griot_stats", {})
        read = await _read(client, "griot://stats")

    assert tool.is_error is False
    tool_out = dict(tool.structured_content)
    # The moment the report was made is the one field two calls cannot share.
    assert read.pop("generated_at") and tool_out.pop("generated_at")
    assert read == tool_out
    assert read["days"] == mcp_server.stats.DEFAULT_DAYS
    assert read["tool_calls"] == {"griot_search": 1}


@pytest.mark.anyio
async def test_a_resource_keeps_only_what_the_tool_schema_declares(monkeypatch):
    """The tool's structured output passes through the SDK's output model,
    which drops a key the schema does not declare. A resource that dumped the
    raw dict would carry that key and disagree with the tool on the first
    field added to the data and not to the schema."""
    real = repos.repo_status
    monkeypatch.setattr(repos, "repo_status",
                        lambda: [dict(e, undeclared="x") for e in real()])
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text('["/repos/alpha"]')

    async with Client(mcp_server.mcp) as client:
        tool = await client.call_tool("griot_repos_list", {})
        read = await _read(client, "griot://repos")

    assert "undeclared" not in tool.structured_content["repos"][0]
    assert read == tool.structured_content


@pytest.mark.anyio
async def test_a_corrupt_repos_file_is_an_error_that_names_the_file_and_the_server_carries_on():
    """The tool raises an actionable error for a corrupt repos.json rather than
    an empty list (which would read as "nothing registered"). The resource
    reads the same file and needs the same answer. The SDK replaces any error
    a resource raises with a bare "Error reading resource", which would hide
    which file to fix, so the message has to survive the protocol."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text('["/repos/alpha", "/repos/tru')

    async with Client(mcp_server.mcp) as client:
        with pytest.raises(MCPError) as caught:
            await client.read_resource("griot://repos")
        message = str(caught.value)
        assert "repos.json" in message and "griot repos add" in message

        # The connection is still usable: one bad read did not take the server down.
        common.REPOS_JSON_PATH.write_text('["/repos/alpha"]')
        read = await _read(client, "griot://repos")
    assert read["count"] == 1


@pytest.mark.anyio
async def test_a_resource_read_is_recorded_under_its_uri():
    """tool_calls is the evidence for whether resources earn their place next
    to the tools they duplicate, so a read is recorded like a call, under the
    URI, which cannot be mistaken for a tool name."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text('["/repos/alpha", "/repos/tru')
    async with Client(mcp_server.mcp) as client:
        with pytest.raises(MCPError):
            await client.read_resource("griot://repos")
        common.REPOS_JSON_PATH.write_text('["/repos/alpha"]')
        await client.read_resource("griot://repos")
        await client.read_resource("griot://index-status")

    calls = [(c["tool"], c["ok"]) for c in logdb.read_tool_calls(common.LOG_DIR, 1)]
    assert sorted(calls) == sorted([("griot://repos", False), ("griot://repos", True),
                                    ("griot://index-status", True)])


@pytest.mark.anyio
async def test_a_resource_read_counts_as_a_call_in_flight(monkeypatch):
    """The idle reaper closes the collection only when no call is in flight.
    Two of these resources open the collection, so a read that did not count
    would let the reaper close it under the read."""
    seen = []

    def status(collection=None):
        seen.append(mcp_server._inflight)
        return real(collection)

    real = common.get_index_status
    monkeypatch.setattr(common, "get_index_status", status)
    async with Client(mcp_server.mcp) as client:
        await client.read_resource("griot://index-status")

    assert seen == [1]
    assert mcp_server._inflight == 0


@pytest.mark.anyio
async def test_the_index_status_resource_reads_the_profile_in_effect_when_read(monkeypatch):
    """Not a value captured when the module was imported: a server outlives
    a profile switch. Whatever the tool reads at call time, the resource
    reads too, because it runs the same function."""
    seen = []
    monkeypatch.setattr(common, "get_index_status",
                        lambda collection=None: seen.append(collection) or {
                            "points_count": 3, "collection": "c", "embed_profile": "p", "running": False,
                            "pid": None, "path": None, "last_indexed": None, "spend_ceiling_exceeded": False})
    async with Client(mcp_server.mcp) as client:
        read = await _read(client, "griot://index-status")
    assert seen == [None] and read["points_count"] == 3


class _ToolManagerWithoutLookup:
    """The SDK's tool manager as a later SDK release may leave it: everything
    the server itself uses still works, but the lookup the resources once
    reached into is gone. Private attributes change without notice."""

    def __init__(self, real):
        self._real = real

    def get_tool(self, name):
        raise AttributeError("get_tool is private to the SDK")

    def __getattr__(self, attr):
        return getattr(self._real, attr)


@pytest.mark.anyio
async def test_the_resources_do_not_reach_into_the_sdks_private_tool_manager(monkeypatch, tmp_path):
    """The resources used to serialize through
    mcp._tool_manager.get_tool(name).fn_metadata.output_model, a private
    attribute of the SDK. Every resource still reads, and still equals its
    tool, with that lookup gone.

    The one test allowed to name the private manager (see
    test_preapprove_public_list.py): it must, to take the lookup away. On an
    SDK that no longer has the attribute there is nothing to take away, and
    the comparison below still holds the resources to their tools."""
    _register(tmp_path)
    monkeypatch.setattr(mcp_server, "_record_call", lambda *a, **k: None)
    real_manager = getattr(mcp_server.mcp, "_tool_manager", None)
    if real_manager is not None:
        monkeypatch.setattr(mcp_server.mcp, "_tool_manager", _ToolManagerWithoutLookup(real_manager))

    async with Client(mcp_server.mcp) as client:
        for uri, tool in RESOURCES.items():
            called = await client.call_tool(tool, {})
            read = await _read(client, uri)
            expected = dict(called.structured_content)
            # The moment a stats report was made is the one field two calls cannot share.
            read.pop("generated_at", None)
            expected.pop("generated_at", None)
            assert read == expected, uri


@pytest.mark.anyio
async def test_a_key_the_tool_may_leave_out_reads_as_null_on_both_surfaces(monkeypatch):
    """points_error, repositories and keyword_search may be absent from what
    the tool's function returns. The tool's structured output gives an absent
    one the value null; the resource has to do the same, not drop the key (a
    plain serializer of the TypedDict would drop it, and the two surfaces
    would disagree exactly when the answer is degraded)."""
    monkeypatch.setattr(common, "get_index_status", lambda collection=None: {
        "points_count": 3, "collection": "c", "embed_profile": "p", "running": False,
        "pid": None, "path": None, "last_indexed": None, "spend_ceiling_exceeded": False})
    async with Client(mcp_server.mcp) as client:
        tool = await client.call_tool("griot_index_status", {})
        read = await _read(client, "griot://index-status")

    assert tool.is_error is False
    assert read == tool.structured_content
    assert "points_error" in read and read["points_error"] is None
