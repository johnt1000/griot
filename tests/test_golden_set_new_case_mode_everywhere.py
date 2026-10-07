"""A new golden-set case gets the same mode whichever command made it.

`griot golden-set add` and griot_golden_set_add default to hybrid (vector
where hybrid cannot run, with a note naming `griot index keywords`), through
golden_set.case_mode_for(). `griot golden-set suggest` wrote its cases with
no mode, which reads as vector: the same question curated two ways measured
two different searches. Every writer of a new case without a mode now
resolves it through case_mode_for().

With nothing indexed yet, case_mode_for() resolved to vector, silently. A
case made then is checked once something is indexed, and `griot index`
makes every new collection with keyword vectors (common._collection_config),
so what a reader will get there is hybrid: that is the default now, with no
note (there is nothing to build). A collection whose config cannot be read
is different: whether it can run hybrid is unknown, so the case stays vector
and the output says why.

Cases already in the file without a mode stay vector (the trend stays
comparable)."""

import json

import pytest
from mcp.client.client import Client

from griot import cli, common, golden_set, mcp_server
from test_keyword_search import fake_embeddings, index, legacy_index  # noqa: F401

OLD_CASE = {"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]}


def _cases() -> list[dict]:
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


def _approve_everything(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")


def _unreadable_collection():
    """A collection whose config file the engine wrote is not JSON any more."""
    path = common.collection_path(common.COLLECTION_NAME)
    common.secure_mkdir(path)
    (path / common.EDGE_CONFIG_MARKER).write_text("{ not json")


@pytest.fixture
def repo_with_a_commit(git_repo):
    git_repo.commit("Fix acquire_lock timeout", filename="locks.py", content="def acquire_lock(): pass")
    return git_repo


def _suggest(repo) -> None:
    assert cli.main(["golden-set", "suggest", str(repo.path)]) == 0


# --- case_mode_for: before anything is indexed, and an unreadable config ------------------------


def test_with_nothing_indexed_the_default_is_hybrid_with_no_note():
    assert not common.collection_exists(common.COLLECTION_NAME)
    assert golden_set.case_mode_for("acquire_lock", None) == ("hybrid", None)


def test_with_nothing_indexed_a_query_with_no_word_to_match_is_still_vector():
    """Hybrid refuses such a query on every collection: the case could never run."""
    assert golden_set.case_mode_for("the and of", None) == ("vector", None)


def test_with_nothing_indexed_an_explicit_mode_is_kept():
    assert golden_set.case_mode_for("acquire_lock", "vector") == ("vector", None)


def test_a_collection_griot_index_makes_has_keyword_vectors(index):
    """What the empty-collection default rests on: an index run into no
    collection makes one that can run hybrid."""
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_with_an_unreadable_collection_config_the_default_is_vector_and_says_why():
    _unreadable_collection()
    mode, note = golden_set.case_mode_for("acquire_lock", None)
    assert mode == "vector"
    assert note and "could not be read" in note and "vector" in note
    assert "griot index keywords" not in note, "building keyword vectors is not what is wrong"


def test_with_an_unreadable_config_a_query_with_no_word_to_match_needs_no_note():
    _unreadable_collection()
    assert golden_set.case_mode_for("the and of", None) == ("vector", None)


# --- griot golden-set suggest ------------------------------------------------------------------


def test_suggest_makes_a_hybrid_case_like_add(index, repo_with_a_commit, monkeypatch, capsys):
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    (case,) = _cases()
    assert case["mode"] == "hybrid", "written explicitly: absent would read as vector"
    assert "griot index keywords" not in capsys.readouterr().out


def test_suggest_on_a_collection_without_keyword_vectors_makes_a_vector_case_and_says_so(
        legacy_index, git_repo, monkeypatch, capsys):
    git_repo.commit("Fix acquire_lock timeout", filename="a.py", content="a = 1")
    git_repo.commit("Fix release_lock race", filename="b.py", content="b = 1")
    _approve_everything(monkeypatch)
    _suggest(git_repo)
    cases = _cases()
    assert len(cases) == 2 and all("mode" not in case for case in cases)
    out = capsys.readouterr().out
    assert out.count("griot index keywords") == 1, "said once for the run, not once per case"


def test_suggest_with_nothing_indexed_makes_a_hybrid_case_with_no_note(repo_with_a_commit, monkeypatch, capsys):
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    assert _cases()[0]["mode"] == "hybrid"
    out = capsys.readouterr().out
    assert "griot index keywords" not in out and "could not be read" not in out


def test_suggest_with_an_unreadable_collection_config_makes_a_vector_case_and_says_why(
        repo_with_a_commit, monkeypatch, capsys):
    _unreadable_collection()
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    assert "mode" not in _cases()[0]
    assert "could not be read" in capsys.readouterr().out


def test_suggest_says_nothing_about_modes_when_nothing_was_approved(legacy_index, repo_with_a_commit, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    _suggest(repo_with_a_commit)
    assert _cases() == [] and "griot index keywords" not in capsys.readouterr().out


def test_suggest_leaves_the_cases_already_in_the_file_as_they_are(index, repo_with_a_commit, monkeypatch):
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text(json.dumps([OLD_CASE]))
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    old, new = _cases()
    assert old == OLD_CASE and golden_set.case_mode(old) == "vector"
    assert new["mode"] == "hybrid"


def test_suggest_and_add_make_the_same_case_of_the_same_question(index, repo_with_a_commit, monkeypatch):
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    (suggested,) = _cases()
    added = golden_set.add_case(suggested["query"], suggested["must_include"],
                                mode=golden_set.case_mode_for(suggested["query"], None)[0])
    assert suggested == added


def test_the_help_says_what_suggest_writes(capsys):
    with pytest.raises(SystemExit):
        golden_set.main(["suggest", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "hybrid" in help_text


# --- through the MCP protocol ------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_tool_with_nothing_indexed_makes_a_hybrid_case_with_no_note():
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "confirm": True})).structured_content
    assert out["changed"] is True and "hybrid" in out["message"]
    assert "griot index keywords" not in out["message"] and "could not be read" not in out["message"]
    assert _cases()[0]["mode"] == "hybrid"


@pytest.mark.anyio
async def test_the_tool_with_an_unreadable_collection_config_makes_a_vector_case_and_says_why():
    _unreadable_collection()
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "confirm": True})).structured_content
    assert out["changed"] is True and "could not be read" in out["message"]
    assert "mode" not in _cases()[0]


@pytest.mark.anyio
async def test_a_suggested_candidate_added_through_the_tool_is_the_case_the_terminal_writes(
        index, repo_with_a_commit, monkeypatch):
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(repo_with_a_commit.path.parent))
    async with Client(mcp_server.mcp) as client:
        found = (await client.call_tool("griot_golden_set_suggest", {
            "path": str(repo_with_a_commit.path)})).structured_content
        (candidate,) = found["candidates"]
        out = (await client.call_tool("griot_golden_set_add", {
            "query": candidate["query"], "must_include": candidate["must_include"],
            "limit": candidate["limit"], "confirm": True})).structured_content
    assert out["changed"] is True
    from_the_tool = _cases()
    common.GOLDEN_SET_PATH.unlink()
    _approve_everything(monkeypatch)
    _suggest(repo_with_a_commit)
    assert _cases() == from_the_tool and from_the_tool[0]["mode"] == "hybrid"


@pytest.mark.anyio
async def test_the_suggest_tool_says_a_candidate_added_without_a_mode_is_hybrid():
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_golden_set_suggest"]
    assert "hybrid" in tool.description
