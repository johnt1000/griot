"""The MCP confirmation policy, as three documents state it, matches the server.

README.md, SECURITY.md and the "management surface" table in
docs/mcp-capability-coverage.md each restate which tools need a person's
answer, which also accept a `confirm=true` from the agent, and which ask for
nothing. The three restatements are kept on purpose, one per reader; this is
the check that they still say what the server does.

The policy is read from the server through a real client, never from a list
kept here: a tool that carries the `anthropic/requiresUserInteraction` marker
needs a person (tests/test_confirmation.py proves that marker coincides with
the tools that refuse `confirm=true`), one that otherwise takes a `confirm`
argument accepts it where nobody can be asked, and one with neither asks for
nothing. The documents are parsed narrowly: only the one paragraph or table
that states the policy, and only tool names written as code, so prose around
them can change freely."""

import importlib
import re
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client

from griot import mcp_server

ROOT = Path(__file__).resolve().parent.parent
MARKER = "anthropic/requiresUserInteraction"

HUMAN, CONFIRM, NONE = "a person's answer only", "confirm=true where nobody can ask", "no confirmation"


async def _listed_tools():
    async with Client(mcp_server.mcp) as client:
        return (await client.list_tools()).tools


@pytest.fixture
def policy(monkeypatch):
    """{tool name: HUMAN | CONFIRM | NONE} for every tool the server can
    register. griot_index_repo exists only with GRIOT_MCP_ENABLE_INDEX, and
    it is the one state-changing tool off by default, so the documents name
    it: the module is reloaded with it on, then back to the default."""
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    importlib.reload(mcp_server)
    try:
        tools = anyio.run(_listed_tools)
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX", raising=False)
        importlib.reload(mcp_server)
    out = {}
    for tool in tools:
        if (tool.meta or {}).get(MARKER) is True:
            out[tool.name] = HUMAN
        elif "confirm" in tool.input_schema.get("properties", {}):
            out[tool.name] = CONFIRM
        else:
            out[tool.name] = NONE
        # The derivation itself: whatever the server says changes state must
        # be behind one of the two confirmations, or "names every
        # state-changing tool" below would quietly mean fewer tools.
        changes_state = tool.annotations.read_only_hint is not True
        assert changes_state == (out[tool.name] != NONE), tool.name
    assert "griot_index_repo" in out, "the reload did not register the conditional tool"
    return out


def _of(policy, kind):
    return {name for name, k in policy.items() if k == kind}


def _code_names(text):
    """Every `griot_...` tool name written as code, with the shorthand
    `griot_repos_add/remove` expanded to both tools."""
    names = set()
    for first, rest in re.findall(r"`(griot_[a-z_]+)((?:/[a-z_]+)*)`", text):
        names.add(first)
        stem = first.rsplit("_", 1)[0]
        names.update(f"{stem}_{alt}" for alt in rest.split("/")[1:])
    return names


def _paragraph(path, opening):
    """The one paragraph (or list item) that begins with `opening`."""
    hits = [block for block in re.split(r"\n\s*\n|\n(?=- )", (ROOT / path).read_text())
            if block.lstrip("- ").startswith(opening)]
    assert len(hits) == 1, f"{path}: expected one paragraph starting {opening!r}, found {len(hits)}"
    return hits[0]


_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _counts_stated(text):
    """Every number word in `text`: the paragraphs that state the policy
    also COUNT the human-only tools ("Those three ..."), and a fourth such
    tool would leave that count wrong while the names were updated."""
    return re.findall(r"\b(" + "|".join(_NUMBER_WORDS[2:]) + r")\b", text, flags=re.IGNORECASE)


# --- the derivation, sanity --------------------------------------------------------


def test_the_policy_has_all_three_kinds(policy):
    """Not a copy of the policy: only that the derivation found each kind,
    so a broken derivation cannot make every document check vacuous."""
    assert _of(policy, HUMAN) and _of(policy, CONFIRM) and _of(policy, NONE)
    assert policy["griot_search"] == NONE


# --- README.md ---------------------------------------------------------------------


def test_readme_names_every_state_changing_tool_and_which_need_a_person(policy):
    text = _paragraph("README.md", "The tools that change something")
    listing, _, rest = text.partition(". ")
    # The bold claim naming the tools that lack the confirm=true fallback.
    human_sentence = re.findall(r"\*\*[^*]*do not have it\*\*", rest)
    assert len(human_sentence) == 1, "README: the bold sentence naming the tools without the confirm=true fallback"

    assert _code_names(listing) == _of(policy, HUMAN) | _of(policy, CONFIRM), \
        "README's first sentence claims to list the tools that change something"
    assert _code_names(human_sentence[0]) == _of(policy, HUMAN), \
        "README: the tools that accept only a person's answer"
    counts = _counts_stated(text)
    assert counts and {c.lower() for c in counts} == {_NUMBER_WORDS[len(_of(policy, HUMAN))]}, counts


# --- SECURITY.md -------------------------------------------------------------------


def test_security_names_every_state_changing_tool_and_which_need_a_person(policy):
    text = _paragraph("SECURITY.md", "The tools that change state")
    confirmed, sep, exceptions = text.partition("**except**")
    assert sep, "SECURITY.md: the '**except**' that introduces the tools needing a person"
    # Up to the exception, the paragraph names the tools and their fallback;
    # after it, the human-only tools, until the link to the full table.
    exceptions = exceptions.split("(Full policy table")[0]

    assert _code_names(confirmed) == _of(policy, HUMAN) | _of(policy, CONFIRM), \
        "SECURITY.md claims to list the tools that change state"
    assert _code_names(exceptions) == _of(policy, HUMAN), \
        "SECURITY.md: the tools that accept only a person's answer"
    counts = _counts_stated(text)
    assert counts and {c.lower() for c in counts} == {_NUMBER_WORDS[len(_of(policy, HUMAN))]}, counts


# --- docs/mcp-capability-coverage.md, "The management surface" ---------------------

# The table names CLI commands, not tools. The rule from command to tool is
# griot_ + the words joined by underscores; `index` is the one command whose
# tool is named for what it indexes.
_TOOL_FOR_COMMAND = {"index": "griot_index_repo"}


def _tools_for_commands(cell):
    names = set()
    for command in re.findall(r"`([a-z][a-z /-]*)`", cell):
        head, *alts = command.split("/")
        words = head.split()
        for last in [words[-1], *alts]:
            cli = " ".join([*words[:-1], last])
            names.add(_TOOL_FOR_COMMAND.get(cli, "griot_" + re.sub(r"[ -]", "_", cli)))
    return names


def _management_section():
    text = (ROOT / "docs/mcp-capability-coverage.md").read_text()
    return text.split("## The management surface", 1)[1].split("\n## ", 1)[0]


def _management_rows():
    section = _management_section()
    rows = [line for line in section.splitlines() if line.startswith("|") and not line.startswith("|---")]
    header, *body = rows
    assert header.split("|")[1:4] == [" Kind of operation ", " Over MCP ", " Mechanism "]
    return [[cell.strip() for cell in row.split("|")[1:4]] for row in body]


def _kind_of_row(over_mcp, mechanism):
    """What the row's own words say the mechanism is: the check reads the
    claim, not the position of the row, so moving a command to another row
    is a change of policy the test sees."""
    if "never" in over_mcp:
        return None
    if "human_required=True" in mechanism and "no argument bypasses it" in mechanism:
        return HUMAN
    if "`confirm=true` otherwise" in mechanism:
        return CONFIRM
    if mechanism == "plain tool":
        return NONE
    raise AssertionError(f"management surface: a mechanism this check cannot classify: {mechanism!r}")


def test_the_management_surface_table_states_each_tools_policy(policy):
    stated = {HUMAN: set(), CONFIRM: set(), NONE: set(), None: set()}
    for kind_of_operation, over_mcp, mechanism in _management_rows():
        stated[_kind_of_row(over_mcp, mechanism)] |= _tools_for_commands(kind_of_operation)

    # The two confirmed rows are the whole state-changing surface.
    assert stated[HUMAN] == _of(policy, HUMAN)
    assert stated[CONFIRM] == _of(policy, CONFIRM)
    # The read-only row names examples, so each one only has to be right.
    assert stated[NONE] and stated[NONE] <= _of(policy, NONE), stated[NONE] - _of(policy, NONE)
    # The secrets row says "never": no such tool may exist at all.
    assert stated[None] and not stated[None] & set(policy), stated[None] & set(policy)


def test_the_management_surface_counts_the_human_only_tools_right(policy):
    counts = re.findall(r"(\w+) human-only tools|for (\w+) rare operations", _management_section())
    words = {w.lower() for pair in counts for w in pair if w}
    assert words == {_NUMBER_WORDS[len(_of(policy, HUMAN))]}, counts
