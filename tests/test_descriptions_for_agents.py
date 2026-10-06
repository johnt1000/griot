"""Every description the server sends is read by an agent, and only by one.

A tool's or a prompt's description used to be its whole docstring, so notes
written for whoever maintains griot (why a prompt is a prompt, which review
asked for a guard, which function holds a check) went to every client that
listed them, on every session. The rule now: a docstring's text up to a
line reading `Maintainer notes:` is the description; what follows stays in
the source. These tests hold every description a real client receives to
that, the two index tools registered on demand included, and render every
prompt the way a client uses it (CLAUDE.md: a prompt can list well and fail
in use)."""

import json
import os
import re
import subprocess
import sys

import pytest
from mcp.client.client import Client

from griot import mcp_server

# Derived from what the source actually writes in comments and docstrings for
# maintainers, as a RULE rather than the list of tags seen so far: any
# bracketed tag ([user-requested], [review], [review finding], [security],
# [security review], [design], [real finding] ...), a numbered decision, a
# to-do marker, a source file, a path into docs/, and a call to an internal
# function written as `name()` or `module.name()`. An agent can do nothing
# with any of them.
_MAINTAINER_MARKERS = {
    "bracketed tag": re.compile(r"\[[A-Za-z][A-Za-z -]*\]"),
    "numbered decision": re.compile(r"\bdecisions? \d+", re.IGNORECASE),
    "to-do marker": re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"),
    "source file": re.compile(r"\b[\w/.-]+\.py\b"),
    "docs path": re.compile(r"\bdocs/"),
    "internal call": re.compile(r"\b[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*\(\)"),
    "maintainer heading": re.compile(r"maintainer notes", re.IGNORECASE),
}


def _markers_in(text):
    return {kind: pattern.search(text).group(0)
            for kind, pattern in _MAINTAINER_MARKERS.items() if pattern.search(text)}


@pytest.mark.parametrize("text, kind", [
    ("[user-requested] A PROMPT, not a tool", "bracketed tag"),
    ("[security review] No confirm= escape hatch", "bracketed tag"),
    ("as decided (decision 112)", "numbered decision"),
    ("TODO: say more", "to-do marker"),
    ("see harnesses.py", "source file"),
    ("see docs/mcp-capability-coverage.md", "docs path"),
    ("all live in griot.jobs.start_index_job().", "internal call"),
    ("reading repo_status()'s exists/is_git", "internal call"),
])
def test_each_marker_is_recognised(text, kind):
    """The markers are only worth what their patterns catch: each kind,
    against a line the source really carried before this rule."""
    assert kind in _markers_in(text)


@pytest.mark.parametrize("text", [
    "Searches everything indexed, all of them at once: `group_by_document=true`.",
    'Search the kinds separately with source_types (one call for ["code"]).',
    "Call griot_search several times; pass mode=\"keyword\" for a commit hash.",
])
def test_ordinary_agent_text_is_not_a_marker(text):
    assert _markers_in(text) == {}


async def _sent():
    """(kind, name, description) for every tool, tool parameter, prompt,
    prompt argument and resource, as a real client receives them, plus the
    server's instructions."""
    async with Client(mcp_server.mcp) as client:
        sent = [("instructions", "griot", client.instructions)]
        for tool in (await client.list_tools()).tools:
            sent.append(("tool", tool.name, tool.description))
            for param, schema in tool.input_schema.get("properties", {}).items():
                if schema.get("description"):
                    sent.append(("tool parameter", f"{tool.name}.{param}", schema["description"]))
        for prompt in (await client.list_prompts()).prompts:
            sent.append(("prompt", prompt.name, prompt.description))
            for argument in prompt.arguments or []:
                sent.append(("prompt argument", f"{prompt.name}.{argument.name}", argument.description))
        for resource in (await client.list_resources()).resources:
            sent.append(("resource", str(resource.uri), resource.description))
    return sent


@pytest.mark.anyio
async def test_no_description_sent_carries_a_maintainer_note():
    found = [(kind, name, _markers_in(text)) for kind, name, text in await _sent()
             if text and _markers_in(text)]
    assert found == []


@pytest.mark.anyio
async def test_every_prompt_and_every_prompt_argument_is_described_for_its_reader():
    """A prompt is what a person picks from a slash-command list: its
    arguments arrived with no description at all."""
    sent = await _sent()
    prompts = [entry for entry in sent if entry[0] in ("prompt", "prompt argument")]
    assert {name for kind, name, _ in prompts if kind == "prompt"} == {"stats", "history", "health", "overview"}
    assert all(text and text.strip() for _, _, text in prompts), prompts


# The arguments a client would pass, per prompt; a prompt added later without
# an entry here fails the test below, rather than going unrendered.
_PROMPT_ARGUMENTS = {"stats": {"days": "7"}, "history": {"question": "why is the lock a file?"},
                     "health": {}, "overview": {}}


@pytest.mark.anyio
async def test_every_prompt_still_renders_through_the_protocol():
    async with Client(mcp_server.mcp) as client:
        listed = {prompt.name for prompt in (await client.list_prompts()).prompts}
        assert listed == set(_PROMPT_ARGUMENTS)
        for name, arguments in _PROMPT_ARGUMENTS.items():
            rendered = await client.get_prompt(name, arguments)
            text = rendered.messages[0].content.text
            assert "griot_" in text, (name, text[:80])
            assert _markers_in(text) == {}, (name, _markers_in(text))
        assert "days=7" in (await client.get_prompt("stats", {"days": "7"})).messages[0].content.text
        assert "why is the lock a file?" in (
            await client.get_prompt("history", {"question": "why is the lock a file?"})).messages[0].content.text


@pytest.mark.anyio
@pytest.mark.parametrize("register", ["tool", "prompt"])
async def test_what_follows_the_maintainer_heading_stays_in_the_source(monkeypatch, register):
    """The rule itself, on a server of its own: the agent's part is sent
    whole, the maintainer's part not at all, whatever the indentation."""
    from mcp.server.mcpserver import MCPServer
    probe = MCPServer("probe")
    monkeypatch.setattr(mcp_server, "mcp", probe)

    def probe_fn() -> str:
        return "x"
    probe_fn.__doc__ = ("For the agent.\n\n    Second paragraph.\n\n    Maintainer notes:\n\n"
                        "    [review] Why this exists.\n    ")
    if register == "tool":
        mcp_server._tool()(probe_fn)
    else:
        mcp_server._prompt(name="probe_fn")(probe_fn)
    async with Client(probe) as client:
        if register == "tool":
            (listed,) = (await client.list_tools()).tools
        else:
            (listed,) = (await client.list_prompts()).prompts
    assert listed.description == "For the agent.\n\nSecond paragraph."


def test_a_docstring_that_is_only_maintainer_notes_is_refused(monkeypatch):
    """An empty description is a tool an agent cannot choose: refused when
    registered, not discovered by a client."""
    from mcp.server.mcpserver import MCPServer
    monkeypatch.setattr(mcp_server, "mcp", MCPServer("probe"))

    def probe_fn() -> str:
        return "x"
    probe_fn.__doc__ = "Maintainer notes:\n\n[review] Only this."
    with pytest.raises(ValueError, match="probe_fn"):
        mcp_server._tool()(probe_fn)


# The index tools are registered only when GRIOT_MCP_ENABLE_INDEX is set at
# import. Reloading the module here would swap objects other test modules
# hold, so a child process imports the server with it on and prints what a
# real client lists.
_LIST_INDEX_TOOLS = """
import json, anyio
from mcp.client.client import Client
from griot import mcp_server

async def listed():
    async with Client(mcp_server.mcp) as client:
        return [(t.name, t.description) for t in (await client.list_tools()).tools]

print(json.dumps(anyio.run(listed)))
"""


def test_the_index_tools_registered_on_demand_carry_no_maintainer_note_either(tmp_path):
    env = {**os.environ, "GRIOT_MCP_ENABLE_INDEX": "true",
           "GRIOT_CONFIG_DIR": str(tmp_path / "config"), "GRIOT_DATA_DIR": str(tmp_path / "data")}
    done = subprocess.run([sys.executable, "-c", _LIST_INDEX_TOOLS], env=env, cwd=tmp_path,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert done.returncode == 0, done.stderr
    tools = dict(json.loads(done.stdout.strip().splitlines()[-1]))
    assert {"griot_index_repo", "griot_index_wait"} <= set(tools)
    found = {name: _markers_in(text) for name, text in tools.items() if _markers_in(text)}
    assert found == {}
