"""A golden case is met when the document it names comes back as a copy.

The golden set searches the way readers do (diverse): the same text in two
places comes back once, the best-placed copy naming the others in
`also_in`. A case written before that, naming the copy that now ranks
lower, failed although the reader got that very text. Such a case passes,
and says which result carried it (`met_by_copy`), so a pass by a copy is
never mistaken for the document itself coming back."""

import hashlib
import json
import random
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import common, golden_set, mcp_server, quality_check

QUERY = "how is the cache invalidated"
SHARED = "the cache is invalidated when the config hash changes"


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
    return {"id": f"{repo}:code:{path}:0", "content": text,
            "metadata": {"source_type": "code", "repo": repo, "file_path": path, "chunk_index": 0}}


DOCS = [
    _doc("alpha", "cache.py", SHARED),
    _doc("beta", "vendor/cache.py", SHARED),
    _doc("gamma", "noise.py", "unrelated text about logging"),
]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(DOCS)
    common.release_lock()


def _shown_and_folded():
    """(the entry of the copy a reader is shown, the entry of the one folded
    into it), read from the search itself: two copies of one text score the
    same, and which one the store ranks first is not this test's to decide."""
    hits = common.search(QUERY, limit=5, mode="vector", diverse=True)
    first = hits[0]
    assert first.payload["content"] == SHARED and len(first.also_in) == 1, [h.payload for h in hits]
    folded = next(d for d in DOCS if d["metadata"]["file_path"] != first.payload["file_path"]
                  and d["content"] == SHARED)
    return common.case_entry(first.payload), common.case_entry(folded["metadata"])


def test_the_premise_one_copy_is_folded_into_the_other(index):
    shown, folded = _shown_and_folded()
    hits = common.search(QUERY, limit=5, mode="vector", diverse=True)
    assert not any(quality_check._matches(h.payload, folded) for h in hits)


def test_a_case_naming_the_folded_copy_passes_and_says_which_result_carried_it(index):
    shown, folded = _shown_and_folded()
    case = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": [folded]}])["cases"][0]
    assert case["passed"] is True and case["missing"] == [], case
    [met] = case["met_by_copy"]
    assert met["expected"] == folded
    assert met["result"] == 1
    assert met["result_source"] == common.source_label(shown)
    assert met["copy"] == common.source_label(folded)


def test_a_case_met_by_the_result_itself_is_not_met_by_a_copy(index):
    shown, folded = _shown_and_folded()
    case = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": [shown]}])["cases"][0]
    assert case["passed"] is True and case["met_by_copy"] == []


def test_both_copies_named_only_the_folded_one_is_met_by_a_copy(index):
    shown, folded = _shown_and_folded()
    case = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": [shown, folded]}])["cases"][0]
    assert case["passed"] is True
    assert [m["expected"] for m in case["met_by_copy"]] == [folded]


def test_a_document_that_is_no_copy_still_fails(index):
    absent = {"source_type": "code", "repo": "beta", "file_path": "elsewhere.py"}
    case = quality_check.run_golden_set([{"query": QUERY, "limit": 5, "must_include": [absent]}])["cases"][0]
    assert case["passed"] is False and case["missing"] == [absent] and case["met_by_copy"] == []


def test_a_case_that_was_not_searched_has_no_copy_either(index):
    case = quality_check.run_golden_set([{"query": QUERY, "must_include": [
        {"source_type": "code", "repo": "nowhere", "file_path": "x.py"}]}])["cases"][0]
    assert case["met_by_copy"] == []


def test_the_terminal_report_says_the_case_was_met_by_a_copy(index, capsys):
    shown, folded = _shown_and_folded()
    golden_set.add_case(QUERY, [folded])
    quality_check.main(["--sample-size", "2"])
    out = capsys.readouterr().out
    assert "[OK]" in out
    line = next(line for line in out.splitlines() if "copy" in line and "result 1" in line)
    assert common.source_label(folded) in line and common.source_label(shown) in line


def test_the_json_report_carries_it(index, capsys):
    shown, folded = _shown_and_folded()
    golden_set.add_case(QUERY, [folded])
    quality_check.main(["--sample-size", "2", "--json"])
    case = json.loads(capsys.readouterr().out)["golden_check"]["cases"][0]
    assert case["passed"] is True and case["met_by_copy"][0]["expected"] == folded


@pytest.mark.anyio
async def test_the_tool_says_so_through_the_protocol(index):
    shown, folded = _shown_and_folded()
    golden_set.add_case(QUERY, [folded])
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_quality_check", {"sample_size": 2})).structured_content
    case = out["golden_check"]["cases"][0]
    assert case["passed"] is True
    assert case["met_by_copy"] == [{"expected": folded, "result": 1, "result_source": common.source_label(shown),
                                    "copy": common.source_label(folded)}]


def test_the_stale_wording_is_gone():
    """The check is no longer a plain or ungrouped search: it searches each
    case in its own mode, the way readers search."""
    note = golden_set._unlike_note(["grouped by document"])
    doc = golden_set._unlike_the_check.__doc__
    for text in (note, doc):
        assert "ungrouped" not in text and "plain vector" not in text
        assert "readers" in text


def _paragraphs_about_the_golden_set(path: str) -> list[str]:
    text = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
    return [p for p in text.split("\n\n") if "golden" in p.lower()]


@pytest.mark.parametrize("path", ["README.md", "docs/indexing-model.md"])
def test_the_docs_do_not_describe_the_check_as_a_raw_search(path):
    """The README and the indexing model still called the check an
    ungrouped search, or said the golden set does not get the arrangement
    readers get, after it had started searching the way readers do."""
    paragraphs = _paragraphs_about_the_golden_set(path)
    assert paragraphs
    for p in paragraphs:
        assert "ungrouped" not in p and "point by point" not in p, p


def test_the_readme_says_a_copy_meets_a_case():
    """Where the README describes the check, it says copies are folded and a
    case met by one passes and says so."""
    [p] = [p for p in _paragraphs_about_the_golden_set("README.md") if p.startswith("`griot quality-check`")]
    assert "also_in" in p and "met_by_copy" in p, p
