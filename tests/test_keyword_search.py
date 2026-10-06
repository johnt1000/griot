"""Keyword (BM25) search next to vector search.

Embeddings are weak at exact identifiers (a function name, an error code, a
commit hash). The store's own sparse vectors hold a BM25 representation of
every point, written next to the dense one, so a search can match the words
themselves: `mode="keyword"`, or `mode="hybrid"` for both rankings fused.

The dense vectors here are random per text, so whatever vector search ranks
first is luck; what keyword search ranks first is the point that holds the
word."""

import hashlib
import json
import os
import random
import shutil

import pytest
import qdrant_edge as qe
from mcp.client.client import Client

from griot import cli, common, doctor, logdb, mcp_server, stats


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


HASH = "f96634a1b2c3d4e5f60718293a4b5c6d7e8f9012"

DOCS = [
    _doc("alpha", "code", "lock.py:0", "def acquire_lock(label):\n    raise CollectionBusyError(label)",
         file_path="src/lock.py", chunk_index=0),
    _doc("alpha", "code", "net.py:0", "retry the request when the server answers ERR_CONNECTION_REFUSED",
         file_path="src/net.py", chunk_index=0),
    _doc("alpha", "commit", HASH, "docs: explain the retention of old logs", commit_hash=HASH,
         author="someone", date="2026-10-01"),
    _doc("beta", "code", "notes.md:0", "a few notes about how the team works together",
         file_path="docs/notes.md", chunk_index=0),
    _doc("beta", "code", "search.py:0", "rank the hits and keep the best of each document",
         file_path="src/zebra_quokka.py", chunk_index=0),
]


@pytest.fixture
def fake_embeddings(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])


@pytest.fixture
def index(fake_embeddings):
    common.index_documents(DOCS)
    common.release_lock()


def _active_path():
    return common._collection_path(common.COLLECTION_NAME)


def _legacy_collection():
    """A collection as every griot before keyword search made it: one dense
    vector per point, no sparse vectors configured."""
    path = _active_path()
    common.secure_mkdir(path)
    shard = qe.EdgeShard.create(str(path), qe.EdgeConfig(
        vectors={"dense": qe.EdgeVectorParams(size=common.EMBED_DIM, distance=qe.Distance.Cosine)}))
    shard.close()


@pytest.fixture
def legacy_index(fake_embeddings):
    _legacy_collection()
    common.index_documents(DOCS)
    common.release_lock()
    common.release_client()


def _first_label(query, **kwargs):
    hits = common.search(query, limit=3, **kwargs)
    assert hits, f"nothing found for {query!r}"
    return common.source_label(hits[0].payload)


# --- a new collection --------------------------------------------------------------------


def test_a_new_collection_is_made_with_keyword_vectors(index):
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_every_point_written_by_an_index_run_has_a_keyword_vector(index):
    client = common.get_client()
    without = client.count(qe.CountRequest(exact=True, filter=qe.Filter(
        must_not=[qe.HasVectorCondition(common.KEYWORD_VECTOR)])))
    assert without == 0
    assert client.info().points_count == len(DOCS)


@pytest.mark.parametrize("query, expected", [
    ("acquire_lock", "alpha/src/lock.py"),
    ("CollectionBusyError", "alpha/src/lock.py"),
    ("ERR_CONNECTION_REFUSED", "alpha/src/net.py"),
])
def test_keyword_search_finds_an_exact_identifier(index, query, expected):
    assert _first_label(query, mode="keyword") == expected


@pytest.mark.parametrize("query", [HASH, HASH[:7], HASH[:12]])
def test_keyword_search_finds_a_commit_by_its_hash(index, query):
    """A commit stores its message, not its hash: the hash comes from what
    is stored beside the text, whole or abbreviated as git abbreviates it."""
    assert _first_label(query, mode="keyword") == f"commit {HASH[:8]} — alpha"


def test_keyword_search_finds_a_file_by_its_name(index):
    """The path of a code chunk is not in its text either."""
    assert _first_label("zebra_quokka.py", mode="keyword") == "beta/src/zebra_quokka.py"


def test_keyword_search_embeds_nothing(index, monkeypatch):
    """No embedding call, so a keyword search costs nothing even on a paid
    profile."""
    def embedded(*args, **kwargs):
        raise AssertionError("a keyword search embedded its query")

    monkeypatch.setattr(common, "embed_texts", embedded)
    assert common.search("acquire_lock", mode="keyword")


def test_keyword_search_returns_only_what_holds_the_word(index):
    """Not a ranking of everything: a point without the word is not a match."""
    hits = common.search("acquire_lock", limit=10, mode="keyword")
    assert [common.source_label(h.payload) for h in hits] == ["alpha/src/lock.py"]


def test_hybrid_search_puts_the_keyword_match_among_the_vector_ones(index):
    hits = common.search("acquire_lock", limit=5, mode="hybrid")
    labels = [common.source_label(h.payload) for h in hits]
    assert labels[0] == "alpha/src/lock.py", "a point both rankings hold comes first"
    assert len(labels) == 5, "hybrid keeps what only the vector ranking found"


def test_hybrid_search_embeds_the_query_once(index, monkeypatch):
    calls = []
    real = common.embed_texts

    def counted(texts, **kwargs):
        calls.append(list(texts))
        return real(texts, **kwargs)

    monkeypatch.setattr(common, "embed_texts", counted)
    common.search("acquire_lock", mode="hybrid")
    assert calls == [["acquire_lock"]]


def test_the_default_mode_is_vector_and_unchanged(index):
    default = [(str(h.id), h.score) for h in common.search("acquire_lock", limit=5)]
    vector = [(str(h.id), h.score) for h in common.search("acquire_lock", limit=5, mode="vector")]
    assert default == vector
    assert len(default) == 5, "vector search ranks every point, whatever words it holds"


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_the_filters_apply_to_every_mode(index, mode):
    hits = common.search("acquire_lock retention notes", limit=10, mode=mode, repos=["beta"])
    assert {h.payload["repo"] for h in hits} == {"beta"}
    hits = common.search("acquire_lock retention", limit=10, mode=mode, source_types=["commit"])
    assert {h.payload["source_type"] for h in hits} == {"commit"}


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_grouping_and_diversity_apply_to_every_mode(index, mode):
    hits = common.search("acquire_lock", limit=5, mode=mode, diverse=True, group_by_document=True)
    keys = [common.document_key(h.payload) for h in hits]
    assert len(keys) == len(set(keys))
    assert all(isinstance(h, common.SearchHit) for h in hits)


@pytest.mark.parametrize("mode", ["fulltext", "", None, "VECTOR"])
def test_an_unknown_mode_is_refused(index, mode):
    with pytest.raises(common.SearchFilterError) as error:
        common.search("acquire_lock", mode=mode)
    for known in common.SEARCH_MODES:
        assert known in str(error.value)


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
@pytest.mark.parametrize("query", ["the and of", "  ", "?!"])
def test_a_query_with_no_word_to_match_is_an_error_not_an_empty_result(index, mode, query):
    with pytest.raises(common.SearchFilterError, match="no word"):
        common.search(query, mode=mode)


def test_an_unknown_mode_is_refused_before_the_collection_is_opened(monkeypatch):
    def opened(*args, **kwargs):
        raise AssertionError("the collection was opened for a mode that does not exist")

    monkeypatch.setattr(common, "get_client", opened)
    with pytest.raises(common.SearchFilterError):
        common.search("x", mode="fulltext")


def test_details_brought_up_to_date_reach_the_keyword_vector(index):
    """A tag whose text stayed the same can get a new commit hash: the
    details are rewritten without embedding anything, and the keyword vector
    has to follow them, or the new hash would find nothing."""
    tag = _doc("alpha", "tag", "v1", "release notes", tag_name="v1", commit_hash="a" * 40)
    common.index_documents([tag])
    common.release_lock()
    moved = _doc("alpha", "tag", "v1", "release notes", tag_name="v1", commit_hash="b" * 40)
    common.index_documents([moved])
    common.release_lock()
    assert _first_label("bbbbbbb", mode="keyword") == "tag v1 — alpha"
    assert common.search("aaaaaaa", mode="keyword") == []


def test_a_credential_in_a_stored_detail_is_not_made_findable_by_its_value(fake_embeddings):
    """The details join the keyword text through the same replacement as the
    text: a token-shaped branch name must not become a word a search finds."""
    token = "".join(("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"))
    common.index_documents([_doc("alpha", "branch", "b", "Branch: wip", branch_name=f"wip-{token}")])
    common.release_lock()
    assert common.search("wip", mode="keyword"), "the branch itself is indexed"
    assert common.search(token, mode="keyword") == []


def test_a_credential_stored_raw_by_an_older_version_is_not_made_findable_by_its_value(fake_embeddings):
    """An older griot stored some text raw, and it stays so until its
    repository is indexed again: the keyword vector is made from the text as
    it may leave griot (stored_text), not from the payload as stored. Today's
    index_documents() replaces it before storing, so the point is written to
    the store directly, as that older griot left it."""
    token = "".join(("ghp_", "Z9y8X7w6V5u4T3s2R1q0", "P9o8N7m6L5k4J3i2"))
    _legacy_collection()
    shard = qe.EdgeShard.load(str(_active_path()))
    shard.update(qe.UpdateOperation.upsert_points([qe.Point(
        id=common.stable_id("alpha:code:old.py:0"),
        vector={"dense": _vector("old", common.EMBED_DIM)},
        payload={"source_type": "code", "repo": "alpha", "file_path": "src/old.py", "chunk_index": 0,
                 "content": f"connect with {token} here"})]))
    shard.flush()
    shard.close()
    common.build_keyword_index()
    assert common.search("connect", mode="keyword"), "the chunk itself is indexed"
    assert common.search(token, mode="keyword") == []


# --- a collection made before keyword search ---------------------------------------------------


def test_a_legacy_collection_has_no_keyword_vectors(legacy_index):
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False


def test_a_collection_that_does_not_exist_is_neither(tmp_path):
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is None


def test_vector_search_still_works_on_a_legacy_collection(legacy_index):
    assert len(common.search("acquire_lock", limit=5)) == 5


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_keyword_modes_on_a_legacy_collection_say_what_to_run(legacy_index, mode):
    with pytest.raises(common.SearchModeUnavailable) as error:
        common.search("acquire_lock", mode=mode)
    message = str(error.value)
    assert "griot index keywords" in message
    assert common.COLLECTION_NAME in message
    assert isinstance(error.value, common.SearchFilterError), "callers that refuse a bad filter refuse this too"


def test_a_legacy_collection_is_refused_before_the_query_is_embedded(legacy_index, monkeypatch):
    def embedded(*args, **kwargs):
        raise AssertionError("the query was embedded for a search that could not run")

    monkeypatch.setattr(common, "embed_texts", embedded)
    with pytest.raises(common.SearchModeUnavailable):
        common.search("acquire_lock", mode="hybrid")


def test_an_index_run_into_a_legacy_collection_still_writes(legacy_index, fake_embeddings):
    """New points cannot carry a vector the collection has no room for; the
    run writes what it can, as before."""
    indexed, skipped, failed = common.index_documents(
        [_doc("alpha", "code", "new.py:0", "something new", file_path="new.py", chunk_index=0)])
    common.release_lock()
    assert (indexed, failed) == (1, 0)


# --- building the keyword vectors of an existing collection ------------------------------------


def _points(include_vectors=True):
    client = common.get_client()
    records, offset = [], None
    while True:
        page, offset = client.scroll(qe.ScrollRequest(offset=offset, limit=100, with_payload=True,
                                                      with_vector=["dense"] if include_vectors else False))
        records.extend(page)
        if offset is None:
            break
    return {str(r.id): (r.payload, r.vector["dense"] if include_vectors else None) for r in records}


def test_building_keeps_every_point_as_it_was_and_embeds_nothing(legacy_index, monkeypatch):
    before = _points()
    common.release_client()

    def embedded(*args, **kwargs):
        raise AssertionError("building keyword vectors embedded something")

    monkeypatch.setattr(common, "embed_texts", embedded)
    result = common.build_keyword_index()
    assert result == {"rebuilt": True, "written": len(DOCS), "points": len(DOCS), "kept": 0}
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True
    after = _points()
    assert after.keys() == before.keys()
    for point_id, (payload, dense) in before.items():
        assert after[point_id][0] == payload
        assert after[point_id][1] == pytest.approx(dense, abs=1e-6)
    assert _first_label("acquire_lock", mode="keyword") == "alpha/src/lock.py"


def test_building_leaves_nothing_beside_the_collection(legacy_index):
    common.release_client()
    common.build_keyword_index()
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_building_twice_does_nothing_the_second_time(legacy_index):
    common.release_client()
    common.build_keyword_index()
    common.release_client()
    inode = os.stat(_active_path()).st_ino
    again = common.build_keyword_index()
    assert again == {"rebuilt": False, "written": 0, "points": len(DOCS), "kept": 0}
    assert os.stat(_active_path()).st_ino == inode, "the collection was not rewritten"


def test_building_on_a_new_collection_fills_points_that_lack_a_keyword_vector(index):
    """A point written without one (by an older griot after the build, say)
    is filled in place: no rebuild."""
    client = common.get_client()
    point_id = common.stable_id("alpha:code:old.py:0")
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        point_id, {"dense": _vector("x", common.EMBED_DIM)},
        {"source_type": "code", "repo": "alpha", "file_path": "old.py", "chunk_index": 0,
         "content": "legacy_identifier_here", "content_hash": "x"})]))
    common.release_client()
    inode = os.stat(_active_path()).st_ino
    result = common.build_keyword_index()
    assert result == {"rebuilt": False, "written": 1, "points": len(DOCS) + 1, "kept": 0}
    assert os.stat(_active_path()).st_ino == inode
    assert _first_label("legacy_identifier_here", mode="keyword") == "alpha/old.py"


def test_building_when_nothing_is_indexed_creates_nothing():
    result = common.build_keyword_index()
    assert result == {"rebuilt": False, "written": 0, "points": 0, "kept": 0}
    assert not common.collection_exists(common.COLLECTION_NAME)


def test_building_holds_the_index_lock(legacy_index, monkeypatch):
    """No index run may write into the collection while it is copied: what it
    wrote would be lost with the old copy."""
    seen = []
    real = common._copy_into_keyword_collection

    def watched(*args, **kwargs):
        seen.append(common.index_lock_status()["running"])
        return real(*args, **kwargs)

    monkeypatch.setattr(common, "_copy_into_keyword_collection", watched)
    common.release_client()
    common.build_keyword_index()
    assert seen == [True]
    assert not common.LOCK_PATH.exists(), "released at the end"


def test_building_is_refused_while_an_index_run_holds_the_lock(legacy_index, monkeypatch):
    monkeypatch.setattr(common, "_read_lock", lambda: {"pid": 1, "start_time": 0.0, "label": None})
    monkeypatch.setattr(common, "_lock_owner_is_alive", lambda info: True)
    common.LOCK_PATH.write_text(json.dumps({"pid": 1, "start_time": 0.0, "label": None}))
    with pytest.raises(RuntimeError, match="already running"):
        common.build_keyword_index()
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False


def test_an_interrupted_copy_is_started_over(legacy_index, monkeypatch):
    common.release_client()
    real = common._copy_into_keyword_collection

    def dies(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(common, "_copy_into_keyword_collection", dies)
    with pytest.raises(KeyboardInterrupt):
        common.build_keyword_index()
    common.release_client()
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False, "the collection is untouched"
    assert len(common.search("acquire_lock", limit=10)) == len(DOCS)
    common.release_client()

    monkeypatch.setattr(common, "_copy_into_keyword_collection", real)
    result = common.build_keyword_index()
    assert result["rebuilt"] is True and result["written"] == len(DOCS)
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def _interrupted_copy(monkeypatch, points: int, batch: int = 2) -> list:
    """Runs a build that dies once `points` keyword points were made, as a
    process killed mid-copy would, with the copy written page by page
    (_KEYWORD_BATCH = `batch`). Returns the list every point made from then
    on is appended to, for counting what a later run copies."""
    monkeypatch.setattr(common, "_KEYWORD_BATCH", batch)
    real = common._point
    made, armed = [], [True]

    def point(point_id, dense, payload, *, keywords):
        if keywords:
            if armed[0] and len(made) == points:
                armed[0] = False
                raise KeyboardInterrupt
            made.append(str(point_id))
        return real(point_id, dense, payload, keywords=keywords)

    monkeypatch.setattr(common, "_point", point)
    common.release_client()
    with pytest.raises(KeyboardInterrupt):
        common.build_keyword_index()
    common.release_client()
    assert len(made) == points and not armed[0]
    made.clear()
    return made


def test_an_interrupted_copy_resumes_where_it_stopped(legacy_index, monkeypatch):
    """What was copied before the interruption is kept and not copied again:
    on a large collection, starting over costs as long as the run that was
    lost."""
    before = _points()
    made = _interrupted_copy(monkeypatch, 4)  # two full pages written, the third lost
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False, "the collection is untouched"

    result = common.build_keyword_index()
    assert result == {"rebuilt": True, "written": len(DOCS) - 4, "points": len(DOCS), "kept": 4}
    assert len(made) == len(DOCS) - 4
    common.release_client()
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True
    after = _points()
    assert after.keys() == before.keys()
    for point_id, (payload, dense) in before.items():
        assert after[point_id][0] == payload
        assert after[point_id][1] == pytest.approx(dense, abs=1e-6)
    assert _first_label("acquire_lock", mode="keyword") == "alpha/src/lock.py"
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_a_resumed_copy_follows_what_changed_in_the_collection_since(legacy_index, monkeypatch):
    """Between the interruption and the next run an index run may change,
    add or delete points. The copy kept from before must not bring back the
    old version of a point, keep a deleted one or miss a new one."""
    made = _interrupted_copy(monkeypatch, 4)
    copied = sorted(_points(include_vectors=False))[:4]  # scroll order is id order
    common.release_client()
    changed, deleted = copied[0], copied[1]
    client = common.get_client()
    old = client.retrieve([changed], True, ["dense"])[0]
    # Only the payload changes, as when an index run brings a point's stored
    # details up to date without re-embedding it.
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        changed, {"dense": old.vector["dense"]},
        {**old.payload, "content": "a brand new word: quetzalcoatl_marker", "content_hash": "changed"})]))
    client.update(qe.UpdateOperation.delete_points([deleted]))
    added = common.stable_id("gamma:code:added.py:0")
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        added, {"dense": _vector("added", common.EMBED_DIM)},
        {"source_type": "code", "repo": "gamma", "file_path": "added.py", "chunk_index": 0,
         "content": "freshly_added_identifier", "content_hash": "added"})]))
    common.release_client()
    before = _points()
    common.release_client()

    result = common.build_keyword_index()
    assert result["rebuilt"] is True and result["points"] == len(DOCS)
    assert changed in made and added in made
    assert len(made) < len(DOCS), "the unchanged part of the copy was kept"
    common.release_client()
    after = _points()
    assert after.keys() == before.keys()
    for point_id, (payload, dense) in before.items():
        assert after[point_id][0] == payload, point_id
        assert after[point_id][1] == pytest.approx(dense, abs=1e-6), point_id
    assert _first_label("quetzalcoatl_marker", mode="keyword") == common.source_label(after[changed][0])
    assert _first_label("freshly_added_identifier", mode="keyword") == "gamma/added.py"


def test_a_kept_copy_whose_vector_changed_alone_is_copied_again(legacy_index, monkeypatch):
    """The payload is not the whole point: a dense vector that differs from
    the collection's (a re-embedding under the same text) is copied again."""
    made = _interrupted_copy(monkeypatch, 2)
    first = sorted(_points(include_vectors=False))[0]
    common.release_client()
    client = common.get_client()
    payload = client.retrieve([first], True, False)[0].payload
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        first, {"dense": _vector("re-embedded", common.EMBED_DIM)}, payload)]))
    common.release_client()
    before = _points()
    common.release_client()
    common.build_keyword_index()
    assert first in made
    common.release_client()
    assert _points()[first][1] == pytest.approx(before[first][1], abs=1e-6)


def test_a_kept_copy_that_cannot_be_opened_is_started_over(legacy_index):
    common.release_client()
    staging = common._keyword_rebuild_paths(_active_path())[0]
    staging.mkdir()
    (staging / common._EDGE_CONFIG_MARKER).write_text("not a config")
    result = common.build_keyword_index()
    assert result == {"rebuilt": True, "written": len(DOCS), "points": len(DOCS), "kept": 0}
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_a_kept_copy_made_without_the_keyword_vector_is_started_over(legacy_index):
    """A leftover that is a shard but not one with room for the keyword
    vector can never become the collection: resuming into it would fail on
    every run."""
    common.release_client()
    staging = common._keyword_rebuild_paths(_active_path())[0]
    common.secure_mkdir(staging)
    qe.EdgeShard.create(str(staging), qe.EdgeConfig(
        vectors={"dense": qe.EdgeVectorParams(size=common.EMBED_DIM, distance=qe.Distance.Cosine)})).close()
    result = common.build_keyword_index()
    assert result == {"rebuilt": True, "written": len(DOCS), "points": len(DOCS), "kept": 0}
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_a_kept_copy_is_deleted_when_the_collection_needs_none(index):
    """Keyword vectors already built (by an index run into a new collection,
    say): a copy left from before is not the collection and is removed."""
    common.release_client()
    staging = common._keyword_rebuild_paths(_active_path())[0]
    common.secure_mkdir(staging)
    qe.EdgeShard.create(str(staging), common._collection_config()).close()
    assert common.build_keyword_index()["rebuilt"] is False
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_a_kept_copy_is_deleted_when_the_collection_is_gone():
    """The collection deleted after an interrupted build (griot profiles
    delete, say): the copy of it has nothing left to become."""
    common.QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    staging = common._keyword_rebuild_paths(_active_path())[0]
    common.secure_mkdir(staging)
    qe.EdgeShard.create(str(staging), common._collection_config()).close()
    assert common.build_keyword_index() == {"rebuilt": False, "written": 0, "points": 0, "kept": 0}
    assert not staging.exists()
    assert not common.collection_exists(common.COLLECTION_NAME)


def test_a_copy_that_came_out_short_never_takes_the_place(legacy_index, monkeypatch):
    """A scroll that stopped early (or points lost on the way) must leave the
    collection as it was, not replace it with less."""
    common.release_client()
    real_get_client = common.get_client

    class StopsEarly:
        def __init__(self, shard):
            self._shard = shard

        def scroll(self, request):
            page, _ = self._shard.scroll(qe.ScrollRequest(limit=2, with_payload=True, with_vector=["dense"]))
            return page, None

        def __getattr__(self, name):
            return getattr(self._shard, name)

    monkeypatch.setattr(common, "get_client", lambda **kw: StopsEarly(real_get_client(**kw)))
    with pytest.raises(RuntimeError, match="left as it was"):
        common.build_keyword_index()
    monkeypatch.setattr(common, "get_client", real_get_client)
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False
    assert len(common.search("acquire_lock", limit=10)) == len(DOCS)


def test_a_swap_interrupted_halfway_is_undone_on_the_next_open(legacy_index):
    """Between the two renames the collection's directory is missing. Opening
    it then must bring the old copy back, never create an empty collection
    in its place."""
    common.release_client()
    path = _active_path()
    os.rename(path, common._keyword_rebuild_paths(path)[1])
    hits = common.search("acquire_lock", limit=10)
    assert len(hits) == len(DOCS)
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False


def test_a_build_after_a_swap_interrupted_halfway_puts_the_collection_back_first(legacy_index):
    """The build deletes what it finds beside the collection; the collection
    itself, left aside by the interrupted swap, must be put back before that,
    or the build would delete everything that was indexed."""
    common.release_client()
    path = _active_path()
    os.rename(path, common._keyword_rebuild_paths(path)[1])
    result = common.build_keyword_index()
    assert result == {"rebuilt": True, "written": len(DOCS), "points": len(DOCS), "kept": 0}
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_another_process_opening_the_collection_mid_swap_leaves_it_whole(legacy_index, monkeypatch):
    """Multi mode: another session may open the collection between the two
    renames, and its open puts the old copy back. The build must then give
    up with the old collection in place, never lose it or merge the two."""
    common.release_client()
    path = _active_path()
    real_rename = os.rename
    calls = []

    def rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            # What get_client() in the other process does on its way in.
            common._restore_interrupted_keyword_swap(path)
        return real_rename(src, dst)

    monkeypatch.setattr(common.os, "rename", rename)
    with pytest.raises(RuntimeError, match="left as it was"):
        common.build_keyword_index()
    monkeypatch.setattr(common.os, "rename", real_rename)
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False
    assert len(common.search("acquire_lock", limit=10)) == len(DOCS)
    common.release_client()
    assert common.build_keyword_index()["rebuilt"] is True
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_a_swap_interrupted_after_the_new_copy_is_in_place_is_finished(legacy_index):
    common.release_client()
    common.build_keyword_index()
    common.release_client()
    path = _active_path()
    staging, previous = common._keyword_rebuild_paths(path)
    previous.mkdir()
    (previous / "leftover").write_text("x")
    staging.mkdir()
    result = common.build_keyword_index()
    assert result["rebuilt"] is False
    common.release_client()
    assert sorted(os.listdir(common.QDRANT_PATH)) == [common.COLLECTION_NAME]


def test_a_collection_in_place_beside_an_old_whole_copy_is_opened_as_it_is(legacy_index):
    """A build that died after its second rename and before deleting the old
    copy leaves a whole collection on each side. The one in place is the new
    one: opening it must neither fail on it nor put the old one back over it
    (the next build deletes the old copy)."""
    common.release_client()
    common.build_keyword_index()
    common.release_client()
    path = _active_path()
    _, previous = common._keyword_rebuild_paths(path)
    shutil.copytree(path, previous)

    assert len(common.search("acquire_lock", limit=10)) == len(DOCS)
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_a_swap_whose_second_rename_fails_puts_the_collection_back_at_once(legacy_index, monkeypatch):
    """Undone there and then, not left for the next open to find: until the
    next open the collection's place would be empty, and anything that looks
    at the directory (status, doctor, a backup) would see nothing indexed."""
    common.release_client()
    path = _active_path()
    real_rename = os.rename
    calls = []

    def rename(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError("cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(common.os, "rename", rename)
    with pytest.raises(RuntimeError, match="left as it was"):
        common.build_keyword_index()
    monkeypatch.setattr(common.os, "rename", real_rename)
    assert (path / common._EDGE_CONFIG_MARKER).exists()
    assert not common._keyword_rebuild_paths(path)[1].exists()
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is False


def test_the_rebuilt_collection_is_closed_to_other_users(legacy_index):
    common.release_client()
    common.build_keyword_index()
    common.release_client()
    for root, dirs, files in os.walk(_active_path()):
        assert os.stat(root).st_mode & 0o077 == 0, root
        for f in files:
            assert os.stat(os.path.join(root, f)).st_mode & 0o077 == 0, f


# --- the command --------------------------------------------------------------------------------


def test_the_command_builds_and_says_so(legacy_index, capsys):
    common.release_client()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"{len(DOCS)} points copied into a collection with keyword vectors" in out
    assert "embedded nothing" in out
    # A fresh copy continued nothing, so it must not claim it did.
    assert "esum" not in out and "already" not in out
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_the_command_says_it_resumed_an_interrupted_copy_and_how_much_was_there(legacy_index, monkeypatch,
                                                                                 capsys):
    """After a resume, the points written in this run alone ('1 point
    copied') read as if the copy had covered one point of the collection:
    the message says it continued an earlier copy and what that copy held."""
    _interrupted_copy(monkeypatch, len(DOCS) - 1)
    capsys.readouterr()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert (f"Resumed an interrupted copy of collection '{common.COLLECTION_NAME}': {len(DOCS) - 1} points "
            f"were already copied, 1 point copied now (embedded nothing)") in out
    assert "1 points" not in out
    assert common.has_keyword_vectors(common.COLLECTION_NAME) is True


def test_the_command_says_a_resumed_copy_that_held_one_point(legacy_index, monkeypatch, capsys):
    _interrupted_copy(monkeypatch, 1, batch=1)
    capsys.readouterr()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"1 point was already copied, {len(DOCS) - 1} points copied now" in out


def test_a_copy_interrupted_before_it_held_anything_reads_as_a_fresh_one(legacy_index, monkeypatch, capsys):
    """Interrupted inside its first page, the copy left holds no point: the
    run that follows copies everything, and saying it resumed would claim
    work that was never done."""
    _interrupted_copy(monkeypatch, 1)
    capsys.readouterr()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"{len(DOCS)} points copied into a collection with keyword vectors" in out
    assert "esum" not in out


def test_the_command_says_when_there_is_nothing_to_do(index, capsys):
    common.release_client()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"All {len(DOCS)} points of collection '{common.COLLECTION_NAME}' already have keyword vectors." in out
    assert "esum" not in out


def test_the_command_counts_a_single_point_in_the_singular(fake_embeddings, capsys):
    """'1 points' on a one-point collection, both when nothing is to do and
    when the one point is filled in place."""
    common.index_documents(DOCS[:1])
    common.release_lock()
    common.release_client()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"The 1 point of collection '{common.COLLECTION_NAME}' already has keyword vectors." in out

    client = common.get_client()
    point = client.retrieve([common.stable_id(DOCS[0]["id"])], True, ["dense"])[0]
    client.update(qe.UpdateOperation.upsert_points([qe.Point(point.id, {"dense": point.vector["dense"]},
                                                             point.payload)]))
    common.release_client()
    assert cli.main(["index", "keywords"]) == 0
    out = capsys.readouterr().out
    assert f"1 point given keyword vectors in collection '{common.COLLECTION_NAME}'" in out


def test_the_command_says_why_it_could_not_build_without_a_traceback(legacy_index, monkeypatch, capsys):
    """An index run holding the lock is a routine reason to stop, and the
    message names it: a traceback around it would read as a crash."""
    monkeypatch.setattr(common, "_read_lock", lambda: {"pid": 1, "start_time": 0.0, "label": None})
    monkeypatch.setattr(common, "_lock_owner_is_alive", lambda info: True)
    common.LOCK_PATH.write_text(json.dumps({"pid": 1, "start_time": 0.0, "label": None}))
    assert cli.main(["index", "keywords"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Error: ") and "already running" in err


def test_the_command_is_not_part_of_index_all():
    assert "keywords" not in cli.INDEX_SOURCES


def test_the_command_does_not_record_an_indexing_run(legacy_index):
    common.release_client()
    before = logdb.read_recent(common.LOG_DIR, "runs", limit=100)
    assert cli.main(["index", "keywords"]) == 0
    assert logdb.read_recent(common.LOG_DIR, "runs", limit=100) == before


# --- the other surfaces -------------------------------------------------------------------------


def test_cli_search_takes_a_mode(index, capsys):
    assert cli.main(["search", "acquire_lock", "--mode", "keyword"]) == 0
    out = capsys.readouterr().out
    assert "alpha/src/lock.py" in out
    assert "src/net.py" not in out


def test_cli_search_explains_a_legacy_collection(legacy_index, capsys):
    assert cli.main(["search", "acquire_lock", "--mode", "hybrid"]) == 2
    assert "griot index keywords" in capsys.readouterr().err


def test_cli_search_refuses_an_unknown_mode(capsys):
    with pytest.raises(SystemExit):
        cli.main(["search", "x", "--mode", "fulltext"])


def test_ask_takes_a_mode(index, monkeypatch, capsys):
    from griot import ask
    prompts = []
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: prompts.append(prompt) or "answer")
    ask.main(["acquire_lock", "--mode", "keyword", "--limit", "5"])
    assert "acquire_lock" in prompts[0] and "ERR_CONNECTION_REFUSED" not in prompts[0]
    assert logdb.read_latest(common.LOG_DIR, "queries")["mode"] == "keyword"


def test_ask_explains_a_legacy_collection_and_pays_for_nothing(legacy_index, monkeypatch, capsys):
    from griot import ask
    prompts = []
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: prompts.append(prompt) or "answer")
    assert ask.main(["acquire_lock", "--mode", "keyword"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: ") and "griot index keywords" in err
    assert prompts == []


def test_index_status_says_whether_keyword_search_is_built(legacy_index):
    assert common.get_index_status()["keyword_search"] is False
    common.release_client()
    common.build_keyword_index()
    assert common.get_index_status()["keyword_search"] is True


@pytest.mark.parametrize("content", ["{not json", "[]"])
def test_index_status_of_a_collection_whose_config_cannot_be_read_cannot_say(index, content):
    """A status read answers whatever the config file holds: unreadable is
    "cannot say" (None), never an exception that takes the status down."""
    common.release_client()
    (_active_path() / common._EDGE_CONFIG_MARKER).write_text(content)
    assert common._keyword_search_status(common.COLLECTION_NAME) is None


def test_index_status_of_a_collection_that_does_not_exist():
    assert common.get_index_status(reuse_active_handle=False)["keyword_search"] is None


def test_doctor_says_how_to_build_keyword_search(legacy_index):
    common.release_client()
    check = doctor.check_index(common)
    assert check["status"] == doctor.OK
    assert "keyword" in check["detail"]
    assert "griot index keywords" in (check["fix"] or "")


def test_doctor_does_not_send_anyone_to_build_what_it_could_not_read(monkeypatch):
    """None is "could not say": telling a person to rebuild on that would
    rewrite a collection that may be fine."""
    monkeypatch.setattr(common, "get_index_status", lambda **kw: {
        "points_count": 5, "collection": "c", "keyword_search": None, "last_indexed": None})
    check = doctor.check_index(common)
    assert "keyword" not in check["detail"] and check["fix"] is None


def test_doctor_is_quiet_about_keywords_once_built(index):
    common.release_client()
    check = doctor.check_index(common)
    assert "keyword" not in check["detail"] and check["fix"] is None


# --- stats -------------------------------------------------------------------------------------


def test_stats_counts_queries_by_mode_and_takes_the_median_score_of_vector_ones_only():
    """A BM25 score and a fused rank score are on other scales than a cosine
    similarity: folded into one median they would make it say nothing."""
    queries = [
        {"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 1, "sources": [], "top_score": 0.6},
        {"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 1, "sources": [], "top_score": 0.8,
         "mode": "vector"},
        {"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 1, "sources": [], "top_score": 9.5,
         "mode": "keyword"},
        {"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 1, "sources": [], "top_score": 9.0,
         "mode": "hybrid"},
    ]
    result = stats.compute_stats([], queries, {"points_count": 1, "embed_profile": "jina-code"})
    assert result["median_top_score"] == 0.7
    assert result["queries_by_mode"] == {"vector": 2, "keyword": 1, "hybrid": 1}


def test_stats_shows_the_modes_only_when_more_than_vector_was_used():
    base = {"points_count": 1, "embed_profile": "jina-code"}
    only_vector = [{"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 0, "sources": []}]
    text = stats.format_stats(stats.compute_stats([], only_vector, base), 7)
    assert "by mode" not in text
    mixed = only_vector + [{"timestamp": "2026-10-01T10:00:00+00:00", "num_sources": 0, "sources": [],
                            "mode": "keyword"}]
    text = stats.format_stats(stats.compute_stats([], mixed, base), 7)
    line = next(line for line in text.splitlines() if "by mode: " in line)
    assert "vector 1" in line and "keyword 1" in line


# --- through the protocol ----------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["vector", "keyword", "hybrid"])
async def test_the_tool_searches_in_each_mode(index, mode):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 5, "mode": mode})
    assert result.is_error is False
    labels = [r["source_label"] for r in result.structured_content["results"]]
    assert labels, mode
    if mode != "vector":
        assert labels[0] == "alpha/src/lock.py"
    if mode == "keyword":
        assert labels == ["alpha/src/lock.py"]


@pytest.mark.anyio
async def test_the_tool_defaults_to_vector(index):
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["griot_search"].input_schema["properties"]["mode"]
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "limit": 5})
    assert schema.get("default") == "vector"
    assert set(schema.get("enum") or []) == set(common.SEARCH_MODES)
    assert len(result.structured_content["results"]) == 5


@pytest.mark.anyio
async def test_the_tool_logs_the_mode(index):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "hybrid"})
        await client.call_tool("griot_search", {"query": "acquire_lock"})
    modes = [q.get("mode") for q in logdb.read_recent(common.LOG_DIR, "queries", limit=2)]
    assert modes == ["vector", "hybrid"], "newest first"


@pytest.mark.anyio
async def test_the_tool_explains_a_legacy_collection(legacy_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "keyword"})
    assert result.is_error is True
    assert "griot index keywords" in result.content[0].text


@pytest.mark.anyio
async def test_the_tool_refuses_an_unknown_mode(index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "fulltext"})
    assert result.is_error is True


def test_the_cli_list_of_modes_is_the_search_list():
    """cli.py keeps a copy so that building the parser does not import the index."""
    assert cli.SEARCH_MODES == common.SEARCH_MODES


@pytest.mark.anyio
async def test_every_mode_the_server_instructions_name_is_one_the_tool_takes():
    import re
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        named = set(re.findall(r"`mode=([a-z]+)`", client.instructions))
    assert named == {"keyword"}
    assert named <= set(tools["griot_search"].input_schema["properties"]["mode"]["enum"])


@pytest.mark.anyio
async def test_the_history_prompt_names_a_mode_the_tool_takes():
    import re
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        prompt = await client.get_prompt("history", {"question": "why"})
    text = prompt.messages[0].content.text
    named = set(re.findall(r'mode="([a-z]+)"', text))
    assert named == {"keyword"}
    assert named <= set(tools["griot_search"].input_schema["properties"]["mode"]["enum"])


@pytest.mark.anyio
async def test_stats_through_the_protocol_carries_the_modes(index):
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "keyword"})
        result = await client.call_tool("griot_stats", {})
    assert result.is_error is False
    assert result.structured_content["queries_by_mode"] == {"keyword": 1}


@pytest.mark.anyio
async def test_index_status_through_the_protocol_carries_keyword_search(index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {})
    assert result.is_error is False
    assert result.structured_content["keyword_search"] is True
