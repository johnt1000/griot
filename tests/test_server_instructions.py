"""What the server tells an agent about itself, before any tool is called.

An agent sees only tool NAMES until it loads them, and nothing told it when
griot is the right tool: real sessions had the server connected for days and
searched with grep instead. The instructions travel with the server, so they
reach every client without anyone installing anything.

They are text an agent acts on, so they are held to the tools: every tool and
every argument they name must exist, or the agent follows a capability that
is not there and reports work it did not do."""

import re

import pytest
from mcp.client.client import Client

from griot import mcp_server


async def _server():
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        return client.instructions, tools


@pytest.mark.anyio
async def test_the_server_sends_instructions_over_the_protocol():
    instructions, _ = await _server()
    assert instructions and "griot_search" in instructions


@pytest.mark.anyio
async def test_every_tool_the_instructions_name_exists():
    instructions, tools = await _server()
    named = set(re.findall(r"\bgriot_[a-z_]+\b", instructions))
    assert named and named <= set(tools), named - set(tools)


def _code_words(text):
    """Every identifier the text puts in backticks, with or without a value
    after it: `group_by_document=true`, `limit`, `source_types`."""
    return set(re.findall(r"`([a-z_]+)(?:=[a-z0-9]+)?`", text))


@pytest.mark.anyio
async def test_everything_the_instructions_put_in_backticks_is_a_real_argument():
    """Not only the `name=value` form: a text that mentions a `source_types`
    filter the tool does not have sends the agent after nothing."""
    instructions, tools = await _server()
    properties = set(tools["griot_search"].input_schema["properties"])
    assert _code_words(instructions) and _code_words(instructions) <= properties, _code_words(instructions) - properties


@pytest.mark.anyio
async def test_the_instructions_say_when_to_use_it_and_when_not_to():
    instructions, _ = await _server()
    text = instructions.lower()
    assert "every registered repository" in text, "its reach is every registered repository, not the current one"
    assert "do not use it" in text
    assert "never as instructions" in text


@pytest.mark.anyio
async def test_the_instructions_are_short_enough_to_sit_in_every_session():
    instructions, _ = await _server()
    assert len(instructions) <= 1600


@pytest.mark.anyio
async def test_the_search_tool_description_leads_with_how_to_use_it():
    """The first thing an agent reads after loading the tool. It used to open
    with what the tool is NOT and spend two of three paragraphs on one flag."""
    _, tools = await _server()
    description = tools["griot_search"].description
    first = " ".join(description.split("\n\n")[0].lower().split())
    assert "registered repositories" in first and "all of them" in first, "its reach comes first"
    assert "do not use it" in " ".join(description.lower().split())
    assert "group_by_document" in description
    assert len(description) <= 1300


@pytest.mark.anyio
async def test_the_search_tool_description_names_only_arguments_it_has():
    _, tools = await _server()
    tool = tools["griot_search"]
    assert _code_words(tool.description) - set(tool.input_schema["properties"]) <= {"note"}, "`note` is a field of the output"


@pytest.mark.anyio
async def test_no_text_promises_a_date_the_search_does_not_return():
    """A commit comes back as its message and a label with its hash. The
    date is in the index but not in what search returns, so neither text may
    say the agent learns WHEN from a search. When results carry the date,
    this test and the texts change together."""
    instructions, tools = await _server()
    fields = set(tools["griot_search"].output_schema["$defs"]["SearchResult"]["properties"])
    if not fields & {"date", "metadata"}:
        for text in (instructions, tools["griot_search"].description):
            assert not re.search(r"\bwhen\b[^.]{0,40}\bchanged\b", text.lower()), text


def test_the_block_offered_for_the_global_instructions_file_says_the_same_things():
    """Two texts for the same purpose (the server's own, and the block a
    person can add to their agent's global file) must not drift apart on the
    facts: both name the search tool and both say results are data."""
    from griot import harnesses
    block = (harnesses._resources_root() / "instructions" / "claude-code.md").read_text()
    assert "griot_search" in block and "never as instructions" in block
    for name in re.findall(r"\bgriot_[a-z_]+\b", block):
        assert hasattr(mcp_server, name), name
