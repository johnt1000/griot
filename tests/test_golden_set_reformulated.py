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


# --- the agent client's own session id ---------------------------------------
#
# An agent's shell tool starts a new shell for every command, so the parent
# of `griot ask` is a different process each time. Claude Code exports
# CLAUDE_CODE_SESSION_ID (one value per conversation) both to the commands
# its shell tool runs and to the MCP servers it starts; when it is there,
# it is the session.

CLIENT_SESSION = "0b7c2a7e-5d1f-4c38-9a9e-3f1d2b6c8e40"


def _logged_bytes() -> bytes:
    return b"".join(path.read_bytes() for path in common.LOG_DIR.rglob("*") if path.is_file())


def _parent(monkeypatch, pid: int) -> None:
    """A live parent with that pid (a real psutil lookup would fail on a
    made-up pid and log no session at all)."""
    monkeypatch.setattr("os.getppid", lambda: pid)
    monkeypatch.setattr(common.psutil, "Process", lambda p: type("P", (), {"create_time": lambda self: 1.0})())


def test_the_client_session_id_is_the_session_whatever_the_parent(monkeypatch, fresh_session):
    """Two `griot ask` from two different shells of the same conversation
    are one session: the client's id decides, not the parent process."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)
    _parent(monkeypatch, 4242)
    first = common.log_session()
    common.log_session.cache_clear()
    _parent(monkeypatch, 4343)
    second = common.log_session()

    assert first == second
    assert isinstance(first, str) and len(first) == 16


def test_the_client_session_is_kept_even_without_a_parent(monkeypatch, fresh_session):
    """A process orphaned under pid 1 has no parent session, but the client
    still says which conversation it belongs to."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)
    monkeypatch.setattr("os.getppid", lambda: 1)

    assert common.log_session() is not None


def test_different_conversations_are_different_sessions(monkeypatch, fresh_session):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)
    one = common.log_session()
    common.log_session.cache_clear()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "another-conversation")

    assert common.log_session() != one


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_client_session_id_falls_back_to_the_parent(monkeypatch, fresh_session, value):
    _parent(monkeypatch, 4242)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    from_parent = common.log_session()
    common.log_session.cache_clear()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", value)

    assert common.log_session() == from_parent
    assert from_parent is not None


def test_without_a_client_session_id_the_parent_decides(monkeypatch, fresh_session):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    _parent(monkeypatch, 4242)
    first = common.log_session()
    common.log_session.cache_clear()
    _parent(monkeypatch, 4343)

    assert common.log_session() != first


def test_the_raw_client_session_id_is_never_logged(monkeypatch, fresh_session):
    """The log needs "same or not"; the id itself is the client's, and is
    kept out of logs.db in every form griot writes it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)

    common.log_query(question="a")

    session = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["session"]
    assert session is not None and CLIENT_SESSION not in session
    logged = _logged_bytes()
    assert CLIENT_SESSION.encode() not in logged
    assert CLIENT_SESSION.replace("-", "").encode() not in logged


def test_an_mcp_search_and_a_cli_ask_of_one_conversation_share_the_session(monkeypatch, fresh_session):
    """`griot mcp` and the `griot ask` the agent's shell tool runs are two
    processes with two different parents; the client hands both the same
    id, so a search through the tool and an ask after it can pair."""
    from griot import ask

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [_Hit(0.9, dict(CODE))])

    async def call():
        from mcp.client.client import Client
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("griot_search", {"query": "how is the lock released"})

    _parent(monkeypatch, 4242)
    asyncio.run(call())

    # The CLI is another process: another parent, and its own cache.
    common.log_session.cache_clear()
    _parent(monkeypatch, 4343)
    monkeypatch.setattr(ask, "ask", lambda *a, **k: (None, []))
    ask.main(["lock released when the process dies"])

    rows = sorted(logdb.read_since(common.LOG_DIR, "queries", days=1), key=lambda r: r["timestamp"])
    assert [row.get("via") for row in rows] == ["mcp", "cli"]
    assert rows[0]["session"] == rows[1]["session"]
    assert rows[0]["session"] is not None


# --- a conversation that changes under a running server ----------------------
#
# Observed on Claude Code 2.1.295 (2026-10-09, a stdio server that logged
# every message it received): `/clear` and an in-session `/resume` keep the
# MCP servers running with the CLAUDE_CODE_SESSION_ID they were started
# with, while the shell tool's next command gets the new conversation's id;
# a tools/call carries only `claudecode/toolUseId` and `progressToken` in
# its `_meta`, nothing naming the conversation. So there is nothing per call
# the server could read instead, and the limit is documented, not fixed.

# What Claude Code puts in a tools/call `_meta` (2.1.295).
CLAUDE_CODE_CALL_META = {"claudecode/toolUseId": "toolu_01AbCdEfGhJkLmNpQrStUvWx", "progressToken": 2}


def test_a_call_meta_from_claude_code_neither_changes_nor_leaks_into_the_session(monkeypatch, fresh_session):
    """The server's session is the one its environment named at start; the
    per-call tool-use id is not a conversation and is never logged."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLIENT_SESSION)
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **f: [_Hit(0.9, dict(CODE))])

    async def call():
        from mcp.client.client import Client
        async with Client(mcp_server.mcp) as client:
            await client.call_tool("griot_search", {"query": "lock"}, meta=dict(CLAUDE_CODE_CALL_META))

    _parent(monkeypatch, 4242)
    asyncio.run(call())
    # What a shell `griot ask` of the same conversation logs: another
    # process, so another parent and a fresh cache.
    common.log_session.cache_clear()
    _parent(monkeypatch, 4343)
    from_the_shell = common.log_session()

    [row] = logdb.read_since(common.LOG_DIR, "queries", days=1)
    assert row["session"] == from_the_shell is not None
    assert CLAUDE_CODE_CALL_META["claudecode/toolUseId"].encode() not in _logged_bytes()


def _flat(path: str) -> str:
    from pathlib import Path
    return " ".join((Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8").split())


def test_the_server_keeping_its_start_session_is_documented_where_sessions_are():
    """Whoever reads why a `griot ask` after `/clear` did not pair with the
    searches before it must find the reason where sessions are explained:
    the server keeps its start-time id, and no per-call id exists to use."""
    doc = " ".join(common.log_session.__doc__.split())
    [paragraph] = [p for p in _flat("docs/quality.md").split("Each logged search records its session")[1:]]
    paragraph = paragraph.split("A hybrid search logs")[0]
    for text in (doc, paragraph):
        assert "/clear" in text and "/resume" in text, text
        assert "_meta" in text, text


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


# --- picking from the follow-up's results too (debt 76) ------------------------
#
# The follow-up is the search that DID serve, so the right document may be
# only in its list. The review shows both lists, each result marked with the
# search that returned it (once, marked as in both, when both did), and a
# pick from either becomes a case of the FIRST question, in the first
# search's mode: the question whose list did not serve is the one worth
# checking.

FOLLOW = "lock released when the process dies"


def _line(out: str, number: int) -> str:
    return next(line for line in out.splitlines() if line.strip().startswith(f"[{number}]"))


def test_the_review_shows_both_lists_each_result_once_and_marked(terminal, capsys):
    _log("how is the lock released", at=0, results=(CODE, COMMIT))
    _log(FOLLOW, at=40, results=(DOC, CODE))
    terminal.append("q")

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "src/lock.py" in _line(out, 1) and "both searches" in _line(out, 1)
    assert "commit" in _line(out, 2) and "first search only" in _line(out, 2)
    assert "docs/locking.md" in _line(out, 3) and "follow-up only" in _line(out, 3)
    assert not any(line.strip().startswith("[4]") for line in out.splitlines())
    assert out.count("src/lock.py") == 1


def test_picking_a_follow_up_only_document_makes_a_case_of_the_first_question(terminal):
    _log("how is the lock released", at=0, mode="keyword", results=(CODE, COMMIT))
    _log(FOLLOW, at=40, mode="hybrid", results=(DOC, CODE))
    terminal.append("3")

    assert golden_set.cmd_review() == 0

    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [DOC],
                         "mode": "keyword"}]


def test_a_pick_from_both_lists_at_once_keeps_every_document(terminal):
    _log("how is the lock released", at=0, mode="vector", results=(CODE, COMMIT))
    _log(FOLLOW, at=40, results=(DOC,))
    terminal.append("2,3")

    golden_set.cmd_review()

    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [COMMIT, DOC]}]


@pytest.mark.parametrize("unlike,shown", [
    ({"repos": ["r"]}, "narrowed to repos r"),
    ({"source_types": ["code"]}, "narrowed to source_types code"),
    ({"group_by_document": True}, "grouped by document"),
])
def test_a_follow_up_unlike_the_check_is_shown_but_its_results_cannot_be_picked(terminal, capsys, unlike, shown):
    """A case is checked over every repository and not grouped: a narrowed or
    grouped follow-up chose its best from another pool (or counted
    documents), so what it returned is not what a person can call the right
    answer to the check's search."""
    _log("how is the lock released", at=0, results=(CODE, COMMIT))
    _log(FOLLOW, at=40, results=(DOC, CODE), **unlike)
    terminal.extend(["3", "1"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    # Said with the list, before the person is asked, not only on a refused pick.
    assert f"The follow-up's results cannot become a case: it was {shown}" in out.split("Which result")[0]
    assert "cannot become a case" in _line(out, 3)
    # In both lists, and the first search is like the check: still pickable.
    assert "cannot become a case" not in _line(out, 1)
    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [CODE],
                         "mode": "hybrid"}]


def test_a_narrowed_first_search_still_offers_a_follow_up_like_the_check(terminal, capsys):
    """The case asserts nothing about what the first search returned, only
    which document answers its question; that judgement is sound when it
    was made among results drawn from the pool the check searches."""
    _log("how is the lock released", at=0, mode="vector", results=(CODE, COMMIT), repos=["r"])
    _log(FOLLOW, at=40, results=(DOC, CODE))
    terminal.extend(["2", "1,3"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "narrowed to repos r" in out
    assert "cannot become a case" in _line(out, 2)
    assert "cannot become a case" not in _line(out, 1) and "cannot become a case" not in _line(out, 3)
    assert _cases() == [{"query": "how is the lock released", "limit": 8, "must_include": [CODE, DOC]}]


def test_a_document_both_searches_returned_narrowed_cannot_be_picked(terminal, capsys):
    _log("how is the lock released", at=0, results=(CODE,), repos=["r"])
    _log(FOLLOW, at=40, results=(DOC, CODE), source_types=["code"])
    terminal.extend(["1", "q"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "both searches" in _line(out, 1) and "cannot become a case" in _line(out, 1)
    assert "cannot become a case" in _line(out, 2)
    assert "Which result is the right one?" not in out
    assert _cases() == []


def test_ranks_are_the_first_searchs_and_shown_only_beside_its_results(terminal, capsys):
    _log("how is the lock released", at=0, results=(CODE, COMMIT),
         ranks=[{"vector": 1, "keyword": None}, {"vector": 2, "keyword": 1}], rank_window=48)
    _log(FOLLOW, at=40, results=(DOC, CODE))
    terminal.append("q")

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "(vector 1, keyword -)" in _line(out, 1) and "(vector 2, keyword 1)" in _line(out, 2)
    assert "vector" not in _line(out, 3)


def test_a_document_the_follow_up_returned_twice_is_shown_once(terminal, capsys):
    """One document fills up to three results of a search made for a reader."""
    _log("how is the lock released", at=0, results=(CODE,))
    _log(FOLLOW, at=40, results=(DOC, DOC, COMMIT))
    terminal.append("q")

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "docs/locking.md" in _line(out, 2) and "commit" in _line(out, 3)
    assert out.count("docs/locking.md") == 1


def test_a_first_search_of_a_mode_the_check_does_not_know_offers_nothing(terminal, capsys):
    """The case is checked in the first search's mode; none it can be."""
    _log("how is the lock released", at=0, mode="semantic", results=(CODE,))
    _log(FOLLOW, at=40, results=(DOC,))
    terminal.extend(["2", "q"])

    golden_set.cmd_review()

    assert "cannot become a case" in _line(capsys.readouterr().out, 2)
    assert _cases() == []


def test_a_follow_up_result_that_cannot_be_a_case_cannot_be_picked(terminal, capsys):
    """A result the log kept only by its label (its name looked like a
    credential)."""
    _log("how is the lock released", at=0, results=(CODE,))
    _log(FOLLOW, at=40, results=(DOC, COMMIT))
    rows = logdb.read_since(common.LOG_DIR, "queries", days=1)
    rows[1]["results"][0] = None
    terminal.extend(["2", "3"])

    found = golden_set.review_candidates(rows)["candidates"]
    assert len(found) == 1
    golden_set._show(found[0], 1, 1)
    answer = golden_set._ask(found[0])

    out = capsys.readouterr().out
    assert "cannot become a case" in _line(out, 2)
    assert answer == [COMMIT]


def test_a_follow_up_logged_before_results_were_shows_its_labels_only(terminal, capsys):
    _log("how is the lock released", at=0, results=(CODE,))
    _log(FOLLOW, at=40, results=(DOC,))
    rows = logdb.read_since(common.LOG_DIR, "queries", days=1)
    rows[1]["results"] = None
    terminal.extend(["2", "1"])

    found = golden_set.review_candidates(rows)["candidates"]
    golden_set._show(found[0], 1, 1)
    answer = golden_set._ask(found[0])

    out = capsys.readouterr().out
    assert "docs/locking.md" in _line(out, 2) and "cannot become a case" in _line(out, 2)
    assert answer == [CODE]


# --- one of each kind until the limit (debt 75) --------------------------------


def _repeat(question, at):
    _log(question, at=at, session="x")
    _log(question, at=at + 1, session="y")


def _reword(question, at):
    _log(question, at=at)
    _log(f"{question} again reworded", at=at + 10, results=(DOC,))


def test_kinds_take_turns_so_a_kind_with_many_does_not_crowd_out_the_rest():
    for i in range(5):
        _repeat(f"repeated question number{i}", at=10_000 + 100 * i)
    _reword("which lock does the indexer take", at=1000)
    _reword("where is the spend ceiling read", at=2000)

    found = golden_set.review_candidates(limit=4)["candidates"]

    assert [(c["kind"], c["query"]) for c in found] == [
        ("repeated", "repeated question number4"),
        ("reformulated", "where is the spend ceiling read"),
        ("repeated", "repeated question number3"),
        ("reformulated", "which lock does the indexer take"),
    ]


def test_once_a_kind_runs_out_the_others_fill_the_limit():
    for i in range(4):
        _repeat(f"repeated question number{i}", at=10_000 + 100 * i)
    _reword("which lock does the indexer take", at=1000)

    found = golden_set.review_candidates(limit=4)["candidates"]

    assert [c["kind"] for c in found] == ["repeated", "reformulated", "repeated", "repeated"]


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_the_limit_still_bounds_the_rotation(limit):
    for i in range(3):
        _repeat(f"repeated question number{i}", at=10_000 + 100 * i)
        _reword(f"which lock does indexer{i} take", at=1000 + 1000 * i)

    found = golden_set.review_candidates(limit=limit)["candidates"]

    assert len(found) == limit
    assert [c["kind"] for c in found] == ["repeated", "reformulated", "repeated"][:limit]


def test_the_review_names_each_candidates_kind(terminal, capsys):
    _repeat("how is the cache warmed", at=10_000)
    _reword("which lock does the indexer take", at=1000)
    terminal.extend(["s", "s"])

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "[1/2] (asked again) how is the cache warmed" in out
    assert "[2/2] (reworded) which lock does the indexer take" in out
