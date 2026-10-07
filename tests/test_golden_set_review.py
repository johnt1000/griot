"""`griot golden-set review`: curated cases from the queries people and
agents actually asked, approved one by one by a person at a terminal.

Candidates are read from the query log (a real logs.db in this test's data
directory): questions asked more than once, and vector searches whose best
result scored in the bottom quarter of that collection's searches. A pick
becomes a case through golden_set.add_case(), so the same validation applies;
nothing is ever written to the index (the review does not even open it).

The terminal is faked: common.is_interactive() says yes and input() answers
from a list."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from griot import ask, common, golden_set, logdb, mcp_server


def T(*parts: str) -> str:
    return "".join(parts)


# Assembled from pieces so this file does not itself look like it holds one.
TOKEN = T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8")

CODE = {"repo": "r", "source_type": "code", "file_path": "src/lock.py"}
COMMIT = {"repo": "r", "source_type": "commit", "commit_hash": "abc123def4567890abc123def4567890abc12345"}


class _Hit:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload
        self.id = "00000000-0000-0000-0000-000000000001"


_clock = [datetime.now(timezone.utc) - timedelta(hours=5)]


def _log(question, *, top_score=0.8, mode="vector", project="p1", results=(CODE, COMMIT),
         collection="c1", limit=8, with_results=True, **extra):
    """One row of the query log, as log_query() writes it today (or, with
    with_results=False, as it wrote it before results were recorded)."""
    _clock[0] += timedelta(seconds=1)
    record = {
        "timestamp": _clock[0].isoformat(), "profile": "p", "collection": collection, "project": project,
        "question": question, "via": "mcp", "limit": limit, "mode": mode, "num_sources": len(results),
        "sources": [common.source_label(r) for r in results], "top_score": top_score, **extra,
    }
    if with_results:
        record["results"] = [dict(r) for r in results]
    logdb.write_query(common.LOG_DIR, record)


@pytest.fixture
def terminal(monkeypatch):
    """A fake terminal: answers are given in order; what was printed is in capsys."""
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    answers: list[str] = []

    def fake_input(prompt=""):
        print(prompt)
        if not answers:
            raise EOFError
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return answers


@pytest.fixture(autouse=True)
def _the_index_is_never_opened(monkeypatch):
    """The review reads the log and writes the golden set, nothing else:
    every way into the index fails the test."""
    def refuse(*a, **k):
        raise AssertionError("the review touched the index")
    monkeypatch.setattr(common, "get_client", refuse)
    monkeypatch.setattr(common, "search", refuse)
    monkeypatch.setattr(common, "embed_query", refuse, raising=False)


def _cases():
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


# --- what the query log records ------------------------------------------


def test_a_search_records_what_a_case_needs_for_each_result(monkeypatch):
    """The labels in `sources` are for display: a commit's is cut to eight
    characters and every one is redacted. A case needs the fields it matches
    on, so they are recorded beside the labels, one per result."""
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [
        _Hit(0.9, {**CODE, "chunk_index": 3, "content": "x"}), _Hit(0.8, {**COMMIT, "content": "y"})])

    mcp_server.griot_search("how is the lock released")

    row = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]
    assert row["results"] == [CODE, COMMIT]


def test_a_result_whose_name_looks_like_a_credential_is_recorded_as_null(monkeypatch):
    """The label is redacted; the raw field must not reach the log either.
    A case cannot be made from that result, which the review says."""
    leaky = {"repo": "r", "source_type": "code", "file_path": "deploy/" + TOKEN + ".sh"}
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [
        _Hit(0.9, {**leaky, "content": "x"}), _Hit(0.8, {**CODE, "content": "y"})])

    mcp_server.griot_search("deploy script")

    row = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]
    assert row["results"] == [None, CODE]
    assert TOKEN not in json.dumps(row)


def test_cli_ask_records_the_results_too(monkeypatch):
    monkeypatch.setattr(ask, "ask", lambda q, model=None, limit=5, mode="vector": ("answer", [_Hit(0.9, dict(CODE))]))
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.0)

    ask.main(["some question"])

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["results"] == [CODE]


def test_the_results_are_recorded_through_the_real_mcp_client(monkeypatch):
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [_Hit(0.9, dict(CODE))])

    async def call():
        from mcp.client.client import Client
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("griot_search", {"query": "lock"})

    asyncio.run(call())

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["results"] == [CODE]


# --- which queries are candidates -----------------------------------------


def test_the_same_question_written_differently_is_one_question():
    """Case, whitespace, punctuation and word order do not make a new
    question: the key is the set of words."""
    keys = {golden_set.query_key(q) for q in
            ["How is the lock released?", "  how is the LOCK released ", "released: how is the lock"]}
    assert len(keys) == 1
    assert golden_set.query_key("how is the lock acquired") not in keys


def test_a_question_asked_twice_is_a_candidate_and_one_asked_once_is_not():
    _log("How is the lock released?", project="p1")
    _log("how is the lock released", project="p2")
    _log("what does stats print")

    found = golden_set.review_candidates()

    assert [(c["kind"], c["times"], c["projects"]) for c in found["candidates"]] == [("repeated", 2, 2)]
    assert found["candidates"][0]["query"] == "how is the lock released"  # the most recent wording


def _fill_vector_scores(n=20, collection="c1"):
    """n vector searches with top scores 0.50, 0.52, ... so the bottom quarter is known."""
    for i in range(n):
        _log(f"filler question number {i}", top_score=round(0.50 + 0.02 * i, 2), collection=collection)


def test_a_vector_search_in_the_bottom_quarter_of_its_collection_is_a_hard_candidate():
    _fill_vector_scores()  # 0.50, 0.52 ... 0.88
    _log("why does the shard refuse to open", top_score=0.45)
    _log("where is the spend ceiling read", top_score=0.80)

    found = golden_set.review_candidates(limit=50)
    hard = [c["query"] for c in found["candidates"] if c["kind"] == "hard"]

    assert hard[0] == "why does the shard refuse to open"  # lowest score first
    assert "where is the spend ceiling read" not in hard
    assert all(c["top_score"] <= c["threshold"] for c in found["candidates"] if c["kind"] == "hard")


def test_too_few_vector_searches_give_no_hard_candidates():
    """A quartile of a handful of scores is noise, not a measure."""
    _fill_vector_scores(n=10)
    _log("why does the shard refuse to open", top_score=0.10)

    found = golden_set.review_candidates(limit=50)

    assert not [c for c in found["candidates"] if c["kind"] == "hard"]


def test_keyword_and_hybrid_scores_never_make_a_hard_candidate():
    """A keyword score grows with the query's words and a hybrid score is a
    rank: neither says the best result was a poor match."""
    _fill_vector_scores()
    for mode in ("keyword", "hybrid"):
        for i in range(25):
            _log(f"{mode} filler {i}", top_score=1.0 + i, mode=mode)
        _log(f"{mode} low one", top_score=0.01, mode=mode)

    _log("where is the spend ceiling read", top_score=0.80)

    found = golden_set.review_candidates(limit=100)

    assert not [c for c in found["candidates"] if c["kind"] == "hard" and c["mode"] != "vector"]
    # Nor do they move the vector threshold: pooled with them, 0.80 would be low.
    assert "where is the spend ceiling read" not in [c["query"] for c in found["candidates"]]


def test_the_threshold_is_per_collection():
    """Scores of two embedding models are not on one scale."""
    _fill_vector_scores(collection="c1")
    for i in range(20):
        _log(f"other model filler {i}", top_score=round(0.20 + 0.01 * i, 2), collection="c2")
    _log("low for c1 but normal for c2", top_score=0.30, collection="c2")

    found = golden_set.review_candidates(limit=100)

    assert "low for c1 but normal for c2" not in [c["query"] for c in found["candidates"]]


def test_repeated_candidates_come_before_hard_ones():
    _fill_vector_scores()
    _log("why does the shard refuse to open", top_score=0.40)
    _log("how is the lock released")
    _log("how is the lock released")

    kinds = [c["kind"] for c in golden_set.review_candidates(limit=50)["candidates"]]

    assert kinds[0] == "repeated" and "hard" in kinds
    assert kinds == sorted(kinds, key=lambda k: k != "repeated")


def test_a_question_already_in_the_golden_set_is_not_a_candidate():
    golden_set.add_case("How is the LOCK released", [CODE])
    _log("how is the lock released")
    _log("how is the lock released")

    assert golden_set.review_candidates()["candidates"] == []


def test_questions_that_were_not_logged_are_counted_not_offered():
    _log(common.OMITTED_QUESTION)
    _log(common.OMITTED_QUESTION)

    found = golden_set.review_candidates()

    assert found["candidates"] == [] and found["omitted"] == 2


def test_limit_bounds_the_candidates():
    for i in range(5):
        _log(f"question {i}")
        _log(f"question {i}")

    assert len(golden_set.review_candidates(limit=3)["candidates"]) == 3


# --- the review itself ------------------------------------------------------


def test_picking_a_result_adds_a_case_that_asserts_it_comes_back(terminal, capsys):
    _log("How is the lock released?", project="p1", limit=8)
    _log("how is the lock released", project="p2", limit=8)
    terminal.append("2")

    assert golden_set.cmd_review() == 0

    out = capsys.readouterr().out
    assert "how is the lock released" in out and "asked 2 times" in out.lower()
    assert "r/src/lock.py" in out  # the logged results are shown
    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [COMMIT]}]


def test_a_pick_goes_through_add_case(terminal, monkeypatch):
    """Same validation and same file as `golden-set add`."""
    _log("q one")
    _log("q one")
    seen = []
    monkeypatch.setattr(golden_set, "add_case", lambda query, must_include, limit=5, mode="vector": seen.append(
        (query, must_include, limit, mode)) or {})
    terminal.append("1,2")

    golden_set.cmd_review()

    assert seen == [("q one", [CODE, COMMIT], 8, "vector")]


def test_a_hard_candidate_says_why(terminal, capsys):
    _fill_vector_scores()
    _log("why does the shard refuse to open", top_score=0.45)
    terminal.extend(["s"] * 10)

    golden_set.cmd_review(limit=1)

    out = capsys.readouterr().out
    assert "why does the shard refuse to open" in out
    # 21 scores, 0.45 then 0.50 ... 0.88: the first quartile (inclusive) is the sixth, 0.58
    assert "0.45" in out and "0.58" in out  # its score and the threshold it is under


def test_reject_is_remembered_privately_and_never_offered_again(terminal, capsys):
    _log("how is the lock released")
    _log("how is the lock released")
    terminal.append("r")

    golden_set.cmd_review()

    assert _cases() == []
    record = golden_set.rejected_path()
    assert record.parent == common.GOLDEN_SET_PATH.parent  # the config dir, never the index
    assert "lock" not in record.read_text()  # the record keeps no question text
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    assert golden_set.review_candidates()["candidates"] == []


def test_skip_and_none_add_nothing_and_are_offered_again(terminal):
    _log("how is the lock released")
    _log("how is the lock released")
    terminal.extend(["s"])
    golden_set.cmd_review()
    terminal.extend(["n"])
    golden_set.cmd_review()

    assert _cases() == []
    assert len(golden_set.review_candidates()["candidates"]) == 1


def test_quit_stops_the_review(terminal):
    for q in ("first q", "second q"):
        _log(q)
        _log(q)
    terminal.extend(["q", "1"])

    golden_set.cmd_review()

    assert _cases() == []


def test_end_of_input_ends_the_review(terminal, capsys):
    """Ctrl-D, or input that ran out, stops at once: not one prompt per
    remaining candidate."""
    for q in ("first q", "second q"):
        _log(q)
        _log(q)

    assert golden_set.cmd_review() == 0

    assert capsys.readouterr().out.count("q = quit") == 1


def test_a_bad_answer_is_asked_again(terminal, capsys):
    _log("how is the lock released")
    _log("how is the lock released")
    terminal.extend(["7", "x", "1"])

    golden_set.cmd_review()

    assert _cases()[0]["must_include"] == [CODE]
    assert "out of range" in capsys.readouterr().out.lower()


def test_older_queries_without_results_can_be_shown_but_not_made_cases(terminal, capsys):
    _log("how is the lock released", with_results=False)
    _log("how is the lock released", with_results=False)
    terminal.extend(["1", "s"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "r/src/lock.py" in out
    assert "These results cannot become a case" in out and "Asked again, the question can be" in out
    assert _cases() == []


def test_a_result_recorded_as_null_cannot_be_picked(terminal, capsys):
    _log("deploy script")
    _log("deploy script")
    # The newest search's first result had a credential-shaped name: recorded as null.
    conn = logdb._connect(common.LOG_DIR)
    with conn:
        newest = conn.execute("SELECT id, data FROM queries ORDER BY id DESC LIMIT 1").fetchone()
        data = json.loads(newest["data"])
        data["results"][0] = None
        conn.execute("UPDATE queries SET data = ? WHERE id = ?", (json.dumps(data), newest["id"]))
    conn.close()
    terminal.extend(["1", "2"])

    golden_set.cmd_review()

    assert "cannot become a case" in capsys.readouterr().out.lower()
    assert _cases()[0]["must_include"] == [COMMIT]


# --- only a search the check repeats can become a case ---------------------
#
# quality_check.run_golden_set() checks every case in its own mode (vector,
# keyword or hybrid: tests/test_golden_set_modes.py) over every repository,
# searched as readers search and never grouped by document. A case made
# from a search that was narrowed or grouped could fail on every check
# without retrieval getting any worse: a permanently red case in the ruler
# the golden set exists to be.


@pytest.mark.parametrize("unlike, why", [
    ({"repos": ["r"]}, "narrowed"),
    ({"source_types": ["commit"]}, "narrowed"),
    ({"group_by_document": True}, "grouped by document"),
])
def test_a_search_the_check_would_not_repeat_is_shown_but_not_made_a_case(terminal, capsys, unlike, why):
    _log("how is the lock released", **unlike)
    _log("how is the lock released", **unlike)
    terminal.extend(["1", "s"])

    assert golden_set.cmd_review() == 0

    out = capsys.readouterr().out
    assert "r/src/lock.py" in out  # still shown: the question is worth seeing
    assert why in out and "cannot become a case" in out.lower()
    assert "Asked again that way, the question can be" in out  # and what would make it one
    assert "which result is the right one" not in out.lower()  # not asked for a pick it would refuse
    assert _cases() == []


def test_a_plain_asking_of_the_same_question_is_the_one_offered(terminal, capsys):
    """Asked once as a plain vector search and later narrowed: the plain
    asking is the one a case can be made from, so it is the one shown."""
    _log("how is the lock released", results=(COMMIT,))
    _log("how is the lock released", repos=["r"], results=(CODE,))
    terminal.append("1")

    golden_set.cmd_review()

    assert _cases()[0]["must_include"] == [COMMIT]


def test_a_grouped_search_is_recorded_as_grouped(monkeypatch):
    """Grouped, `limit` counts documents and the results are one chunk per
    document; the review has to know, so the log says so."""
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [_Hit(0.9, dict(CODE))])

    async def call():
        from mcp.client.client import Client
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("griot_search", {"query": "lock", "group_by_document": True})
            await client.call_tool("griot_search", {"query": "lock"})

    asyncio.run(call())

    rows = logdb.read_since(common.LOG_DIR, "queries", days=1)
    assert [bool(r.get("group_by_document")) for r in rows] == [True, False]


def test_without_a_terminal_nothing_is_asked_or_changed(monkeypatch, capsys):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked without a terminal"))
    _log("how is the lock released")
    _log("how is the lock released")

    assert golden_set.cmd_review() == 2

    assert "terminal" in capsys.readouterr().err
    assert _cases() == []


def test_when_questions_are_not_logged_the_review_says_how_to_turn_it_on(terminal, capsys):
    _log(common.OMITTED_QUESTION)
    _log(common.OMITTED_QUESTION)

    assert golden_set.cmd_review() == 0

    out = capsys.readouterr().out
    assert "griot config set log-questions true" in out
    assert "2 " in out


def test_an_empty_log_says_so(terminal, capsys):
    assert golden_set.cmd_review() == 0
    assert "no candidates" in capsys.readouterr().out.lower()


def test_the_cli_runs_the_review(terminal, capsys):
    _log("how is the lock released")
    _log("how is the lock released")
    terminal.append("1")

    assert golden_set.main(["review", "--limit", "1"]) == 0

    assert _cases()[0]["must_include"] == [CODE]
