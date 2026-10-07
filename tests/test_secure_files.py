"""Data-at-rest permissions (findings M2/L4 from the security audit,
2026-08-19): only .env had permission handling — logs (with the user's
question and source labels), .spend_state.json, repos.json,
quality_golden_set.json, and the entire qdrant_data/ (a full,
recoverable plaintext copy of every indexed repo) were created with the
default umask (0644/0755, readable by any local user).

Contract: griot's data files are created 0600, data directories 0700 —
and writes to pre-existing files repair the permission (old installations
inherit the fix on the first write).
"""

import json
import stat

from griot import common, golden_set, logdb, repos


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_run_and_query_history_db_created_0600_and_log_dir_0700():
    """[2026-08-21] logs/runs.jsonl and logs/queries.jsonl were replaced by
    a single SQLite file (logs/logs.db, see logdb.py) — same 0600/0700
    contract as every other data file."""
    common.log_run_summary(script="t.py", repo="r", indexed=1, skipped=0, failed=0)
    common.log_query(question="sensitive question", model="m", limit=1, num_sources=0, sources=[])
    assert _mode(common.LOG_DIR / "logs.db") == 0o600
    assert _mode(common.LOG_DIR) == 0o700


def test_existing_world_readable_log_db_gets_repaired_on_next_write():
    common.log_run_summary(script="t.py", repo="r", indexed=0, skipped=0, failed=0)
    path = common.LOG_DIR / "logs.db"
    path.chmod(0o644)

    common.log_run_summary(script="t.py", repo="r", indexed=0, skipped=0, failed=0)
    assert _mode(path) == 0o600


def test_spend_state_created_0600():
    """[2026-08-21] Spend state moved out of .spend_state.json into
    logs.db's spend_state table (so concurrent processes stop losing each
    other's writes) — same 0600 contract, new file."""
    common.record_spend(0.001)
    assert _mode(common.LOG_DIR / logdb.DB_FILENAME) == 0o600


# --- common.secure_write_text_atomic() (repos.json needs the -------------- ---
# --- same crash-safety _save_spend_state() already had) --------------------

def test_secure_write_text_atomic_writes_content_0600(tmp_path):
    target = tmp_path / "state.json"
    common.secure_write_text_atomic(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    assert _mode(target) == 0o600


def test_secure_write_text_atomic_leaves_no_tmp_file_behind(tmp_path):
    target = tmp_path / "state.json"
    common.secure_write_text_atomic(target, "content")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_secure_write_text_atomic_overwrites_existing_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old")
    common.secure_write_text_atomic(target, "new")
    assert target.read_text() == "new"


def test_secure_write_text_atomic_uses_a_private_tmp_name_per_writer(tmp_path, monkeypatch):
    """[real bug, reproduced with 6 concurrent processes] The scratch file
    was a FIXED sibling name (`<target>.tmp`), so two processes writing the
    same target raced on the SAME scratch path: one os.replace()d it away
    while the other was still writing to it, and the loser died with
    FileNotFoundError mid-write. That crashed a real griot process during a
    paid indexing run, not just corrupted a value. The scratch name must be
    unique per writer so concurrent writers can never touch each other's."""
    captured = []
    real_secure_write_text = common.secure_write_text
    monkeypatch.setattr(common, "secure_write_text",
                        lambda p, t: (captured.append(p), real_secure_write_text(p, t))[1])

    target = tmp_path / "state.json"
    monkeypatch.setattr(common.os, "getpid", lambda: 1111)
    common.secure_write_text_atomic(target, "a")
    monkeypatch.setattr(common.os, "getpid", lambda: 2222)
    common.secure_write_text_atomic(target, "b")

    assert captured[0] != captured[1], "two writers shared one scratch path"
    assert target.read_text() == "b"


def test_the_scratch_file_the_writer_uses_is_the_one_atomic_scratch_path_names(tmp_path, monkeypatch):
    """The progress sweep (jobs._is_progress_scratch_file) recognises a
    scratch file a killed writer left by this name: writer and sweep read it
    from one function, so they cannot drift apart."""
    captured = []
    real_secure_write_text = common.secure_write_text
    monkeypatch.setattr(common, "secure_write_text",
                        lambda p, t: (captured.append(p), real_secure_write_text(p, t))[1])
    monkeypatch.setattr(common.os, "getpid", lambda: 3131)
    target = tmp_path / "state.json"
    common.secure_write_text_atomic(target, "a")
    assert captured == [common.atomic_scratch_path(target, 3131)]
    assert common.atomic_scratch_path(target) == captured[0], "this process's, when no pid is given"


def test_griot_log_file_created_0600(monkeypatch):
    import logging
    # brand-new handler in this test (conftest already isolates LOG_DIR)
    logger = logging.getLogger("griot")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    common.log_and_print("any line")
    assert _mode(common.LOG_DIR / "griot.log") == 0o600


# --- log_and_print(echo=): quality-check --json needs the ------------------
# --- self-check's log entries WITHOUT them landing on stdout, which in ------
# --- --json mode carries ONLY the JSON payload (jobs.py's subprocess --------
# --- contract depends on that) ----------------------------------------------

def test_log_and_print_echo_true_writes_to_stdout_by_default(capsys):
    common.log_and_print("visible line")
    assert "visible line" in capsys.readouterr().out


def test_log_and_print_echo_false_omits_stdout_but_still_logs():
    common.log_and_print("silent line", echo=False)
    assert "silent line" in (common.LOG_DIR / "griot.log").read_text()


def test_log_and_print_echo_false_produces_no_stdout(capsys):
    common.log_and_print("silent line", echo=False)
    assert capsys.readouterr().out == ""


def test_repos_json_created_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REPOS_JSON_PATH", tmp_path / "cfg" / "repos.json")
    repo_dir = tmp_path / "meu-repo"
    repo_dir.mkdir()

    repos.cmd_add(str(repo_dir))
    assert _mode(common.REPOS_JSON_PATH) == 0o600


def test_golden_set_created_0600(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "GOLDEN_SET_PATH", tmp_path / "cfg" / "quality_golden_set.json")
    golden_set._save([{"query": "q", "must_include": []}])
    assert _mode(common.GOLDEN_SET_PATH) == 0o600
    assert json.loads(common.GOLDEN_SET_PATH.read_text())[0]["query"] == "q"


def test_qdrant_data_dir_created_0700(monkeypatch):
    # get_client() creates the collection directory — the parent (qdrant_data)
    # and the subdirectory itself must be created 0700: it's a recoverable
    # plaintext copy of everything that was indexed.
    client = common.get_client()
    assert client is not None
    assert _mode(common.QDRANT_PATH) == 0o700


def test_lock_file_created_0600():
    """[review] the lock carries pid/start_time/label (label is designed to
    receive a repo path) — same 0600 contract as the other files."""
    common.acquire_lock()
    try:
        assert _mode(common.LOCK_PATH) == 0o600
    finally:
        common.release_lock()


def test_config_dir_created_0700_by_repos_add(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REPOS_JSON_PATH", tmp_path / "cfg" / "repos.json")
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    repos.cmd_add(str(repo_dir))
    assert _mode((tmp_path / "cfg")) == 0o700
