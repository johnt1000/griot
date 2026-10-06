"""tools_safe_to_preapprove() asks the server's PUBLIC tool list, not the SDK's
private tool manager.

It used to read the SDK's private tool manager: an attribute that a later SDK
release may rename or reshape without notice, and the list it gives is what
`griot assist install` writes into a user's settings as "may run without
asking". The public list_tools() is async while its callers are not all async
(the CLI and doctor are sync; the server's own tools run inside an event
loop), so the function has to work from both.

The tests hold it to what a real MCP client is offered through tools/list,
never to the private manager: a test that reads it breaks on an SDK that drops
it while the server, which no longer reads it, still works."""

import inspect
import re
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client

from griot import mcp_server

# The attribute as a whole identifier, so that a name merely ending in it (the
# tests below are called ..._private_tool_manager) is not a use of it. Spelled
# in two halves so that this file's own scan does not find it.
_PRIVATE_MANAGER = re.compile(r"(?<![A-Za-z0-9_])" + "_tool" + "_manager" + r"(?![A-Za-z0-9_])")

# Test files allowed to name the private manager, and why. Each one must still
# survive an SDK that no longer has it, and must still name it: a stale
# exception would silently widen the scan.
_PRIVATE_MANAGER_ALLOWED_IN_TESTS = {
    # Simulates a later SDK whose private manager lost the lookup the
    # resources once used. It has to touch the attribute to take that away,
    # and it skips that step when the attribute is already gone.
    "test_mcp_resources.py",
}


def _what_a_client_is_offered() -> list[str]:
    """The oracle: what a real MCP client sees in tools/list, filtered by the
    policy (readOnlyHint, the human-only marker, the named exceptions). It
    reads only the public protocol, so a reshaped SDK internal cannot break
    the test while the server still works."""
    async def listed():
        async with Client(mcp_server.mcp) as client:
            return (await client.list_tools()).tools

    names = []
    for tool in anyio.run(listed):
        read_only = tool.annotations is not None and tool.annotations.read_only_hint is True
        human_only = (tool.meta or {}).get("anthropic/requiresUserInteraction")
        if read_only and not human_only and tool.name not in mcp_server._READ_ONLY_BUT_ASKED:
            names.append(tool.name)
    return sorted(names)


def test_the_server_does_not_reach_into_the_sdks_private_tool_manager():
    source = inspect.getsource(mcp_server)
    assert not _PRIVATE_MANAGER.search(source), "use the public list_tools(), not the SDK's private tool manager"


def test_the_tests_do_not_reach_into_the_sdks_private_tool_manager():
    """Only the named exceptions may name the private manager, and each of
    them must still need to."""
    tests_dir = Path(__file__).parent
    naming_it = {path.name for path in tests_dir.rglob("*.py") if _PRIVATE_MANAGER.search(path.read_text())}
    assert naming_it - _PRIVATE_MANAGER_ALLOWED_IN_TESTS == set()
    assert _PRIVATE_MANAGER_ALLOWED_IN_TESTS <= naming_it, "drop an exception that no longer applies"


def test_the_oracle_sees_the_markers_through_the_protocol():
    """An oracle that lost the annotations or the meta on the way through the
    protocol would agree with a wrong list: hold it to what the server is
    known to mark."""
    offered = _what_a_client_is_offered()
    assert "griot_search" in offered
    assert "griot_quality_check" not in offered  # read-only, but one of the exceptions
    assert "griot_repos_remove" not in offered  # not read-only
    assert "griot_assist_install" not in offered  # human-only


def test_the_list_is_the_one_a_client_is_offered():
    offered = mcp_server.tools_safe_to_preapprove()
    assert offered == _what_a_client_is_offered()
    assert "griot_search" in offered and "griot_quality_check" not in offered


def test_it_works_from_inside_a_running_event_loop():
    """griot_assist_install and the other tools run inside the server's event
    loop: running the async list there must neither fail nor deadlock."""
    async def from_a_loop():
        return mcp_server.tools_safe_to_preapprove()

    assert anyio.run(from_a_loop) == _what_a_client_is_offered()


def test_a_tool_marked_human_only_is_left_out_although_read_only(monkeypatch):
    """The meta key travels through the public list as `meta`: a read-only
    tool that requires a person must still not be offered."""
    from mcp.types import ToolAnnotations

    def griot_fake_human_only() -> str:
        """A read-only tool that only a person may run."""
        return "x"

    mcp_server.mcp.add_tool(griot_fake_human_only, annotations=ToolAnnotations(readOnlyHint=True),
                            meta={"anthropic/requiresUserInteraction": True})
    try:
        assert "griot_fake_human_only" not in mcp_server.tools_safe_to_preapprove()
        # No real tool is both read-only and human-only, so only this one
        # shows the oracle reading the marker from the client's view.
        assert "griot_fake_human_only" not in _what_a_client_is_offered()
    finally:
        mcp_server.mcp.remove_tool("griot_fake_human_only")


def test_a_read_only_unmarked_tool_is_offered_by_both():
    """The positive control for the two exclusions around it: a tool that
    only says it reads is offered, by the server and by the oracle alike, so
    their `not in` cannot pass merely because a fake tool never got listed."""
    from mcp.types import ToolAnnotations

    def griot_fake_reader() -> str:
        """A read-only tool anyone may run."""
        return "x"

    mcp_server.mcp.add_tool(griot_fake_reader, annotations=ToolAnnotations(readOnlyHint=True))
    try:
        offered = mcp_server.tools_safe_to_preapprove()
        assert "griot_fake_reader" in offered
        assert offered == _what_a_client_is_offered()
    finally:
        mcp_server.mcp.remove_tool("griot_fake_reader")


@pytest.mark.parametrize("annotations", [None, {"readOnlyHint": False}])
def test_a_tool_not_marked_read_only_is_left_out(annotations):
    from mcp.types import ToolAnnotations

    def griot_fake_unmarked() -> str:
        """A tool that does not say it only reads."""
        return "x"

    mcp_server.mcp.add_tool(griot_fake_unmarked,
                            annotations=ToolAnnotations(**annotations) if annotations is not None else None)
    try:
        assert "griot_fake_unmarked" not in mcp_server.tools_safe_to_preapprove()
    finally:
        mcp_server.mcp.remove_tool("griot_fake_unmarked")
