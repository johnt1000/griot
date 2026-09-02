import json
import os

import psutil
import pytest
import qdrant_edge as qe

from griot import common, logdb


def _fake_embed(monkeypatch):
    def fake(texts, **kwargs):
        return [[0.1] * common.EMBED_DIM for _ in texts]
    monkeypatch.setattr(common, "embed_texts", fake)


def _docs(n: int) -> list[dict]:
    return [
        {"id": f"repo:code:file{i}.py:0", "content": "content", "metadata": {"source_type": "code", "repo": "repo", "file_path": f"file{i}.py", "chunk_index": 0}}
        for i in range(n)
    ]


def test_status_on_empty_collection_is_day_one_not_error():
    """Collection doesn't exist yet (fresh install) — points_count 0 and
    last_indexed None, without propagating a Qdrant exception (see the design notes)."""
    status = common.get_index_status()
    assert status["points_count"] == 0
    assert status["collection"] == common.COLLECTION_NAME
    assert status["embed_profile"] == common.ACTIVE_PROFILE_NAME
    assert status["last_indexed"] is None
    assert status["running"] is False
    assert status["pid"] is None
    assert status["path"] is None
    assert status["spend_ceiling_exceeded"] is False


def test_status_reports_points_count_and_last_indexed(monkeypatch):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))
    common.release_lock()  # atexit only releases at the end of the process

    common.log_run_summary(script="index_code.py", indexed=3, skipped=0, failed=0)

    status = common.get_index_status()
    assert status["points_count"] == 3
    assert status["last_indexed"]["script"] == "index_code.py"
    assert status["last_indexed"]["indexed"] == 3
    assert status["last_indexed"]["skipped"] == 0
    assert status["last_indexed"]["failed"] == 0
    assert "timestamp" in status["last_indexed"]


def test_status_last_indexed_picks_latest_entry_for_matching_collection():
    """logs/logs.db's `runs` table is insertion-ordered — the last write
    whose 'collection' matches the parameter is the most recent. Writes to
    other collections are ignored."""
    common.log_run_summary(script="index_code.py", indexed=1, skipped=0, failed=0)
    common.log_run_summary(collection="codebase__other-profile", script="index_code.py", indexed=99, skipped=0, failed=0)
    common.log_run_summary(script="index_commits.py", indexed=2, skipped=1, failed=0)
    common.log_run_summary(script="index_tags.py", indexed=5, skipped=0, failed=1)

    status = common.get_index_status()
    assert status["last_indexed"]["script"] == "index_tags.py"
    assert status["last_indexed"]["indexed"] == 5


def test_status_last_indexed_exposes_the_error_of_a_died_run():
    """[that decisionincident] A run that died is recorded with an `error`
    and no counts. get_index_status() must pass that through, or every
    consumer (griot_index_status) would render a failure as
    'None indexed, None skipped, None failed' — which reads like a
    successful no-op instead of a failure."""
    common.log_run_summary(script="index_code.py", indexed=None, skipped=None, failed=None,
                           error="RuntimeError: Kind(WouldBlock)")

    last = common.get_index_status()["last_indexed"]
    assert last["error"] == "RuntimeError: Kind(WouldBlock)"
    assert last["indexed"] is None


def test_status_last_indexed_has_no_error_for_a_successful_run():
    common.log_run_summary(script="index_code.py", indexed=1, skipped=0, failed=0)
    assert common.get_index_status()["last_indexed"]["error"] is None


def test_status_missing_runs_file_is_none():
    assert not (common.LOG_DIR / logdb.DB_FILENAME).exists()
    status = common.get_index_status()
    assert status["last_indexed"] is None


def test_status_running_true_with_valid_lock():
    """Lock written by a real, live process (the test process itself) —
    running should reflect that, with the correct pid/path (label)."""
    common.acquire_lock(label="/repos/meu-projeto")
    try:
        status = common.get_index_status()
        assert status["running"] is True
        assert status["pid"] == os.getpid()
        assert status["path"] == "/repos/meu-projeto"
    finally:
        common.release_lock()


def test_index_lock_status_matches_get_index_status_running_fields():
    """index_lock_status() is the lock-only extraction of
    get_index_status() — same running/pid/path, zero shard I/O."""
    common.acquire_lock(label="/repos/meu-projeto")
    try:
        lock_status = common.index_lock_status()
        full_status = common.get_index_status()
        assert lock_status == {"running": True, "pid": os.getpid(), "path": "/repos/meu-projeto"}
        assert lock_status["running"] == full_status["running"]
        assert lock_status["pid"] == full_status["pid"]
        assert lock_status["path"] == full_status["path"]
    finally:
        common.release_lock()


def test_index_lock_status_false_when_no_lock():
    assert common.index_lock_status() == {"running": False, "pid": None, "path": None}


def test_index_lock_status_never_touches_qdrant_at_all():
    """The whole point: a confirmation screen / status badge rendered on
    every page load must never open a shard just to check the lock —
    zero risk of the EdgeShard.load()-vs-collection-held-open warning
    get_index_status() documents for a collection currently being written."""
    assert not common.QDRANT_PATH.exists()
    common.index_lock_status()
    assert not common.QDRANT_PATH.exists()
    assert common._client is None


def test_status_running_false_with_orphan_lock_dead_pid():
    dead_pid = 2**31 - 1
    common.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.LOCK_PATH.write_text(json.dumps({"pid": dead_pid, "start_time": 0.0, "label": "/repos/orfao"}))

    status = common.get_index_status()
    assert status["running"] is False
    assert status["pid"] is None
    assert status["path"] is None


def test_status_running_false_with_recycled_pid_start_time_mismatch():
    """Live PID (ours), but start_time doesn't match the real create_time() —
    PID recycling, treated as orphaned (not 'still running')."""
    real_start_time = psutil.Process(os.getpid()).create_time()
    common.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "start_time": real_start_time - 999, "label": "/repos/reciclado"}))

    status = common.get_index_status()
    assert status["running"] is False
    assert status["pid"] is None
    assert status["path"] is None


def test_status_spend_ceiling_exceeded_reflects_circuit_breaker(monkeypatch):
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1.0)
    common.record_spend(1.5)
    status = common.get_index_status()
    assert status["spend_ceiling_exceeded"] is True


def test_status_other_collection_lock_error_returns_points_count_none_without_raising():
    """[review] EdgeShard.load() of a NON-active collection that another
    process has open at this exact moment raises a generic runtime exception
    ('failed to open WAL... WouldBlock', confirmed via introspection —
    not a specific lock-error type). get_index_status() needs to
    swallow this and return points_count=None best-effort, not propagate —
    otherwise it breaks the function's '100% read, zero cost' promise."""
    other_collection = "codebase__ocupada-por-outro-processo"
    other_path = common._collection_path(other_collection)
    other_path.mkdir(parents=True, exist_ok=True)
    cfg = qe.EdgeConfig(vectors={"dense": qe.EdgeVectorParams(size=common.EMBED_DIM, distance=qe.Distance.Cosine)})
    # simulates "another process" with the shard already open at the same path —
    # create() itself writes the marker and leaves the directory with the WAL lock held
    holder = qe.EdgeShard.create(str(other_path), cfg)
    try:
        status = common.get_index_status(collection=other_collection)
        assert status["points_count"] is None
        assert status["collection"] == other_collection
    finally:
        holder.close()


def test_status_active_collection_lock_error_returns_points_count_none_without_raising():
    """[real finding, 2026-08-20] The branch above (collection != active)
    already swallowed this error — but the branch for the ACTIVE collection
    (the common case, without passing `collection=`) called get_client() with
    no try/except at all and propagated the raw exception. Real scenario:
    griot_index_repo (MCP) spawns an indexing subprocess; if griot_index_status
    is called while that subprocess is still writing, the whole tool would break."""
    path = common._collection_path(common.COLLECTION_NAME)
    path.mkdir(parents=True, exist_ok=True)
    cfg = qe.EdgeConfig(vectors={"dense": qe.EdgeVectorParams(size=common.EMBED_DIM, distance=qe.Distance.Cosine)})
    holder = qe.EdgeShard.create(str(path), cfg)  # simulates another process with the handle open
    try:
        status = common.get_index_status()  # without collection= -> active collection path
        assert status["points_count"] is None
        assert status["collection"] == common.COLLECTION_NAME
    finally:
        holder.close()


def test_status_reuse_active_handle_false_never_creates_qdrant_path():
    """A long-lived read-only caller must
    never trigger get_client()'s side effect of CREATING the active
    collection on day 1 just by checking its status — reuse_active_handle=
    False routes the active collection through the same read-only
    EdgeShard.load()-or-"doesn't exist" path already used for non-active
    collections, instead of get_client() (which calls EdgeShard.create()
    when nothing exists yet, see common.py:919)."""
    assert not common.QDRANT_PATH.exists()
    status = common.get_index_status(reuse_active_handle=False)
    assert status["points_count"] == 0
    assert status["collection"] == common.COLLECTION_NAME
    assert not common.QDRANT_PATH.exists()


def test_status_reuse_active_handle_false_does_not_memoize_client(monkeypatch):
    """Must not leave common._client populated — the whole point
    is to never hold the collection open across calls (that's exactly what
    blocks a concurrent `griot index` subprocess, see release_client())."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(2))
    common.release_lock()
    assert common._client is not None  # index_documents() does memoize via get_client()
    common.release_client()
    assert common._client is None

    status = common.get_index_status(reuse_active_handle=False)
    assert status["points_count"] == 2
    assert common._client is None


def test_status_reuse_active_handle_default_true_matches_legacy_behavior():
    """Default (no kwarg passed) must be byte-for-byte the same as before —
    griot_index_status (MCP) and `griot stats` call this with no kwarg and
    must keep memoizing via get_client(), unchanged."""
    status_default = common.get_index_status()
    status_explicit_true = common.get_index_status(reuse_active_handle=True)
    assert status_default == status_explicit_true
    # get_client() was used (not the read-only load-or-0 path) — the
    # collection now exists on disk as a side effect, same as always.
    assert common.QDRANT_PATH.exists()


def test_collection_name_for_matches_the_active_collection_pattern():
    """common.collection_name_for() formalizes the f-string that
    used to only exist inline for COLLECTION_NAME (common.py) — the UI needs
    to generate the collection name for all 8 embedding profiles, not just
    the active one."""
    assert common.collection_name_for(common.ACTIVE_PROFILE_NAME) == common.COLLECTION_NAME
    assert common.collection_name_for("bge-small") == "codebase__bge-small"


def test_status_accepts_explicit_collection_param(monkeypatch):
    _fake_embed(monkeypatch)
    other_collection = "codebase__other-profile"
    # With Edge, each collection is a shard in its own directory — no need
    # to explicitly create anything here: a collection not yet existing on
    # disk already reports points_count 0 (same behavior as the old
    # client.collection_exists()). What this test covers is
    # get_index_status(collection=other_collection) reading the correct
    # last_indexed without confusing it with the active collection.
    common.log_run_summary(collection=other_collection, script="index_code.py", indexed=7, skipped=0, failed=0)

    status = common.get_index_status(collection=other_collection)
    assert status["collection"] == other_collection
    assert status["points_count"] == 0
    assert status["last_indexed"]["indexed"] == 7

    # the default collection remains empty/without an entry — confirms that
    # the 'collection' filter really isolates the two cases.
    default_status = common.get_index_status()
    assert default_status["collection"] == common.COLLECTION_NAME
    assert default_status["last_indexed"] is None


# --- collection_exists()/delete_collection() ("delete an index you no ------
# --- longer want", user-requested) -----------------------------------------
# griot had no way to reclaim disk space from a profile's collection except
# manually deleting the qdrant_data/<collection> directory by hand — these
# two functions are the shared primitive for `griot profiles delete` (CLI)
# and the Collections page's "Delete" button (UI).


def test_collection_exists_true_when_marker_present():
    collection = "codebase__some-profile"
    path = common._collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")

    assert common.collection_exists(collection) is True


def test_collection_exists_false_when_marker_absent():
    assert common.collection_exists("codebase__never-indexed") is False


def test_delete_collection_removes_the_directory():
    collection = "codebase__to-delete"
    path = common._collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    (path / "extra_file.bin").write_text("data")  # confirms rmtree of the whole dir, not just the marker

    common.delete_collection(collection)

    assert not path.exists()


def test_delete_collection_raises_when_collection_does_not_exist():
    with pytest.raises(ValueError, match="does not exist"):
        common.delete_collection("codebase__never-existed")


def test_delete_collection_refuses_while_an_indexing_run_holds_the_lock():
    """[review finding] The lock is process-wide, not per-collection (see
    acquire_lock()) — a run writing to a DIFFERENT collection still means
    THIS one could be next in the same run. rmtree()ing a collection an
    indexer still has open would race its WAL rather than raise cleanly
    (an unlinked-while-open file just silently vanishes on close — no
    crash, no clean error, worse than refusing up front)."""
    collection = "codebase__locked-target"
    path = common._collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    common.acquire_lock(label="some other source")
    try:
        with pytest.raises(ValueError, match="in progress"):
            common.delete_collection(collection)
        assert path.exists()  # never touched
    finally:
        common.release_lock()
