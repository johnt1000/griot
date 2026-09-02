"""Tests for search's optional one-result-per-document mode.

[user-requested, after measuring] The motivating observation was wrong at
first: a search returning the same file several times looked like waste,
but the repeated hits carry DIFFERENT chunk_index values — 1500 chars each,
overlapping by only the chunker's 200. They are the document contributing
at several points, not one point repeated.

What the measurement did show is concentration: on a focused query, 8 slots
held 4 documents, and grouping surfaced 4 more (including a release and a
commit) at a slightly LOWER score than the eighth ungrouped hit. That is a
depth-versus-breadth trade, not a fix — which is why this is opt-in and the
default keeps today's behaviour.
"""

import pytest

from griot import common


class FakeHit:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


def _code(score, path, chunk, repo="r"):
    return FakeHit(score, {"repo": repo, "source_type": "code",
                           "file_path": path, "chunk_index": chunk})


def test_document_key_distinguishes_files_not_chunks():
    a = common.document_key({"repo": "r", "source_type": "code", "file_path": "a.py", "chunk_index": 0})
    b = common.document_key({"repo": "r", "source_type": "code", "file_path": "a.py", "chunk_index": 7})

    assert a == b, "chunks of one file are one document"


def test_document_key_separates_repos_with_the_same_path():
    a = common.document_key({"repo": "one", "source_type": "code", "file_path": "README.md"})
    b = common.document_key({"repo": "two", "source_type": "code", "file_path": "README.md"})

    assert a != b


@pytest.mark.parametrize("source_type,field,value", [
    ("commit", "commit_hash", "abc123"),
    ("tag", "tag_name", "v1.0.0"),
    ("branch", "branch_name", "main"),
    ("merge_request", "mr_iid", 42),
    ("release", "tag_name", "v2.0.0"),
    ("issue", "issue_iid", 7),
])
def test_document_key_covers_every_indexed_source_type(source_type, field, value):
    """All seven types the indexers write. A type whose identifier is not
    recognised would collapse every item of that type into one document and
    silently drop the rest of them."""
    one = common.document_key({"repo": "r", "source_type": source_type, field: value})
    other = common.document_key({"repo": "r", "source_type": source_type, field: "different"})

    assert one != other, f"{source_type} items are not distinguished by {field}"


def test_search_returns_every_chunk_by_default(monkeypatch):
    """The default must not change: today's behaviour favours depth, and for
    "how does this work" — the common question — depth is what serves."""
    hits = [_code(0.9, "spec.md", 4), _code(0.8, "spec.md", 0), _code(0.7, "other.md", 0)]
    monkeypatch.setattr(common, "embed_texts", lambda texts: [[0.0]])
    monkeypatch.setattr(common, "get_client", lambda: type("C", (), {"query": lambda self, req: hits})())

    result = common.search("q", limit=3)

    assert len(result) == 3
    assert [h.payload["chunk_index"] for h in result] == [4, 0, 0]


def test_search_grouped_keeps_the_best_chunk_per_document(monkeypatch):
    hits = [_code(0.9, "spec.md", 4), _code(0.8, "spec.md", 0),
            _code(0.7, "other.md", 0), _code(0.6, "third.md", 2)]
    monkeypatch.setattr(common, "embed_texts", lambda texts: [[0.0]])
    monkeypatch.setattr(common, "get_client", lambda: type("C", (), {"query": lambda self, req: hits})())

    result = common.search("q", limit=3, group_by_document=True)

    assert [h.payload["file_path"] for h in result] == ["spec.md", "other.md", "third.md"]
    # The surviving chunk is the highest-scoring one, not the first indexed.
    assert result[0].payload["chunk_index"] == 4


def test_search_grouped_overfetches_so_limit_still_means_limit(monkeypatch):
    """[design decision] With grouping, `limit` counts DOCUMENTS, so asking
    the store for exactly `limit` points would return fewer after collapsing.
    The store is queried for more and the result is cut — measured as free:
    limit=32 was no slower than limit=8, since the query embedding is the
    only real cost and the vector search is in-process."""
    asked = {}

    def _query(self, req):
        asked["limit"] = req.limit
        return [_code(1.0 - i / 100, f"f{i // 3}.md", i % 3) for i in range(req.limit)]

    monkeypatch.setattr(common, "embed_texts", lambda texts: [[0.0]])
    monkeypatch.setattr(common, "get_client", lambda: type("C", (), {"query": _query})())

    result = common.search("q", limit=5, group_by_document=True)

    assert asked["limit"] > 5, "must over-fetch to fill `limit` documents"
    assert len(result) == 5


def test_search_grouped_returns_what_exists_when_documents_run_out(monkeypatch):
    """Fewer documents than requested is a legitimate answer, not a reason to
    pad the result with more chunks of the same file."""
    hits = [_code(0.9, "only.md", i) for i in range(10)]
    monkeypatch.setattr(common, "embed_texts", lambda texts: [[0.0]])
    monkeypatch.setattr(common, "get_client", lambda: type("C", (), {"query": lambda self, req: hits})())

    result = common.search("q", limit=5, group_by_document=True)

    assert len(result) == 1
