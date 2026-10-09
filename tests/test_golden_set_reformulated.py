"""`griot golden-set review`, fourth kind: a search followed, in the same
session and shortly after, by another one that looks like a rewording of
it. The rewording is an implicit sign the first list did not serve, so the
FIRST search is offered, with the follow-up shown beside it.

"Same session" is only what the log says: every row log_query() writes
carries `session`, an opaque digest of the process that holds the
conversation (common.log_session()). Rows without it (everything logged
before it existed) produce no candidate and break nothing.

Rows are written to a real logs.db; the terminal is faked as in
test_golden_set_review.py."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from griot import common, golden_set, logdb, mcp_server

CODE = {"repo": "r", "source_type": "code", "file_path": "src/lock.py"}
COMMIT = {"repo": "r", "source_type": "commit", "commit_hash": "abc123def4567890abc123def4567890abc12345"}
DOC = {"repo": "r", "source_type": "code", "file_path": "docs/locking.md"}

_START = datetime.now(timezone.utc) - timedelta(hours=5)


def _log(question, *, at=0.0, session="s1", results=(CODE, COMMIT), mode="hybrid", collection="c1",
         timestamp=..., **extra):
    """One query-log row `at` seconds after a fixed start, as log_query()
    writes it today (session included unless session=... says otherwise)."""
    record = {
        "timestamp": timestamp if timestamp is not ... else (_START + timedelta(seconds=at)).isoformat(),
        "profile": "p", "collection": collection, "project": "p1", "question": question, "via": "mcp",
        "limit": 8, "mode": mode, "num_sources": len(results),
        "sources": [common.source_label(r) for r in results], "results": [dict(r) for r in results],
        "top_score": 0.5, **extra,
    }
    if session is not ...:
        record["session"] = session
    logdb.write_query(common.LOG_DIR, record)


@pytest.fixture
def terminal(monkeypatch):
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
    def refuse(*a, **k):
        raise AssertionError("the review touched the index")
    monkeypatch.setattr(common, "get_client", refuse)
    monkeypatch.setattr(common, "embed_query", refuse, raising=False)


def _reformulated():
    return [(c["query"], c["followup"]) for c in golden_set.review_candidates(limit=100)["candidates"]
            if c["kind"] == "reformulated"]


def _cases():
    return json.loads(common.GOLDEN_SET_PATH.read_text()) if common.GOLDEN_SET_PATH.exists() else []


# --- what the log records ---------------------------------------------------


class _Hit:
    def __init__(self, score, payload):
        self.score, self.payload, self.id = score, payload, "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def fresh_session():
    common.log_session.cache_clear()
    yield
    common.log_session.cache_clear()


def test_every_logged_search_says_which_session_it_came_from(fresh_session):
    """One process, one conversation: the same opaque value on every row, and
    not the process number itself."""
    common.log_query(question="a")
    common.log_query(question="b")

    rows = logdb.read_since(common.LOG_DIR, "queries", days=1)
    sessions = {row.get("session") for row in rows}
    assert len(sessions) == 1
    session = sessions.pop()
    assert isinstance(session, str) and len(session) == 16
    import os
    assert str(os.getppid()) not in session


def test_a_process_without_a_real_parent_logs_no_session(monkeypatch, fresh_session):
    """Orphaned processes all hang off pid 1: giving them a session would
    make one session of every unrelated orphan."""
    monkeypatch.setattr("os.getppid", lambda: 1)

    common.log_query(question="a")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["session"] is None


def test_a_parent_that_cannot_be_read_logs_no_session(monkeypatch, fresh_session):
    def gone(pid):
        raise common.psutil.NoSuchProcess(pid)
    monkeypatch.setattr(common.psutil, "Process", gone)

    common.log_query(question="a")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["session"] is None


def test_the_session_is_recorded_through_the_real_mcp_client(monkeypatch, fresh_session):
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [_Hit(0.9, dict(CODE))])

    async def call():
        from mcp.client.client import Client
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("griot_search", {"query": "lock"})
            await client.call_tool("griot_search", {"query": "lock release"})

    asyncio.run(call())

    rows = logdb.read_since(common.LOG_DIR, "queries", days=1)
    assert len(rows) == 2 and rows[0]["session"] == rows[1]["session"] == common.log_session()
    assert rows[0]["session"] is not None


# --- which searches are offered ---------------------------------------------


def test_a_search_reworded_soon_after_in_the_same_session_is_offered():
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=40, results=(DOC,))

    assert _reformulated() == [("how is the lock released", "lock released when the process dies")]


def test_the_rule_ignores_case_punctuation_and_short_words():
    """'how', 'is', 'the' are shared by unrelated questions; what makes the
    follow-up the same subject is a longer word in common."""
    _log("How is the LOCK released?", at=0)
    _log("how is the index built", at=10, results=(DOC,))

    assert _reformulated() == []


def test_shared_longer_words_must_be_half_of_the_shorter_question():
    """One common word out of many is a different question that happens to
    mention the same thing."""
    _log("where lock released process exits", at=0)          # 5 longer words
    _log("where cache entries expire", at=10, results=(DOC,))  # shares only "where"

    assert _reformulated() == []


def test_the_same_question_again_is_repeated_not_reformulated():
    _log("how is the lock released", at=0)
    _log("released: how is the LOCK", at=10, results=(DOC,))

    kinds = [c["kind"] for c in golden_set.review_candidates()["candidates"]]
    assert kinds == ["repeated"]


def test_another_session_does_not_count():
    _log("how is the lock released", at=0, session="s1")
    _log("lock released when the process dies", at=40, session="s2", results=(DOC,))

    assert _reformulated() == []


@pytest.mark.parametrize("first,second", [(..., "s1"), ("s1", ...), (None, None), ("", "")])
def test_rows_without_a_session_produce_nothing_and_break_nothing(first, second):
    """Everything logged before sessions were: no session, no pairing (never
    guessed from timestamps alone)."""
    _log("how is the lock released", at=0, session=first)
    _log("lock released when the process dies", at=40, session=second, results=(DOC,))

    assert _reformulated() == []


def test_a_follow_up_after_the_window_does_not_count():
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=golden_set.REFORMULATION_WINDOW_SECONDS + 1, results=(DOC,))

    assert _reformulated() == []


def test_a_follow_up_at_the_edge_of_the_window_counts():
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=golden_set.REFORMULATION_WINDOW_SECONDS, results=(DOC,))

    assert _reformulated() == [("how is the lock released", "lock released when the process dies")]


def test_only_the_next_search_of_the_session_is_the_follow_up():
    """A search between the two means the person moved on before coming back."""
    _log("how is the lock released", at=0)
    _log("what does stats print", at=10, results=(DOC,))
    _log("lock released when the process dies", at=20, results=(DOC,))

    assert _reformulated() == []


def test_a_search_logged_without_its_question_in_between_still_interrupts():
    _log("how is the lock released", at=0)
    _log(common.OMITTED_QUESTION, at=10, results=(DOC,))
    _log("lock released when the process dies", at=20, results=(DOC,))

    assert _reformulated() == []


def test_another_sessions_search_in_between_does_not_interrupt():
    _log("how is the lock released", at=0, session="s1")
    _log("what does stats print", at=10, session="s2", results=(DOC,))
    _log("lock released when the process dies", at=20, session="s1", results=(DOC,))

    assert _reformulated() == [("how is the lock released", "lock released when the process dies")]


def test_a_follow_up_that_returned_the_same_results_does_not_count():
    """The rewording changed nothing the person saw: no sign the first list
    was the one that failed."""
    _log("how is the lock released", at=0, results=(CODE, COMMIT))
    _log("lock released when the process dies", at=10, results=(COMMIT, CODE))

    assert _reformulated() == []


def test_a_follow_up_in_another_collection_does_not_count():
    _log("how is the lock released", at=0, collection="c1")
    _log("lock released when the process dies", at=10, collection="c2", results=(DOC,))

    assert _reformulated() == []


def test_rows_are_ordered_by_time_not_by_where_they_sit_in_the_log():
    """The log reads in time order, but the rows given may not be."""
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=10, results=(DOC,))
    rows = list(reversed(logdb.read_since(common.LOG_DIR, "queries", days=1)))

    found = golden_set.review_candidates(rows)["candidates"]

    assert [(c["kind"], c["query"]) for c in found] == [("reformulated", "how is the lock released")]


def test_two_searches_at_the_same_instant_are_not_a_rewording():
    """Parallel searches (two subagents sharing one MCP server): neither
    could have been written after reading the other's list."""
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=0, results=(DOC,))

    assert _reformulated() == []


@pytest.mark.parametrize("bad", ["not a time", None, 12, "2026-10-09T10:00:00"])
def test_a_row_whose_time_cannot_be_read_produces_nothing(bad):
    """Read as the log holds it (a time the database column could not order
    may still be in the JSON). A time without a zone cannot be compared with
    one that has it, so it is unreadable too."""
    _log("how is the lock released", timestamp=bad)
    _log("lock released when the process dies", at=10, results=(DOC,))
    rows = [json.loads(r) for (r,) in logdb._connect(common.LOG_DIR).execute("SELECT data FROM queries")]

    candidates = golden_set.review_candidates(rows, limit=100)["candidates"]

    assert [c for c in candidates if c["kind"] == "reformulated"] == []


def test_a_follow_up_logged_without_its_question_does_not_count():
    """Not even when the first question happens to share the placeholder's
    words: the placeholder is not a question anyone asked."""
    _log(f"why is the question {common.OMITTED_QUESTION} logged", at=0)
    _log(common.OMITTED_QUESTION, at=10, results=(DOC,))

    assert _reformulated() == []


def test_a_follow_up_without_its_result_list_does_not_count():
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=10, results=(DOC,), sources=None)

    assert _reformulated() == []


def test_a_rejected_or_curated_first_question_is_not_offered():
    golden_set.add_case("how is the lock released", [CODE])
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=10, results=(DOC,))
    golden_set._reject(golden_set.query_key("which lock does the indexer take"))
    _log("which lock does the indexer take", at=1000)
    _log("indexer lock taken", at=1010, results=(DOC,))

    assert _reformulated() == []


def test_reformulated_candidates_come_after_the_other_kinds_newest_first():
    _log("asked twice", at=0, session="x")
    _log("asked twice", at=1, session="y")
    _log("how is the lock released", at=100)
    _log("lock released when the process dies", at=110, results=(DOC,))
    _log("which lock does the indexer take", at=1000)
    _log("indexer lock taken", at=1010, results=(DOC,))

    found = golden_set.review_candidates()["candidates"]

    assert [(c["kind"], c["query"]) for c in found] == [
        ("repeated", "asked twice"),
        ("reformulated", "which lock does the indexer take"),
        ("reformulated", "how is the lock released"),
    ]


# --- the review ---------------------------------------------------------------


def test_the_review_shows_the_follow_up_and_a_pick_keeps_the_first_searchs_mode(terminal, capsys):
    _log("how is the lock released", at=0, mode="keyword")
    _log("lock released when the process dies", at=40, results=(DOC,))
    terminal.append("1")

    assert golden_set.cmd_review() == 0

    out = capsys.readouterr().out
    assert "lock released when the process dies" in out
    assert "40 s later" in out
    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [CODE],
                         "mode": "keyword"}]


def test_a_narrowed_first_search_is_shown_but_cannot_be_picked(terminal, capsys):
    _log("how is the lock released", at=0, repos=["r"])
    _log("lock released when the process dies", at=40, results=(DOC,))
    terminal.extend(["1", "q"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "narrowed to repos r" in out and "cannot become a case" in out
    assert _cases() == []


def test_rejecting_one_never_offers_it_again(terminal):
    _log("how is the lock released", at=0)
    _log("lock released when the process dies", at=40, results=(DOC,))
    terminal.append("r")

    golden_set.cmd_review()

    assert _reformulated() == []


def test_the_empty_review_names_the_fourth_kind(terminal, capsys):
    golden_set.cmd_review()

    assert "reworded" in capsys.readouterr().out
