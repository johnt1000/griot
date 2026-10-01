import json
import os
import subprocess
import sys

import pytest
import qdrant_edge as qe

from griot import common, logdb


def _spy_optimize(monkeypatch) -> list:
    """qe.EdgeShard is a pyo3 object — monkeypatch.setattr can't be used on an
    INSTANCE (shard.optimize = ...) because the attribute is read-only
    ('builtins.EdgeShard object attribute is read-only', confirmed via
    introspection). Swap the method on the CLASS instead; monkeypatch
    restores it on its own at the end of the test."""
    calls = []
    monkeypatch.setattr(qe.EdgeShard, "optimize", lambda self: calls.append(1))
    return calls


def _count() -> int:
    """common.get_client() now returns a qe.EdgeShard (Qdrant Edge engine,
    no longer an embedded QdrantClient) — shard.count() takes a CountRequest
    and returns the int directly (no more nested .count from the old
    CountResult)."""
    return common.get_client().count(qe.CountRequest())


def _docs(n: int, content: str = "original content") -> list[dict]:
    return [
        {"id": f"repo:code:file{i}.py:0", "content": content, "metadata": {"source_type": "code", "repo": "repo", "file_path": f"file{i}.py", "chunk_index": 0}}
        for i in range(n)
    ]


def _fake_embed(monkeypatch):
    def fake(texts, **kwargs):
        return [[0.1] * common.EMBED_DIM for _ in texts]
    monkeypatch.setattr(common, "embed_texts", fake)


def test_count_pending_all_new_on_empty_collection(monkeypatch):
    _fake_embed(monkeypatch)
    docs = _docs(5)
    pending, up_to_date = common.count_pending(docs)
    assert (pending, up_to_date) == (5, 0)


def test_index_documents_indexes_all_new_docs(monkeypatch):
    _fake_embed(monkeypatch)
    docs = _docs(5)
    indexed, skipped, failed = common.index_documents(docs)
    assert (indexed, skipped, failed) == (5, 0, 0)
    assert _count() == 5


def test_reindexing_unchanged_docs_is_fully_skipped(monkeypatch):
    _fake_embed(monkeypatch)
    docs = _docs(5)
    common.index_documents(docs)

    indexed, skipped, failed = common.index_documents(docs)
    assert (indexed, skipped, failed) == (0, 5, 0)
    assert _count() == 5  # no duplication


def test_changed_content_triggers_reembed_and_overwrites_not_duplicates(monkeypatch):
    _fake_embed(monkeypatch)
    docs = _docs(3)
    common.index_documents(docs)

    docs[0]["content"] = "content changed"
    indexed, skipped, failed = common.index_documents(docs)
    assert (indexed, skipped, failed) == (1, 2, 0)
    assert _count() == 3  # point overwritten, not duplicated


def test_count_pending_never_calls_embed_or_writes(monkeypatch):
    calls = []

    def fake(texts, **kwargs):
        calls.append(texts)
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(common, "embed_texts", fake)
    common.count_pending(_docs(5))
    assert calls == []  # --dry-run: zero cost, never calls embed_texts
    assert _count() == 0


def test_fully_failed_batch_is_counted_and_not_written(monkeypatch):
    def fake(texts, **kwargs):
        return [None] * len(texts)
    monkeypatch.setattr(common, "embed_texts", fake)
    monkeypatch.setattr(common, "MAX_CONSECUTIVE_FAILED_BATCHES", 100)  # don't trip the breaker in this test

    indexed, skipped, failed = common.index_documents(_docs(3))
    assert (indexed, skipped, failed) == (0, 0, 3)
    assert _count() == 0


def test_consecutive_failures_trip_the_breaker(monkeypatch):
    def fake(texts, **kwargs):
        return [None] * len(texts)
    monkeypatch.setattr(common, "embed_texts", fake)
    monkeypatch.setattr(common, "INDEX_BATCH_SIZE", 1)  # 1 doc per batch -> 1 failure per batch
    monkeypatch.setattr(common, "MAX_CONSECUTIVE_FAILED_BATCHES", 3)

    with pytest.raises(RuntimeError, match="consecutive batches failed"):
        common.index_documents(_docs(10))


def test_partial_batch_failure_resets_consecutive_counter(monkeypatch):
    """A batch that fails entirely adds to the counter; a batch that passes
    resets it — the breaker should only trip on CONSECUTIVE failures, not on
    failures accumulated over the whole run's total."""
    call_count = {"n": 0}

    def fake(texts, **kwargs):
        call_count["n"] += 1
        # fails only on odd batches -> never 2 consecutive failures
        if call_count["n"] % 2 == 1:
            return [None] * len(texts)
        return [[0.1] * common.EMBED_DIM for _ in texts]

    monkeypatch.setattr(common, "embed_texts", fake)
    monkeypatch.setattr(common, "INDEX_BATCH_SIZE", 1)
    monkeypatch.setattr(common, "MAX_CONSECUTIVE_FAILED_BATCHES", 2)

    # should not raise, even with several failures in total, because never 2 in a row
    indexed, skipped, failed = common.index_documents(_docs(6))
    assert indexed + failed == 6


def test_index_documents_calls_optimize_when_indexed_gt_zero(monkeypatch):
    """[review] shard.optimize() needs to run at the end of a run that
    actually indexed something — otherwise the HNSW index is never (re)built
    and searches on large corpora stay permanently on brute force."""
    _fake_embed(monkeypatch)
    calls = _spy_optimize(monkeypatch)

    common.index_documents(_docs(3))

    assert calls == [1]


def test_index_documents_does_not_call_optimize_when_nothing_indexed(monkeypatch):
    """Entire run skipped (everything already up to date) or fully failed:
    nothing new to optimize, not worth the cost (~8.8s measured for 30k
    points)."""
    def fake(texts, **kwargs):
        return [None] * len(texts)
    monkeypatch.setattr(common, "embed_texts", fake)
    monkeypatch.setattr(common, "MAX_CONSECUTIVE_FAILED_BATCHES", 100)  # don't trip the breaker in this test
    calls = _spy_optimize(monkeypatch)

    common.index_documents(_docs(3))

    assert calls == []


def test_index_documents_holds_lock_during_run_and_releases_when_done(monkeypatch):
    """index_documents() needs to hold the lock DURING the run (so
    griot_index_status reports running=True) but release it upon returning,
    not only via atexit — see test_index_documents_two_calls_in_same_process_both_succeed
    for the real bug the previous version (atexit-only) caused in `griot
    index all` (5 calls to index_documents() in the SAME process, one per
    source)."""
    seen_during_run = {}

    def fake_embed(texts, **kwargs):
        seen_during_run["lock_exists"] = common.LOCK_PATH.exists()
        if seen_during_run["lock_exists"]:
            seen_during_run["pid"] = json.loads(common.LOCK_PATH.read_text())["pid"]
        return [[0.1] * common.EMBED_DIM for _ in texts]

    monkeypatch.setattr(common, "embed_texts", fake_embed)
    assert not common.LOCK_PATH.exists()

    common.index_documents(_docs(1))

    assert seen_during_run["lock_exists"] is True
    assert seen_during_run["pid"] == os.getpid()
    assert not common.LOCK_PATH.exists()  # released upon returning, not only at atexit


def test_index_documents_two_calls_in_same_process_both_succeed(monkeypatch):
    """Reproduces `griot index all`: several sources calling index_documents()
    in sequence, in the SAME process — without this, the 2nd call would fail
    with "A griot is already running (PID <itself>)" because the 1st call's
    lock was only released at the whole process's atexit, never between
    calls. Real bug, reproduced manually before this fix."""
    _fake_embed(monkeypatch)
    docs2 = [
        {"id": f"repo:commit:{i}", "content": "lote dois", "metadata": {"source_type": "commit", "repo": "repo", "commit_hash": str(i)}}
        for i in range(2)
    ]

    indexed1, _, _ = common.index_documents(_docs(3, content="lote um"))
    indexed2, _, _ = common.index_documents(docs2)

    assert (indexed1, indexed2) == (3, 2)
    assert _count() == 5


# --- release_client() (real finding, 2026-08-20) ----------------------------
# griot mcp is a long-lived process that memoizes get_client() on the first
# read (griot_search/griot_index_status/griot_quality_check) and holds the
# collection open for the rest of the server's life — permanently blocking
# any indexing subprocess later spawned via griot_index_repo (same
# directory, Qdrant Edge's process-level mutual exclusion). Confirmed
# empirically: a separate subprocess trying to open the same collection
# while another process already holds it open dies with "failed to open WAL
# ... WouldBlock". release_client() is what griot_index_repo calls before
# spawning the subprocess, to release the server's handle.


def test_release_client_noop_when_never_opened():
    assert common._client is None
    common.release_client()  # must not raise
    assert common._client is None


def test_release_client_closes_and_clears_memoized_client():
    client = common.get_client()
    assert common._client is client

    common.release_client()

    assert common._client is None


def test_get_client_reopens_after_release():
    first = common.get_client()
    common.release_client()
    second = common.get_client()

    assert second is not None
    assert second is not first  # actually reopened, did not reuse the closed handle


def test_release_then_reopen_allows_a_second_process_style_open():
    """Proves the real effect: after release_client(), a NEW handle on the
    same path (simulating the indexing subprocess) can open without
    colliding with what the 'server' had open before."""
    common.get_client()  # 'server' opens the collection
    common.release_client()  # releases before 'spawning the subprocess'

    path = common._collection_path(common.COLLECTION_NAME)
    other_handle = qe.EdgeShard.load(str(path))  # 'subprocess' opens successfully
    try:
        assert other_handle is not None
    finally:
        other_handle.close()


# --- concurrency mode (user decision, 2026-08-20) ----------------------------
# 'single' (opt-in): behaves exactly as before, no change at all. 'multi' (default)
# (GRIOT_MCP_CONCURRENCY_MODE=multi): the handle is let go after
# IDLE_RELEASE_SECONDS of no use, by mcp_server's reaper (tested in
# test_mcp_server.py), and reopened with retry-with-backoff. get_client() does
# not close it itself: see tests/test_client_release_race.py.


def test_default_concurrency_mode_is_multi():
    # A fresh interpreter, so a GRIOT_MCP_CONCURRENCY_MODE exported in the
    # developer's shell cannot turn this into a test of their environment.
    env = {k: v for k, v in os.environ.items() if k != "GRIOT_MCP_CONCURRENCY_MODE"}
    out = subprocess.run([sys.executable, "-c", "from griot import common; print(common.CONCURRENCY_MODE)"],
                         env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "multi"


def test_single_mode_never_retries_on_load_failure(monkeypatch):
    """In single mode, get_client() needs to keep behaving EXACTLY as
    before: a single attempt, a raw exception, zero sleep — no
    behavior/latency regression for those who opted into single mode."""
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    common.get_client()
    common.release_client()  # leaves the marker on disk, but closes the handle

    calls = {"n": 0}

    def fake_load(path):
        calls["n"] += 1
        raise RuntimeError("simulado")

    monkeypatch.setattr(qe.EdgeShard, "load", fake_load)
    slept = []
    monkeypatch.setattr(common.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="simulado"):
        common.get_client()

    assert calls["n"] == 1
    assert slept == []


def test_single_mode_never_releases_on_idle(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    first = common.get_client()
    now = common._client_last_used_at  # None in single mode — but the client stays memoized regardless
    monkeypatch.setattr(common.time, "time", lambda: 999999999.0)  # "much later"

    second = common.get_client()

    assert second is first  # never reopened, single mode has no notion of idleness


def test_multi_mode_retries_transient_lock_then_succeeds(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    common.get_client()
    common.release_client()  # marker on disk, handle closed

    real_load = qe.EdgeShard.load
    calls = {"n": 0}

    def flaky_load(path):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("failed to open WAL: Kind(WouldBlock)")
        return real_load(path)

    monkeypatch.setattr(qe.EdgeShard, "load", flaky_load)
    slept = []
    monkeypatch.setattr(common.time, "sleep", lambda s: slept.append(s))

    client = common.get_client()

    assert client is not None
    assert calls["n"] == 3
    assert slept == [common._LOCK_RETRY_DELAYS[0], common._LOCK_RETRY_DELAYS[1]]


def test_multi_mode_raises_clear_error_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    common.get_client()
    common.release_client()

    def always_fails(path):
        raise RuntimeError("failed to open WAL: Kind(WouldBlock), always")

    monkeypatch.setattr(qe.EdgeShard, "load", always_fails)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="Could not open"):
        common.get_client()


def test_multi_mode_does_not_reopen_the_client_on_its_own_after_the_idle_window(monkeypatch):
    """It used to: get_client() closed and reopened the handle once the
    window had passed, which closed it under any call still using it. The
    reaper, which counts calls in flight, is what lets go now."""
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    monkeypatch.setattr(common, "IDLE_RELEASE_SECONDS", 30.0)

    t = {"now": 1000.0}
    monkeypatch.setattr(common.time, "time", lambda: t["now"])

    first = common.get_client()
    t["now"] += 31.0  # past the idleness ceiling

    second = common.get_client()

    assert second is first


def test_multi_mode_reuses_client_within_idle_window(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    monkeypatch.setattr(common, "IDLE_RELEASE_SECONDS", 30.0)

    t = {"now": 1000.0}
    monkeypatch.setattr(common.time, "time", lambda: t["now"])

    first = common.get_client()
    t["now"] += 5.0  # well within the window

    second = common.get_client()

    assert second is first  # reused it, didn't pay the cost of reopening


# --- _secure_collection_dir() (security review finding) ---------------------
# Qdrant Edge's own Rust engine writes its files (WAL, segments, payload
# storage) with the process umask, not through griot's secure_* helpers —
# a real gap: those files can end up group/world-readable even though the
# outer collection directory is 0700. Harmless only while the outer
# directories stay 0700 (traversal permission required at every level);
# fragile against a backup/restore/sync tool that doesn't preserve modes.


def test_secure_collection_dir_fixes_loose_permissions(tmp_path, monkeypatch):
    collection = "codebase__test-perms"
    path = common._collection_path(collection)
    path.mkdir(parents=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    subdir = path / "segments"
    subdir.mkdir()
    loose_file = subdir / "page_0.dat"
    loose_file.write_text("payload data")
    os.chmod(subdir, 0o755)
    os.chmod(loose_file, 0o644)

    common._secure_collection_dir(collection)

    assert (subdir.stat().st_mode & 0o777) == 0o700
    assert (loose_file.stat().st_mode & 0o777) == 0o600


def test_secure_collection_dir_never_crashes_on_missing_collection():
    common._secure_collection_dir("codebase__never-existed")  # no-op, must not raise


def test_secure_collection_dir_skips_chmod_when_mode_already_correct(tmp_path, monkeypatch):
    """Redundant chmod syscalls on every cold open (including every
    multi-mode idle-release reopen) are a real, unoptimized cost on large
    collections — skip the write when the mode is already right, but keep
    visiting every entry (a new file from another process must still be
    checked)."""
    collection = "codebase__test-perms-skip"
    path = common._collection_path(collection)
    path.mkdir(parents=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    subdir = path / "segments"
    subdir.mkdir()
    correct_file = subdir / "correct.dat"
    correct_file.write_text("x")
    wrong_file = subdir / "wrong.dat"
    wrong_file.write_text("y")
    os.chmod(subdir, 0o700)
    os.chmod(correct_file, 0o600)
    os.chmod(wrong_file, 0o644)

    calls = []
    real_chmod = os.chmod

    def recording_chmod(target, mode, *args, **kwargs):
        calls.append(str(target))
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)

    common._secure_collection_dir(collection)

    assert str(subdir) not in calls
    assert str(correct_file) not in calls
    assert str(wrong_file) in calls
    assert (wrong_file.stat().st_mode & 0o777) == 0o600


def test_secure_collection_dir_logs_warning_on_chmod_failure(tmp_path, monkeypatch):
    """A chmod failure must never be raised out of this function (it must
    never fail a real indexing run or search over a permission race), but
    silently swallowing it left no trace anywhere — it must be observable
    in griot.log without touching stdout, since this path also runs from
    the MCP server where stdout is the JSON-RPC transport."""
    collection = "codebase__test-perms-fail"
    path = common._collection_path(collection)
    path.mkdir(parents=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    subdir = path / "segments"
    subdir.mkdir()
    bad_file = subdir / "bad.dat"
    bad_file.write_text("x")
    good_file = subdir / "good.dat"
    good_file.write_text("y")
    os.chmod(subdir, 0o755)
    os.chmod(bad_file, 0o644)
    os.chmod(good_file, 0o644)

    real_chmod = os.chmod

    def failing_chmod(target, mode, *args, **kwargs):
        if str(target) == str(bad_file):
            raise OSError("simulated failure")
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", failing_chmod)

    logged = []

    def fake_log_and_print(msg, level="info", echo=True):
        logged.append((msg, level, echo))

    monkeypatch.setattr(common, "log_and_print", fake_log_and_print)

    common._secure_collection_dir(collection)  # must not raise

    assert len(logged) == 1
    msg, level, echo = logged[0]
    assert str(bad_file) in msg
    assert level == "warning"
    assert echo is False
    assert (good_file.stat().st_mode & 0o777) == 0o600  # sibling still repaired
    assert (bad_file.stat().st_mode & 0o777) == 0o644  # unchanged, repair failed


# --- which documents failed, not just how many -----------------------------
# [user-requested] `failed=50` is a number, not a diagnosis: learning that
# those 50 were oversized commit bodies required grepping griot.log and
# correlating by hand. The identity and the reason are what turn a count
# into something actionable — especially during a validation run.


def _embed_failing(monkeypatch, fail_indexes):
    """Embeds normally except for the given positions, which come back None
    — exactly how embed_texts() reports a per-item failure today."""
    def fake(texts, **kwargs):
        return [None if i in fail_indexes else [0.1] * common.EMBED_DIM
                for i in range(len(texts))]
    monkeypatch.setattr(common, "embed_texts", fake)


def test_index_documents_reports_which_documents_failed(monkeypatch):
    _embed_failing(monkeypatch, {1})

    indexed, skipped, failed = common.index_documents(_docs(3))

    assert failed == 1
    assert common.last_run_failures() == [{"id": "repo:code:file1.py:0", "reason": "embedding returned no vector"}]


def test_failures_are_cleared_between_runs(monkeypatch):
    _embed_failing(monkeypatch, {0})
    common.index_documents(_docs(2))
    common.release_lock()

    _fake_embed(monkeypatch)
    common.index_documents(_docs(2, content="changed"))

    assert common.last_run_failures() == []


def test_a_run_summary_carries_the_failed_document_ids(monkeypatch):
    """The whole point: the record that survives the process must name what
    failed, so `griot stats` (or a human, days later) can act on it."""
    _embed_failing(monkeypatch, {0, 2})
    common.index_documents(_docs(3))
    common.release_lock()

    common.log_run_summary(script="index_code.py", indexed=1, skipped=0, failed=2,
                           failures=common.last_run_failures())

    record = logdb.read_since(common.LOG_DIR, "runs", days=1)[0]
    assert [f["id"] for f in record["failures"]] == ["repo:code:file0.py:0", "repo:code:file2.py:0"]


def test_failure_detail_is_capped(monkeypatch):
    """A systemic failure (bad credential, API down) fails every document —
    storing thousands of ids would bloat a summary record for no extra
    insight, since they all share one reason."""
    _embed_failing(monkeypatch, set(range(60)))

    common.index_documents(_docs(60))

    failures = common.last_run_failures()
    assert len(failures) == common.MAX_RECORDED_FAILURES


def test_get_client_repairs_permissions_on_creation(monkeypatch):
    """[security review, lower-priority gap] index_documents() already
    calls _secure_collection_dir() in its own finally block, but
    get_client()'s OTHER callers (search(), count_pending(),
    get_index_status()) also reach EdgeShard.create() on a first call and
    were not getting the same repair — a read-only session that never
    calls index_documents() could sit on a collection with loose Edge
    file permissions indefinitely. get_client() must repair the files it
    just created itself, not just chmod the outer directory."""
    common.get_client()

    path = common._collection_path(common.COLLECTION_NAME)
    for root, dirs, files in os.walk(path):
        assert (os.stat(root).st_mode & 0o777) == 0o700
        for f in files:
            assert (os.stat(os.path.join(root, f)).st_mode & 0o777) == 0o600


def test_get_client_repairs_permissions_on_reload(monkeypatch):
    """[review follow-up] The creation-branch test above only exercises
    EdgeShard.create() — get_client()'s OTHER branch, EdgeShard.load() of
    an already-existing collection (reached after release_client(), e.g.
    idle-release in 'multi' concurrency mode), must get the same repair.
    A collection whose files were loosened after the first open (e.g. by
    an external backup/restore tool — see docs/lessons-and-debts.md) stays
    loose forever if only the create-branch is repaired."""
    common.get_client()
    common.release_client()

    path = common._collection_path(common.COLLECTION_NAME)
    for root, _dirs, files in os.walk(path):
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    common.get_client()

    for root, dirs, files in os.walk(path):
        assert (os.stat(root).st_mode & 0o777) == 0o700
        for f in files:
            assert (os.stat(os.path.join(root, f)).st_mode & 0o777) == 0o600


def test_index_documents_repairs_permissions_after_writing(monkeypatch):
    """Integration-level: index_documents() must call the repair itself —
    a caller shouldn't have to remember to do it separately after every
    real indexing run."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))

    path = common._collection_path(common.COLLECTION_NAME)
    for root, dirs, files in os.walk(path):
        assert (os.stat(root).st_mode & 0o777) == 0o700
        for f in files:
            assert (os.stat(os.path.join(root, f)).st_mode & 0o777) == 0o600


# --- a held collection is a typed condition ----------------------------------


def _held_load(monkeypatch, exc):
    common.get_client()
    common.release_client()  # marker on disk, so the next get_client() LOADS

    def load(path):
        raise exc

    monkeypatch.setattr(qe.EdgeShard, "load", load)


def test_single_mode_turns_the_engines_lock_error_into_collection_busy(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    _held_load(monkeypatch, RuntimeError("Service runtime error: failed to open WAL: Kind(WouldBlock)"))

    with pytest.raises(common.CollectionBusyError) as info:
        common.get_client()

    assert info.value.collection == common.COLLECTION_NAME
    assert isinstance(info.value.__cause__, RuntimeError)


def test_single_mode_leaves_any_other_load_failure_untouched(monkeypatch):
    # Telling someone "another process has it" about a corrupt shard would send them the wrong way.
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    _held_load(monkeypatch, ValueError("segment header is corrupt"))

    with pytest.raises(ValueError, match="corrupt"):
        common.get_client()


def test_a_status_read_reports_a_held_collection_as_collection_busy_without_waiting(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    _held_load(monkeypatch, RuntimeError("failed to open WAL: Kind(WouldBlock)"))
    slept = []
    monkeypatch.setattr(common.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(common.CollectionBusyError):
        common.get_client(wait=False)

    assert slept == []


def test_multi_mode_exhausting_its_retries_raises_collection_busy(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    _held_load(monkeypatch, RuntimeError("failed to open WAL: Kind(WouldBlock)"))
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    with pytest.raises(common.CollectionBusyError, match="Could not open collection") as info:
        common.get_client()

    assert isinstance(info.value, RuntimeError)  # callers that caught RuntimeError still do


# --- who holds it ------------------------------------------------------------


def _fake_lsof(monkeypatch, stdout="", exc=None):
    def run(cmd, **kwargs):
        assert cmd[0] == "lsof" and kwargs.get("timeout")  # a hung lsof must never hang griot
        if exc:
            raise exc
        return type("R", (), {"stdout": stdout, "returncode": 0})()

    monkeypatch.setattr(common.subprocess, "run", run)


def _sleeper():
    import subprocess
    import sys

    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_holders_exclude_this_process_and_describe_the_others(monkeypatch, tmp_path):
    other = _sleeper()
    try:
        _fake_lsof(monkeypatch, f"{os.getpid()}\n{other.pid}\n")

        holders = common.find_collection_holders(tmp_path)

        assert [h["pid"] for h in holders] == [other.pid]
        assert holders[0]["started"] is not None
    finally:
        other.kill()
        other.wait()


def test_holders_show_a_non_griot_processs_name_but_never_its_arguments(monkeypatch, tmp_path):
    # An unrelated process (a backup tool with a password on its command line) must not be echoed into a terminal or an agent's context.
    import subprocess
    import sys

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "--password=hunter2"])
    try:
        _fake_lsof(monkeypatch, f"{other.pid}\n")

        holders = common.find_collection_holders(tmp_path)

        assert "hunter2" not in str(holders)
        assert holders[0]["command"]
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("argv", [
    ["/Users/j/.local/bin/griot", "mcp"],
    ["/opt/homebrew/Cellar/python@3.14/Resources/Python.app/Contents/MacOS/Python", "/Users/j/.local/bin/griot", "mcp"],
    ["python", "-m", "griot.cli", "index", "all"],
    ["python3.14", "-u", "-m", "griot.mcp_server"],
])
def test_a_griot_command_line_is_recognised(argv):
    assert common._is_griot_command(argv)


@pytest.mark.parametrize("argv", [
    # the interpreter lives under a checkout called griot: the word is in the path, not in what runs
    ["/Users/j/Development/projects/griot/.venv/bin/python", "-c", "import time", "--password=x"],
    ["/Users/j/Development/projects/griot/.venv/bin/python", "backup.py", "--token=x"],
    ["rsync", "-a", "--password-file=p", "/data/griot", "/backup"],
    ["python", "-c", "print(1)", "griot"],
    ["python", "-m", "notgriot"],
    ["python", "-m", "griotx"],
    ["python", "other.py", "griot"],
    [],
])
def test_a_command_line_that_only_mentions_griot_is_not_recognised(argv):
    assert not common._is_griot_command(argv)


@pytest.mark.parametrize("argv, role", [
    (["/Users/j/.local/bin/griot", "mcp"], "mcp"),
    (["Python", "/Users/j/.local/bin/griot", "mcp"], "mcp"),
    (["python", "-m", "griot.mcp_server"], "mcp"),
    (["python", "-m", "griot.cli", "index", "all", "--repo", "x"], "index"),
    (["python", "-m", "griot.index_code", "--dry-run"], "index"),
    (["griot", "--profile", "openai-small", "index", "all"], "index"),
    (["griot", "index", "all", "--repo", "mcp-gateway"], "index"),  # "mcp" is a repo name here, not what runs
    (["griot", "--chat-profile", "groq", "ask", "why"], "other"),
    (["griot", "--version"], "other"),
    (["python", "-W", "ignore", "-m", "griot.mcp_server"], "mcp"),  # interpreter options that take a value
    (["python", "-X", "dev", "-m", "griot.cli", "index", "all"], "index"),
])
def test_a_griot_process_is_classified_by_what_it_runs(argv, role):
    assert common._griot_role(argv) == role


def test_an_interpreter_option_value_is_not_mistaken_for_the_script():
    assert common._griot_role(["python", "-W", "ignore", "/opt/tools/backup.py", "griot"]) is None
    assert common._griot_role(["python", "-X", "dev", "-m", "notgriot"]) is None


def test_a_process_that_is_not_griot_has_no_role():
    assert common._griot_role(["mcp-proxy", "--listen", "9000"]) is None
    assert common._griot_role(["python", "/opt/tools/mcp-proxy.py", "index"]) is None


def test_holders_carry_the_role_of_what_they_run(monkeypatch, tmp_path):
    import subprocess
    import sys

    script = tmp_path / "griot"
    script.write_text("import time; time.sleep(60)\n")
    other = tmp_path / "mcp-proxy"
    other.write_text("import time; time.sleep(60)\n")
    a = subprocess.Popen([sys.executable, str(script), "index", "all", "--repo", "mcp-gateway"])
    b = subprocess.Popen([sys.executable, str(other)])
    try:
        _fake_lsof(monkeypatch, f"{a.pid}\n{b.pid}\n")

        by_pid = {h["pid"]: h for h in common.find_collection_holders(tmp_path)}

        assert by_pid[a.pid]["role"] == "index"
        assert by_pid[b.pid]["role"] is None
    finally:
        for p in (a, b):
            p.kill()
            p.wait()


def test_holders_show_the_full_command_of_a_griot_process(monkeypatch, tmp_path):
    import subprocess
    import sys

    script = tmp_path / "griot"  # the shape a pipx install has: interpreter + a script called griot
    script.write_text("import time; time.sleep(60)\n")
    other = subprocess.Popen([sys.executable, str(script), "mcp"])
    try:
        _fake_lsof(monkeypatch, f"{other.pid}\n")

        assert "griot mcp" in common.find_collection_holders(tmp_path)[0]["command"]
    finally:
        other.kill()
        other.wait()


def test_a_holder_that_vanished_is_still_reported_by_pid(monkeypatch, tmp_path):
    _fake_lsof(monkeypatch, "999999\n")

    holders = common.find_collection_holders(tmp_path)

    assert holders == [{"pid": 999999, "command": None, "started": None, "role": None}]


@pytest.mark.parametrize("exc", [FileNotFoundError("no lsof"), OSError("denied")])
def test_holders_are_empty_when_lsof_is_unavailable(monkeypatch, tmp_path, exc):
    _fake_lsof(monkeypatch, exc=exc)

    assert common.find_collection_holders(tmp_path) == []


def test_holders_are_empty_when_lsof_hangs(monkeypatch, tmp_path):
    import subprocess

    _fake_lsof(monkeypatch, exc=subprocess.TimeoutExpired("lsof", 5))

    assert common.find_collection_holders(tmp_path) == []
