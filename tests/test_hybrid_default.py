"""Hybrid search is the default of every reader-facing surface.

`griot search`, `griot ask` and the MCP `griot_search` rank by meaning and by
the exact words fused, unless a mode is asked for. A collection indexed before
keyword search cannot run hybrid: there the DEFAULT falls back to vector and
says so, with the command that builds keyword search; an EXPLICIT keyword or
hybrid mode is still refused there (tests/test_keyword_search.py). The quality
check measures retrieval itself and keeps asking for vector; a golden-set case
without a mode is a vector case (its own mode otherwise:
tests/test_golden_set_modes.py).
"""

import json

import pytest
from mcp.client.client import Client

from griot import ask, cli, common, golden_set, logdb, mcp_server, quality_check
from test_keyword_search import DOCS, _active_path, fake_embeddings, index, legacy_index  # noqa: F401


def _ids(hits):
    return [str(h.id) for h in hits]


# --- the mode a default search runs --------------------------------------------------------------


def test_the_default_mode_is_hybrid():
    assert common.SEARCH_DEFAULT_MODE == "hybrid"


def test_an_explicit_mode_is_kept_as_it_is(legacy_index):
    """An explicit mode is never replaced, even where it cannot run: the
    search refuses it with what to run, which is what the caller asked."""
    for mode in common.SEARCH_MODES:
        assert common.search_mode_for("acquire_lock", mode) == (mode, None)


def test_the_default_runs_hybrid_where_keyword_search_is_built(index):
    assert common.search_mode_for("acquire_lock", None) == ("hybrid", None)


def test_the_default_falls_back_to_vector_on_a_collection_without_keyword_vectors(legacy_index):
    mode, note = common.search_mode_for("acquire_lock", None)
    assert mode == "vector"
    assert "griot index keywords" in note


def test_the_default_falls_back_to_vector_when_nothing_is_indexed():
    """No collection: nothing to build keyword search on, so no command to
    suggest; vector search says what it says about an empty index."""
    assert common.search_mode_for("acquire_lock", None) == ("vector", None)


def test_the_default_falls_back_to_vector_when_the_collection_config_cannot_be_read(index):
    common.release_client()
    (_active_path() / common._EDGE_CONFIG_MARKER).write_text("{not json")
    assert common.search_mode_for("acquire_lock", None) == ("vector", None)


@pytest.mark.parametrize("query", ["the and of", "?!"])
def test_the_default_falls_back_to_vector_for_a_query_keyword_search_cannot_match(index, query):
    """Hybrid needs a word to match; a default search must not turn a query of
    stopwords into an error that vector search would have answered."""
    assert common.search_mode_for(query, None) == ("vector", None)
    assert common.search(query, limit=3, mode="vector")


# --- griot search ------------------------------------------------------------------------------


def test_cli_search_defaults_to_hybrid_and_says_so(index, capsys):
    assert cli.main(["search", "acquire_lock", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "Mode: hybrid" in out
    hybrid = _ids(common.search("acquire_lock", limit=3, diverse=True, mode="hybrid"))
    assert out.splitlines()[0].endswith("alpha/src/lock.py"), "hybrid puts the keyword match first"
    assert len(hybrid) == 3


def test_cli_search_says_which_explicit_mode_ran(index, capsys):
    assert cli.main(["search", "acquire_lock", "--mode", "vector"]) == 0
    assert "Mode: vector" in capsys.readouterr().out


def test_cli_search_default_on_a_legacy_collection_falls_back_and_suggests_the_command_once(legacy_index, capsys):
    assert cli.main(["search", "acquire_lock"]) == 0
    captured = capsys.readouterr()
    assert "Mode: vector" in captured.out
    assert (captured.out + captured.err).count("griot index keywords") == 1
    assert "Error" not in captured.err


def test_cli_search_explicit_hybrid_on_a_legacy_collection_is_still_refused(legacy_index, capsys):
    assert cli.main(["search", "acquire_lock", "--mode", "hybrid"]) == 2
    assert "griot index keywords" in capsys.readouterr().err


# --- griot ask ---------------------------------------------------------------------------------


@pytest.fixture
def chat(monkeypatch):
    prompts = []
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: prompts.append(prompt) or "answer")
    return prompts


def test_ask_defaults_to_hybrid_and_logs_it(index, chat, capsys):
    ask.main(["acquire_lock", "--limit", "5", "--show-sources"])
    assert logdb.read_latest(common.LOG_DIR, "queries")["mode"] == "hybrid"
    assert "Mode: hybrid" in capsys.readouterr().out
    assert chat and "acquire_lock" in chat[0]


def test_ask_default_on_a_legacy_collection_falls_back_logs_vector_and_suggests_the_command(legacy_index, chat,
                                                                                            capsys):
    assert ask.main(["acquire_lock", "--limit", "5"]) in (0, None)
    captured = capsys.readouterr()
    assert logdb.read_latest(common.LOG_DIR, "queries")["mode"] == "vector"
    assert (captured.out + captured.err).count("griot index keywords") == 1
    assert chat, "the question is still answered"


def test_ask_explicit_keyword_on_a_legacy_collection_is_still_refused(legacy_index, chat, capsys):
    assert ask.main(["acquire_lock", "--mode", "keyword"]) == 2
    assert chat == []


# --- griot_search, through the protocol --------------------------------------------------------


@pytest.mark.anyio
async def test_the_tool_defaults_to_hybrid_and_says_which_mode_ran(index):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["griot_search"].input_schema["properties"]["mode"]
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 5})
    assert result.is_error is False
    offered = json.dumps(schema)
    assert all(f'"{mode}"' in offered for mode in common.SEARCH_MODES), "every mode can still be asked for"
    content = result.structured_content
    assert content["mode"] == "hybrid"
    assert content["results"][0]["source_label"] == "alpha/src/lock.py"
    assert "griot index keywords" not in content["note"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_the_tool_says_which_explicit_mode_ran(index, mode):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 5, "mode": mode})
    assert result.is_error is False
    assert result.structured_content["mode"] == mode


@pytest.mark.anyio
async def test_the_tool_default_on_a_legacy_collection_falls_back_and_suggests_the_command_once(legacy_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 5})
        stats = await client.call_tool("griot_stats", {})
    assert result.is_error is False
    content = result.structured_content
    assert content["mode"] == "vector"
    assert len(content["results"]) == 5
    assert content["note"].count("griot index keywords") == 1
    assert stats.structured_content["queries_by_mode"] == {"vector": 1}, "the mode that ran is counted"


@pytest.mark.anyio
async def test_the_tool_explicit_hybrid_on_a_legacy_collection_is_still_refused(legacy_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "hybrid"})
    assert result.is_error is True
    assert "griot index keywords" in result.content[0].text


@pytest.mark.anyio
async def test_the_tool_logs_the_mode_that_ran(index):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_search", {"query": "acquire_lock"})
        await client.call_tool("griot_search", {"query": "the and of"})
        result = await client.call_tool("griot_stats", {})
    modes = [q.get("mode") for q in logdb.read_recent(common.LOG_DIR, "queries", limit=2)]
    assert modes == ["vector", "hybrid"], "newest first: a stopword query fell back to vector"
    assert result.structured_content["queries_by_mode"] == {"hybrid": 1, "vector": 1}


# --- what measures retrieval itself keeps measuring vector ---------------------------------------


@pytest.fixture
def searches(monkeypatch):
    """Every common.search call, with the mode it was given (None when it was
    not given: the measurement would then follow whatever the default is)."""
    calls = []
    real = common.search

    def recorded(query, *args, **kwargs):
        calls.append(kwargs.get("mode"))
        return real(query, *args, **kwargs)

    monkeypatch.setattr(common, "search", recorded)
    return calls


def test_the_self_check_searches_by_vector_explicitly(index, searches):
    common.release_client()
    quality_check.run_self_check(common.COLLECTION_NAME, sample_size=2)
    assert searches and set(searches) == {"vector"}


def test_the_golden_set_check_searches_by_vector_explicitly(index, searches):
    quality_check.run_golden_set([{"query": "acquire_lock", "limit": 3,
                                   "must_include": [{"repo": "alpha", "file_path": "src/lock.py"}]}])
    assert searches and set(searches) == {"vector"}


def test_adding_a_golden_case_shows_what_the_check_will_search_by_vector(index, searches, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "")
    golden_set.cmd_add("acquire_lock", limit=3)
    assert searches and set(searches) == {"vector"}


def test_the_retrieval_evaluation_searches_by_vector_explicitly(index, searches):
    from griot import retrieval_eval
    retrieval_eval.evaluate_qrels([{"query": "acquire_lock", "repo": "alpha", "commit_hash": "x",
                                    "relevant_file_paths": ["src/lock.py"]}], k_values=[3])
    assert searches and set(searches) == {"vector"}


# --- the fallback note: once per MCP server process, with every CLI command ----------------------


@pytest.mark.anyio
async def test_the_tool_gives_the_fallback_note_on_the_first_default_search_only(legacy_index):
    """An agent reads every result of a session: the full `griot index
    keywords` note on each default search is noise after the first. Later
    searches still say which mode ran, and nothing replaces the note."""
    async with Client(mcp_server.mcp) as client:
        first = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 3})
        second = await client.call_tool("griot_search", {"query": "release_lock", "limit": 3})
    assert first.is_error is False and second.is_error is False
    assert first.structured_content["note"].count("griot index keywords") == 1
    assert "griot index keywords" not in second.structured_content["note"]
    assert second.structured_content["note"] == mcp_server.SEARCH_RESULT_NOTE
    assert first.structured_content["mode"] == second.structured_content["mode"] == "vector"


@pytest.mark.anyio
async def test_a_refused_default_search_does_not_use_up_the_fallback_note(legacy_index):
    """The note counts as given when a result carried it: a first search
    refused (here, an unknown repository) returned no note to anyone."""
    async with Client(mcp_server.mcp) as client:
        refused = await client.call_tool("griot_search", {"query": "acquire_lock", "repos": ["nowhere"]})
        answered = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 3})
    assert refused.is_error is True
    assert answered.structured_content["note"].count("griot index keywords") == 1


@pytest.mark.anyio
async def test_the_fallback_note_given_for_another_collection_does_not_silence_this_one(legacy_index, monkeypatch):
    """The note is about a collection: one given for another profile's
    collection told nobody about this one."""
    monkeypatch.setattr(mcp_server, "_KEYWORD_NOTE_GIVEN_FOR", {"another-collection"})
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 3})
    assert result.structured_content["note"].count("griot index keywords") == 1


@pytest.mark.anyio
async def test_an_explicit_vector_search_does_not_use_up_the_fallback_note(legacy_index):
    """An explicit `vector` ran what it asked for and carries no note, so the
    first DEFAULT search after it still has to say why hybrid did not run."""
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 3, "mode": "vector"})
        default = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 3})
    assert default.structured_content["note"].count("griot index keywords") == 1


def test_the_cli_gives_the_fallback_note_with_every_command(legacy_index, capsys):
    """One process per command, read by a person: each default search that
    fell back says why and what to run, even when one process runs several
    (as this test does)."""
    for _ in range(2):
        assert cli.main(["search", "acquire_lock", "--limit", "3"]) == 0
        captured = capsys.readouterr()
        assert "Mode: vector" in captured.out
        assert (captured.out + captured.err).count("griot index keywords") == 1
