"""Every directory griot makes for itself is for its owner alone.

The index is a readable copy of everything that was indexed, the logs hold
questions and paths, the configuration holds credentials. Each of those
files and their own directories were closed (0600, 0700), and the directory
that holds them all, `<data>/griot`, was not: whichever command ran first
created it on the way to `logs/` with the permissions of the day (0755),
and only an index run closed it afterwards. The model cache, made by the
embedding library, was open too.

What protects a file is every directory on the way to it. A backup or a
copy that keeps modes carries the inner ones along and drops the outer."""

import os
import stat
import subprocess
import sys

import pytest

from griot import common, logdb

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _griot(tmp_path, *argv, umask=0o022):
    """A griot command in a fresh process, with the usual permissive umask."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "home" / "config"), GRIOT_DATA_DIR=str(tmp_path / "home" / "share"))
    return subprocess.run([sys.executable, "-m", "griot.cli", *argv], env=env, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL, preexec_fn=lambda: os.umask(umask))


def _home(tmp_path):
    """Where the user keeps configuration and data: there before griot, and open, as `~/.config` is."""
    home = tmp_path / "home"
    for name in ("config", "share"):
        (home / name).mkdir(parents=True)
        os.chmod(home / name, 0o755)
    return home


def _directories(root):
    return [os.path.join(base, name) for base, names, _ in os.walk(root) for name in names]


# --- a new installation -------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["stats"], ["repos", "list"], ["profiles", "list"], ["config", "list"]])
def test_whatever_command_runs_first_leaves_only_private_directories(tmp_path, argv):
    home = _home(tmp_path)
    assert _griot(tmp_path, *argv).returncode == 0
    open_ones = [(oct(_mode(d)), d.replace(str(home), "")) for root in ("config", "share")
                 for d in _directories(home / root) if _mode(d) & 0o077]
    assert open_ones == []


def test_the_directory_griot_was_told_to_live_in_is_not_its_own_to_close(tmp_path):
    """`~/.local/share` belongs to every program: griot closes `griot/` in
    it and leaves the rest as it found it."""
    home = _home(tmp_path)
    _griot(tmp_path, "stats")
    assert _mode(home / "share") == 0o755 and _mode(home / "config") == 0o755
    assert _mode(home / "share" / "griot") == 0o700


# --- an installation made by an earlier version ---------------------------------------------------


def test_a_data_directory_left_open_by_an_earlier_version_is_closed(tmp_path):
    old = _home(tmp_path) / "share" / "griot"
    old.mkdir()
    os.chmod(old, 0o755)
    assert _griot(tmp_path, "profiles", "list").returncode == 0
    assert _mode(old) == 0o700


def test_a_directory_that_cannot_be_closed_does_not_stop_griot(tmp_path, monkeypatch):
    """Closing is a repair: on a file system that refuses it (a mounted
    share), the command still runs."""
    real = os.chmod

    def refuses(path, mode, *a, **k):
        if str(path) == str(common.DATA_DIR):
            raise PermissionError(1, "Operation not permitted")
        return real(path, mode, *a, **k)

    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(os, "chmod", refuses)
    common.keep_own_directories_private()
    common.secure_mkdir(common.LOG_DIR)
    assert common.LOG_DIR.is_dir()


# --- each way a directory comes to exist --------------------------------------------------------


def test_the_log_database_does_not_open_the_way_to_itself(tmp_path, monkeypatch):
    """The first write to the log made `<data>/griot` on the way with the
    default mode. (A read creates nothing at all since `griot doctor`.)"""
    old = os.umask(0o022)
    try:
        log_dir = tmp_path / "share" / "griot" / "logs"
        logdb.write_run(log_dir, {"timestamp": "2026-10-03T00:00:00+00:00", "script": "index_code.py"})
    finally:
        os.umask(old)
    assert _mode(log_dir) == 0o700 and _mode(log_dir.parent) == 0o700


def test_a_directory_made_through_secure_mkdir_closes_the_ones_it_made_on_the_way(tmp_path):
    old = os.umask(0o022)
    try:
        common.secure_mkdir(tmp_path / "a" / "b" / "c")
    finally:
        os.umask(old)
    assert [_mode(tmp_path / p) for p in ("a", "a/b", "a/b/c")] == [0o700] * 3
    assert _mode(tmp_path) & 0o700 == 0o700, "what was already there keeps its mode"


def test_a_directory_that_was_already_there_on_the_way_keeps_its_mode(tmp_path):
    (tmp_path / "shared").mkdir(mode=0o755)
    os.chmod(tmp_path / "shared", 0o755)
    common.secure_mkdir(tmp_path / "shared" / "mine")
    assert _mode(tmp_path / "shared") == 0o755 and _mode(tmp_path / "shared" / "mine") == 0o700


def test_the_model_cache_is_private_before_anything_is_downloaded_into_it(monkeypatch):
    seen = {}

    class _Model:
        def __init__(self, model_name, threads, cache_dir):
            seen["mode"] = _mode(cache_dir)
            seen["dir"] = cache_dir

    monkeypatch.setattr(common, "_text_embedding_class", lambda: _Model)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "local", "model": "fake", "dim": 1})
    old = os.umask(0o022)
    try:
        common.get_embed_model()
    finally:
        os.umask(old)
    assert seen["mode"] == 0o700 and seen["dir"] == str(common.DATA_DIR / "models")


def test_opening_the_index_first_does_not_open_the_way_to_it(monkeypatch, tmp_path):
    """A search can be the first thing a new installation does."""
    data = tmp_path / "elsewhere" / "griot"
    monkeypatch.setattr(common, "DATA_DIR", data)
    monkeypatch.setattr(common, "QDRANT_PATH", data / "qdrant_data")
    old = os.umask(0o022)
    try:
        common.get_client()
    finally:
        os.umask(old)
        common.release_client()
    assert _mode(data) == 0o700 and _mode(data / "qdrant_data") == 0o700


def test_a_directory_of_griot_s_own_that_is_already_there_and_open_is_closed(tmp_path):
    """The directory asked for, as opposed to one that is merely on the way."""
    mine = tmp_path / "logs"
    mine.mkdir()
    os.chmod(mine, 0o755)
    common.secure_mkdir(mine)
    assert _mode(mine) == 0o700


def test_an_unusual_umask_does_not_leave_a_directory_its_owner_cannot_enter(tmp_path):
    """mkdir's mode goes through the umask, which can take bits away from
    the owner too."""
    old = os.umask(0o177)
    try:
        common.secure_mkdir(tmp_path / "a" / "b")
    finally:
        os.umask(old)
    assert [_mode(tmp_path / p) for p in ("a", "a/b")] == [0o700, 0o700]


def test_a_file_where_the_directory_should_be_is_an_error_and_keeps_its_mode(tmp_path):
    """It used to be one (mkdir refused it). Closing "the directory" must
    not turn into making a file executable and failing somewhere else."""
    in_the_way = tmp_path / "logs"
    in_the_way.write_text("not a directory")
    os.chmod(in_the_way, 0o644)
    with pytest.raises(NotADirectoryError):
        common.secure_mkdir(in_the_way)
    assert _mode(in_the_way) == 0o644
