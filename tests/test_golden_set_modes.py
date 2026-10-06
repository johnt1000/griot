"""A golden-set case keeps the search mode it was made in.

A case asserts what ONE search returned. Since hybrid became the default of
griot search, ask and griot_search, most questions are asked in hybrid, and
a case made from one was held to a vector search that may never return the
same results. Now a case records its mode (vector, keyword or hybrid;
absent means vector, which is what every case written before meant), and the
quality check repeats each case in its own mode. A keyword or hybrid case on
a collection without keyword vectors is SKIPPED with the command that builds
them: never quietly run by meaning, never counted as a failure of search.

Real tiny Edge indexes (tests/test_keyword_search.py's documents, with a
stand-in embedding where only the same text gives the same vector)."""

import json

import pytest
from mcp.client.client import Client

from griot import common, golden_set, logdb, mcp_server, quality_check, stats
from test_golden_set_review import CODE, _log, terminal  # noqa: F401
from test_keyword_search import fake_embeddings, index, legacy_index  # noqa: F401

NET = {"repo": "alpha", "file_path": "src/net.py"}
# Keyword search ranks the one document holding the word first; with random
# dense vectors a vector search of one result finds it only by luck.
KEYWORD_CASE = {"query": "ERR_CONNECTION_REFUSED", "limit": 1, "mode": "keyword", "must_include": [NET]}


def _cases():
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


def _write(cases) -> None:
    """The file as someone editing it by hand leaves it."""
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text(json.dumps(cases))


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


# --- what a case holds --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_a_case_records_its_mode(mode):
    golden_set.add_case("acquire_lock", [{"repo": "alpha"}], mode=mode)
    assert _cases()[0]["mode"] == mode


def test_a_vector_case_is_written_as_every_case_was_before():
    """Absent means vector: the file of a vector case does not change, so
    a case written before modes and one written now read the same."""
    golden_set.add_case("acquire_lock", [{"repo": "alpha"}])
    golden_set.add_case("acquire_lock", [{"repo": "alpha"}], mode="vector")
    assert all("mode" not in case for case in _cases())
    assert [golden_set.case_mode(case) for case in _cases()] == ["vector", "vector"]


@pytest.mark.parametrize("mode", ["semantic", "", None, "HYBRID", 1])
def test_a_mode_that_is_not_a_search_mode_is_refused_and_nothing_is_written(mode):
    with pytest.raises(ValueError, match="mode"):
        golden_set.add_case("acquire_lock", [{"repo": "alpha"}], mode=mode)
    assert not common.GOLDEN_SET_PATH.exists()


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_a_word_search_of_a_query_with_no_word_to_match_is_refused(mode):
    """Keyword and hybrid search refuse a query of only common words: such
    a case could never run."""
    with pytest.raises(ValueError, match="word"):
        golden_set.add_case("the and of", [{"repo": "alpha"}], mode=mode)
    assert not common.GOLDEN_SET_PATH.exists()
    golden_set.add_case("the and of", [{"repo": "alpha"}], mode="vector")  # by meaning it can


@pytest.mark.parametrize("mode", ["semantic", None, "", ["hybrid"]])
def test_a_hand_written_mode_that_cannot_run_makes_the_file_an_error_naming_the_case(mode):
    """Read time is the only point every writer passes (hand edits too)."""
    cases = [{"query": "q", "must_include": [{"repo": "r"}]},
             {"query": "q", "must_include": [{"repo": "r"}], "mode": mode}]
    with pytest.raises(ValueError, match="case 2.*mode"):
        golden_set.check_cases(cases)


def test_every_search_mode_and_no_mode_are_valid_in_the_file():
    golden_set.check_cases([{"query": "q", "must_include": [{"repo": "r"}], **({"mode": m} if m else {})}
                            for m in (None, *common.SEARCH_MODES)])


def test_the_list_shows_each_case_s_mode(capsys):
    _write([{"query": "old one", "must_include": [{"repo": "r"}]},
            {"query": "new one", "must_include": [{"repo": "r"}], "mode": "hybrid"}])
    assert golden_set.cmd_list() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "old one" in lines[0] and "vector" in lines[0]
    assert "new one" in lines[1] and "hybrid" in lines[1]


# --- griot golden-set add --------------------------------------------------------------------


def test_add_searches_in_the_mode_asked_and_the_case_keeps_it(index, searches, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "1")
    assert golden_set.main(["add", "ERR_CONNECTION_REFUSED", "--limit", "1", "--mode", "keyword"]) == 0
    assert searches == ["keyword"]
    (case,) = _cases()
    assert case["mode"] == "keyword"
    assert case["must_include"] == [{"repo": "alpha", "source_type": "code", "file_path": "src/net.py"}]


def test_add_without_a_mode_searches_by_vector(index, searches, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "1")
    assert golden_set.main(["add", "acquire_lock", "--limit", "1"]) == 0
    assert searches == ["vector"] and "mode" not in _cases()[0]


def test_add_in_a_word_mode_on_a_collection_without_keyword_vectors_says_what_to_run(legacy_index, capsys,
                                                                                      monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked to pick from a search that cannot run"))
    assert golden_set.main(["add", "acquire_lock", "--mode", "hybrid"]) == 1
    assert "griot index keywords" in capsys.readouterr().err
    assert not common.GOLDEN_SET_PATH.exists()


# --- the quality check -------------------------------------------------------------------------


def test_each_case_is_run_in_its_own_mode(index, searches):
    result = quality_check.run_golden_set([
        {"query": "acquire_lock", "limit": 3, "must_include": [{"repo": "alpha"}]},
        KEYWORD_CASE,
        {"query": "acquire_lock", "limit": 3, "mode": "hybrid",
         "must_include": [{"repo": "alpha", "file_path": "src/lock.py"}]},
    ])
    assert searches == ["vector", "keyword", "hybrid"]
    assert [case["mode"] for case in result["cases"]] == ["vector", "keyword", "hybrid"]
    assert result["cases"][1]["passed"], "the keyword search finds the word that vector search would not"
    assert result["ran_by_mode"] == {"vector": 1, "keyword": 1, "hybrid": 1}
    assert result["skipped"] == 0


def test_a_word_case_without_keyword_vectors_is_skipped_with_what_to_run(legacy_index, searches):
    result = quality_check.run_golden_set([
        {"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]},
        {**KEYWORD_CASE, "mode": "hybrid"},
        KEYWORD_CASE,
    ])
    assert searches == ["vector"], "never quietly run by meaning, and no query is embedded for it"
    skipped = result["cases"][1:]
    assert all(case["skipped"] and not case["passed"] for case in skipped)
    assert all("griot index keywords" in case["reason"] for case in skipped)
    assert [case["mode"] for case in skipped] == ["hybrid", "keyword"]
    assert result["cases"][0]["skipped"] is False
    assert (result["total"], result["passed"], result["failed"], result["skipped"]) == (1, 1, 0, 2), \
        "total is the cases that ran; skipped ones are apart"
    assert result["ran_by_mode"] == {"vector": 1}


def test_a_case_that_cannot_pass_is_failed_before_it_is_skipped(legacy_index):
    """A skip must never hide a failure: a case whose repository has nothing
    indexed fails, whatever its mode."""
    result = quality_check.run_golden_set([
        {"query": "acquire_lock", "mode": "hybrid", "must_include": [{"repo": "gone"}]}])
    assert result["failed"] == 1 and result["skipped"] == 0
    assert "gone" in result["cases"][0]["reason"]


def test_a_hand_written_word_case_with_no_word_to_match_fails_by_name_and_the_rest_run(index, searches):
    result = quality_check.run_golden_set([
        {"query": "the and of", "mode": "keyword", "must_include": [{"repo": "alpha"}]},
        KEYWORD_CASE,
    ])
    first = result["cases"][0]
    assert not first["passed"] and not first["skipped"] and "word" in first["reason"]
    assert result["cases"][1]["passed"]
    assert (result["failed"], result["passed"]) == (1, 1)


def test_a_mode_no_search_knows_fails_and_is_never_skipped(legacy_index):
    """Skipping is for a mode this collection cannot run YET; a mode that
    does not exist would never run, so it fails, even where keyword search
    is not built (check_cases refuses it first; this is what run_golden_set
    does when given one anyway)."""
    result = quality_check.run_golden_set([{**KEYWORD_CASE, "mode": "semantic"}])
    assert (result["failed"], result["skipped"]) == (1, 0)
    assert "semantic" in result["cases"][0]["reason"]


def test_the_cli_says_skipped_not_failed_and_the_gate_stays_open(legacy_index, capsys):
    _write([{"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]},
            {**KEYWORD_CASE, "mode": "hybrid"}])
    common.release_client()
    quality_check.main(["--sample-size", "2"])  # exit 0: nothing failed
    out = capsys.readouterr().out
    assert "[SKIPPED]" in out and "griot index keywords" in out
    assert "FAILED" not in out
    assert "1 vector" in out and "1 skipped" in out


def test_the_cli_refuses_a_file_with_a_mode_that_cannot_run_before_embedding_anything(index, capsys, monkeypatch):
    _write([{**KEYWORD_CASE, "mode": "semantic"}])
    common.release_client()
    monkeypatch.setattr(common, "embed_texts", lambda *a, **k: pytest.fail("embedded before refusing the file"))
    with pytest.raises(SystemExit) as exit_:
        quality_check.main(["--json"])
    assert exit_.value.code == 1
    assert "case 1" in capsys.readouterr().err


def test_json_output_counts_what_ran_per_mode(index, capsys):
    _write([KEYWORD_CASE, {"query": "acquire_lock", "limit": 3, "must_include": [{"repo": "alpha"}]}])
    common.release_client()
    quality_check.main(["--json", "--sample-size", "2"])
    golden = json.loads(capsys.readouterr().out)["golden_check"]
    assert golden["ran_by_mode"] == {"keyword": 1, "vector": 1} and golden["skipped"] == 0


# --- the trend in griot stats -------------------------------------------------------------------


def test_stats_says_how_many_cases_the_last_run_skipped(legacy_index):
    _write([{"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]}, KEYWORD_CASE])
    common.release_client()
    quality_check.main(["--sample-size", "2"])
    state = stats.load_state()
    assert state["last_golden_check"]["skipped"] == 1
    assert state["last_golden_check"]["total"] == 1, "the cases that ran"
    record = logdb.read_latest(common.LOG_DIR, "quality_checks")
    assert record["collection"] == common.COLLECTION_NAME, "the trend stays per collection"
    status = {"points_count": 5, "embed_profile": "any", "spend_ceiling_exceeded": False, "last_indexed": None}
    result = stats.compute_stats([], [], status, state=state)
    assert result["golden_set"]["last_skipped"] == 1
    assert "1 of 1 passed, 1 skipped" in stats.format_stats(result, 7)


# --- griot golden-set review ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["hybrid", "keyword"])
def test_a_word_search_from_the_log_can_become_a_case_in_its_mode(terminal, capsys, mode):
    _log("how is the lock released", mode=mode)
    _log("how is the lock released", mode=mode)
    terminal.append("1")

    assert golden_set.cmd_review() == 0

    assert "which result is the right one" in capsys.readouterr().out.lower()
    assert _cases() == [{"query": "how is the lock released", "limit": 8, "mode": mode, "must_include": [CODE]}]


def test_a_logged_mode_griot_does_not_know_is_shown_but_not_made_a_case(terminal, capsys):
    _log("how is the lock released", mode="semantic")
    _log("how is the lock released", mode="semantic")
    terminal.extend(["1", "s"])

    golden_set.cmd_review()

    assert "cannot become a case" in capsys.readouterr().out.lower()
    assert _cases() == []


@pytest.mark.parametrize("unlike", [{"repos": ["r"]}, {"group_by_document": True}])
def test_a_narrowed_or_grouped_word_search_is_still_shown_only(terminal, capsys, unlike):
    _log("how is the lock released", mode="hybrid", **unlike)
    _log("how is the lock released", mode="hybrid", **unlike)
    terminal.extend(["1", "s"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "cannot become a case" in out.lower() and "hybrid search" not in out.split("cannot become")[1]
    assert _cases() == []


# --- MCP --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_mcp_tool_adds_a_case_in_the_mode_given():
    async with Client(mcp_server.mcp) as client:
        out = await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "mode": "hybrid", "confirm": True})
        listed = await client.call_tool("griot_golden_set_list", {})
    assert out.structured_content["changed"] is True
    assert _cases()[0]["mode"] == "hybrid"
    assert listed.structured_content["cases"][0]["mode"] == "hybrid"


@pytest.mark.anyio
async def test_the_mcp_list_says_vector_for_a_case_written_without_a_mode():
    _write([{"query": "q", "must_include": [{"repo": "r"}]}])
    async with Client(mcp_server.mcp) as client:
        listed = await client.call_tool("griot_golden_set_list", {})
    assert listed.structured_content["cases"][0]["mode"] == "vector"
    assert "mode" not in _cases()[0], "listing does not rewrite the file"


@pytest.mark.anyio
async def test_the_mcp_tool_refuses_a_mode_that_is_not_a_search_mode():
    async with Client(mcp_server.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        out = await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock", "must_include": [{"repo": "alpha"}], "mode": "semantic", "confirm": True})
    schema = tools["griot_golden_set_add"].input_schema["properties"]["mode"]
    assert set(schema.get("enum") or []) == set(common.SEARCH_MODES)
    assert "mode" in tools["griot_golden_set_add"].description
    assert out.is_error and not common.GOLDEN_SET_PATH.exists()


@pytest.mark.anyio
async def test_the_mcp_quality_check_says_which_cases_were_skipped_and_why(legacy_index):
    _write([{"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]}, KEYWORD_CASE])
    common.release_client()
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_quality_check", {"sample_size": 2})).structured_content
    golden = out["golden_check"]
    assert (golden["passed"], golden["failed"], golden["skipped"]) == (1, 0, 1)
    assert golden["ran_by_mode"] == {"vector": 1}
    vector, keyword = golden["cases"]
    assert keyword["skipped"] is True and keyword["mode"] == "keyword"
    assert "griot index keywords" in keyword["reason"] and keyword["limit_reduced_from"] is None
    assert vector["skipped"] is False and vector["mode"] == "vector"


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["legacy", "2026-07-28"])
async def test_a_person_is_not_asked_about_a_word_case_that_could_never_run(protocol):
    """Through a client that CAN ask a person: the question is skipped, not
    asked and then refused (a confirmation that cannot change the outcome
    trains click-through)."""
    async def ask(ctx, params):
        raise AssertionError("a person was asked about a case that cannot be written")

    async with Client(mcp_server.mcp, mode=protocol, elicitation_callback=ask) as client:
        out = await client.call_tool("griot_golden_set_add", {
            "query": "the and of", "must_include": [{"repo": "alpha"}], "mode": "hybrid"})
    assert out.structured_content["changed"] is False and "word" in out.structured_content["message"]
    assert not common.GOLDEN_SET_PATH.exists()


class _NoElicitCtx:
    client_capabilities = None


@pytest.mark.anyio
async def test_the_mcp_hint_repeats_the_search_in_the_case_s_mode(monkeypatch):
    """Refused without a confirmation, the tool names the terminal command:
    run there, it must search the way the case will be checked."""
    import shlex

    from test_cli_hints import _command_in

    out = await mcp_server.griot_golden_set_add("acquire_lock", must_include=[{"repo": "alpha"}], mode="hybrid",
                                                confirm=False, ctx=_NoElicitCtx())
    argv = shlex.split(_command_in(out["message"]))
    assert argv == ["griot", "golden-set", "add", "--mode", "hybrid", "--", "acquire_lock"]
    seen = []
    monkeypatch.setattr(golden_set, "cmd_add", lambda query, limit, mode: seen.append((query, mode)) or 0)
    assert golden_set.main(argv[2:]) == 0 and seen == [("acquire_lock", "hybrid")]


@pytest.mark.anyio
@pytest.mark.parametrize("mode, query", [("hybrid", "the and of"), ("semantic", "acquire_lock")])
async def test_the_mcp_tool_refuses_a_case_that_could_never_run_before_asking_anyone(monkeypatch, mode, query):
    async def asked(*a, **k):
        raise AssertionError("a person was asked about a case that cannot be written")
    monkeypatch.setattr(mcp_server, "_confirmed", asked)
    out = await mcp_server.griot_golden_set_add(query, must_include=[{"repo": "alpha"}], mode=mode, confirm=True,
                                                ctx=_NoElicitCtx())
    assert out["changed"] is False and "mode" in out["message"]
    assert not common.GOLDEN_SET_PATH.exists()
