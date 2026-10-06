"""What the server tells an agent about itself, before any tool is called.

An agent sees only tool NAMES until it loads them, and nothing told it when
griot is the right tool: real sessions had the server connected for days and
searched with grep instead. The instructions travel with the server, so they
reach every client without anyone installing anything.

They are text an agent acts on, so they are held to the tools: every tool and
every argument they name must exist, or the agent follows a capability that
is not there and reports work it did not do."""

import inspect
import re

import pytest
from mcp.client.client import Client

from griot import common, mcp_server


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
    # Raised from 1600 when keyword search came: the one place an agent learns
    # that an exact identifier in ANOTHER repository is now worth a search.
    # A string literal at column 0, not a docstring, so it measures the same
    # on every Python; measured like the tool description all the same, so
    # moving the text into a docstring cannot change what the limit means.
    assert _measured(instructions) <= 1700


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


def _as_python_before_3_13_sends_it(description):
    """The description as a server on Python 3.10 to 3.12 sends it. The SDK
    passes the function's __doc__ through untouched (no inspect.cleandoc),
    and before 3.13 the compiler kept the docstring's indentation, so every
    line after the first arrives with the four spaces of the function body
    (blank lines stay empty; the closing quotes sit on the last line)."""
    first, *rest = inspect.cleandoc(description).split("\n")
    return "\n".join([first] + [f"    {line}" if line else line for line in rest])


def _measured(text):
    """The length of the words an agent reads, the same on every Python:
    inspect.cleandoc removes the indentation Python < 3.13 leaves in a
    docstring, which is what 3.13 already does when it compiles one."""
    return len(inspect.cleandoc(text))


@pytest.mark.anyio
@pytest.mark.parametrize("sent_by", ["this python", "python < 3.13"])
async def test_the_search_tool_description_is_short_enough_on_every_python(sent_by):
    _, tools = await _server()
    description = tools["griot_search"].description
    if sent_by == "python < 3.13":
        description = _as_python_before_3_13_sends_it(description)
    # Raised from 1300 when the filters and the metadata of a result had to
    # be described, then held at 1700 for the search modes, a number read on
    # Python 3.10 where the same text measured 1692 with its indentation and
    # 1600 without it on 3.13. The ceiling is on the words, measured the same
    # on every Python, so a run on 3.13 cannot pass a text 3.10 would refuse.
    # Still a ceiling: every agent that loads the tool reads it.
    assert _measured(description) <= 1600


def _stored_fields():
    """Every field an indexer stores with a point, which is what a result's
    `metadata` can hold: the keys of the dict literals in the five indexers."""
    from pathlib import Path
    fields = set()
    for module in ("index_code", "index_commits", "index_tags", "index_branches", "index_platform"):
        fields |= set(re.findall(r'"([a-z_]+)":', (Path(common.__file__).parent / f"{module}.py").read_text()))
    return fields


@pytest.mark.anyio
async def test_the_search_tool_description_names_only_arguments_it_has():
    _, tools = await _server()
    tool = tools["griot_search"]
    # Besides arguments: the fields of what the tool returns, the fields of
    # the repository list the text sends the agent to for names, and the
    # kinds of source, which are the VALUES `source_types` takes (held to
    # what the indexers write by tests/test_search_filters.py).
    output = tool.output_schema
    returned = set(output["properties"]) | set(output["$defs"]["SearchResult"]["properties"])
    listed = set(tools["griot_repos_list"].output_schema["$defs"]["RepoEntry"]["properties"])
    allowed = set(tool.input_schema["properties"]) | returned | listed | set(common.SOURCE_TYPES)
    # And the values `mode` takes, read from the schema the agent gets.
    allowed |= set(tool.input_schema["properties"]["mode"]["enum"])
    # The names of stored fields are allowed ONLY in the paragraph that
    # introduces `metadata`. Anywhere else `author` or `state` in backticks
    # would read as an argument, which is the mistake this test exists for.
    for paragraph in tool.description.split("\n\n"):
        here = allowed | _stored_fields() if "`metadata`" in paragraph else allowed
        assert _code_words(paragraph) <= here, (_code_words(paragraph) - here, paragraph[:60])
    assert "name" in listed and "repo" in returned, "the two names the text points at exist"


@pytest.mark.anyio
async def test_no_text_promises_a_date_the_search_does_not_return():
    """Neither text may say the agent learns WHEN from a search unless a
    result carries what was stored with the source. It does now (`metadata`,
    with the date of a commit: tests/test_search_results.py asks a real
    client for it), so the texts say "when"; this holds them to that field."""
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
