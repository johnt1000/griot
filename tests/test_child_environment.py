"""A griot process that griot starts sees the person's environment, not the
values griot's own settings file put into its parent's.

common.py loads <config>/.env into os.environ at import (load_dotenv), so a
child handed os.environ as it is reads every value the file holds as if the
person had exported it in their shell. Most of the time that only mislabels
where a value came from; for an MCP server it changes what the value MEANS
(see griot.ENVIRONMENT_ONLY_NARROWS: a server's environment may only narrow
what the file says, and the two are reported apart). A long-lived host also
hands on the file's value as it was at start, after the person edited it.

Each place griot starts griot is checked here: the index job and its dry
run, the quality-check job, the harness CLI that starts `griot mcp` to
check a registration (end to end, with a real server), and the upgraded
doctor (tests/test_update.py)."""

import hashlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

from griot import common, harnesses, jobs

# Taken before conftest's autouse fixture replaces it for every test: the
# harness test below runs the real seam with subprocess.run stood in for.
_REAL_RUN_HARNESS_COMMAND = harnesses._run_harness_command

FILE_ONLY = "GRIOT_TEST_ONLY_IN_THE_FILE"


def _digest(value):
    # As common.py keeps what the environment and the file said: a digest, so
    # that no second copy of a secret is held.
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


@pytest.fixture
def file_value(monkeypatch):
    """A variable the settings file holds and the person never exported,
    present in os.environ the way load_dotenv leaves it."""
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text(f"{FILE_ONLY}=from-the-file\n")
    monkeypatch.setenv(FILE_ONLY, "from-the-file")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE",
                        {k: v for k, v in common.EXPORTED_BEFORE_ENV_FILE.items() if k != FILE_ONLY})
    monkeypatch.setattr(common, "SET_BY_ENV_FILE", {FILE_ONLY: _digest("from-the-file")})


# === the rule =====================================================================================


def test_a_value_only_the_file_gave_is_left_for_the_child_to_read(file_value):
    env = common.environment_without_the_env_file()

    assert FILE_ONLY not in env
    assert env["GRIOT_CONFIG_DIR"] == os.environ["GRIOT_CONFIG_DIR"]
    assert env["GRIOT_DATA_DIR"] == os.environ["GRIOT_DATA_DIR"]


def test_what_the_file_gave_at_start_goes_even_after_the_file_was_edited(file_value):
    """A server runs for hours: the person changes the file, and its index
    job must read the new value, not the old one as if it were exported."""
    common.ENV_PATH.write_text(f"{FILE_ONLY}=edited-since\n")

    assert FILE_ONLY not in common.environment_without_the_env_file()


_EDIT_AFTER_START = """\
from griot import common
common.ENV_PATH.write_text("GRIOT_TEST_ONLY_IN_THE_FILE=edited-since\\nGRIOT_TEST_EXPORTED=edited-since\\n")
env = common.environment_without_the_env_file()
print("GRIOT_TEST_ONLY_IN_THE_FILE" in env, env.get("GRIOT_TEST_EXPORTED"))
"""


def test_what_a_real_process_took_from_its_file_at_start_is_known_as_the_file_s(tmp_path):
    """The record of what load_dotenv added, taken at import in a process of
    its own: the tests above stand in for it."""
    config = tmp_path / "own-config"
    (config / "griot").mkdir(parents=True)
    env_file = config / "griot" / ".env"
    env_file.write_text("GRIOT_TEST_ONLY_IN_THE_FILE=at-start\nGRIOT_TEST_EXPORTED=at-start\n")
    env_file.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(config), GRIOT_DATA_DIR=str(tmp_path / "own-data"),
               GRIOT_TEST_EXPORTED="exported", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    done = subprocess.run([sys.executable, "-c", _EDIT_AFTER_START], env=env, capture_output=True, text=True,
                          timeout=60)

    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "exported"]


def test_a_value_the_file_holds_now_goes_too(monkeypatch):
    """What a server put back from the file after ignoring a widening
    export (common._take_from_the_environment_only_what_narrows) is the
    file's word, not the environment's."""
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text(f"{FILE_ONLY}=narrow\n")
    monkeypatch.setenv(FILE_ONLY, "narrow")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE",
                        {**common.EXPORTED_BEFORE_ENV_FILE, FILE_ONLY: _digest("wide")})
    monkeypatch.setattr(common, "SET_BY_ENV_FILE", {})

    assert FILE_ONLY not in common.environment_without_the_env_file()


def test_an_exported_value_stays_even_where_the_file_says_the_same(monkeypatch):
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text(f"{FILE_ONLY}=same\n")
    monkeypatch.setenv(FILE_ONLY, "same")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE",
                        {**common.EXPORTED_BEFORE_ENV_FILE, FILE_ONLY: _digest("same")})
    monkeypatch.setattr(common, "SET_BY_ENV_FILE", {})

    assert common.environment_without_the_env_file()[FILE_ONLY] == "same"


def test_what_this_process_set_itself_stays(file_value, monkeypatch):
    """--quiet, --profile and the like: griot set them on purpose, for its
    children as much as for itself."""
    monkeypatch.setenv(FILE_ONLY, "set-by-griot")

    assert common.environment_without_the_env_file()[FILE_ONLY] == "set-by-griot"


def test_the_upgraded_doctor_is_given_the_same_rule():
    from griot import update

    assert not hasattr(update, "_environment_for_the_doctor"), "one copy of the rule, in common.py"


# === every place griot starts griot ===============================================================


def _registered_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "--allow-empty", "-q",
                    "-m", "c"], cwd=repo, check=True)
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo)])
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "LOG_DIR", tmp_path / "logs")
    return repo


def test_the_index_job(file_value, tmp_path, monkeypatch):
    repo = _registered_repo(tmp_path, monkeypatch)
    real_popen = jobs.subprocess.Popen
    captured = {}

    class Running:
        pid = 1

        def poll(self):
            return None

    def popen(argv, **kwargs):
        if argv[0] == "git":
            return real_popen(argv, **kwargs)
        captured.update(kwargs)
        return Running()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    monkeypatch.setattr(jobs.psutil, "Process", lambda pid: (_ for _ in ()).throw(jobs.psutil.Error()))

    assert jobs.start_index_job(str(repo), ["code"], release=lambda: None)["started"] is True
    env = captured["env"]
    assert FILE_ONLY not in env
    assert env[common.INDEX_PROGRESS_ENV] and env["GRIOT_DATA_DIR"] == os.environ["GRIOT_DATA_DIR"]


def test_the_dry_run(file_value, tmp_path, monkeypatch):
    repo = _registered_repo(tmp_path, monkeypatch)
    captured = {}

    def dry_run(argv, env, timeout):
        captured.update(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(jobs, "_run_dry_run", dry_run)

    assert jobs.run_index_preview(str(repo), ["code"], release=lambda: None)["ok"] is True
    assert FILE_ONLY not in captured
    assert captured["GRIOT_DRY_RUN_REPORT"] and captured["GRIOT_CONFIG_DIR"] == os.environ["GRIOT_CONFIG_DIR"]


def test_the_quality_check_job(file_value, monkeypatch):
    captured = {}

    def run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(jobs.subprocess, "run", run)

    jobs.run_quality_check_job(5)
    assert captured["env"] is not None and FILE_ONLY not in captured["env"]
    assert captured["env"]["GRIOT_CONFIG_DIR"] == os.environ["GRIOT_CONFIG_DIR"]


def test_the_harness_cli(file_value, monkeypatch):
    """`claude mcp get griot` starts the registered `griot mcp` to check it,
    and that server inherits what the harness CLI inherited."""
    captured = {}

    def run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(harnesses.subprocess, "run", run)

    _REAL_RUN_HARNESS_COMMAND(["claude", "mcp", "get", "griot"])
    assert captured["env"] is not None and FILE_ONLY not in captured["env"]
    assert captured["env"]["GRIOT_DATA_DIR"] == os.environ["GRIOT_DATA_DIR"]


# === end to end: the server a registration check starts ===========================================

# Stands in for `claude`: `claude mcp get griot` starts the registered server
# with the environment it was given, which is the point here. It asks the
# server where its settings came from and answers as a harness that knows of
# no server, so that the check reads nothing else into it.
_FAKE_HARNESS = """\
#!{python}
import json, os, subprocess, sys
server = subprocess.Popen([sys.executable, "-m", "griot.cli", "mcp"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
for message in (
        {{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {{
            "protocolVersion": "2025-06-18", "capabilities": {{}}, "clientInfo": {{"name": "t", "version": "0"}}}}}},
        {{"jsonrpc": "2.0", "method": "notifications/initialized"}},
        {{"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {{"name": "griot_config_list", "arguments": {{}}}}}}):
    server.stdin.write(json.dumps(message) + "\\n")
    server.stdin.flush()
answer = None
for line in server.stdout:
    try:
        decoded = json.loads(line)
    except ValueError:
        continue
    if decoded.get("id") == 2:
        answer = decoded
        break
server.kill()
_, stderr = server.communicate()
with open(os.environ["FAKE_HARNESS_REPORT"], "w") as f:
    json.dump({{"answer": answer, "stderr": stderr}}, f)
print("No MCP server named griot found.")
sys.exit(1)
"""

# `griot assist install` reaches harnesses.py through cli.py, which loads the
# configuration first (common.ensure_env_template()), and the MCP tool runs in
# a server that has loaded it: either way the file is in os.environ by then.
_DRIVER = """\
from griot import common, harnesses
claude = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
print(harnesses.mcp_registration(claude))
"""


def test_a_server_started_to_check_a_registration_reads_the_file_as_the_file(tmp_path):
    allowed = tmp_path / "code"
    allowed.mkdir()
    # Not tmp_path/config or tmp_path/data: conftest points this process there.
    config, data, project, bin_dir = (tmp_path / "e2e-config", tmp_path / "e2e-data", tmp_path / "project",
                                      tmp_path / "bin")
    for directory in (config / "griot", data, project, bin_dir):
        directory.mkdir(parents=True)
    env_file = config / "griot" / ".env"
    env_file.write_text(f"GRIOT_MCP_INDEX_ROOTS={allowed}\nGRIOT_SPEND_CEILING_USD=2.5\n")
    env_file.chmod(0o600)
    fake = bin_dir / "claude"
    fake.write_text(_FAKE_HARNESS.format(python=sys.executable))
    fake.chmod(0o755)
    report = tmp_path / "report.json"

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "CLAUDE_"))}
    env.update(GRIOT_CONFIG_DIR=str(config), GRIOT_DATA_DIR=str(data), FAKE_HARNESS_REPORT=str(report),
               PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
               PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    done = subprocess.run([sys.executable, "-c", _DRIVER], env=env, cwd=project, capture_output=True, text=True,
                          timeout=120)

    assert done.returncode == 0, done.stderr
    found = json.loads(report.read_text())
    assert found["answer"] is not None, found["stderr"]
    settings = {entry["name"]: entry for entry in found["answer"]["result"]["structuredContent"]["settings"]}
    assert (settings["mcp-index-roots"]["value"], settings["mcp-index-roots"]["source"]) == (str(allowed), "file")
    assert settings["spend-ceiling"]["source"] == "file"
