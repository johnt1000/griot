"""tools_safe_to_preapprove() asks the server's PUBLIC tool list, not the SDK's
private tool manager.

It used to read mcp._tool_manager.list_tools(): a private attribute that a
later SDK release may rename or reshape without notice, and the list it gives
is what `griot assist install` writes into a user's settings as "may run
without asking". The public list_tools() is async while its callers are not
all async (the CLI and doctor are sync; the server's own tools run inside an
event loop), so the function has to work from both."""

import inspect

import anyio
import pytest

from griot import mcp_server


def _what_the_private_manager_said() -> list[str]:
    """The previous implementation, verbatim, kept here only as the oracle the
    new one must agree with."""
    names = []
    for tool in mcp_server.mcp._tool_manager.list_tools():
        read_only = getattr(tool.annotations, "read_only_hint", None) is True
        human_only = (tool.meta or {}).get("anthropic/requiresUserInteraction")
        if read_only and not human_only and tool.name not in mcp_server._READ_ONLY_BUT_ASKED:
            names.append(tool.name)
    return sorted(names)


def test_the_server_does_not_reach_into_the_sdks_private_tool_manager():
    source = inspect.getsource(mcp_server)
    assert "_tool_manager" not in source, "use the public list_tools(), not the SDK's private tool manager"


def test_the_list_is_the_one_the_private_manager_gave():
    offered = mcp_server.tools_safe_to_preapprove()
    assert offered == _what_the_private_manager_said()
    assert "griot_search" in offered and "griot_quality_check" not in offered


def test_it_works_from_inside_a_running_event_loop():
    """griot_assist_install and the other tools run inside the server's event
    loop: running the async list there must neither fail nor deadlock."""
    async def from_a_loop():
        return mcp_server.tools_safe_to_preapprove()

    assert anyio.run(from_a_loop) == _what_the_private_manager_said()


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
    finally:
        mcp_server.mcp.remove_tool("griot_fake_human_only")


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
