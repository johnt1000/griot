"""The MCP confirmation policy, as three documents state it, matches the server.

README.md, SECURITY.md and the "management surface" table in
docs/mcp-capability-coverage.md each restate which tools need a person's
answer, which also accept a `confirm=true` from the agent, and which ask for
nothing. The three restatements are kept on purpose, one per reader; this is
the check that they still say what the server does.

The policy is read from the server through a real client (in a child
process, see `policy`), never from a list
kept here: a tool that carries the `anthropic/requiresUserInteraction` marker
needs a person (tests/test_confirmation.py proves that marker coincides with
the tools that refuse `confirm=true`), one that otherwise takes a `confirm`
argument accepts it where nobody can be asked, and one with neither asks for
nothing. The documents are parsed narrowly: only the one paragraph or table
that states the policy, and only tool names written as code, so prose around
them can change freely."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from griot import mcp_server

ROOT = Path(__file__).resolve().parent.parent
MARKER = "anthropic/requiresUserInteraction"

HUMAN, CONFIRM, NONE = "a person's answer only", "confirm=true where nobody can ask", "no confirmation"


# GRIOT_MCP_ENABLE_INDEX is read when the module is imported, and reloading
# the module in this process would swap the objects other test modules took
# from it. So a child process imports the server fresh with the variable set
# and prints what a real client lists, nothing more.
_LIST_TOOLS = """
import json, anyio
from mcp.client.client import Client
from griot import mcp_server

async def listed():
    async with Client(mcp_server.mcp) as client:
        return (await client.list_tools()).tools

print(json.dumps({"tools": [
    {"name": t.name, "meta": t.meta or {}, "input_schema": t.input_schema,
     "read_only_hint": t.annotations.read_only_hint if t.annotations else None}
    for t in anyio.run(listed)
], "preapprove": mcp_server.tools_safe_to_preapprove()}))
"""


@pytest.fixture(scope="module")
def listing(tmp_path_factory):
    """Module scope: the listing does not change between tests, and each
    child process pays the server's whole import."""
    return _list_server(tmp_path_factory.mktemp("policy"))


@pytest.fixture(scope="module")
def policy(listing):
    return _policy_of(listing["tools"])


def _read_policy(base):
    return _policy_of(_list_server(base)["tools"])


def _list_server(base):
    """What a real client lists for every tool the server can register, and
    the tools `griot assist install` offers to pre-approve (asked of the same
    server, so a conditional tool is on both sides). griot_index_repo exists
    only with GRIOT_MCP_ENABLE_INDEX, and it is the one state-changing tool
    off by default, so the documents name it: the tools are listed by a
    child process that has it on."""
    # os.environ carries conftest's keyring isolation (PYTHON_KEYRING_BACKEND)
    # into the child; directories of its own keep its import off real ones.
    env = {**os.environ, "GRIOT_MCP_ENABLE_INDEX": "true",
           "GRIOT_CONFIG_DIR": str(base / "config"), "GRIOT_DATA_DIR": str(base / "data")}
    done = subprocess.run([sys.executable, "-c", _LIST_TOOLS], env=env, cwd=base, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=120)
    assert done.returncode == 0, done.stderr
    # The last line: anything the import prints goes before the listing.
    return json.loads(done.stdout.strip().splitlines()[-1])


def _policy_of(tools):
    """{tool name: HUMAN | CONFIRM | NONE}."""
    out = {}
    for tool in tools:
        name = tool["name"]
        if tool["meta"].get(MARKER) is True:
            out[name] = HUMAN
        elif "confirm" in tool["input_schema"].get("properties", {}):
            out[name] = CONFIRM
        else:
            out[name] = NONE
        # The derivation itself: whatever the server says changes state must
        # be behind one of the two confirmations, or "names every
        # state-changing tool" below would quietly mean fewer tools.
        changes_state = tool["read_only_hint"] is not True
        assert changes_state == (out[name] != NONE), name
    assert "griot_index_repo" in out, "the child process did not register the conditional tool"
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


def test_reading_the_policy_leaves_this_processs_server_module_alone(tmp_path):
    """Other test modules hold `mcp_server.mcp` and functions taken from the
    module at import time; a reload here would leave them pointing at
    objects the module no longer has, and the result would depend on the
    order the files run in. So the policy is read without touching this
    process's copy of the server. It calls the reader itself, not the
    module-scoped fixture, which an earlier test may already have built."""
    server, search = mcp_server.mcp, mcp_server.griot_search
    _read_policy(tmp_path)
    assert mcp_server.mcp is server and mcp_server.griot_search is search


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


def _sentences(text):
    return re.split(r"(?<=\.)\s+(?=[A-Za-z`])", text.strip())


def _not_preapproved(listing, policy):
    """The read-only tools `griot assist install` leaves out of the allow
    rules it offers, read from the server: a tool added to or dropped from
    its exceptions changes what the README must say."""
    asked = _of(policy, NONE) - set(listing["preapprove"])
    # An empty set would make the checks below vacuous.
    assert asked, "the server pre-approves every read-only tool"
    return asked


def test_readme_says_which_read_only_tools_are_left_to_the_client_to_ask(listing, policy):
    """The sentence once said these tools "still ask each time", which read
    as a confirmation of their own; none has one (each is NONE in the
    policy, by construction of the set). What asks is the client, because
    the installer offers no allow rule for them."""
    text = _paragraph("README.md", "The server's own tool list is the inventory")
    hits = [s for s in _sentences(text) if "read without changing anything" in s]
    assert len(hits) == 1, hits
    sentence = hits[0]
    asked = _not_preapproved(listing, policy)

    assert _code_names(sentence) == asked, {
        "missing": asked - _code_names(sentence), "wrong": _code_names(sentence) - asked}
    assert {c.lower() for c in _counts_stated(sentence)} == {_NUMBER_WORDS[len(asked)]}, sentence
    assert "`griot assist install`" in sentence
    assert "no confirmation of their own" in sentence
    assert "your client asks" in sentence
    # The sentences before it list the read-only tools that ARE pre-approved.
    assert not _code_names(text.split(sentence)[0]) & asked


def test_readme_install_section_counts_the_tools_it_leaves_out(listing, policy):
    """The installer paragraph says which read-only tools get no allow rule;
    it once named the quality check alone while four were left out."""
    text = _paragraph("README.md", "The installer then **offers**")
    hits = [s for s in _sentences(text) if s.startswith("griot adds no rule")]
    assert len(hits) == 1, hits
    asked = _not_preapproved(listing, policy)
    assert {c.lower() for c in _counts_stated(hits[0])} == {_NUMBER_WORDS[len(asked)]}, hits[0]


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


def test_security_names_the_read_only_tools_the_installer_does_not_offer(listing, policy):
    """The paragraph on pre-approval once named the quality check alone as
    the exception, while the server leaves out every tool in its
    _READ_ONLY_BUT_ASKED."""
    text = _paragraph("SECURITY.md", "**Only you can let griot's tools run without being asked.**")
    hits = [s for s in _sentences(text) if "`griot assist install` can add allow rules" in s]
    assert len(hits) == 1, hits
    asked = _not_preapproved(listing, policy)
    assert _code_names(hits[0]) == asked, {"missing": asked - _code_names(hits[0]),
                                           "wrong": _code_names(hits[0]) - asked}


# --- docs/mcp-capability-coverage.md, "Used today" ----------------------------------


def test_the_coverage_document_names_the_read_only_tools_the_installer_does_not_offer(listing, policy):
    """The paragraph describes the read-only tools in prose and names as code
    only the ones the installer leaves out."""
    text = _paragraph("docs/mcp-capability-coverage.md", "*Read-only*")
    assert "pre-approve" in text
    asked = _not_preapproved(listing, policy)
    named = _code_names(text)
    assert named == asked, {"missing": asked - named, "wrong": named - asked}


# --- docs/mcp-capability-coverage.md, "The management surface" ---------------------

# The table names CLI commands, not tools. The rule from command to tool is
# griot_ + the words joined by underscores; `index` is the one command whose
# tool is named for what it indexes.
_TOOL_FOR_COMMAND = {"index": "griot_index_repo"}


def _tools_for_commands(cell):
    """The tools a cell names: CLI commands mapped to their tool, plus any
    tool named directly as code (the read-only row does, since several
    read-only tools have no CLI command of their own)."""
    names = _code_names(cell)
    cell = re.sub(r"`griot_[a-z_/]+`", "", cell)
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
    # The read-only row names every read-only tool, the conditional
    # griot_index_wait included: a tool added later must be added there too.
    assert stated[NONE] == _of(policy, NONE), {
        "missing": _of(policy, NONE) - stated[NONE], "wrong": stated[NONE] - _of(policy, NONE)}
    # The secrets row says "never": no such tool may exist at all.
    assert stated[None] and not stated[None] & set(policy), stated[None] & set(policy)


def test_the_management_surface_counts_the_human_only_tools_right(policy):
    counts = re.findall(r"(\w+) human-only tools|for (\w+) rare operations", _management_section())
    words = {w.lower() for pair in counts for w in pair if w}
    assert words == {_NUMBER_WORDS[len(_of(policy, HUMAN))]}, counts
