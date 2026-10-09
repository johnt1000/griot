"""End to end for a reworded search in `griot golden-set review`: two real
griot_search calls through the MCP client on a real Edge index log two rows
of one session to the real logs.db, and the review, reading only the log,
offers the first question with both lists and makes a case from a document
only the follow-up returned.

Vector searches with chosen embeddings, so which documents each question
returns is fixed: the first question finds LOCK and SHARED, the follow-up
finds DEATH and SHARED."""

import asyncio
import json
import math

import pytest
from mcp.client.client import Client

from griot import common, golden_set, mcp_server

FIRST = "how is the lock released"
FOLLOW = "lock released when the process dies"


def _doc(name: str, text: str) -> dict:
    return {"id": f"alpha:code:{name}:0", "content": text,
            "metadata": {"source_type": "code", "repo": "alpha", "file_path": f"src/{name}", "chunk_index": 0}}


LOCK = _doc("lock.py", "lock bookkeeping")
SHARED = _doc("shared.py", "shared helpers")
DEATH = _doc("death.py", "process death handling")
FAR = _doc("far.py", "unrelated notes")

# Angles on a circle: each question sits on its first document, SHARED
# between the two, FAR opposite.
ANGLES = {LOCK["content"]: 0.0, SHARED["content"]: 0.3, DEATH["content"]: 0.6, FAR["content"]: math.pi,
          FIRST: 0.0, FOLLOW: 0.6}


def _unit(angle: float) -> list[float]:
    vector = [0.0] * common.EMBED_DIM
    vector[0], vector[1] = math.cos(angle), math.sin(angle)
    return vector


@pytest.fixture
def index(monkeypatch):
    def embed(texts, **kw):
        return [_unit(next((a for key, a in ANGLES.items() if key == text or key in text), math.pi))
                for text in texts]

    monkeypatch.setattr(common, "embed_texts", embed)
    common.index_documents([LOCK, SHARED, DEATH, FAR])
    common.release_lock()


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


def _search_twice():
    async def call():
        async with Client(mcp_server.mcp) as client:
            for query in (FIRST, FOLLOW):
                result = await client.call_tool("griot_search", {"query": query, "limit": 2, "mode": "vector"})
                assert not result.is_error

    common.log_session.cache_clear()
    asyncio.run(call())
    common.release_client()


def test_a_document_only_the_follow_up_found_becomes_a_case_of_the_first_question(index, terminal, capsys,
                                                                                    monkeypatch):
    _search_twice()
    monkeypatch.setattr(common, "get_client", lambda *a, **k: pytest.fail("the review touched the index"))
    terminal.append("3")

    assert golden_set.cmd_review() == 0

    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines() if line.strip().startswith("[")]
    assert "(reworded)" in lines[0] and FIRST in lines[0]
    assert lines[1].startswith("[1] alpha/src/lock.py") and "first search only" in lines[1]
    assert lines[2].startswith("[2] alpha/src/shared.py") and "both searches" in lines[2]
    assert lines[3].startswith("[3] alpha/src/death.py") and "follow-up only" in lines[3]
    assert len(lines) == 4
    assert json.loads(common.GOLDEN_SET_PATH.read_text()) == [{
        "query": FIRST, "limit": 2,  # a vector case is written without its mode, the default
        "must_include": [{"source_type": "code", "repo": "alpha", "file_path": "src/death.py"}],
    }]
