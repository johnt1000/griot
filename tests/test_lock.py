import json
import os

import psutil
import pytest

from griot import common


def _read_lock_json():
    return json.loads(common.LOCK_PATH.read_text())


def test_acquire_then_release_removes_lock_file():
    common.acquire_lock()
    assert common.LOCK_PATH.exists()
    info = _read_lock_json()
    assert info["pid"] == os.getpid()
    assert info["label"] is None
    assert info["start_time"] == pytest.approx(psutil.Process(os.getpid()).create_time(), abs=1.0)
    common.release_lock()
    assert not common.LOCK_PATH.exists()


def test_acquire_with_label_is_persisted():
    common.acquire_lock(label="/repos/meu-projeto")
    assert _read_lock_json()["label"] == "/repos/meu-projeto"
    common.release_lock()


def test_acquire_twice_in_same_process_blocks():
    """The test process itself is alive, with the same create_time() it wrote
    into the lock, so a second acquire_lock() recognizes the owner as alive
    and should refuse — the same behavior as two concurrent indexers."""
    common.acquire_lock()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            common.acquire_lock()
    finally:
        common.release_lock()


def test_stale_lock_with_dead_pid_is_recovered():
    # PID above the practical limit of any real system — virtually
    # guaranteed not to exist, so psutil.Process(pid) raises NoSuchProcess.
    dead_pid = 2**31 - 1
    common.LOCK_PATH.write_text(json.dumps({"pid": dead_pid, "start_time": 0.0, "label": None}))
    common.acquire_lock()  # should not raise — orphaned lock, overwrite
    assert _read_lock_json()["pid"] == os.getpid()
    common.release_lock()


def test_lock_with_mismatched_start_time_is_recovered():
    """Live PID (our own, to guarantee psutil.Process() doesn't raise
    NoSuchProcess), but the recorded start_time doesn't match the process's
    real create_time() — a sign the PID was recycled by another process
    after the original griot died without cleaning up the lock. Must be
    treated as orphaned, not as "still running"."""
    real_start_time = psutil.Process(os.getpid()).create_time()
    common.LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "start_time": real_start_time - 999, "label": None}))
    common.acquire_lock()  # should not raise — start_time mismatch, orphaned
    assert _read_lock_json()["pid"] == os.getpid()
    common.release_lock()


def test_corrupted_lock_content_is_recovered():
    common.LOCK_PATH.write_text("not-json")
    common.acquire_lock()  # JSONDecodeError on parse -> treated as orphaned
    assert _read_lock_json()["pid"] == os.getpid()
    common.release_lock()


def test_legacy_raw_pid_lock_is_recovered():
    """Lock in the old format (raw PID, from before this change to JSON) —
    must not break with JSONDecodeError, treated as an orphaned lock."""
    common.LOCK_PATH.write_text(str(os.getpid()))
    common.acquire_lock()
    assert _read_lock_json()["pid"] == os.getpid()
    common.release_lock()


def test_access_denied_on_lock_owner_is_treated_as_orphan(monkeypatch):
    """PID belonging to another owner (e.g. recycled by another process
    after the original griot died) makes psutil.Process(pid) raise
    AccessDenied — before this handling, it would propagate and break with
    a confusing traceback instead of the clear lock-busy/orphaned message."""
    other_pid = 424242
    common.LOCK_PATH.write_text(json.dumps({"pid": other_pid, "start_time": 123.0, "label": None}))

    real_process = psutil.Process

    def fake_process(pid):
        if pid == other_pid:
            raise psutil.AccessDenied(pid)
        return real_process(pid)

    monkeypatch.setattr(psutil, "Process", fake_process)
    common.acquire_lock()  # should not propagate AccessDenied
    assert _read_lock_json()["pid"] == os.getpid()
    common.release_lock()


def test_release_lock_does_not_remove_lock_owned_by_another_pid():
    other_pid = os.getpid() + 1  # any PID that isn't ours
    common.LOCK_PATH.write_text(json.dumps({"pid": other_pid, "start_time": 0.0, "label": None}))
    common.release_lock()
    assert common.LOCK_PATH.exists()
    assert _read_lock_json()["pid"] == other_pid


def test_concurrent_creation_race_raises_clear_error(monkeypatch):
    """Simulates the real race: two processes pass the 'no lock' check and
    try to create the file at the same time — only one can win the O_EXCL."""
    real_open = os.open

    def fake_open(path, flags, *a, **kw):
        if str(path) == str(common.LOCK_PATH) and (flags & os.O_EXCL):
            raise FileExistsError("simulated race")
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(RuntimeError, match="same instant"):
        common.acquire_lock()
