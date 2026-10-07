"""The golden set is searched the way readers search.

Every surface a person or an agent reads results from (griot_search,
`griot search`, `griot ask`) asks common.search() for diverse results: at
most SEARCH_MAX_CHUNKS_PER_DOCUMENT chunks of one document, copies folded.
The golden set's cases are made from those lists (griot_golden_set_add after
a griot_search, `griot golden-set review` from the logged results), but the
check ran a raw search, where one long document could take every slot and
push out the document the case was made from. The case then failed with
retrieval no worse than when it was curated. The self-check is not touched:
it asks whether one exact point comes back, which a cap per document would
hide."""

import hashlib
import json
import math
import random

import pytest
from mcp.client.client import Client

from griot import common, golden_set, logdb, mcp_server, quality_check

QUERY = "where is the retry policy decided"
BIG = [f"big module chunk {i} about retries" for i in range(5)]
TARGET = "the retry policy lives here"


def _unit(dim: int, axis: int) -> list[float]:
    v = [0.0] * dim
    v[axis] = 1.0
    return v


def _vector(text: str, dim: int) -> list[float]:
    """The query is axis 0. Each chunk of the long document is a hair away
    from it (cosine ~0.995), the target a little further (~0.96), anything
    else is noise: the raw top 5 is the long document alone."""
    if text == QUERY:
        return _unit(dim, 0)
    if text in BIG:
        v = _unit(dim, 0)
        v[1 + BIG.index(text)] = 0.1
        return v
    if text == TARGET:
        v = _unit(dim, 0)
        v[10] = 0.3
        return v
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    v = [rng.uniform(-1, 1) for _ in range(dim)]
    v[0] = 0.0
    return v


def _doc(repo: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:code:{key}", "content": text,
            "metadata": {"source_type": "code", "repo": repo, **meta}}


DOCS = [
    *[_doc("alpha", f"big.py:{i}", text, file_path="big.py", chunk_index=i) for i, text in enumerate(BIG)],
    _doc("alpha", "target.py:0", TARGET, file_path="target.py", chunk_index=0),
    _doc("beta", "other.py:0", "unrelated text about logging", file_path="other.py", chunk_index=0),
]
EXPECTED = [{"repo": "alpha", "source_type": "code", "file_path": "target.py"}]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(DOCS)
    common.release_lock()


def test_the_index_is_crowded_the_way_this_file_says(index):
    """The premise: a raw search's top 5 is the long document alone, so a
    test below that passes is not passing for another reason."""
    raw = common.search(QUERY, limit=5, mode="vector")
    assert {h.payload["file_path"] for h in raw} == {"big.py"}
    assert any(h.payload["file_path"] == "target.py"
               for h in common.search(QUERY, limit=5, mode="vector", diverse=True))


def test_a_case_a_reader_saw_is_not_pushed_out_by_another_documents_chunks(index):
    result = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": EXPECTED}])
    case = result["cases"][0]
    assert case["passed"] is True, case
    # What the case is shown with is what was searched: one document at most
    # three times, not five chunks of it.
    assert len(case["top_results"]) == 5


def test_the_run_says_how_it_searched(index):
    result = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": EXPECTED}])
    assert result["diverse"] is True
    # Also when nothing was searched: the field describes the run.
    assert quality_check.run_golden_set([])["diverse"] is True


def test_the_terminal_report_says_so_and_the_record_keeps_it(index, capsys):
    golden_set.add_case(QUERY, EXPECTED)
    quality_check.main(["--sample-size", "2"])
    out = capsys.readouterr().out
    assert f"at most {common.SEARCH_MAX_CHUNKS_PER_DOCUMENT} chunks per document" in out
    assert "[OK]" in out
    # A run recorded before this change has no `diverse`: it was searched
    # raw, and the two are not the same ruler.
    recorded = logdb.read_recent(common.LOG_DIR, "quality_checks", limit=1)[0]
    assert recorded["golden_check"]["diverse"] is True


def test_the_json_report_carries_it(index, capsys):
    golden_set.add_case(QUERY, EXPECTED)
    quality_check.main(["--sample-size", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["golden_check"]["diverse"] is True and payload["golden_check"]["passed"] == 1


@pytest.mark.anyio
async def test_the_tool_runs_it_the_same_way_and_says_so(index):
    golden_set.add_case(QUERY, EXPECTED)
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_quality_check", {"sample_size": 2})).structured_content
    golden = out["golden_check"]
    assert golden["passed"] == 1 and golden["diverse"] is True


def test_the_self_check_still_measures_the_exact_point(index, monkeypatch):
    """A cap per document would hide a sampled chunk behind its siblings:
    the self-check keeps the raw search."""
    asked = []
    real = common.search

    def search(query, limit=5, **kw):
        asked.append(kw.get("diverse", False))
        return real(query, limit=limit, **kw)

    monkeypatch.setattr(common, "search", search)
    quality_check.run_self_check(common.COLLECTION_NAME, sample_size=3)
    assert asked and not any(asked)


def test_a_case_is_curated_from_the_list_it_will_be_held_to(index, monkeypatch, capsys):
    """`griot golden-set add` shows the results the check will search for,
    so the document a person picks is one the check can find."""
    shown = iter(["", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(shown))
    golden_set.cmd_add(QUERY, limit=5)
    out = capsys.readouterr().out
    assert "target.py" in out
    assert out.count("big.py") == common.SEARCH_MAX_CHUNKS_PER_DOCUMENT


def test_the_premise_holds_on_scores():
    """The geometry above, checked once: chunks of the long document score
    above the target, so only a cap per document lets the target in."""
    dim = 16
    q = _vector(QUERY, dim)

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    assert min(cos(q, _vector(t, dim)) for t in BIG) > cos(q, _vector(TARGET, dim)) > 0.9
