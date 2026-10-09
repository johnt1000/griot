"""A hybrid search logs where each result stood in the vector ranking and in
the keyword ranking, and `griot golden-set review` offers the searches where
the two rankings disagreed about what comes first.

A hybrid search is two rankings fused (reciprocal rank fusion). The fused
list alone cannot say whether the result a reader got first was everybody's
first or the favourite of one ranking that the other did not rank at all;
the second is a question where a person saying which result was right tells
which ranking was. So the log keeps, per result, its rank in each ranking
(1 is first, null when that ranking did not place it among the points it
was asked for), from the two lists the fusion already has: no extra
embedding and no extra search.

The index here is a real Edge collection with keyword vectors; the dense
vectors are chosen so that the vector ranking and the keyword ranking
disagree on purpose."""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
import qdrant_edge as qe
from mcp.client.client import Client

from griot import ask, common, golden_set, logdb, mcp_server, stats


def T(*parts: str) -> str:
    return "".join(parts)


# Assembled from pieces so this file does not itself look like it holds one.
TOKEN = T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8")


def _doc(key: str, text: str, path: str) -> dict:
    return {"id": f"alpha:code:{key}", "content": text,
            "metadata": {"source_type": "code", "repo": "alpha", "file_path": path, "chunk_index": 0}}


# NEAR is the vector ranking's first for both queries and holds neither
# "zanzibar" nor anything a filler holds; FAR holds the word and is the
# vector ranking's last. Fillers sit between, closer to the query the lower
# their number.
NEAR = _doc("near.py:0", "the lock is released when the run ends", "src/near.py")
FAR = _doc("far.py:0", "zanzibar handling for imports", "src/far.py")
FILLERS = [_doc(f"f{i}.py:0", f"filler note number{i} about other things", f"src/f{i}.py") for i in range(15)]
DOCS = [NEAR, FAR, *FILLERS]


def _unit(angle: float, dim: int) -> list[float]:
    vector = [0.0] * dim
    vector[0], vector[1] = math.cos(angle), math.sin(angle)
    return vector


def _angles() -> dict[str, float]:
    angles = {NEAR["content"]: 0.01, FAR["content"]: math.pi}
    for i, filler in enumerate(FILLERS):
        angles[filler["content"]] = 0.1 + 0.1 * i
    return angles


@pytest.fixture
def index(monkeypatch):
    angles = _angles()

    def embed(texts, **kw):
        out = []
        for text in texts:
            # A query embeds next to NEAR; a stored chunk by the content it holds.
            angle = next((a for content, a in angles.items() if content in text), 0.0)
            out.append(_unit(angle, common.EMBED_DIM))
        return out

    monkeypatch.setattr(common, "embed_texts", embed)
    common.index_documents(DOCS)
    common.release_lock()


def _path(hit) -> str:
    return (hit.payload or {}).get("file_path")


# --- the ranks a hybrid search carries -------------------------------------------------------------


@pytest.mark.parametrize("diverse", [False, True])
def test_each_hybrid_result_carries_its_rank_in_each_ranking(index, diverse):
    hits = common.search("zanzibar", limit=5, mode="hybrid", diverse=diverse)

    window = 5 * 6 if diverse else 5
    # Each ranking on its own, as a vector and a keyword search return it.
    dense = {_path(h): n for n, h in enumerate(common.search("zanzibar", limit=window, mode="vector"), start=1)}
    words = {_path(h): n for n, h in enumerate(common.search("zanzibar", limit=window, mode="keyword"), start=1)}
    assert hits
    for hit in hits:
        assert (hit.vector_rank, hit.keyword_rank) == (dense.get(_path(hit)), words.get(_path(hit)))
        assert hit.rank_window == window
    by_path = {_path(h): h for h in hits}
    assert (by_path["src/near.py"].vector_rank, by_path["src/near.py"].keyword_rank) == (1, None)
    assert by_path["src/far.py"].keyword_rank == 1


def test_the_fusion_ranks_and_scores_as_the_engine_fusion_did(index):
    """Fused in griot rather than in the store, so that both rankings are at
    hand; the scores are the store's own reciprocal rank fusion, k=60."""
    client = common.get_client()
    query_vector = common.embed_texts(["lock zanzibar"])[0]
    words = qe.Bm25().embed_query("lock zanzibar")  # the engine's BM25 with its defaults, as griot makes it
    engine = client.query(qe.QueryRequest(
        prefetches=[qe.Prefetch(limit=10, query=qe.Query.Nearest(query_vector, using="dense")),
                    qe.Prefetch(limit=10, query=qe.Query.Nearest(words, using=common.KEYWORD_VECTOR))],
        query=qe.Fusion.Rrf(k=60), limit=10, with_payload=True))

    ours = common.search("lock zanzibar", limit=10, mode="hybrid")

    assert sorted(round(h.score, 6) for h in ours) == sorted(round(h.score, 6) for h in engine)
    engine_score = {str(h.id): h.score for h in engine}
    assert all(abs(engine_score[str(h.id)] - h.score) < 1e-6 for h in ours)
    assert [h.score for h in ours] == sorted((h.score for h in ours), reverse=True)


def test_ties_put_the_vector_rankings_point_first(index):
    """Two points each first in one ranking score the same: the store left
    their order to chance; griot puts the vector ranking's first."""
    hits = common.search("zanzibar", limit=2, mode="hybrid")

    assert hits[0].score == hits[1].score
    assert [_path(h) for h in hits] == ["src/near.py", "src/far.py"]


class _CountingClient:
    def __init__(self, client):
        self._client, self.queries = client, 0

    def query(self, request):
        self.queries += 1
        return self._client.query(request)

    def __getattr__(self, name):
        return getattr(self._client, name)


def test_a_hybrid_search_costs_two_store_queries_and_one_embedding(index, monkeypatch):
    real_client, real_embed = common.get_client(), common.embed_texts
    counting = _CountingClient(real_client)
    embedded = []
    monkeypatch.setattr(common, "get_client", lambda: counting)
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: embedded.append(texts) or real_embed(texts))

    common.search("zanzibar", limit=5, mode="hybrid")

    assert counting.queries == 2 and len(embedded) == 1


@pytest.mark.parametrize("mode", ["vector", "keyword"])
def test_other_modes_carry_no_ranks(index, mode):
    for hit in common.search("zanzibar", limit=3, mode=mode, diverse=True):
        assert (getattr(hit, "vector_rank", None), getattr(hit, "keyword_rank", None)) == (None, None)


# --- what the query log records -----------------------------------------------------------------


def _rows():
    return logdb.read_since(common.LOG_DIR, "queries", days=1)


def _call(**arguments):
    async def call():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool("griot_search", arguments)

    return asyncio.run(call())


def test_a_hybrid_search_through_the_mcp_client_logs_both_ranks_per_result(index):
    result = _call(query="zanzibar")

    # What the agent gets is unchanged: the ranks go to the log, not the answer.
    assert not result.is_error
    out = result.structured_content
    assert out["mode"] == "hybrid"
    assert [r["metadata"]["file_path"] for r in out["results"]][:2] == ["src/far.py", "src/near.py"]
    assert all("vector_rank" not in r and "ranks" not in r for r in out["results"])
    row = _rows()[0]
    assert row["mode"] == "hybrid"
    assert len(row["ranks"]) == len(row["results"]) == len(row["sources"])
    by_path = {r["file_path"]: rank for r, rank in zip(row["results"], row["ranks"])}
    assert by_path["src/near.py"] == {"vector": 1, "keyword": None}
    assert by_path["src/far.py"]["keyword"] == 1 and by_path["src/far.py"]["vector"] > 10
    assert row["rank_window"] == mcp_server.SEARCH_LIMIT_DEFAULT * 6


def test_a_vector_search_logs_no_ranks(index):
    _call(query="zanzibar", mode="vector")

    row = _rows()[0]
    assert "ranks" not in row and "rank_window" not in row


def test_cli_ask_logs_the_ranks_too(index, monkeypatch):
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.0)

    ask.main(["zanzibar"])

    row = _rows()[0]
    assert row["mode"] == "hybrid" and len(row["ranks"]) == len(row["results"])
    assert row["rank_window"] == ask.DEFAULT_LIMIT * 6


def test_a_result_whose_name_looks_like_a_credential_still_logs_only_numbers(index, monkeypatch):
    """The rank is a number: it carries nothing of the name the results
    entry withholds."""
    leaky = _doc("leak.py:0", "zanzibar again here", "deploy/" + TOKEN + ".sh")
    common.index_documents([leaky])
    common.release_lock()

    _call(query="zanzibar")

    row = _rows()[0]
    assert None in row["results"]
    assert TOKEN not in json.dumps(row)
    for rank in row["ranks"]:
        assert set(rank) == {"vector", "keyword"}
        assert all(v is None or (isinstance(v, int) and v >= 1) for v in rank.values())


def test_stats_reads_rows_with_and_without_ranks(index):
    """Older rows have no ranks; newer hybrid rows do. Every reader of the
    query log reads both."""
    _call(query="zanzibar")
    _call(query="zanzibar", mode="vector")

    async def call():
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool("griot_stats", {})

    result = asyncio.run(call())
    assert not result.is_error
    assert stats.main([]) in (0, None)


# --- the review offers rankings that disagree ------------------------------------------------------

CODE = {"repo": "r", "source_type": "code", "file_path": "src/lock.py"}
OTHER = {"repo": "r", "source_type": "code", "file_path": "src/other.py"}
THIRD = {"repo": "r", "source_type": "code", "file_path": "src/third.py"}

_clock = [datetime.now(timezone.utc) - timedelta(hours=5)]


def _log(question, *, ranks, mode="hybrid", results=(CODE, OTHER, THIRD), window=48, **extra):
    _clock[0] += timedelta(seconds=1)
    record = {
        "timestamp": _clock[0].isoformat(), "profile": "p", "collection": "c1", "project": "p1",
        "question": question, "via": "mcp", "limit": 8, "mode": mode, "num_sources": len(results),
        "sources": [common.source_label(r) for r in results], "results": [dict(r) for r in results],
        "top_score": 0.03, **extra,
    }
    if ranks is not None:
        record["ranks"] = [{"vector": v, "keyword": k} for v, k in ranks]
    if window is not None:
        record["rank_window"] = window
    logdb.write_query(common.LOG_DIR, record)


DISAGREE = [(1, None), (None, 1), (2, 3)]  # each ranking's first is not in the other's first 10


def _kinds(**kwargs):
    return [(c["kind"], c["query"]) for c in golden_set.review_candidates(limit=50, **kwargs)["candidates"]]


@pytest.fixture
def no_index(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the review touched the index")
    monkeypatch.setattr(common, "get_client", refuse)
    monkeypatch.setattr(common, "search", refuse)


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


def test_rankings_whose_firsts_are_far_apart_are_a_candidate(no_index):
    _log("where are imports handled", ranks=DISAGREE)

    assert _kinds() == [("disagree", "where are imports handled")]


@pytest.mark.parametrize("ranks", [
    [(1, 1), (2, 2), (3, 3)],              # the same first
    [(1, 4), (4, 1), (2, 2)],              # different firsts, each near the top of the other
    [(1, 3), (11, 1), (2, 2)],             # the keyword first is far in the vector ranking, but not the other way
    [(1, None), (3, 1), (2, 2)],           # the vector first is far in the keyword ranking, but not the other way
    [(1, 10), (None, 1), (2, 2)],          # 10 is still within the first 10
    [(1, None), (2, None), (3, None)],     # keyword matched nothing: no second opinion to disagree with
    [(2, None), (None, 1), (3, 3)],        # the vector ranking's first is not among the results read
])
def test_rankings_that_do_not_disagree_strongly_are_not_candidates(no_index, ranks):
    _log("where are imports handled", ranks=ranks)

    assert _kinds() == []


def test_a_rank_beyond_the_depth_counts_as_far(no_index):
    _log("where are imports handled", ranks=[(1, 11), (12, 1), (2, 2)])

    assert _kinds() == [("disagree", "where are imports handled")]


def test_null_counts_as_far_only_when_the_window_reached_the_depth(no_index):
    """Null means "not among the first `rank_window`": with a window under
    the depth, a null could still be within the first 10."""
    _log("narrow window", ranks=DISAGREE, window=6)
    _log("no window recorded", ranks=DISAGREE, window=None)
    _log("wide window", ranks=DISAGREE, window=10)

    assert _kinds() == [("disagree", "wide window")]


def test_only_hybrid_searches_are_candidates(no_index):
    _log("a vector one", ranks=DISAGREE, mode="vector")
    _log("a keyword one", ranks=DISAGREE, mode="keyword")

    assert _kinds() == []


def _r(v, k):
    return {"vector": v, "keyword": k}


@pytest.mark.parametrize("ranks", [
    "garbage", [["1", None]] * 3, [1, 2, 3], [None, None, None],
    [_r(1, None), _r(None, 1)],                       # one rank short of the results
    [_r(True, None), _r(None, 1), _r(2, 2)],          # a bool is not a rank
    [_r(1, None), _r(None, 1), _r(0, 2)],             # nor is 0
    [_r(1, None), _r(None, 1.0), _r(2, 2)],           # nor a float
    [_r(1, None), _r(None, "1"), _r(2, 2)],           # nor a string
    [_r(1, None), {"keyword": 1}, _r(2, 2)],          # a key missing
])
def test_ranks_that_cannot_be_read_make_no_candidate_and_break_nothing(no_index, ranks):
    _clock[0] += timedelta(seconds=1)
    logdb.write_query(common.LOG_DIR, {
        "timestamp": _clock[0].isoformat(), "collection": "c1", "question": "odd one", "mode": "hybrid",
        "sources": ["a", "b", "c"], "results": [CODE, OTHER, THIRD], "rank_window": 48, "ranks": ranks,
    })

    assert _kinds() == []


@pytest.mark.parametrize("window", ["48", True, 0, 48.0])
def test_a_window_that_cannot_be_read_counts_no_null_as_far(no_index, window):
    _log("odd window", ranks=DISAGREE, window=window)

    assert _kinds() == []


def test_older_rows_without_ranks_read_as_before(no_index):
    _log("asked before ranks", ranks=None, window=None)
    _log("asked before ranks", ranks=None, window=None)
    _log("once, before ranks", ranks=None, window=None)

    assert _kinds() == [("repeated", "asked before ranks")]


def test_disagreements_come_after_repeated_and_hard_ones_newest_first(no_index):
    for i in range(20):
        _log(f"filler question number {i}", ranks=None, mode="vector", top_score=round(0.50 + 0.02 * i, 2))
    _log("why does the shard refuse to open", ranks=None, mode="vector", top_score=0.40)
    _log("older disagreement", ranks=DISAGREE)
    _log("newer disagreement", ranks=DISAGREE)
    _log("how is the lock released", ranks=None)
    _log("how is the lock released", ranks=None)

    kinds = _kinds()

    assert kinds[0] == ("repeated", "how is the lock released")
    assert kinds[1] == ("hard", "why does the shard refuse to open")
    assert kinds[-2:] == [("disagree", "newer disagreement"), ("disagree", "older disagreement")]
    assert [k for k, _ in kinds] == sorted((k for k, _ in kinds), key=["repeated", "hard", "disagree"].index)


def test_a_disagreement_already_a_case_or_rejected_is_not_offered(no_index, terminal):
    golden_set.add_case("where are imports handled", [CODE], mode="hybrid")
    _log("where are imports handled", ranks=DISAGREE)
    _log("rejected one", ranks=DISAGREE)
    terminal.append("r")

    golden_set.cmd_review()

    assert _kinds() == []


def test_the_review_says_why_and_shows_each_rank(no_index, terminal, capsys):
    _log("where are imports handled", ranks=DISAGREE)
    terminal.append("s")

    golden_set.cmd_review()

    out = capsys.readouterr().out
    assert "disagree" in out.lower()
    assert "first 10" in out
    assert "[1] r/src/lock.py  (vector 1, keyword -)" in out
    assert "[2] r/src/other.py  (vector -, keyword 1)" in out


def test_a_pick_keeps_the_hybrid_mode(no_index, terminal):
    _log("where are imports handled", ranks=DISAGREE)
    terminal.append("2")

    assert golden_set.cmd_review() == 0

    cases = json.loads(common.GOLDEN_SET_PATH.read_text())
    assert cases == [{"query": "where are imports handled", "limit": 8, "must_include": [OTHER], "mode": "hybrid"}]


def test_a_narrowed_disagreement_is_shown_but_cannot_be_picked(no_index, terminal, capsys):
    _log("where are imports handled", ranks=DISAGREE, repos=["r"])
    terminal.extend(["1", "s"])

    golden_set.cmd_review()

    assert "cannot become a case" in capsys.readouterr().out.lower()
    assert not common.GOLDEN_SET_PATH.exists() or json.loads(common.GOLDEN_SET_PATH.read_text()) == []


def test_a_real_hybrid_search_becomes_a_disagreement_candidate(index, monkeypatch):
    """End to end: a search through the MCP client on a real index, its row in
    the real logs.db, and the review reading it without opening the index."""
    _call(query="zanzibar")
    common.release_client()
    monkeypatch.setattr(common, "get_client", lambda *a, **k: pytest.fail("the review touched the index"))

    assert _kinds() == [("disagree", "zanzibar")]


# --- the window every logging surface records reaches the depth ------------------------------------
#
# A null rank only says "not among the first rank_window", so the review can
# count it as past DISAGREE_DEPTH only when the window reached the depth (see
# golden_set._disagreement). Counting it against a narrower window would
# offer rankings that agree: a result at rank w+1..DISAGREE_DEPTH of the
# other ranking is one the depth calls near. The searches that log ranks are
# the ones made for a reader (griot ask, griot_search), and those fuse
# rankings wider than the list they return. These pin that every such
# search that can disagree at all, the smallest limit included, logs a
# window that reaches the depth, so a narrow window never hides one.


def _ask(monkeypatch, *argv):
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: "answer")
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.0)
    ask.main(["zanzibar", *argv])


@pytest.mark.parametrize("limit", [2, 3, 5, 20])
@pytest.mark.parametrize("group_by_document", [False, True])
def test_griot_search_logs_a_window_that_reaches_the_depth(index, limit, group_by_document):
    _call(query="zanzibar", limit=limit, group_by_document=group_by_document)

    assert _rows()[0]["rank_window"] >= golden_set.DISAGREE_DEPTH


@pytest.mark.parametrize("limit", [2, 3, 5, 20])
def test_griot_ask_logs_a_window_that_reaches_the_depth(index, monkeypatch, limit):
    _ask(monkeypatch, "--limit", str(limit))

    assert _rows()[0]["rank_window"] >= golden_set.DISAGREE_DEPTH


@pytest.mark.parametrize("limit", [2, 20])
def test_a_small_or_large_limit_search_through_the_mcp_client_is_offered(index, monkeypatch, limit):
    """End to end at the two ends: the smallest limit that returns two
    results to disagree about, and a large one, each logged by a real search
    to the real logs.db and offered by the review."""
    _call(query="zanzibar", limit=limit)
    common.release_client()
    monkeypatch.setattr(common, "get_client", lambda *a, **k: pytest.fail("the review touched the index"))

    assert _kinds() == [("disagree", "zanzibar")]


@pytest.mark.parametrize("limit", [2, 20])
def test_a_small_or_large_limit_ask_is_offered(index, monkeypatch, limit):
    _ask(monkeypatch, "--limit", str(limit))
    common.release_client()
    monkeypatch.setattr(common, "get_client", lambda *a, **k: pytest.fail("the review touched the index"))

    assert _kinds() == [("disagree", "zanzibar")]


def test_a_one_result_search_is_never_a_disagreement(index):
    """Limit 1 is the one search a reader makes whose window (6) is under
    the depth, and it cannot disagree whatever the window: two rankings
    disagree about two results, and it logs one."""
    _call(query="zanzibar", limit=1)

    row = _rows()[0]
    assert len(row["results"]) == 1
    assert _kinds() == []


@pytest.mark.parametrize("window", [2, 5, golden_set.DISAGREE_DEPTH - 1])
def test_a_window_under_the_depth_offers_nothing_even_when_every_other_rank_is_null(no_index, window):
    """With a window w under the depth a null is only known to be past w;
    the other ranking may have had it within the depth, where the two agree.
    So a narrow window is not compared against itself (min(depth, w))."""
    _log("narrow", ranks=DISAGREE, window=window)

    assert _kinds() == []
