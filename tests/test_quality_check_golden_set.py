"""`griot_quality_check` runs the curated golden set too.

It ran the self-check only: indexed points retrieving themselves, which says
the pipeline is intact and nothing about whether a real question is answered.
The half that measures that could only be run at a terminal, so an agent
asked "can this index be trusted?" reported half an answer, and the prompt
that recommends the tool had to send the person to a terminal for the rest."""

import hashlib
import json
import random
import typing

import pytest
from mcp.client.client import Client

from griot import common, golden_set, logdb, mcp_server, stats


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


DOCS = [
    _doc("alpha", "code", "a.py:0", "alpha code about retries", file_path="a.py", chunk_index=0),
    _doc("alpha", "commit", "abc", "alpha commit about retries", commit_hash="abc"),
    _doc("beta", "code", "b.py:0", "beta code about retries", file_path="b.py", chunk_index=0),
]

# The query is the text of the document it expects: with the stand-in
# embedding the same text is the same vector, so this one is found first.
FOUND = ("alpha code about retries", [{"repo": "alpha", "source_type": "code"}])
NOT_FOUND = ("beta code about retries", [{"repo": "beta", "source_type": "issue"}])


@pytest.fixture
def index(monkeypatch):
    embedded = []

    def embed(texts, **kw):
        embedded.extend(texts)
        return [_vector(t, common.EMBED_DIM) for t in texts]

    monkeypatch.setattr(common, "embed_texts", embed)
    common.index_documents(DOCS)
    common.release_lock()
    embedded.clear()
    return embedded


def _write(text: str) -> None:
    """The file as someone editing it by hand leaves it."""
    common.GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.GOLDEN_SET_PATH.write_text(text)


async def _check(**arguments):
    async with Client(mcp_server.mcp) as client:
        return await client.call_tool("griot_quality_check", arguments)


# --- it runs --------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_curated_cases_are_run_and_each_says_whether_it_passed(index):
    golden_set.add_case(*FOUND)
    golden_set.add_case(*NOT_FOUND)
    out = (await _check()).structured_content
    golden = out["golden_check"]
    assert (golden["total"], golden["passed"], golden["failed"]) == (2, 1, 1)
    passed, failed = golden["cases"]
    assert passed["query"] == FOUND[0] and passed["passed"] is True and passed["missing"] == []
    assert failed["passed"] is False and failed["missing"] == NOT_FOUND[1]
    assert failed["top_results"] and {"score", "repo", "source_type"} <= set(failed["top_results"][0])
    assert out["golden_set_not_run"] is None
    assert out["sampled"] == len(DOCS) and out["failed"] == 0, "the self-check is still there"


@pytest.mark.anyio
async def test_what_a_curator_wrote_is_marked_as_data(index):
    """The query and the expected fields can be written by another agent
    (griot_golden_set_add): they come back with the same note the list has."""
    golden_set.add_case(*FOUND)
    assert (await _check()).structured_content["golden_check"]["note"] == mcp_server.GOLDEN_SET_NOTE


@pytest.mark.anyio
async def test_a_case_that_cannot_pass_says_why(index):
    golden_set.add_case("where is the deploy script", [{"repo": "never-indexed", "source_type": "code"}])
    case = (await _check()).structured_content["golden_check"]["cases"][0]
    assert case["passed"] is False and "never-indexed" in case["reason"] and "nothing is indexed" in case["reason"]


@pytest.mark.anyio
async def test_a_case_with_nothing_to_explain_has_no_reason(index):
    golden_set.add_case(*FOUND)
    assert (await _check()).structured_content["golden_check"]["cases"][0]["reason"] is None


# --- when it does not run, it says so -------------------------------------------------------


@pytest.mark.anyio
async def test_asked_for_the_self_check_alone_nothing_curated_is_run(index):
    golden_set.add_case("a question that is in no document", FOUND[1])
    out = (await _check(golden_set=False)).structured_content
    assert out["golden_check"] is None and "golden_set=false" in out["golden_set_not_run"]
    assert "a question that is in no document" not in index, "not asked for, not embedded"


@pytest.mark.anyio
async def test_with_no_curated_case_that_is_said_and_is_not_a_pass(index):
    out = (await _check()).structured_content
    assert out["golden_check"] is None
    assert "no curated case" in out["golden_set_not_run"] and "griot_golden_set_add" in out["golden_set_not_run"]


@pytest.mark.anyio
async def test_more_cases_than_one_call_runs_are_not_run_and_cost_nothing(index, monkeypatch):
    """Every case embeds a query, and the number of cases is whatever the
    file holds. The sample of the self-check is bounded for the same reason."""
    monkeypatch.setattr(mcp_server, "QUALITY_CHECK_GOLDEN_SET_MAX", 2)
    for n in range(3):
        golden_set.add_case(f"curated question {n}", FOUND[1])
    out = (await _check()).structured_content
    assert out["golden_check"] is None
    assert "3 curated cases" in out["golden_set_not_run"] and "griot quality-check" in out["golden_set_not_run"]
    assert not [text for text in index if text.startswith("curated question")]


# --- a file that cannot be run is an error, before anything is spent ------------------------


@pytest.mark.anyio
async def test_a_file_that_is_not_json_is_an_error_and_nothing_is_embedded(index):
    _write("{ not json")
    result = await _check()
    assert result.is_error is True
    assert common.GOLDEN_SET_PATH.name in str(result.content) and "corrupted" in str(result.content)
    assert index == [], "the cheap check comes before the one that embeds"


@pytest.mark.parametrize("content", [
    {"query": "x"},                                         # not a list of cases
    [{"query": "no must_include"}],
    [{"must_include": [{"repo": "alpha"}]}],                # no query
    [{"query": "x", "must_include": "alpha"}],
    [{"query": "x", "must_include": ["alpha"]}],
    [{"query": 7, "must_include": [{"repo": "alpha"}]}],
    [{"query": "x", "must_include": [{"repo": "alpha"}], "limit": "many"}],
    ["just a string"],
])
@pytest.mark.anyio
async def test_a_file_whose_cases_have_another_shape_is_an_error_that_names_the_file(index, content):
    """The file is edited by hand as a documented workflow. A case without
    its fields used to surface as the name of the missing key."""
    _write(json.dumps(content))
    result = await _check()
    assert result.is_error is True
    text = str(result.content)
    assert common.GOLDEN_SET_PATH.name in text and "KeyError" not in text and "Traceback" not in text
    assert index == []


@pytest.mark.anyio
async def test_the_self_check_alone_does_not_read_the_file(index):
    _write("{ not json")
    out = (await _check(golden_set=False)).structured_content
    assert out["sampled"] == len(DOCS)


# --- bounded ---------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_case_asks_for_no_more_results_than_a_search_gives(index, monkeypatch):
    """A limit written by hand is spent by a call the person approved for
    something else: it is held to what griot_search itself allows."""
    asked = []
    real = common.search

    def search(query, limit=5, **kw):
        asked.append(limit)
        return real(query, limit=limit, **kw)

    _write(json.dumps([{"query": FOUND[0], "must_include": FOUND[1], "limit": 100000}]))
    monkeypatch.setattr(common, "search", search)
    out = (await _check(sample_size=1)).structured_content
    assert max(asked) <= mcp_server.SEARCH_LIMIT_MAX
    case = out["golden_check"]["cases"][0]
    assert case["passed"] is True
    # Said on the case: with fewer results than it asks for, a case that
    # passes at a terminal can fail here, and that is not a search failure.
    assert case["limit_reduced_from"] == 100000


@pytest.mark.anyio
async def test_a_case_run_as_written_says_nothing_about_its_limit(index):
    golden_set.add_case(*FOUND)
    assert (await _check()).structured_content["golden_check"]["cases"][0]["limit_reduced_from"] is None


@pytest.mark.anyio
async def test_a_limit_this_tool_can_search_is_not_called_reduced(index):
    _write(json.dumps([{"query": FOUND[0], "must_include": FOUND[1], "limit": mcp_server.SEARCH_LIMIT_MAX}]))
    case = (await _check()).structured_content["golden_check"]["cases"][0]
    assert case["limit_reduced_from"] is None


@pytest.mark.anyio
async def test_a_case_that_was_never_searched_does_not_blame_its_limit(index):
    """Its repository has nothing indexed: no search ran, with any limit.
    Saying "it may pass at a terminal" would send the person the wrong way."""
    _write(json.dumps([{"query": "x", "must_include": [{"repo": "never-indexed"}], "limit": 100000}]))
    case = (await _check()).structured_content["golden_check"]["cases"][0]
    assert case["reason"] and case["limit_reduced_from"] is None


# --- the curated half stops in the middle -----------------------------------------------------


@pytest.mark.anyio
async def test_when_the_curated_half_stops_the_self_check_is_not_lost(index, monkeypatch):
    """A search can fail in the middle (the spend ceiling of a paid profile,
    the embedding API). The self-check was already paid for: it is recorded,
    and the error says what it found."""
    golden_set.add_case(*FOUND)
    golden_set.add_case("the question on which the search stops", FOUND[1])
    real = common.search

    def search(query, limit=5, **kw):
        if query == "the question on which the search stops":
            raise RuntimeError("daily spend ceiling reached")
        return real(query, limit=limit, **kw)

    monkeypatch.setattr(common, "search", search)
    result = await _check()
    text = str(result.content)
    assert result.is_error is True and "daily spend ceiling reached" in text
    assert f"{len(DOCS)} of {len(DOCS)}" in text and "self-check" in text, "what was measured is said"
    recorded = logdb.read_recent(common.LOG_DIR, "quality_checks", limit=1)
    assert recorded and recorded[0]["self_check"]["sampled"] == len(DOCS) and recorded[0]["golden_check"] is None


@pytest.mark.anyio
async def test_the_results_shown_for_a_case_are_a_handful(index, monkeypatch):
    many = [_doc("gamma", "code", f"f{n}.py:0", f"gamma file number {n}", file_path=f"f{n}.py", chunk_index=0)
            for n in range(30)]
    common.index_documents(many)
    common.release_lock()
    _write(json.dumps([{"query": "gamma", "must_include": [{"repo": "nowhere-x"}], "limit": 30}]))
    monkeypatch.setattr(common, "repository_is_indexed", lambda client, repo: True)
    case = (await _check(sample_size=1)).structured_content["golden_check"]["cases"][0]
    assert case["passed"] is False and len(case["top_results"]) == mcp_server.QUALITY_CHECK_TOP_RESULTS_SHOWN


# --- it leaves the same trace as the terminal ---------------------------------------------


@pytest.mark.anyio
async def test_the_run_is_recorded_and_stats_reports_it(index):
    golden_set.add_case(*FOUND)
    golden_set.add_case(*NOT_FOUND)
    await _check()
    recorded = logdb.read_recent(common.LOG_DIR, "quality_checks", limit=1)[0]
    assert (recorded["golden_check"]["passed"], recorded["golden_check"]["total"]) == (1, 2)
    async with Client(mcp_server.mcp) as client:
        state = (await client.call_tool("griot_stats", {})).structured_content["golden_set"]
    assert (state["last_passed"], state["last_total"]) == (1, 2) and state["last_run_at"]


@pytest.mark.anyio
async def test_a_run_of_the_self_check_alone_does_not_erase_the_last_curated_result(index):
    golden_set.add_case(*FOUND)
    await _check()
    await _check(golden_set=False)
    assert stats.load_state()["last_golden_check"]["passed"] == 1


# --- what is said about it -------------------------------------------------------------------


def test_the_tool_is_still_asked_about():
    """It embeds a query per sample and per case: on a paid profile, money."""
    assert "griot_quality_check" not in mcp_server.tools_safe_to_preapprove()


@pytest.mark.anyio
async def test_the_description_says_what_it_runs_and_what_it_costs():
    async with Client(mcp_server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "griot_quality_check")
    text = " ".join(tool.description.split())
    assert "golden set" in text and "golden_set=false" in text
    assert "Does not run the curated golden set" not in text
    assert "one query per" in text, "the cost is per sample and per case"


@pytest.mark.anyio
async def test_the_health_prompt_reads_both_results_and_names_fields_that_exist():
    async with Client(mcp_server.mcp) as client:
        text = (await client.get_prompt("health")).messages[0].content.text
    fields = typing.get_type_hints(mcp_server.QualityCheckOutput)
    for name in ("golden_check", "golden_set_not_run"):
        assert name in text and name in fields
    case_fields = typing.get_type_hints(mcp_server.GoldenCheckCase)
    for name in ("reason", "missing", "top_results", "limit_reduced_from"):
        assert name in text and name in case_fields
    assert "SELF-CHECK only" not in text and "you did not run it" not in text
    assert "billed" in text.lower(), "it still says the check can cost money, before running it"


# --- the same file, read by the terminal command -------------------------------------------


def test_the_terminal_command_names_the_file_too_instead_of_a_missing_key(index, capsys):
    from griot import quality_check
    _write(json.dumps([{"query": "no must_include"}]))
    with pytest.raises(SystemExit) as stop:
        quality_check.main(["--sample-size", "1"])
    assert stop.value.code == 1
    out = capsys.readouterr()
    assert common.GOLDEN_SET_PATH.name in out.out + out.err and "must_include" in out.out + out.err
    assert "case 1" in out.out + out.err
    assert "Self-check" not in out.out, "refused before anything is embedded, as the tool does"
    assert index == []


def test_the_terminal_command_records_the_self_check_when_the_curated_half_stops(index, monkeypatch):
    from griot import quality_check
    golden_set.add_case("the question on which the search stops", FOUND[1])
    real = common.search

    def search(query, limit=5, **kw):
        if query == "the question on which the search stops":
            raise RuntimeError("daily spend ceiling reached")
        return real(query, limit=limit, **kw)

    monkeypatch.setattr(common, "search", search)
    with pytest.raises(RuntimeError, match="ceiling"):
        quality_check.main(["--sample-size", "2"])
    recorded = logdb.read_recent(common.LOG_DIR, "quality_checks", limit=1)
    assert recorded and recorded[0]["self_check"]["sampled"] == 2 and recorded[0]["golden_check"] is None


def test_the_terminal_command_without_a_file_still_runs_the_self_check(index, capsys):
    from griot import quality_check
    quality_check.main(["--sample-size", "2"])
    out = capsys.readouterr().out
    assert "does not exist, skipping golden set" in out and "Quality OK" in out


def test_told_to_skip_the_curated_half_the_terminal_command_does_not_read_the_file(index, capsys):
    from griot import quality_check
    _write("{ not json")
    quality_check.main(["--sample-size", "2", "--skip-golden-set"])
    assert "Quality OK" in capsys.readouterr().out
