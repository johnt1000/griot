"""A text that sends its reader somewhere is true of where it sends them.

griot-interaction-policy, section 2: never promise what the text cannot
guarantee. Two texts did: the golden-set note for an unreadable collection
config sent the reader to `griot doctor`, which said nothing about it; and a
stopped `griot index all` said "Its error is above" to the agent of
griot_index_preview, which received only the last lines, without the error
or its command. These tests read each text where its reader reads it."""

import os
import shlex
import subprocess

import pytest

from griot import cli, common, doctor, golden_set, jobs, mcp_server, repos


# --- the unreadable config: the note's command shows it ------------------------------------------


def _unreadable_config(monkeypatch, collection="codebase__x"):
    """A collection whose config file is on disk and is not JSON, with a
    status read that counted its points (another read path opened it)."""
    path = common.collection_path(collection)
    path.mkdir(parents=True, exist_ok=True)
    (path / common.EDGE_CONFIG_MARKER).write_text("{not json")
    monkeypatch.setattr(common, "get_index_status", lambda **kw: {
        "points_count": 42, "collection": collection, "keyword_search": None, "last_indexed": None})
    return path


def test_doctor_says_the_config_it_could_not_read(monkeypatch):
    path = _unreadable_config(monkeypatch)
    row = doctor.check_index(common)
    assert row["status"] == doctor.WARN, row
    assert "the config of collection 'codebase__x' could not be read" in row["detail"], row
    assert str(path / common.EDGE_CONFIG_MARKER) in row["detail"], row
    # Unknown is not "missing": nobody is sent to rebuild a collection that may be fine.
    assert "griot index keywords" not in (row["fix"] or "")


def test_doctor_says_it_while_another_process_holds_the_collection(monkeypatch):
    _unreadable_config(monkeypatch)
    monkeypatch.setattr(common, "get_index_status", lambda **kw: {
        "points_count": None, "points_error": "busy", "collection": "codebase__x", "keyword_search": None,
        "last_indexed": None})
    row = doctor.check_index(common)
    assert row["status"] == doctor.WARN and "could not be read" in row["detail"], row


def test_the_golden_set_note_sends_the_reader_where_the_cause_is_said(monkeypatch):
    _unreadable_config(monkeypatch)
    note = golden_set.unreadable_config_note("codebase__x")
    assert note.endswith("\n  griot doctor"), note
    row = doctor.check_index(common)
    assert "the config of collection 'codebase__x' could not be read" in row["detail"]


@pytest.mark.anyio
async def test_golden_set_add_hands_the_agent_the_note_and_doctor_shows_the_cause(monkeypatch):
    from mcp.client.client import Client

    path = common.collection_path(common.COLLECTION_NAME)
    path.mkdir(parents=True, exist_ok=True)
    (path / common.EDGE_CONFIG_MARKER).write_text("{not json")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_golden_set_add", {
            "query": "acquire_lock timeout", "must_include": [{"repo": "app"}], "confirm": True})
    message = result.structured_content["message"]
    assert f"the config of collection '{common.COLLECTION_NAME}' could not be read" in message, message
    assert message.endswith("\n  griot doctor"), message
    # Unopened, the collection fails to load on the same file (the FAIL row);
    # held by another process, the row warns of the config (tests above).
    # Either way doctor names the file the note is about.
    row = doctor.check_index(common)
    assert row["status"] in (doctor.WARN, doctor.FAIL), row
    assert common.EDGE_CONFIG_MARKER in row["detail"], row


# --- a stopped index all, as the agent of griot_index_preview receives it ------------------------


def test_a_dry_run_resume_does_not_speak_of_skipping_what_is_indexed():
    text = cli._stopped_at("platform", ["platform", "tags"], ["--dry-run", "--repo", "app"], "failed (exit 1)")
    assert "already indexed is skipped" not in text, text
    assert shlex.split(text.splitlines()[-1].strip())[:3] == ["griot", "index", "all"]


def test_a_real_resume_still_says_what_is_skipped():
    text = cli._stopped_at("platform", ["platform", "tags"], ["--repo", "app"], "failed (exit 1)")
    assert "already indexed is skipped" in text


def test_the_reason_keeps_the_failed_source_s_error_and_its_commands():
    """The agent reads the reason as its whole account of the failure: the
    failed source's error, its command on a line of its own, and the resume
    last, never one source's command glued in front of the next line."""
    stderr = ("=== griot index platform ===\n"
              "Platform: github for app.\n"
              "GITHUB_TOKEN is not set, so nothing was asked. A credential is set in a terminal; then index the "
              "platform again:\n  griot auth set github\n  griot index platform\n"
              "Error: 'griot index platform' failed (exit 1). tags did not run. Its error is above. Once it is "
              "fixed, run them again:\n  griot index all --dry-run --repo app --sources platform,tags\n")
    reason = jobs._failure_reason(stderr)
    assert "GITHUB_TOKEN is not set" in reason and "\n  griot auth set github\n" in reason, reason
    assert "griot index platform Error" not in reason, reason
    assert reason.endswith("\n  griot index all --dry-run --repo app --sources platform,tags"), reason
    assert "===" not in reason


def test_the_reason_drops_what_came_before_the_failed_source():
    stderr = ("=== griot index code ===\nRead 3 files.\n=== griot index platform ===\nboom\n"
              "Error: 'griot index platform' failed (exit 1). Its error is above. Once it is fixed, run it again:\n"
              "  griot index all --sources platform\n")
    reason = jobs._failure_reason(stderr)
    assert "Read 3 files" not in reason and reason.startswith("boom"), reason


def test_a_long_reason_keeps_its_end_and_stays_under_the_agent_limit():
    stderr = "=== griot index code ===\n" + "".join(f"line {i} " + "x" * 80 + "\n" for i in range(100)) + \
             "Error: stopped.\n  griot index all --sources code\n"
    reason = jobs._failure_reason(stderr)
    assert len(reason) <= jobs.REASON_LIMIT <= 2000
    assert reason.endswith("\n  griot index all --sources code")
    assert reason.splitlines()[0].startswith("line "), "cut at a line, not inside one"


@pytest.fixture
def github_repo(git_repo):
    git_repo.commit("first", filename="a.py", content="def a():\n    return 1\n")
    subprocess.run(["git", "-C", str(git_repo.path), "remote", "add", "origin", "https://github.com/you/app.git"],
                   check=True)
    repos.add_repo(str(git_repo.path))
    return git_repo


@pytest.mark.anyio
async def test_a_preview_stopped_by_a_missing_token_reads_with_its_cause_and_steps(github_repo, monkeypatch):
    from mcp.client.client import Client

    for name in [k for k in os.environ if k.endswith("_TOKEN") or k.startswith("GRIOT_GITHUB")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_preview", {"path": str(github_repo.path),
                                                                "sources": ["platform", "tags"]})
    reason = result.structured_content["reason"]
    assert "GITHUB_TOKEN is not set" in reason, reason
    assert "\n  griot auth set github\n" in reason, reason
    assert "already indexed is skipped" not in reason, reason
    assert shlex.split(reason.splitlines()[-1].strip())[:4] == ["griot", "index", "all", "--dry-run"], reason
    assert len(reason) <= 2000


# --- an unknown profile: doctor says where it is set, as the CLI does -----------------------------


def _unknown_profile(exported: bool):
    from griot import UnknownEmbedProfile
    fix = "unset GRIOT_EMBED_PROFILE" if exported else "griot profiles use jina-code"
    return UnknownEmbedProfile("Unknown", "bogus", ["jina-code", "gemini"], "", fix, exported=exported)


def test_an_unknown_profile_exported_is_said_to_be_exported(tmp_path):
    env = tmp_path / ".env"
    env.write_text("")
    row = doctor.check_settings(env, _unknown_profile(True))
    assert "exported in this environment, which wins over the file" in row["detail"], row
    assert str(env) not in row["detail"] and "or in" not in row["detail"], row
    assert row["fix"] == "unset GRIOT_EMBED_PROFILE"


def test_an_unknown_profile_in_the_file_is_not_told_to_unset(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GRIOT_EMBED_PROFILE=bogus\n")
    row = doctor.check_settings(env, _unknown_profile(False))
    assert f"in {env}" in row["detail"] and "unset" not in row["detail"], row
    assert "set in the environment, or" not in row["detail"], row
    assert row["fix"] == "griot profiles use jina-code"


def test_an_unknown_profile_beside_broken_lines_keeps_the_set_a_valid_value_tail(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GRIOT_SPEND_CEILING_USD=lots\n")
    row = doctor.check_settings(env, _unknown_profile(True))
    assert "GRIOT_SPEND_CEILING_USD" in row["detail"], row
    assert row["detail"].endswith("; or set a valid value with griot config set NAME VALUE"), row
    assert row["fix"] == "unset GRIOT_EMBED_PROFILE\ngriot config unset spend-ceiling", row


# --- a job that exits at once: the terminal command runs under the server's profile ----------------


@pytest.mark.parametrize("profile", ["gemini", None])
def test_the_terminal_command_of_a_job_that_exited_keeps_the_server_s_profile(monkeypatch, tmp_path, profile):
    import griot

    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setattr(jobs, "running_index_job", lambda: None)
    monkeypatch.setattr(common, "release_client", lambda: None)
    monkeypatch.setattr(common, "LOG_DIR", tmp_path)
    monkeypatch.setattr(griot, "PROFILE_FROM_COMMAND_LINE", profile)

    class Exited:
        pid, returncode = 1, 2

        def poll(self):
            return 2

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda cmd, **kw: Exited())
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    result = jobs.start_index_job(None, sources=["code"])
    assert result["started"] is False
    last = shlex.split(result["reason"].splitlines()[-1])
    expected = ["griot", "index", "all", "--sources", "code"] + (["--profile", profile] if profile else [])
    assert last == expected, result["reason"]


# --- the server-environment row says only the steps its fixes are ---------------------------------


def test_the_server_environment_detail_names_unset_only_for_a_value_that_is_not_valid(monkeypatch):
    monkeypatch.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE",
                        {"GRIOT_EMBED_PROFILE": "gemini", "GRIOT_SPEND_CEILING_USD": "100"})
    row = doctor.check_server_environment(common)
    assert row["fix"] == "griot profiles use gemini\ngriot config set spend-ceiling 100", row
    assert "unset" not in row["detail"], row
    assert "griot profiles use" in row["detail"]


def test_the_server_environment_detail_names_profiles_use_only_for_the_profile(monkeypatch):
    monkeypatch.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE", {"GRIOT_SPEND_CEILING_USD": "100"})
    row = doctor.check_server_environment(common)
    assert "profiles use" not in row["detail"] and "unset" not in row["detail"], row


def test_the_server_environment_detail_names_unset_for_a_value_that_is_not_valid(monkeypatch):
    monkeypatch.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE", {"GRIOT_MCP_INDEX_ROOTS": "/Users/you/a b"})
    row = doctor.check_server_environment(common)
    assert row["fix"] == "unset GRIOT_MCP_INDEX_ROOTS"
    assert "unset a value that is not valid" in row["detail"], row
