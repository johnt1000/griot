"""Tests for `griot.jobs` — child-process job spawning shared by the MCP
server (`griot_index_repo`) and a second, since-removed host. Most of the
spawn/allowlist logic is exercised end-to-end via tests/test_mcp_server.py
(same code, one real consumer already) — this file covers what's NEW for
v2 and not exercised by the MCP path: `allow_env_roots=False` (the
that host's stricter policy), `child_env()`, and `path=None` ("index all
registered repos").
"""

import json
import subprocess
import time
from pathlib import Path

from griot import common, jobs


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "c"], cwd=path, check=True)
    return path


# --- index_path_allowed() ---------------------------------------------------


def test_index_path_allowed_true_for_registered_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo)])
    assert jobs.index_path_allowed(repo.resolve()) is True


def test_index_path_allowed_false_when_unregistered_and_no_roots(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(common, "load_repos", lambda: [])
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    assert jobs.index_path_allowed(repo.resolve()) is False


def test_index_path_allowed_true_under_env_roots_when_allowed(tmp_path, monkeypatch):
    repo = tmp_path / "root" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(common, "load_repos", lambda: [])
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path / "root"))
    assert jobs.index_path_allowed(repo.resolve(), allow_env_roots=True) is True


def test_index_path_allowed_false_under_env_roots_when_disallowed(tmp_path, monkeypatch):
    """[decision C] The stricter policy of the since-removed host —
    GRIOT_MCP_INDEX_ROOTS must NOT extend that caller's allowlist, even
    though it extends the MCP tool's. Pins the deliberate divergence so
    nobody 'unifies' it away by accident later."""
    repo = tmp_path / "root" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(common, "load_repos", lambda: [])
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path / "root"))
    assert jobs.index_path_allowed(repo.resolve(), allow_env_roots=False) is False


def test_index_path_allowed_fails_closed_on_corrupted_repos_json(tmp_path, monkeypatch):
    """[risk 5.1] The original mcp_server._index_path_allowed
    only caught FileNotFoundError — a truncated/corrupted repos.json (real
    scenario: crash mid non-atomic write, before Step 1's fix) raised
    json.JSONDecodeError and propagated, breaking the allowlist's fail-
    closed contract. Must fail closed (refuse), not 500/propagate."""
    def broken_load_repos():
        raise json.JSONDecodeError("bad", "doc", 0)
    monkeypatch.setattr(common, "load_repos", broken_load_repos)
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    assert jobs.index_path_allowed(repo.resolve()) is False


# --- child_env() -------------------------------------------------------


def test_child_env_removes_var_that_only_came_from_the_file():
    boot_env = {"GRIOT_EMBED_PROFILE": "jina-code", "PATH": "/bin"}
    boot_file = {"GRIOT_EMBED_PROFILE": "jina-code"}
    env = jobs.child_env(boot_env, boot_file)
    assert "GRIOT_EMBED_PROFILE" not in env


def test_child_env_preserves_var_genuinely_exported_in_the_shell(monkeypatch):
    """boot_env differs from boot_file for this var -> it came from the
    shell, not from load_dotenv() reading the file -> must survive into
    the child (same var the UI already flags "overridden by shell").

    child_env() reads the LIVE os.environ as its base (not boot_env
    itself — a long-lived host's os.environ doesn't change after boot
    except for exactly this kind of shell export), so the live value has
    to be set for real here via monkeypatch.setenv, matching what boot_env
    claims it was at boot time."""
    monkeypatch.setenv("GRIOT_SPEND_CEILING_USD", "999.0")
    boot_env = {"GRIOT_SPEND_CEILING_USD": "999.0", "PATH": "/bin"}
    boot_file = {"GRIOT_SPEND_CEILING_USD": "3.0"}
    env = jobs.child_env(boot_env, boot_file)
    assert env["GRIOT_SPEND_CEILING_USD"] == "999.0"


def test_child_env_always_preserves_config_and_data_dir(monkeypatch):
    monkeypatch.setenv("GRIOT_CONFIG_DIR", "/x/config")
    monkeypatch.setenv("GRIOT_DATA_DIR", "/x/data")
    boot_env = {"GRIOT_CONFIG_DIR": "/x/config", "GRIOT_DATA_DIR": "/x/data"}
    boot_file = {"GRIOT_CONFIG_DIR": "/x/config", "GRIOT_DATA_DIR": "/x/data"}
    env = jobs.child_env(boot_env, boot_file)
    assert env["GRIOT_CONFIG_DIR"] == "/x/config"
    assert env["GRIOT_DATA_DIR"] == "/x/data"


def test_child_env_keeps_vars_absent_from_boot_file(monkeypatch):
    """A var present in the live process env but never part of .env at
    all (e.g. an arbitrary custom var) is untouched — child_env() only
    ever REMOVES vars whose boot value demonstrably came from the file."""
    monkeypatch.setenv("GRIOT_TEST_ENV_MARKER", "/bin")
    boot_env = {"GRIOT_TEST_ENV_MARKER": "/bin"}
    boot_file = {}
    env = jobs.child_env(boot_env, boot_file)
    assert env["GRIOT_TEST_ENV_MARKER"] == "/bin"


# --- start_index_job(path=None) — "index all registered repos" ---------


def _fake_popen_letting_git_through(captured):
    """Shared fake for tests that spawn a real registered git repo:
    start_index_job() validates the repo via a REAL `subprocess.run(["git",
    ...])` call before spawning the index job — that internally calls
    Popen too, so a blanket fake would break the git check. Real popen is
    captured BEFORE the monkeypatch (using jobs.subprocess.Popen by name
    here would recurse into this same fake)."""
    real_popen = jobs.subprocess.Popen

    class FakeProc:
        pid = 1
        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        if cmd[0] == "git":
            return real_popen(cmd, **kw)
        captured["cmd"] = cmd
        return FakeProc()

    return fake_popen


def _stub_no_job_in_flight(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "release_client", lambda: None)
    monkeypatch.setattr(common, "LOG_DIR", tmp_path)


# --- start_index_job() prefers --repo over --path when unambiguous ---------
# --- [real bug fix] --path and --repo/repos.json produce DIFFERENT ---------
# --- stable_id()s for identical content — see _repo_name_if_uniquely_ ------
# --- registered()'s docstring in jobs.py for the full story. ---------------


def test_start_index_job_uses_repo_flag_for_a_uniquely_registered_path(monkeypatch, tmp_path):
    """The common case: a repo registered in repos.json, indexed via the
    MCP — must produce the SAME id space a plain `griot index
    all`/`--repo` run would, or every reindex through the UI/MCP silently
    duplicates the whole repo's data (confirmed against real production
    data: a user's collection had every chunk of a reindexed repo
    duplicated exactly 2x after this bug)."""
    repo = _git_repo(tmp_path / "my-repo")
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo)])
    _stub_no_job_in_flight(monkeypatch, tmp_path)

    captured = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _fake_popen_letting_git_through(captured))
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = jobs.start_index_job(str(repo), sources=["code"])

    assert result["started"] is True
    assert "--path" not in captured["cmd"]
    assert "--repo" in captured["cmd"]
    assert "my-repo" in captured["cmd"]


def test_start_index_job_falls_back_to_path_when_repo_name_is_ambiguous(monkeypatch, tmp_path):
    """Two DIFFERENT registered repos sharing the same basename in
    different locations — `--repo <name>` (index_code.py's plain
    `Path(p).name == args.repo` matching) would resolve ambiguously back
    to BOTH, not just the one requested. Falls back to the disambiguating
    `--path` rather than risk indexing the wrong (or an extra) directory."""
    repo_a = _git_repo(tmp_path / "a" / "my-repo")
    repo_b_dir = tmp_path / "b" / "my-repo"
    repo_b_dir.mkdir(parents=True)
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo_a), str(repo_b_dir)])
    _stub_no_job_in_flight(monkeypatch, tmp_path)

    captured = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _fake_popen_letting_git_through(captured))
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = jobs.start_index_job(str(repo_a), sources=["code"])

    assert result["started"] is True
    assert "--repo" not in captured["cmd"]
    assert "--path" in captured["cmd"]
    assert str(repo_a.resolve()) in captured["cmd"]


def test_start_index_job_uses_path_when_allowed_only_via_env_roots(monkeypatch, tmp_path):
    """A path allowed only via GRIOT_MCP_INDEX_ROOTS (not registered in
    repos.json at all) has no registered NAME to reference — must keep
    using --path, exactly as before this fix."""
    repo = _git_repo(tmp_path / "unregistered-repo")
    monkeypatch.setattr(common, "load_repos", lambda: [])
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path))
    _stub_no_job_in_flight(monkeypatch, tmp_path)

    captured = {}
    monkeypatch.setattr(jobs.subprocess, "Popen", _fake_popen_letting_git_through(captured))
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = jobs.start_index_job(str(repo), sources=["code"], allow_env_roots=True)

    assert result["started"] is True
    assert "--path" in captured["cmd"]
    assert "--repo" not in captured["cmd"]


def test_start_index_job_path_none_omits_the_path_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "release_client", lambda: None)
    monkeypatch.setattr(common, "LOG_DIR", tmp_path)

    captured = {}

    class FakeProc:
        pid = 1
        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = jobs.start_index_job(None, sources=["code"])

    assert result["started"] is True
    assert result["path"] is None
    assert "--path" not in captured["cmd"]
    assert captured["cmd"][-4:] == ["index", "all", "--sources", "code"]


def test_start_index_job_path_none_skips_allowlist_check(monkeypatch, tmp_path):
    """[decision, plan] 'index all' never checks index_path_allowed() —
    the target already IS the full allowed list (repos.json itself)."""
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "release_client", lambda: None)
    monkeypatch.setattr(common, "LOG_DIR", tmp_path)

    def should_not_be_called(*a, **kw):
        raise AssertionError("index_path_allowed() must not be called for path=None")
    monkeypatch.setattr(jobs, "index_path_allowed", should_not_be_called)

    class FakeProc:
        pid = 1
        def poll(self):
            return None
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = jobs.start_index_job(None)
    assert result["started"] is True


def test_start_index_job_refuses_when_a_job_is_already_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: {"pid": 123, "path": None, "sources": ["code"], "started_at": time.time()})

    result = jobs.start_index_job(None)

    assert result["started"] is False
    assert "already in progress" in result["reason"]


# --- registry test isolation (real finding: a fake Popen whose poll() ------
# --- always returns None — the normal way to simulate "still running" — ----
# --- leaves a permanent fake entry in the module-level _registry dict if ---
# --- nothing resets it between tests) ---------------------------------------


def test_registry_is_empty_at_the_start_of_every_test():
    """Pins conftest.py's autouse reset of jobs._registry — without it, a
    prior test's fake 'still running forever' job would make every
    subsequent start_index_job() call in the same pytest session refuse
    with a false 'already in progress'."""
    assert jobs._registry == {}
    assert jobs.running_index_job() is None


# --- run_quality_check_job(): subprocess contract ---------------------------


class _FakeCompleted:
    """subprocess.CompletedProcess stand-in — run_quality_check_job() only
    ever reads .stdout/.stderr/.returncode off it (see jobs.py), so this
    covers the whole surface it depends on without spawning anything."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_run_quality_check_job_parses_successful_json(monkeypatch):
    payload = {"collection": "codebase__jina-code", "self_check": {"sampled": 5, "passed": 5, "failed": 0, "failures": [], "avg_score": 1.0}, "golden_check": None, "ok": True}
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout=json.dumps(payload), returncode=0))

    result = jobs.run_quality_check_job(5)

    assert result["ok"] is True
    assert result["result"] == payload


def test_run_quality_check_job_calls_the_right_argv(monkeypatch):
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["kwargs"] = kw
        return _FakeCompleted(stdout=json.dumps({"ok": True, "self_check": {}, "golden_check": None, "collection": "x"}))
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    jobs.run_quality_check_job(12)

    argv = captured["argv"]
    assert "--json" in argv
    assert "--skip-golden-set" in argv
    assert "--sample-size" in argv and "12" in argv
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_run_quality_check_job_forwards_collection_when_given(monkeypatch):
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _FakeCompleted(stdout=json.dumps({"ok": True, "self_check": {}, "golden_check": None, "collection": "codebase__bge-small"}))
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    jobs.run_quality_check_job(5, collection="codebase__bge-small")

    argv = captured["argv"]
    assert "--collection" in argv
    assert "codebase__bge-small" in argv


def test_run_quality_check_job_omits_collection_flag_when_not_given(monkeypatch):
    """Default behavior (None) must stay byte-identical to before this
    param existed — the CLI's own default (the active profile's
    collection) is what every existing caller (MCP-less, no UI selection)
    still relies on."""
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _FakeCompleted(stdout=json.dumps({"ok": True, "self_check": {}, "golden_check": None, "collection": "x"}))
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    jobs.run_quality_check_job(5)

    assert "--collection" not in captured["argv"]


def test_run_quality_check_job_exit_1_with_valid_json_is_still_ok_result(monkeypatch):
    """[contract, Step 3] exit 1 + valid JSON = the self-check FOUND
    failures — a real, renderable result, not an execution error."""
    payload = {"collection": "x", "self_check": {"sampled": 3, "passed": 1, "failed": 2, "failures": [{"id": "a", "repo": "r", "reason": "low score"}], "avg_score": 0.5}, "golden_check": None, "ok": False}
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout=json.dumps(payload), returncode=1))

    result = jobs.run_quality_check_job(3)

    assert result["ok"] is True  # the JOB ran fine — the CHECK found failures, that's in result["result"]
    assert result["result"]["ok"] is False
    assert result["result"]["self_check"]["failed"] == 2


def test_run_quality_check_job_tolerates_interleaved_warning_before_json(monkeypatch):
    """[security review finding] embed_texts()'s retry warnings
    (log_and_print(echo=True) on a 429/connection retry) print straight to
    stdout even under --json — quality_check.py only suppresses its OWN
    print() calls, not the deeper retry logging. The JSON payload is always
    the LAST thing quality_check.py prints in --json mode, so a warning
    line ahead of it must not make a real result look like garbage output."""
    payload = {"collection": "x", "self_check": {"sampled": 1, "passed": 1, "failed": 0, "failures": [], "avg_score": 1.0}, "golden_check": None, "ok": True}
    stdout = "Warning: rate limited by provider, retrying in 2s...\n" + json.dumps(payload)
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout=stdout, returncode=0))

    result = jobs.run_quality_check_job(1)

    assert result["ok"] is True
    assert result["result"] == payload


def test_run_quality_check_job_non_json_stdout_is_an_error(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout="not json at all", stderr="", returncode=1))

    result = jobs.run_quality_check_job(5)

    assert result["ok"] is False
    assert "reason" in result


def test_run_quality_check_job_empty_stdout_surfaces_stderr(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout="", stderr="GEMINI_TOKEN not found", returncode=1))

    result = jobs.run_quality_check_job(5)

    assert result["ok"] is False
    assert "GEMINI_TOKEN" in result["reason"]


def test_run_quality_check_job_timeout_reports_a_clear_reason(monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    result = jobs.run_quality_check_job(5, timeout=1)

    assert result["ok"] is False
    assert "timed out after 1s" in result["reason"]


def test_run_quality_check_job_single_flight_refuses_concurrent_run(monkeypatch):
    """[risk 3] That host served requests on threads — two concurrent
    quality-check clicks must not both spawn a subprocess (each loads a
    fresh embedding model in ITS OWN process — still real disk/CPU cost
    doubled for nothing, and a real accidental-OOM risk if sample_size or
    the repo is large)."""
    acquired_lock_manually = jobs._quality_check_lock.acquire(blocking=False)
    assert acquired_lock_manually  # sanity: lock starts free
    try:
        result = jobs.run_quality_check_job(5)
        assert result["ok"] is False
        assert "already" in result["reason"]
    finally:
        jobs._quality_check_lock.release()


def test_run_quality_check_job_releases_lock_after_completion(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "run", lambda argv, **kw: _FakeCompleted(stdout=json.dumps({"ok": True, "self_check": {}, "golden_check": None, "collection": "x"})))

    jobs.run_quality_check_job(5)

    # lock must be free again — a second call right after must NOT be refused
    acquired = jobs._quality_check_lock.acquire(blocking=False)
    assert acquired
    jobs._quality_check_lock.release()
