"""A new golden-set case is a hybrid case unless asked otherwise.

griot search, griot ask and griot_search default to hybrid, so that is what
a reader gets; a case made in vector mode by default measured a search
nobody runs. `griot golden-set add` and griot_golden_set_add now default to
hybrid too, and write `mode: hybrid` into the case. Two things stay as they
were: a case WITHOUT a mode in the file is still a vector case (the trend
stays comparable), and on a collection without keyword vectors the default
falls back to vector the way search does, saying so and naming
`griot index keywords` (a hybrid case there would only ever be skipped).

Real tiny Edge indexes, with and without keyword vectors
(tests/test_keyword_search.py's documents and stand-in embedding)."""

import json

import pytest
from mcp.client.client import Client
from mcp_types import ElicitResult

from griot import cli, common, golden_set, mcp_server
from test_keyword_search import fake_embeddings, index, legacy_index  # noqa: F401

OLD_CASE = {"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]}


def _cases():
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


@pytest.fixture
def searches(monkeypatch):
    """The mode of every search run, in order."""
    calls = []
    real = common.search

    def recorded(query, *args, **kwargs):
        calls.append(kwargs.get("mode"))
        return real(query, *args, **kwargs)

    monkeypatch.setattr(common, "search", recorded)
    return calls


@pytest.fixture
def pick_first(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "1")


# --- griot golden-set add ----------------------------------------------------------------------


def test_add_without_a_mode_makes_a_hybrid_case_written_as_such(index, searches, pick_first, capsys):
    assert cli.main(["golden-set", "add", "acquire_lock", "--limit", "1"]) == 0
    assert searches == ["hybrid"]
    (case,) = _cases()
    assert case["mode"] == "hybrid", "written explicitly: absent would read as vector"
    out = capsys.readouterr()
    assert "hybrid" in out.out
    assert "griot index keywords" not in out.out + out.err, "nothing to build on this collection"


def test_add_without_a_mode_on_a_collection_without_keyword_vectors_makes_a_vector_case_and_says_so(
        legacy_index, searches, pick_first, capsys):
    assert cli.main(["golden-set", "add", "acquire_lock", "--limit", "1"]) == 0
    assert searches == ["vector"]
    (case,) = _cases()
    assert "mode" not in case and golden_set.case_mode(case) == "vector"
    out = capsys.readouterr()
    said = out.out + out.err
    assert "griot index keywords" in said and "vector" in said


def test_add_with_mode_vector_still_makes_a_vector_case(index, searches, pick_first, capsys):
    assert cli.main(["golden-set", "add", "acquire_lock", "--limit", "1", "--mode", "vector"]) == 0
    assert searches == ["vector"] and "mode" not in _cases()[0]
    assert "griot index keywords" not in capsys.readouterr().out


def test_a_case_already_in_the_file_without_a_mode_stays_vector(index, pick_first):
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text(json.dumps([OLD_CASE]))
    assert cli.main(["golden-set", "add", "acquire_lock", "--limit", "1"]) == 0
    old, new = _cases()
    assert old == OLD_CASE and golden_set.case_mode(old) == "vector"
    assert new["mode"] == "hybrid"


def test_the_help_says_the_default_is_hybrid(capsys):
    with pytest.raises(SystemExit):
        golden_set.main(["add", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "default: hybrid" in help_text
    assert "default: vector" not in help_text


# --- griot_golden_set_add ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_tool_without_a_mode_makes_a_hybrid_case(index):
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "confirm": True})).structured_content
    assert out["changed"] is True and "hybrid" in out["message"]
    assert "griot index keywords" not in out["message"]
    assert _cases()[0]["mode"] == "hybrid"


@pytest.mark.anyio
async def test_the_tool_without_a_mode_on_a_collection_without_keyword_vectors_makes_a_vector_case(legacy_index):
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "confirm": True})).structured_content
    assert out["changed"] is True
    assert "griot index keywords" in out["message"] and "vector" in out["message"]
    assert "mode" not in _cases()[0]


@pytest.mark.anyio
async def test_the_tool_with_mode_vector_makes_a_vector_case(index):
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "mode": "vector",
            "confirm": True})).structured_content
    assert out["changed"] is True and "mode" not in _cases()[0]


@pytest.mark.anyio
async def test_the_tool_schema_and_description_say_hybrid_is_the_default():
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_golden_set_add"]
    schema = tool.input_schema["properties"]["mode"]
    assert schema.get("default") != "vector"
    assert 'Default "vector"' not in tool.description
    assert "hybrid" in tool.description and "griot index keywords" in tool.description


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["legacy", "2026-07-28"])
async def test_a_person_is_asked_about_a_default_mode_case_and_their_yes_writes_it(index, protocol):
    """The question is resolved before the tool body runs: a default left
    as no mode there must not read as a mode that cannot run."""
    asked = []

    async def ask(ctx, params):
        asked.append(params)
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, mode=protocol, elicitation_callback=ask) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}]})).structured_content
    assert asked and out["changed"] is True
    assert _cases()[0]["mode"] == "hybrid"


class _NoElicitCtx:
    client_capabilities = None


@pytest.mark.anyio
async def test_the_hint_for_an_explicit_vector_case_keeps_the_mode(index, monkeypatch):
    """The terminal command now defaults to hybrid: an agent that asked for
    vector must be handed a command that searches by vector."""
    import shlex

    from test_cli_hints import _command_in

    out = await mcp_server.griot_golden_set_add("acquire_lock", must_include=[{"repo": "alpha"}], mode="vector",
                                                confirm=False, ctx=_NoElicitCtx())
    argv = shlex.split(_command_in(out["message"]))
    assert argv == ["griot", "golden-set", "add", "--mode", "vector", "--", "acquire_lock"]


@pytest.mark.anyio
async def test_a_query_with_no_word_to_match_falls_back_to_vector_as_search_does(index):
    """Hybrid refuses a query of only common words; the default never makes
    a case that could not run, it makes a vector one and says which."""
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "the and of", "must_include": [{"repo": "alpha"}], "confirm": True})).structured_content
    assert out["changed"] is True and "vector" in out["message"]
    assert "griot index keywords" not in out["message"], "the collection has keyword vectors: nothing to build"
    assert "mode" not in _cases()[0]
