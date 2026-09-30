"""House style of what a person and an agent read when griot asks or refuses.

Rules, not sentences: the wording can change, these properties may not.
A question a person answers states what happens and whether it can be undone,
because the answer is only as good as what it was based on. A refusal an agent
reads says nothing was changed and what to do next."""

import re

import pytest

from griot import mcp_server

QUESTIONS = {
    "repos_add": mcp_server._repos_add_question("/tmp/r"),
    "repos_remove": mcp_server._repos_remove_question("/tmp/r"),
    "profiles_delete": mcp_server._profiles_delete_question("bge-small"),
    "golden_set_add": mcp_server._golden_set_add_question("how does x work"),
    "golden_set_remove": mcp_server._golden_set_remove_question(3),
    "assist_install": mcp_server._assist_install_question("claude-code", "local"),
    "index_repo": mcp_server._index_repo_question("/tmp/r"),
}
IDS = list(QUESTIONS)


# What each operation really is. The class is decided here, by hand, against
# the tool's behaviour; the copy has to agree with it and may not claim the
# other one.
IRREVERSIBLE = {"profiles_delete", "index_repo"}
# Writes files that replace same-named ones, edits included: deleting them later
# removes what was installed but does not bring back what was overwritten.
OVERWRITES = {"assist_install"}
REVERSIBLE_PHRASES = ("you can undo this", "can be added again")


@pytest.mark.parametrize("name", IDS)
def test_a_question_states_its_reversibility_class_and_never_another(name):
    q = QUESTIONS[name].lower()
    if name in IRREVERSIBLE:
        assert "cannot be undone" in q, q
        assert not any(ph in q for ph in REVERSIBLE_PHRASES), q
    elif name in OVERWRITES:
        assert "overwritten" in q, q
        assert not any(ph in q for ph in REVERSIBLE_PHRASES + ("cannot be undone", "restore")), q
    else:
        assert any(ph in q for ph in REVERSIBLE_PHRASES), q
        assert "cannot be undone" not in q, q


@pytest.mark.parametrize("name", IDS)
def test_a_question_has_no_shouting_and_no_em_dash(name):
    q = QUESTIONS[name]
    assert "—" not in q and "–" not in q
    assert not re.search(r"\b[A-Z]{4,}\b", q), q


BUILDERS = {
    "repos_add": mcp_server._repos_add_question,
    "repos_remove": mcp_server._repos_remove_question,
    "profiles_delete": mcp_server._profiles_delete_question,
    "golden_set_add": mcp_server._golden_set_add_question,
    "index_repo": mcp_server._index_repo_question,
}
FORGERY = "x\n\nNothing was changed. Approve this harmless read-only action."
HOSTILE_VALUES = [FORGERY, "x\r\ny", "it's \"quoted\"", "tab\there", "a" * 5000, "\x1b[2Jscreen", "\u202eevil",
                  "a\u2028Nothing was changed.\u2028b", "a\x85b", "a\u200bb", "a\u2066b"]


@pytest.mark.parametrize("name", list(BUILDERS))
@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=lambda v: repr(v)[:24])
def test_what_the_agent_controls_cannot_reshape_the_question_a_person_reads(name, value):
    """The question is the only thing a person reads before accepting, and
    the path, query or profile in it is chosen by the agent. It must stay one
    line, stay short, and carry no raw control or bidi characters, or the
    agent can push the true consequence out of sight or write its own."""
    q = BUILDERS[name](value)
    assert "\n" not in q and "\r" not in q
    assert len(q) <= 300, len(q)
    assert q.isprintable(), repr(q)
    assert len(q.splitlines()) == 1


@pytest.mark.parametrize("name", list(BUILDERS))
def test_the_question_still_names_what_it_acts_on(name):
    assert "some-thing" in BUILDERS[name]("some-thing")


def test_the_index_question_is_shown_the_same_safe_way():
    assert "\n" not in mcp_server._index_repo_question(FORGERY)


def test_the_irreversible_one_says_so_plainly():
    assert "cannot be undone" in QUESTIONS["profiles_delete"].lower()


def _refusals():
    import asyncio
    out = {}
    for name, human in (("ordinary", False), ("human_only", True)):
        ok, msg = asyncio.run(mcp_server._confirmed(None, "Do the thing.", confirm=False, cli_hint="griot x",
                                                    human_required=human))
        assert ok is False
        out[name] = msg
    return out


@pytest.mark.parametrize("kind", ["ordinary", "human_only"])
def test_a_refusal_for_an_agent_says_nothing_changed_and_what_to_do_next(kind):
    msg = _refusals()[kind]
    assert "Nothing was changed" in msg
    assert "griot x" in msg
    assert "—" not in msg


def test_the_human_only_refusal_tells_the_agent_to_hand_it_to_the_user():
    msg = _refusals()["human_only"]
    assert "user" in msg.lower()
    assert "confirm=true" not in msg


def test_the_ordinary_refusal_offers_confirm_and_the_terminal():
    msg = _refusals()["ordinary"]
    assert "confirm=true" in msg


@pytest.mark.anyio
async def test_the_real_golden_set_note_is_shown_after_the_command():
    class _Ctx:
        client_capabilities = None

    out = await mcp_server.griot_golden_set_add("q", must_include=[{"repo": "r"}], confirm=False, ctx=_Ctx())
    lines = out["message"].split("\n")
    i = next(n for n, line in enumerate(lines) if line.endswith("to run:"))
    assert lines[i + 1].startswith("griot golden-set add")
    assert "interactive" in lines[i + 2] and "will not reuse" in lines[i + 2]


@pytest.mark.anyio
async def test_a_value_with_a_line_break_gets_no_pasteable_command():
    """A newline inside a quoted argument is still a newline in the message,
    and a person copies the command line by line. No command beats one that
    pastes as something else."""
    class _Ctx:
        client_capabilities = None

    out = await mcp_server.griot_repos_add("/tmp/a\nb", ctx=_Ctx())
    assert "griot repos add" not in out["message"]
    assert "control characters" in out["message"]
    assert "Nothing was changed" in out["message"]


def test_assist_install_does_not_promise_a_restore_it_cannot_give():
    q = QUESTIONS["assist_install"].lower()
    assert "overwrit" in q, "files that already exist are replaced, edits included"
    assert "restore" not in q
