"""Confirmation flow of the state-changing MCP tools.

The human is asked through the SDK's resolver mechanism, which works on the
protocol Claude Code negotiates (2026-07-28) as well as on older ones. The
rule these tests protect: only an ACCEPTED answer carrying the confirmation
model authorizes. A client that cannot ask must never read as a yes."""

import pytest
from mcp.client.client import Client
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.mcpserver.resolve import Elicit
from mcp_types import ElicitResult

from griot import mcp_server, repos


from test_mcp_server import _accepted  # noqa: E402  same helper, one definition


class _Caps:
    def __init__(self, elicitation):
        self.elicitation = elicitation


class _Ctx:
    def __init__(self, elicitation=None):
        self.client_capabilities = _Caps(elicitation)


# --- _confirmed: what each kind of answer means ------------------------------


@pytest.mark.anyio
async def test_an_accepted_answer_authorizes():
    ok, refusal = await mcp_server._confirmed(None, "Do it?", confirm=False, cli_hint="griot x",
                                              answer=_accepted())
    assert ok is True and refusal is None


@pytest.mark.anyio
async def test_an_accepted_answer_carrying_the_wrong_data_does_not_authorize():
    """The SDK wraps a resolver's non-question return value as an accept. That
    is how a client that cannot ask reaches the tool, so it must read as 'no
    channel', never as a yes."""
    answer = AcceptedElicitation(data=mcp_server._NO_CHANNEL)
    ok, refusal = await mcp_server._confirmed(None, "Do it?", confirm=False, cli_hint="griot x",
                                              answer=answer)
    assert ok is False
    assert "confirm=true" in refusal


@pytest.mark.anyio
async def test_a_declined_answer_is_a_no_and_says_nothing_changed():
    ok, refusal = await mcp_server._confirmed(None, "Do it?", confirm=False, cli_hint="griot x",
                                              answer=DeclinedElicitation())
    assert ok is False
    assert "Declined" in refusal and "Nothing was changed" in refusal
    assert "griot x" not in refusal, "an explicit no is not a prompt to try the terminal"
    assert "confirm=true" not in refusal, "an explicit no must not teach the agent to retry with confirm"


@pytest.mark.anyio
async def test_a_dismissed_answer_is_distinct_from_a_decline():
    ok, refusal = await mcp_server._confirmed(None, "Do it?", confirm=False, cli_hint="griot x",
                                              answer=CancelledElicitation())
    assert ok is False
    assert "Dismissed" in refusal and "Nothing was changed" in refusal
    assert "confirm=true" not in refusal


@pytest.mark.anyio
async def test_no_answer_falls_back_to_the_confirm_argument():
    ok, refusal = await mcp_server._confirmed(None, "Delete x?", confirm=False, cli_hint="griot x")
    assert ok is False
    assert "confirm=true" in refusal and "Delete x?" in refusal and "griot x" in refusal


@pytest.mark.anyio
async def test_confirm_argument_authorizes_an_ordinary_operation():
    ok, refusal = await mcp_server._confirmed(None, "Do it?", confirm=True, cli_hint="griot x")
    assert ok is True and refusal is None


@pytest.mark.anyio
async def test_human_required_ignores_the_confirm_argument():
    ok, refusal = await mcp_server._confirmed(None, "Widen?", confirm=True, cli_hint="griot x",
                                              human_required=True)
    assert ok is False
    assert "confirm=true" not in refusal and "griot x" in refusal


@pytest.mark.anyio
async def test_human_required_accepts_a_real_human_answer():
    ok, _ = await mcp_server._confirmed(None, "Widen?", confirm=False, cli_hint="griot x",
                                        human_required=True, answer=_accepted())
    assert ok is True


@pytest.mark.anyio
async def test_human_required_with_no_channel_needs_the_terminal():
    answer = AcceptedElicitation(data=mcp_server._NO_CHANNEL)
    ok, refusal = await mcp_server._confirmed(None, "Widen?", confirm=False, cli_hint="griot x",
                                              human_required=True, answer=answer)
    assert ok is False
    assert "needs a person" in refusal and "griot x" in refusal


# --- the resolver: when to ask -----------------------------------------------


def test_resolver_asks_when_the_client_can_ask():
    out = mcp_server._resolve_ask(_Ctx(elicitation=object()), "Do it?", confirm=False, human_required=False)
    assert isinstance(out, Elicit)
    assert out.message == "Do it?" and out.schema is mcp_server._Ask


def test_resolver_does_not_ask_a_client_that_cannot():
    out = mcp_server._resolve_ask(_Ctx(elicitation=None), "Do it?", confirm=False, human_required=False)
    assert out is mcp_server._NO_CHANNEL


def test_resolver_does_not_bother_the_human_when_confirm_already_authorizes():
    out = mcp_server._resolve_ask(_Ctx(elicitation=object()), "Do it?", confirm=True, human_required=False)
    assert out is mcp_server._NO_CHANNEL


def test_resolver_still_asks_a_human_only_operation_even_with_confirm():
    out = mcp_server._resolve_ask(_Ctx(elicitation=object()), "Widen?", confirm=True, human_required=True)
    assert isinstance(out, Elicit)


def test_the_confirmation_schema_has_no_fields_and_no_leaked_docstring():
    schema = mcp_server._Ask.model_json_schema()
    assert schema.get("properties", {}) == {}
    assert "description" not in schema


# --- end to end through a real MCP client, on both protocols ------------------

MODES = ["legacy", "2026-07-28"]


async def _remove_via_client(mode, action, tmp_path, monkeypatch, with_callback=True):
    removed = []
    monkeypatch.setattr(repos, "remove_repo", lambda p: removed.append(p) or p)

    async def callback(ctx, params):
        return ElicitResult(action=action, content={})

    kwargs = {"mode": mode}
    if with_callback:
        kwargs["elicitation_callback"] = callback
    async with Client(mcp_server.mcp, **kwargs) as client:
        result = await client.call_tool("griot_repos_remove", {"path": "/tmp/some-repo"})
    return removed, result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_remove_happens_only_when_the_human_accepts(mode, tmp_path, monkeypatch):
    removed, out = await _remove_via_client(mode, "accept", tmp_path, monkeypatch)
    assert removed == ["/tmp/some-repo"] and out["changed"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_remove_does_not_happen_when_the_human_says_no(mode, action, tmp_path, monkeypatch):
    removed, out = await _remove_via_client(mode, action, tmp_path, monkeypatch)
    assert removed == [] and out["changed"] is False
    assert "Nothing was changed" in out["message"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_a_client_that_cannot_ask_never_reads_as_a_yes(mode, tmp_path, monkeypatch):
    removed, out = await _remove_via_client(mode, "accept", tmp_path, monkeypatch, with_callback=False)
    assert removed == [] and out["changed"] is False
    assert "confirm=true" in out["message"]


@pytest.mark.anyio
async def test_the_answer_parameter_is_not_part_of_the_agents_input_schema():
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools["griot_repos_remove"].input_schema["properties"]) == {"path", "confirm"}


# --- every migrated tool, end to end -------------------------------------------
# name, arguments, module+attribute that performs the change, human_required
def _effects(monkeypatch):
    calls = []
    monkeypatch.setattr(repos, "add_repo", lambda p: calls.append("repos_add") or p)
    monkeypatch.setattr(repos, "remove_repo", lambda p: calls.append("repos_remove") or p)
    monkeypatch.setattr(mcp_server.cli, "delete_profile", lambda name, **kw: calls.append("profiles_delete") or "deleted")
    monkeypatch.setattr(mcp_server.golden_set, "remove_case", lambda i: calls.append("golden_set_remove") or {"query": "q"})
    monkeypatch.setattr(mcp_server.golden_set, "add_case",
                        lambda **kw: calls.append("golden_set_add") or {"query": "q", "must_include": [{"repo": "r"}]})
    monkeypatch.setattr(mcp_server.harnesses, "install_many", lambda *a, **k: calls.append("assist_install") or [])
    return calls


TOOLS = [
    ("griot_repos_add", {"path": "/tmp/some-repo"}, "repos_add", True),
    ("griot_repos_remove", {"path": "/tmp/some-repo"}, "repos_remove", False),
    ("griot_profiles_delete", {"profile": "bge-small"}, "profiles_delete", True),
    ("griot_golden_set_remove", {"index": 1}, "golden_set_remove", False),
    ("griot_golden_set_add", {"query": "q", "must_include": [{"repo": "r"}]}, "golden_set_add", False),
    ("griot_assist_install", {"harness": "claude-code", "scope": "local"}, "assist_install", True),
]
IDS = [t[0] for t in TOOLS]


async def _call(mode, name, args, action, monkeypatch, with_callback=True):
    calls = _effects(monkeypatch)

    async def callback(ctx, params):
        return ElicitResult(action=action, content={})

    kwargs = {"mode": mode}
    if with_callback:
        kwargs["elicitation_callback"] = callback
    async with Client(mcp_server.mcp, **kwargs) as client:
        result = await client.call_tool(name, args)
    return calls, result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name,args,effect,human", TOOLS, ids=IDS)
async def test_every_tool_acts_on_an_accept(mode, name, args, effect, human, monkeypatch):
    calls, out = await _call(mode, name, args, "accept", monkeypatch)
    assert calls == [effect], out


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("action", ["decline", "cancel"])
@pytest.mark.parametrize("name,args,effect,human", TOOLS, ids=IDS)
async def test_every_tool_does_nothing_on_a_no(mode, action, name, args, effect, human, monkeypatch):
    calls, out = await _call(mode, name, args, action, monkeypatch)
    assert calls == [], out
    assert "Nothing was changed" in str(out)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name,args,effect,human", TOOLS, ids=IDS)
async def test_no_tool_reads_a_client_that_cannot_ask_as_a_yes(mode, name, args, effect, human, monkeypatch):
    calls, out = await _call(mode, name, args, "accept", monkeypatch, with_callback=False)
    assert calls == [], out


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name,args,effect,human", [t for t in TOOLS if t[3]], ids=[t[0] for t in TOOLS if t[3]])
async def test_human_only_tools_ignore_confirm_when_nobody_can_ask(mode, name, args, effect, human, monkeypatch):
    calls, out = await _call(mode, name, {**args, "confirm": True}, "accept", monkeypatch, with_callback=False)
    assert calls == [], out


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,effect,human", TOOLS, ids=IDS)
async def test_the_answer_parameter_never_reaches_the_agents_schema(name, args, effect, human):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "answer" not in tools[name].input_schema["properties"]


# --- resolvers skip the question when the call cannot succeed ------------------


def test_assist_install_is_not_asked_about_an_unknown_harness_or_scope():
    ctx = _Ctx(elicitation=object())
    assert mcp_server._ask_assist_install(ctx, harness="nonsense", scope="local") is mcp_server._NO_CHANNEL
    assert mcp_server._ask_assist_install(ctx, harness="all", scope="nonsense") is mcp_server._NO_CHANNEL
    assert isinstance(mcp_server._ask_assist_install(ctx, harness="all", scope="local"), Elicit)


def test_golden_set_add_is_not_asked_when_the_limit_is_invalid():
    ctx = _Ctx(elicitation=object())
    assert mcp_server._ask_golden_set_add(ctx, query="q", limit=0) is mcp_server._NO_CHANNEL
    assert isinstance(mcp_server._ask_golden_set_add(ctx, query="q", limit=5), Elicit)


def test_index_repo_is_not_asked_about_a_path_that_would_be_refused(monkeypatch):
    ctx = _Ctx(elicitation=object())
    monkeypatch.setattr(mcp_server.jobs, "index_job_refusal", lambda p: "not allowed")
    assert mcp_server._ask_index_repo(ctx, path="/tmp/x") is mcp_server._NO_CHANNEL
    monkeypatch.setattr(mcp_server.jobs, "index_job_refusal", lambda p: None)
    assert isinstance(mcp_server._ask_index_repo(ctx, path="/tmp/x"), Elicit)
