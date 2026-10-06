"""`griot doctor`: one command that checks what the scattered error messages
check one at a time.

Settings that parse, directories and the configuration file closed to other
users, the active profile and its credential, the collection, the registered
repositories and whether their index is behind, today's spend against the
ceiling, the MCP registration and which read-only tools still ask, the
variables a server would ignore, git itself, the log. Each check says ok,
warn, FAIL or skip, with what to do; the exit status is 1 only for a FAIL.
Nothing is changed by it."""

import json
import os
import stat
import subprocess
import sys

import pytest

from griot import common, config, doctor, harnesses


def _by_name(checks):
    return {check["check"]: check for check in checks}


@pytest.fixture(autouse=True)
def _no_harness_on_this_machine(monkeypatch):
    """The harness checks ask the real `claude` otherwise (the suite forbids
    it); the tests that want a harness say so with the `claude_code` fixture."""
    monkeypatch.setattr(harnesses, "HARNESSES", [])


def _cli(tmp_path, *args, file="", exported=None):
    """`griot doctor` in a fresh process, against a configuration of its own
    (under `tmp_path/isolated`: the suite's own fixture keeps this process's
    directories under `tmp_path/config` and `tmp_path/data`)."""
    tmp_path = tmp_path / "isolated"
    env_file = tmp_path / "config" / "griot" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(file)
    env_file.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "CLAUDE_"))}
    # No harness on the machine this process sees: asking a real one about
    # its registration starts the registered server for a moment, which
    # writes a log of its own into the data directory under test.
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               HOME=str(tmp_path / "home"), PATH="/usr/bin:/bin", **(exported or {}))
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, "-m", "griot.cli", "doctor", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=subprocess.DEVNULL)


# --- the command ----------------------------------------------------------------------------------


def test_the_command_exists_and_is_listed(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "isolated" / "c"), GRIOT_DATA_DIR=str(tmp_path / "isolated" / "d"))
    listing = subprocess.run([sys.executable, "-m", "griot.cli", "--help"], env=env, capture_output=True, text=True, timeout=120)

    assert "doctor" in listing.stdout
    assert _cli(tmp_path, "--help").returncode == 0


def test_a_fresh_installation_reports_without_failing(tmp_path):
    done = _cli(tmp_path)

    assert done.returncode == 0, done.stderr[-1500:]
    assert "settings" in done.stdout and "repositories" in done.stdout and "warn" in done.stdout
    assert "FAIL" not in done.stdout


def test_json_is_one_document_with_one_entry_per_check(tmp_path):
    done = _cli(tmp_path, "--json")

    checks = json.loads(done.stdout)
    assert isinstance(checks, list) and {"check", "status", "detail"} <= set(checks[0])
    assert {check["status"] for check in checks} <= {"ok", "warn", "FAIL", "skip"}


def test_a_setting_griot_cannot_start_with_is_a_failure_in_one_line(tmp_path):
    """The one command that must not die on a broken file: it is what says
    which line is broken."""
    done = _cli(tmp_path, file="GRIOT_SPEND_CEILING_USD=lots\n")

    assert done.returncode == 1 and "Traceback" not in done.stderr
    assert "FAIL" in done.stdout and "GRIOT_SPEND_CEILING_USD" in done.stdout and "griot config set" in done.stdout
    assert "finite number" in done.stdout, "what a valid value looks like, as `griot config set` would say"


def test_a_setting_griot_starts_with_but_should_not_is_a_failure_too(tmp_path):
    """The readers of some settings accept what `griot config set` would
    refuse (a platform URL that is not https): griot starts, and the token
    would go to that address. The file is held to what the command checks."""
    done = _cli(tmp_path, file="GRIOT_GITLAB_API_BASE=ftp://gitlab.example.com/api/v4\n")

    assert done.returncode == 1 and "FAIL" in done.stdout and "GRIOT_GITLAB_API_BASE" in done.stdout
    assert "https" in done.stdout and "griot config set gitlab-api-base" in done.stdout


def test_a_profile_that_does_not_exist_is_a_failure_in_one_line(tmp_path):
    done = _cli(tmp_path, file="GRIOT_EMBED_PROFILE=nope\n")

    assert done.returncode == 1 and "Traceback" not in done.stderr
    assert "FAIL" in done.stdout and "nope" in done.stdout and "griot profiles use" in done.stdout
    assert "one of:" in done.stdout and "jina-code" in done.stdout, "the profiles there are, to pick from"


def test_a_profile_set_in_the_environment_that_does_not_exist_names_the_environment(tmp_path):
    """`griot profiles use` writes the file; a variable exported beside it
    would still win, so the person has to know where the name came from."""
    done = _cli(tmp_path, exported={"GRIOT_EMBED_PROFILE": "nope"})

    assert done.returncode == 1 and "FAIL" in done.stdout and "nope" in done.stdout
    assert "set in the environment" in done.stdout and "unset the variable" in done.stdout


def test_nothing_is_created_by_a_check(tmp_path):
    """Looking is not writing: no collection, no log directory, no log
    database, no template, for a status that is only read."""
    done = _cli(tmp_path)
    assert done.returncode == 0, done.stderr[-1500:]

    root = tmp_path / "isolated"
    made = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if "config/griot/.env" not in str(p) and str(p.relative_to(root)) != "home")
    assert made == ["config", "config/griot"], (made, done.stdout, done.stderr)


def test_a_settings_file_open_to_others_is_reported_even_though_griot_closes_it_on_the_way(tmp_path):
    """Every griot command closes an open .env as it loads; a check that
    looked afterwards found it closed and said nothing."""
    tmp_path = tmp_path / "isolated"
    env_file = tmp_path / "config" / "griot" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("")
    env_file.chmod(0o644)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "CLAUDE_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               HOME=str(tmp_path), PATH="/usr/bin:/bin")

    done = subprocess.run([sys.executable, "-m", "griot.cli", "doctor"], env=env, capture_output=True, text=True, timeout=180)

    assert done.returncode == 0 and "warn  settings" in done.stdout and "0644" in done.stdout
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600, "closed, as every command does, and said so"
    assert "closed it" in done.stdout


# --- each check -----------------------------------------------------------------------------------


def test_the_settings_file_open_to_others_is_a_warning(tmp_path, monkeypatch):
    common.secure_mkdir(common.CONFIG_DIR)
    common.ENV_PATH.write_text("")
    common.ENV_PATH.chmod(0o644)

    check = _by_name(doctor.run_checks())["settings"]

    assert check["status"] == "warn" and "0600" in check["detail"]


def test_directories_not_made_yet_are_said_so_not_called_private(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "CONFIG_DIR", tmp_path / "never" / "griot")
    monkeypatch.setattr(common, "DATA_DIR", tmp_path / "never" / "data" / "griot")

    check = _by_name(doctor.run_checks())["directories"]

    assert check["status"] == "ok" and "not created yet" in check["detail"] and "private (0700)" not in check["detail"]


def test_a_data_directory_open_to_others_is_a_warning(tmp_path):
    common.secure_mkdir(common.DATA_DIR)
    os.chmod(common.DATA_DIR, 0o755)

    check = _by_name(doctor.run_checks())["directories"]

    assert check["status"] == "warn" and str(common.DATA_DIR) in check["detail"]


def test_a_local_profile_needs_no_credential(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["jina-code"])

    check = _by_name(doctor.run_checks())["profile"]

    assert check["status"] == "ok" and "jina-code" in check["detail"] and "local" in check["detail"]


def test_a_paid_profile_without_its_credential_is_a_failure(monkeypatch):
    from griot import auth

    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(auth, "provider_status", lambda: [{"provider": "openai", "env_var": "GRIOT_OPENAI_API_KEY",
                                                           "configured": False}])

    check = _by_name(doctor.run_checks())["profile"]

    assert check["status"] == "FAIL" and "griot auth set openai" in check["fix"]


def test_a_paid_profile_with_its_credential_is_fine(monkeypatch):
    from griot import auth

    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(auth, "provider_status", lambda: [{"provider": "openai", "env_var": "GRIOT_OPENAI_API_KEY",
                                                           "configured": True}])

    assert _by_name(doctor.run_checks())["profile"]["status"] == "ok"


def test_an_empty_index_is_a_warning_that_says_how_to_fill_it():
    check = _by_name(doctor.run_checks())["index"]

    assert check["status"] == "warn" and "griot index all" in check["fix"]


def test_an_index_held_by_another_process_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda **k: {"points_count": None, "points_error": "busy",
                                                                 "last_indexed": None, "repositories": []})

    check = _by_name(doctor.run_checks())["index"]

    assert check["status"] == "ok" and "another process" in check["detail"]


def test_an_index_that_cannot_be_read_is_a_failure(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda **k: {"points_count": None, "points_error": "unreadable: boom",
                                                                 "last_indexed": None, "repositories": []})

    assert _by_name(doctor.run_checks())["index"]["status"] == "FAIL"


def test_a_last_run_that_died_is_a_warning(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda **k: {
        "points_count": 12, "points_error": None, "repositories": [],
        "last_indexed": {"timestamp": "2026-10-01T00:00:00+00:00", "error": "RuntimeError: x"}})

    check = _by_name(doctor.run_checks())["index"]

    assert check["status"] == "warn" and "did not finish" in check["detail"]


def test_a_last_run_that_counted_and_failed_is_not_said_to_have_died(monkeypatch):
    """A run with counts and an error finished: the platform refused every
    fetch, say. "Did not finish" would send someone looking for a crash."""
    monkeypatch.setattr(common, "get_index_status", lambda **k: {
        "points_count": 12, "points_error": None, "repositories": [],
        "last_indexed": {"timestamp": "2026-10-01T00:00:00+00:00", "indexed": 0, "failed": 3,
                         "error": "the platform refused every fetch (HTTP 401)"}})

    check = _by_name(doctor.run_checks())["index"]

    assert check["status"] == "warn" and "did not finish" not in check["detail"]
    assert "failed" in check["detail"] and "refused every fetch" in check["detail"]


def test_no_registered_repository_is_a_warning_that_says_how_to_register_one(monkeypatch):
    monkeypatch.setattr(common, "load_repos", lambda: [])

    check = _by_name(doctor.run_checks())["repositories"]

    assert check["status"] == "warn" and "griot repos add" in check["fix"]


def test_a_registered_repository_that_is_gone_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "load_repos", lambda: [str(tmp_path / "gone")])

    check = _by_name(doctor.run_checks())["repositories"]

    assert check["status"] == "FAIL" and "gone" in check["detail"] and "griot repos remove" in check["fix"]


def test_repositories_behind_their_index_are_a_warning(tmp_path, monkeypatch):
    from griot import freshness

    repo = tmp_path / "one"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo)])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [
        {"repo": "one", "path": str(repo), "head": "a" * 40, "behind": True, "commits_behind": 4,
         "behind_sources": ["code"], "missing_sources": [], "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}}])

    check = _by_name(doctor.run_checks())["repositories"]

    assert check["status"] == "warn" and "one" in check["detail"] and "4 commits" in check["detail"]
    assert "griot index all" in check["fix"]


def test_a_repository_the_platform_refused_is_a_warning(tmp_path, monkeypatch):
    """[debt 17 follow-up] The last platform run indexed the others and
    could fetch nothing of this one: the doctor names it."""
    from griot import freshness

    repos = []
    for name in ("good", "bad\x1b[2J"):
        repo = tmp_path / name
        repo.mkdir()
        repos.append(repo)
    monkeypatch.setattr(common, "load_repos", lambda: [str(r) for r in repos])
    monkeypatch.setattr(freshness, "repository_freshness", lambda *a, **k: [
        {"repo": r.name, "path": str(r), "head": "a" * 40, "behind": False, "commits_behind": 0,
         "behind_sources": [], "missing_sources": [], "platform_refused": r.name != "good",
         "last_indexed_at": "2026-10-01T00:00:00+00:00", "sources": {}} for r in repos])

    check = _by_name(doctor.run_checks())["repositories"]

    assert check["status"] == "warn" and "refused" in check["detail"]
    assert "bad?[2J" in check["detail"] and "good" not in check["detail"]
    assert "griot auth list" in check["fix"]


def test_spend_at_the_ceiling_is_a_warning(monkeypatch):
    monkeypatch.setattr(common, "get_spend_today", lambda: 3.0)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)

    check = _by_name(doctor.run_checks())["spend"]

    assert check["status"] == "warn" and "ceiling" in check["detail"]


def test_spend_below_the_ceiling_is_fine(monkeypatch):
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.2)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)

    check = _by_name(doctor.run_checks())["spend"]

    assert check["status"] == "ok" and "0.20" in check["detail"] and "3.00" in check["detail"]


# --- the harness --------------------------------------------------------------------------------------


ALL_HARNESSES = list(harnesses.HARNESSES)  # before the autouse fixture empties the list
CLAUDE_CODE = next(h for h in ALL_HARNESSES if h.id == "claude-code")


@pytest.fixture
def claude_code(monkeypatch):
    """Claude Code, present on this machine whatever the machine has."""
    import dataclasses

    harness = dataclasses.replace(CLAUDE_CODE, detect=lambda: True)
    monkeypatch.setattr(harnesses, "HARNESSES", [harness])
    return harness


def test_a_harness_that_is_not_there_is_skipped(monkeypatch):
    monkeypatch.setattr(harnesses, "HARNESSES", [])

    checks = _by_name(doctor.run_checks())

    assert checks["mcp registration"]["status"] == "skip" and checks["tool approval"]["status"] == "skip"


def test_a_server_not_registered_is_a_warning(monkeypatch, claude_code):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {})

    check = _by_name(doctor.run_checks())["mcp registration"]

    assert check["status"] == "warn" and "griot assist install" in check["fix"]


def test_a_harness_that_could_not_be_asked_is_skipped_not_failed(monkeypatch, claude_code):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: None)

    assert _by_name(doctor.run_checks())["mcp registration"]["status"] == "skip"


def test_a_registration_whose_command_is_gone_is_a_failure(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration",
                        lambda harness: {"scope": "user", "command": str(tmp_path / "old" / "griot"), "args": "mcp"})

    check = _by_name(doctor.run_checks())["mcp registration"]

    assert check["status"] == "FAIL" and "griot assist install" in check["fix"]


def test_a_registration_that_runs_this_griot_is_fine(monkeypatch, claude_code):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})

    check = _by_name(doctor.run_checks())["mcp registration"]

    assert check["status"] == "ok" and "user" in check["detail"]


def test_read_only_tools_that_still_ask_are_a_warning(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": []}}))

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert check["status"] == "warn" and "ask" in check["detail"] and "griot assist install" in check["fix"]


def test_read_only_tools_all_allowed_is_fine(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": harnesses.tool_rules(claude_code)}}))

    assert _by_name(doctor.run_checks(home=tmp_path))["tool approval"]["status"] == "ok"


def test_a_settings_file_that_cannot_be_used_is_a_warning_not_an_ok(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert check["status"] == "warn" and "settings.json" in check["detail"] and "not valid JSON" in check["detail"]


def test_a_harness_without_a_settings_file_griot_knows_is_said_so(monkeypatch):
    import dataclasses

    opencode = next(h for h in ALL_HARNESSES if h.id == "opencode")
    monkeypatch.setattr(harnesses, "HARNESSES", [dataclasses.replace(opencode, detect=lambda: True)])

    check = _by_name(doctor.run_checks())["tool approval"]

    assert check["status"] == "skip" and "opencode" in check["detail"] and "no supported agent harness" not in check["detail"]


def test_a_harness_without_a_settings_file_is_named_beside_one_that_has(monkeypatch, tmp_path):
    import dataclasses

    both = [dataclasses.replace(h, detect=lambda: True) for h in ALL_HARNESSES if h.id in ("claude-code", "opencode")]
    monkeypatch.setattr(harnesses, "HARNESSES", both)
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": harnesses.tool_rules(both[0])}}))

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert check["status"] == "ok" and "opencode" in check["detail"] and "no settings file" in check["detail"]


def test_a_settings_file_that_cannot_be_used_is_not_fixed_by_the_installer(monkeypatch, claude_code, tmp_path):
    """`griot assist install` refuses such a file too: the fix is the file."""
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert "fix the file" in check["fix"] and "then" in check["fix"]


def test_a_harness_directory_that_cannot_be_used_is_a_warning_with_the_reason(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/dir")

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert check["status"] == "warn" and "absolute" in check["detail"] and "CLAUDE_CONFIG_DIR" in check["fix"]


def test_a_rule_the_person_denied_is_their_decision_not_a_warning(monkeypatch, claude_code, tmp_path):
    monkeypatch.setattr(harnesses, "mcp_registration", lambda harness: {"scope": "user", "command": "griot", "args": "mcp"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"deny": ["mcp__griot"]}}))

    check = _by_name(doctor.run_checks(home=tmp_path))["tool approval"]

    assert check["status"] == "ok" and "deny" in check["detail"]


# --- what a server would make of this environment --------------------------------------------------


def test_a_variable_a_server_would_ignore_is_a_warning(monkeypatch):
    monkeypatch.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE", {"GRIOT_MCP_ENABLE_INDEX": "true"})

    check = _by_name(doctor.run_checks())["server environment"]

    assert check["status"] == "warn" and "GRIOT_MCP_ENABLE_INDEX" in check["detail"] and "griot config set" in check["fix"]


def test_an_environment_a_server_would_obey_is_fine(monkeypatch):
    monkeypatch.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE", {"GRIOT_MCP_CONCURRENCY_MODE": "single"})

    assert _by_name(doctor.run_checks())["server environment"]["status"] == "ok"


# --- the tools griot itself needs -----------------------------------------------------------------


def test_git_is_found_and_its_version_said():
    check = _by_name(doctor.run_checks())["git"]

    assert check["status"] == "ok" and "git version" in check["detail"]


def test_git_missing_is_a_failure(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    assert _by_name(doctor.run_checks())["git"]["status"] == "FAIL"


def test_the_log_is_read():
    assert _by_name(doctor.run_checks())["log"]["status"] == "ok"


def test_a_log_directory_without_a_database_is_not_opened(monkeypatch):
    """Opening it would create the database."""
    from griot import logdb

    common.secure_mkdir(common.LOG_DIR)
    (common.LOG_DIR / logdb.DB_FILENAME).unlink(missing_ok=True)

    check = _by_name(doctor.run_checks())["log"]

    assert check["status"] == "ok" and "no run yet" in check["detail"]
    assert not (common.LOG_DIR / logdb.DB_FILENAME).exists()


def test_a_log_that_cannot_be_read_is_a_failure(monkeypatch):
    from griot import logdb

    common.secure_mkdir(common.LOG_DIR)
    (common.LOG_DIR / logdb.DB_FILENAME).write_bytes(b"not a database")

    def broken(*a, **k):
        raise logdb.sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(logdb, "read_recent", broken)

    checks = _by_name(doctor.run_checks())

    assert checks["log"]["status"] == "FAIL"
    assert checks["index"]["status"] == "FAIL" and "DatabaseError" in checks["index"]["detail"], "reported, not a dead doctor"
    assert checks["git"]["status"] == "ok", "and the other checks still ran"


# --- the verdict ----------------------------------------------------------------------------------


def test_the_exit_status_is_one_only_for_a_failure():
    assert doctor.exit_status([{"status": "ok"}, {"status": "warn"}, {"status": "skip"}]) == 0
    assert doctor.exit_status([{"status": "ok"}, {"status": "FAIL"}]) == 1


def test_the_report_puts_the_status_first_and_the_fix_under_it():
    text = doctor.format_report([{"check": "profile", "status": "FAIL", "detail": "openai-small has no credential",
                                  "fix": "griot auth set openai"},
                                 {"check": "git", "status": "ok", "detail": "git version 2.54.0", "fix": None}])

    assert text.splitlines()[0].startswith("FAIL  profile") and "griot auth set openai" in text
    assert any(line.startswith("ok    git") for line in text.splitlines())
    assert "1 failure" in text


# --- an empty value the reader takes as its default ----------------------------------------
#
# [real bug, 2026-10-05] The .env griot writes on first use has
# GRIOT_MCP_INDEX_ROOTS= and GRIOT_GITEA_HOSTS= empty, which their readers
# take as "none". The settings check asked the validator of `griot config
# set`, which refuses an empty value, so every new installation read FAIL.
# The question the check asks is whether griot can start with the value.


@pytest.mark.parametrize("name", ["log-questions", "mcp-index", "mcp-index-roots", "gitea-hosts"])
def test_an_empty_value_the_reader_takes_as_its_default_is_not_a_failure(tmp_path, name):
    setting = config.find(name)
    env_path = tmp_path / ".env"
    env_path.write_text(f"{setting.variable}=\n")

    assert doctor._settings_in_file(env_path) == []


@pytest.mark.parametrize("name", ["spend-ceiling", "max-failed-batches", "mcp-concurrency", "groq-chat-model",
                                  "gitlab-api-base"])
def test_an_empty_value_the_reader_cannot_use_is_still_a_failure(tmp_path, name):
    setting = config.find(name)
    env_path = tmp_path / ".env"
    env_path.write_text(f"{setting.variable}=\n")

    assert [b["variable"] for b in doctor._settings_in_file(env_path)] == [setting.variable]


def test_the_file_griot_writes_on_first_use_passes_the_settings_check():
    common.ENV_PATH.unlink(missing_ok=True)
    common.ensure_env_template()

    assert doctor._settings_in_file(common.ENV_PATH) == []
