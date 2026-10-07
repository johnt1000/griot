"""A folded copy reaches a reader only as its `also_in` label.

A diverse search shows the same text once and names the other places it
was found in `also_in`. Behind each label, `SearchHit.copies` keeps that
copy's whole stored payload (the golden set needs the stored fields to tell
whether a case's document came back as one): its text again, and every
field the label does not show. Nothing but a docstring kept it from being
serialized. This holds the rule on every surface a reader gets results
from: the terminal (`griot search`, `griot ask` and the context it sends the
chat model), the query log, and griot_search through the protocol. Derived
from the rule, not from a field name: every value a copy holds that the
result shown does not is looked for, once the also_in labels are taken out,
and the shared text appears no more often than the results that show it.
"""

import hashlib
import json
import random

import pytest
from mcp.client.client import Client

from griot import ask, cli, common, logdb, mcp_server

QUERY = "how is the cache invalidated"
SHARED = "the cache is invalidated when the config hash changes"
LIMIT = 5


def _vector(text: str, dim: int) -> list[float]:
    """The query is axis 0; the shared text is close to it; anything else is
    noise orthogonal to the query."""
    if text in (QUERY, SHARED):
        v = [0.0] * dim
        v[0] = 1.0
        if text == SHARED:
            v[1] = 0.2
        return v
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    v = [rng.uniform(-1, 1) for _ in range(dim)]
    v[0] = 0.0
    return v


def _doc(repo: str, path: str, text: str) -> dict:
    # `marker` is a field no label shows, different in each copy: what a
    # leaked payload would carry that the result shown does not.
    return {"id": f"{repo}:code:{path}:0", "content": text,
            "metadata": {"source_type": "code", "repo": repo, "file_path": path, "chunk_index": 0,
                         "marker": f"stored-only-in-{repo}"}}


DOCS = [
    _doc("alpha-origin", "src/cache_origin.py", SHARED),
    _doc("beta-vendored", "vendor/cache_vendored.py", SHARED),
    _doc("gamma-noise", "noise.py", "unrelated text about logging"),
]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: _prompts.append(prompt) or "answer")
    _prompts.clear()
    common.index_documents(DOCS)
    common.release_lock()


_prompts: list[str] = []


def _hits():
    """What every surface below is asked for, searched directly: which copy
    a reader is shown is the store's to decide (both score the same)."""
    hits = common.search(QUERY, limit=LIMIT, mode="vector", diverse=True)
    assert hits and hits[0].payload["content"] == SHARED and hits[0].copies, [h.payload for h in hits]
    return hits


def _assert_no_copy_reaches(output: str, hits, *, shows_text: bool) -> None:
    """Out of `output`, once every also_in label is taken out, nothing a
    folded copy holds that its result does not: not its place, not a field
    the label never showed. And the shared text no more than once per result
    that shows it (once in this index): a copy is the same text again."""
    labels = [label for h in hits for label in h.also_in]
    assert labels, "premise: the search folded a copy"
    rest = output
    for label in labels:
        rest = rest.replace(label, "")
    for hit in hits:
        for copy in hit.copies:
            only_in_copy = {str(v) for k, v in copy.items()
                            if isinstance(v, str) and v.strip() and v != hit.payload.get(k)}
            assert only_in_copy, "premise: a copy differs from its result"
            leaked = sorted(v for v in only_in_copy if v in rest)
            assert not leaked, (leaked, output)
    assert output.count(SHARED) == (1 if shows_text else 0), output


def test_griot_search_in_the_terminal(index, capsys):
    hits = _hits()
    assert cli.main(["search", QUERY, "--mode", "vector", "--limit", str(LIMIT)]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "same text in:" in out
    _assert_no_copy_reaches(out, hits, shows_text=True)


def test_griot_ask_its_context_and_its_sources(index, capsys):
    hits = _hits()
    ask.main([QUERY, "--mode", "vector", "--limit", str(LIMIT), "--show-sources"])
    captured = capsys.readouterr()
    [prompt] = _prompts
    _assert_no_copy_reaches(prompt, hits, shows_text=True)
    _assert_no_copy_reaches(captured.out + captured.err, hits, shows_text=False)


def test_the_query_log(index, capsys):
    hits = _hits()
    ask.main([QUERY, "--mode", "vector", "--limit", str(LIMIT)])
    capsys.readouterr()
    rows = logdb.read_recent(common.LOG_DIR, "queries", limit=10)
    assert rows
    _assert_no_copy_reaches(json.dumps(rows, ensure_ascii=False), hits, shows_text=False)


@pytest.mark.anyio
async def test_griot_search_through_the_protocol(index):
    hits = _hits()
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": QUERY, "limit": LIMIT, "mode": "vector"})
    structured = json.dumps(result.structured_content, ensure_ascii=False)
    assert any(r.get("also_in") for r in result.structured_content["results"])
    _assert_no_copy_reaches(structured, hits, shows_text=True)
    text = "\n".join(block.text for block in result.content if getattr(block, "text", None))
    _assert_no_copy_reaches(json.dumps(json.loads(text), ensure_ascii=False) if text.startswith("{") else text,
                            hits, shows_text=True)
    # The log the MCP search wrote, too: golden-set review reads it.
    rows = logdb.read_recent(common.LOG_DIR, "queries", limit=10)
    assert rows
    _assert_no_copy_reaches(json.dumps(rows, ensure_ascii=False), hits, shows_text=False)
