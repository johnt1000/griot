"""The permission repair of a collection, on a cold open and after a write.

`_secure_collection_dir()` closes whatever the engine wrote with the process
umask. It runs on every cold open, and in `multi` mode (the default) that is
every reopen after an idle release: walking and stat()ing every file each
time is a cost that grows with the collection. A directory whose entries did
not change since it was last checked keeps its entries' modes, unless
something chmods a file in it, which no directory records: that case is
covered by a full check after every write and at least every
`_FULL_CHECK_INTERVAL` seconds.

And a chmod that fails is tried again, then left for the next open and
reported: the repair never raises (it runs inside search and indexing) and
never prints (stdout is the MCP transport), so `griot doctor` is where a
file still open to others shows up."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from griot import common, doctor, harnesses

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def no_racy_window(monkeypatch):
    """Everything a test makes is younger than the racy window, and a
    directory's ctime cannot be set back: without this, nothing a test made
    would ever be remembered as checked."""
    monkeypatch.setattr(common, "_RACY_WINDOW_NS", 0)


def _collection(name="codebase__perm-repair", files=3):
    path = common._collection_path(name)
    common.secure_mkdir(path / "segments" / "a")
    for i in range(files):
        (path / "segments" / "a" / f"f{i}.dat").write_text("x")
    return name, path


def _recording(monkeypatch):
    """The entries the repair looked at, in order."""
    seen = []
    real = common._repair_mode

    def recording(target, mode):
        seen.append(str(target))
        return real(target, mode)

    monkeypatch.setattr(common, "_repair_mode", recording)
    return seen


# --- an unchanged collection is not walked file by file again ---------------------------------


def test_a_second_open_of_an_unchanged_collection_checks_no_file(monkeypatch, no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    seen = _recording(monkeypatch)

    common._secure_collection_dir(name)

    assert [s for s in seen if s.endswith(".dat")] == []


def test_a_file_created_since_is_closed(no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    new = path / "segments" / "a" / "new.dat"
    new.write_text("y")
    os.chmod(new, 0o644)

    common._secure_collection_dir(name)

    assert _mode(new) == 0o600


def test_a_file_replaced_by_a_rename_is_closed(no_racy_window, tmp_path):
    """What a restore or a sync does: writes a copy elsewhere and renames it
    in, with a new inode and the mode of the day."""
    name, path = _collection()
    common._secure_collection_dir(name)
    copy = path / "segments" / "a" / ".f0.tmp"
    copy.write_text("restored")
    os.chmod(copy, 0o644)
    os.replace(copy, path / "segments" / "a" / "f0.dat")

    common._secure_collection_dir(name)

    assert _mode(path / "segments" / "a" / "f0.dat") == 0o600


def test_a_file_added_with_the_directory_time_set_back_is_closed(no_racy_window):
    """What `rsync -t` or `tar` do after writing into a directory: set its
    mtime back to the archived one, which can be the very one remembered.
    The ctime cannot be set back."""
    name, path = _collection()
    common._secure_collection_dir(name)
    directory = path / "segments" / "a"
    before = os.stat(directory)
    added = directory / "restored.dat"
    added.write_text("r")
    os.chmod(added, 0o644)
    os.utime(directory, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(directory).st_mtime_ns == before.st_mtime_ns

    common._secure_collection_dir(name)

    assert _mode(added) == 0o600


def test_a_directory_created_since_is_closed_with_what_is_in_it(no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    deep = path / "segments" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "g.dat").write_text("z")
    os.chmod(path / "segments" / "b", 0o755)
    os.chmod(deep, 0o755)
    os.chmod(deep / "g.dat", 0o644)

    common._secure_collection_dir(name)

    assert (_mode(path / "segments" / "b"), _mode(deep), _mode(deep / "g.dat")) == (0o700, 0o700, 0o600)


def test_a_directory_opened_by_a_chmod_is_closed_and_its_files_checked(no_racy_window):
    """A recursive chmod (`chmod -R`, a restore that resets modes) changes
    every directory's mode, which is part of what is remembered."""
    name, path = _collection()
    common._secure_collection_dir(name)
    for root, _dirs, files in os.walk(path):
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    common._secure_collection_dir(name)

    for root, _dirs, files in os.walk(path):
        assert _mode(root) == 0o700
        for f in files:
            assert _mode(os.path.join(root, f)) == 0o600


def test_a_link_to_a_directory_elsewhere_is_neither_followed_nor_chmodded(no_racy_window, tmp_path):
    """chmod follows a link: closing one would close whatever it points at,
    and walking into it would close everything there."""
    name, path = _collection()
    elsewhere = tmp_path / "shared"
    elsewhere.mkdir()
    (elsewhere / "public.txt").write_text("p")
    os.chmod(elsewhere, 0o755)
    os.chmod(elsewhere / "public.txt", 0o644)
    os.symlink(elsewhere, path / "segments" / "link")

    common._secure_collection_dir(name, full=True)

    assert (_mode(elsewhere), _mode(elsewhere / "public.txt")) == (0o755, 0o644)


def test_a_removed_directory_is_forgotten_not_an_error(no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    for f in (path / "segments" / "a").iterdir():
        f.unlink()
    (path / "segments" / "a").rmdir()

    common._secure_collection_dir(name)  # must not raise

    assert _mode(path / "segments") == 0o700


def test_a_collection_removed_and_made_again_is_checked_whole(no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.unlink(os.path.join(root, f))
        os.rmdir(root)
    _collection(name)
    os.chmod(path / "segments" / "a" / "f1.dat", 0o644)

    common._secure_collection_dir(name)

    assert _mode(path / "segments" / "a" / "f1.dat") == 0o600


# --- what a directory does not record ----------------------------------------------------------


def test_a_file_chmodded_in_place_waits_for_the_full_check(monkeypatch, no_racy_window):
    """A chmod of a file changes nothing in its directory: the incremental
    pass cannot see it, and the full check after the interval does."""
    name, path = _collection()
    common._secure_collection_dir(name)
    loosened = path / "segments" / "a" / "f2.dat"
    os.chmod(loosened, 0o644)

    common._secure_collection_dir(name)
    assert _mode(loosened) == 0o644  # the documented gap, bounded below

    clock = [common.time.monotonic() + common._FULL_CHECK_INTERVAL + 1]
    monkeypatch.setattr(common.time, "monotonic", lambda: clock[0])
    common._secure_collection_dir(name)

    assert _mode(loosened) == 0o600


def test_the_interval_counts_from_the_last_full_check_not_the_last_open(monkeypatch, no_racy_window):
    name, path = _collection()
    start = common.time.monotonic()
    clock = [start]
    monkeypatch.setattr(common.time, "monotonic", lambda: clock[0])
    common._secure_collection_dir(name)
    for step in range(1, 4):  # opened often: an open must not push the full check away
        clock[0] = start + step * common._FULL_CHECK_INTERVAL / 3 - 1
        common._secure_collection_dir(name)
    loosened = path / "segments" / "a" / "f0.dat"
    os.chmod(loosened, 0o644)

    clock[0] = start + common._FULL_CHECK_INTERVAL + 1
    common._secure_collection_dir(name)

    assert _mode(loosened) == 0o600


def test_a_full_check_is_asked_for_after_a_write(no_racy_window):
    name, path = _collection()
    common._secure_collection_dir(name)
    loosened = path / "segments" / "a" / "f1.dat"
    os.chmod(loosened, 0o644)

    common._secure_collection_dir(name, full=True)

    assert _mode(loosened) == 0o600


def test_an_index_run_checks_the_whole_collection(monkeypatch, no_racy_window):
    """index_documents() is a write: the engine made files, and the run is
    long enough that a full walk is nothing next to it."""
    monkeypatch.setattr(common, "embed_texts", lambda texts, **k: [[0.1] * common.EMBED_DIM for _ in texts])
    common.get_client()
    common.release_client()
    path = common._collection_path(common.COLLECTION_NAME)
    extra = path / "kept"
    common.secure_mkdir(extra)
    (extra / "k.dat").write_text("k")
    common._secure_collection_dir(common.COLLECTION_NAME)
    os.chmod(extra / "k.dat", 0o644)

    common.index_documents([{"id": "repo:code:a.py:0", "content": "a",
                             "metadata": {"source_type": "code", "repo": "repo", "file_path": "a.py",
                                          "chunk_index": 0}}])

    assert _mode(extra / "k.dat") == 0o600


def test_a_prune_run_checks_the_whole_collection(monkeypatch, no_racy_window, tmp_path):
    """Removing points is a write too."""
    import json

    monkeypatch.setattr(common, "embed_texts", lambda texts, **k: [[0.1] * common.EMBED_DIM for _ in texts])
    repo = tmp_path / "a" / "proj"
    repo.mkdir(parents=True)
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(repo.resolve())]))

    def docs(names):
        return [{"id": f"proj:code:{n}:0", "content": f"content of {n}",
                 "metadata": {"source_type": "code", "repo": "proj", "file_path": n, "chunk_index": 0}} for n in names]

    common.index_documents(docs(["a.py", "b.py", "c.py"]))
    path = common._collection_path(common.COLLECTION_NAME)
    common.secure_mkdir(path / "kept")
    (path / "kept" / "k.dat").write_text("k")
    common._secure_collection_dir(common.COLLECTION_NAME)
    os.chmod(path / "kept" / "k.dat", 0o644)

    assert common.prune_orphans(docs(["a.py", "b.py"]), source_type="code", repo_paths=[repo]) == 1

    assert _mode(path / "kept" / "k.dat") == 0o600


def test_a_directory_changed_just_now_is_not_remembered(monkeypatch):
    """A directory's times have a granularity: one changed within it after
    the check may keep the same time. One that is that recent is checked
    again on the next open rather than trusted."""
    name, path = _collection()
    common._secure_collection_dir(name)
    seen = _recording(monkeypatch)

    common._secure_collection_dir(name)

    assert any(s.endswith("f0.dat") for s in seen)


# --- what the engine writes when it closes -----------------------------------------------------


def _open_to_others(path) -> list[str]:
    return [os.path.join(root, n) for root, dirs, files in os.walk(path) for n in dirs + files
            if not os.path.islink(os.path.join(root, n)) and _mode(os.path.join(root, n)) & 0o077]


@pytest.fixture
def loose_umask():
    """The umask most machines run with: what the engine's own writes get."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


_ONE_DOC = [{"id": "repo:code:a.py:0", "content": "a",
             "metadata": {"source_type": "code", "repo": "repo", "file_path": "a.py", "chunk_index": 0}}]


def test_a_release_leaves_nothing_the_engine_wrote_on_close_open(monkeypatch, loose_umask):
    """Closing a shard rewrites each segment's segment.json with the process
    umask, after every repair that ran while it was open (found on a real
    collection by the doctor check below). The MCP server releases after
    every idle spell."""
    monkeypatch.setattr(common, "embed_texts", lambda texts, **k: [[0.1] * common.EMBED_DIM for _ in texts])
    common.index_documents(_ONE_DOC)

    common.release_client()

    assert _open_to_others(common._collection_path(common.COLLECTION_NAME)) == []


def test_a_process_that_exits_holding_the_collection_leaves_nothing_open(tmp_path):
    """A CLI run never releases: the engine writes the same file when the
    process drops the handle on its way out."""
    import subprocess

    script = (
        "import os, json\n"
        "os.umask(0o022)\n"
        "from griot import common\n"
        "common.embed_texts = lambda texts, **k: [[0.1] * common.EMBED_DIM for _ in texts]\n"
        f"common.index_documents(json.loads({json.dumps(json.dumps(_ONE_DOC))}))\n"
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr

    collection = tmp_path / "data" / "griot" / "qdrant_data" / common.COLLECTION_NAME
    assert collection.is_dir()
    assert _open_to_others(collection) == []


# --- a chmod that fails ------------------------------------------------------------------------


def _failing_chmod(monkeypatch, target, times):
    """os.chmod refuses `target` the first `times` calls."""
    real = os.chmod
    calls = []

    def chmod(path, mode, *a, **k):
        if str(path) == str(target):
            calls.append(mode)
            if len(calls) <= times:
                raise PermissionError(1, "Operation not permitted")
        return real(path, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", chmod)
    return calls


def _logged(monkeypatch):
    logged = []
    monkeypatch.setattr(common, "log_and_print", lambda msg, level="info", echo=True: logged.append((msg, level, echo)))
    return logged


def test_a_chmod_that_fails_once_is_tried_again(monkeypatch):
    name, path = _collection()
    target = path / "segments" / "a" / "f0.dat"
    os.chmod(target, 0o644)
    calls = _failing_chmod(monkeypatch, target, times=1)
    logged = _logged(monkeypatch)

    common._secure_collection_dir(name)

    assert len(calls) == 2 and _mode(target) == 0o600 and logged == []


def test_a_chmod_that_keeps_failing_is_logged_never_raised_or_printed(monkeypatch, capsys):
    name, path = _collection()
    target = path / "segments" / "a" / "f0.dat"
    os.chmod(target, 0o644)
    calls = _failing_chmod(monkeypatch, target, times=99)
    logged = _logged(monkeypatch)

    common._secure_collection_dir(name)  # must not raise

    assert len(calls) == 2
    assert len(logged) == 1 and str(target) in logged[0][0] and logged[0][1:] == ("warning", False)
    assert capsys.readouterr().out == ""


def test_a_directory_with_a_failed_repair_is_checked_again_on_the_next_open(monkeypatch, no_racy_window):
    name, path = _collection()
    target = path / "segments" / "a" / "f0.dat"
    os.chmod(target, 0o644)
    calls = _failing_chmod(monkeypatch, target, times=2)
    _logged(monkeypatch)
    common._secure_collection_dir(name)
    assert _mode(target) == 0o644

    common._secure_collection_dir(name)

    assert len(calls) == 3 and _mode(target) == 0o600


def test_a_directory_whose_own_repair_failed_is_checked_again_on_the_next_open(monkeypatch, no_racy_window):
    name, path = _collection()
    target = path / "segments" / "a"
    os.chmod(target, 0o755)
    calls = _failing_chmod(monkeypatch, target, times=2)
    _logged(monkeypatch)
    common._secure_collection_dir(name)
    assert _mode(target) == 0o755

    common._secure_collection_dir(name)

    assert len(calls) == 3 and _mode(target) == 0o700


def test_a_file_gone_before_its_chmod_is_not_a_failure(monkeypatch):
    """The engine removes files of its own while the repair walks: nothing
    is left open to anyone, so nothing is worth a warning."""
    name, path = _collection()
    target = path / "segments" / "a" / "f0.dat"
    os.chmod(target, 0o644)
    real = os.chmod

    def vanishing(p, mode, *a, **k):
        if str(p) == str(target):
            os.unlink(target)
        return real(p, mode, *a, **k)

    monkeypatch.setattr(os, "chmod", vanishing)
    logged = _logged(monkeypatch)

    common._secure_collection_dir(name)

    assert logged == []


def test_a_target_already_gone_counts_as_closed(tmp_path, monkeypatch):
    """_repair_mode's contract: gone is closed (open to no one), so the
    caller does not count it as a failed repair and nothing is logged."""
    logged = _logged(monkeypatch)

    assert common._repair_mode(str(tmp_path / "never-there"), 0o600) is True
    assert logged == []


def test_a_directory_removed_after_its_parent_was_listed_is_skipped(monkeypatch):
    """The engine removes whole segment directories while the repair walks:
    one listed by its parent and gone before its own turn is open to no one,
    and the walk goes on instead of raising out of a search."""
    import shutil

    name, path = _collection()
    doomed = path / "segments" / "a"
    real_scandir = os.scandir

    class _ThenRemove:
        def __init__(self, directory):
            self.directory = directory
            self.inner = real_scandir(directory)

        def __enter__(self):
            return self.inner.__enter__()

        def __exit__(self, *exc):
            result = self.inner.__exit__(*exc)
            # shutil.rmtree lists by file descriptor: only the walk's own call matches.
            if isinstance(self.directory, str) and Path(self.directory) == path / "segments":
                shutil.rmtree(doomed)
            return result

    monkeypatch.setattr(os, "scandir", _ThenRemove)
    logged = _logged(monkeypatch)

    common._secure_collection_dir(name)  # must not raise

    assert not doomed.exists() and logged == []


# --- griot doctor shows what is still open -----------------------------------------------------


@pytest.fixture
def _no_harness(monkeypatch):
    """No harness asked (the suite forbids reaching a real one), and the
    test log the suite's own fixture opens closed as griot opens its own:
    the suite makes it with the umask of the day."""
    monkeypatch.setattr(harnesses, "HARNESSES", [])
    common.secure_mkdir(common.LOG_DIR)
    os.chmod(common.LOG_DIR, 0o700)
    if (common.LOG_DIR / "griot.log").exists():
        os.chmod(common.LOG_DIR / "griot.log", 0o600)


def _directories_check():
    return {c["check"]: c for c in doctor.run_checks()}["directories"]


def test_doctor_names_an_index_file_open_to_others(_no_harness):
    common.secure_mkdir(common.DATA_DIR)
    name, path = _collection()
    common._secure_collection_dir(name)
    left_open = path / "segments" / "a" / "f1.dat"
    os.chmod(left_open, 0o644)

    check = _directories_check()

    assert check["status"] == "warn" and str(left_open) in check["detail"]
    assert str(common.DATA_DIR) in check["fix"]


def test_doctor_counts_every_open_entry_and_names_a_few(_no_harness):
    common.secure_mkdir(common.DATA_DIR)
    name, path = _collection(files=8)
    for f in (path / "segments" / "a").iterdir():
        os.chmod(f, 0o644)
    os.chmod(path / "segments", 0o755)

    check = _directories_check()

    assert check["status"] == "warn" and "9 " in check["detail"]
    assert check["detail"].count(str(path)) <= doctor.OPEN_ENTRIES_NAMED


def test_doctor_is_quiet_about_a_closed_data_directory(_no_harness):
    common.secure_mkdir(common.DATA_DIR)
    name, path = _collection()
    common._secure_collection_dir(name)

    check = _directories_check()

    assert check["status"] == "ok"


def test_doctor_leaves_the_model_cache_alone(_no_harness):
    """The embedding library writes the downloaded model with its own modes:
    public weights, not anything indexed."""
    common.secure_mkdir(common.DATA_DIR)
    common.secure_mkdir(common.DATA_DIR / "models" / "some-model")
    weights = common.DATA_DIR / "models" / "some-model" / "model.onnx"
    weights.write_text("w")
    os.chmod(weights, 0o644)

    check = _directories_check()

    assert check["status"] == "ok"


def test_doctor_does_not_follow_a_link_out_of_the_data_directory(_no_harness, tmp_path):
    common.secure_mkdir(common.DATA_DIR)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("o")
    os.chmod(outside, 0o644)
    os.symlink(outside, common.DATA_DIR / "link")

    check = _directories_check()

    assert check["status"] == "ok"


def test_doctor_repairs_nothing(_no_harness):
    common.secure_mkdir(common.DATA_DIR)
    name, path = _collection()
    left_open = path / "segments" / "a" / "f1.dat"
    os.chmod(left_open, 0o644)

    doctor.run_checks()

    assert _mode(left_open) == 0o644
