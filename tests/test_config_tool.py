"""`griot_config_list` shows the settings an MCP server is running with.

`griot config list` answers "what does griot's configuration say" for a new
process at a terminal. A server is another question: it read the file once,
when it started, and its environment can come from wherever it was
registered. So the tool says what THIS server runs with, where each value
came from, and whether the file has changed since: the three things someone
needs when a server does not behave as the file says it should."""

import json
import os
import subprocess
import sys

import pytest
from mcp.client.client import Client

from griot import common, config, mcp_server

# What a fresh server process answers, as JSON on its last line. `after_start`
# runs once the server module is loaded: the place to edit the file "later".
_SERVER = """
import asyncio, json, sys
from mcp.client.client import Client
from griot import common, mcp_server
{after_start}
async def main():
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_config_list", {{}})
        tools = [tool.name for tool in (await client.list_tools()).tools]
        print(json.dumps({{**result.structured_content, "tools": tools}}))
asyncio.run(main())
"""


def _server(tmp_path, *, file: str = "", after_start: str = "", **exported) -> dict:
    """The answer of a server started the way a real one is: the file is
    read when the process loads, and never again."""
    env_file = tmp_path / "config" / "griot" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(file)
    env_file.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"), **exported)
    done = subprocess.run([sys.executable, "-c", _SERVER.format(after_start=after_start)], env=env,
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    out["by_name"] = {entry["name"]: entry for entry in out["settings"]}
    return out


async def _list() -> dict:
    async with Client(mcp_server.mcp) as client:
        out = (await client.call_tool("griot_config_list", {})).structured_content
    out["by_name"] = {entry["name"]: entry for entry in out["settings"]}
    return out


# --- where each value comes from, in a server started for real ------------------------------


def test_each_setting_says_its_value_and_where_it_comes_from(tmp_path):
    out = _server(tmp_path, file="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=7\n", GRIOT_MCP_CONCURRENCY_MODE="single")
    assert set(out["by_name"]) == {setting.name for setting in config.SETTINGS}
    from_file, exported, untouched = (out["by_name"][n] for n in ("max-failed-batches", "mcp-concurrency", "chat-profile"))
    assert (from_file["value"], from_file["source"]) == ("7", "file")
    assert (exported["value"], exported["source"]) == ("single", "environment")
    assert (untouched["value"], untouched["source"]) == (untouched["default"], "default") and untouched["default"]
    assert exported["variable"] == "GRIOT_MCP_CONCURRENCY_MODE" and exported["description"]
    assert out["env_file"].endswith(".env")
    assert out["restart_needed"] is False and not any(e["restart_needed"] for e in out["settings"])


def test_the_environment_wins_over_the_file_and_the_file_is_still_shown(tmp_path):
    out = _server(tmp_path, file="GRIOT_MCP_CONCURRENCY_MODE=multi\n", GRIOT_MCP_CONCURRENCY_MODE="single")
    entry = out["by_name"]["mcp-concurrency"]
    assert (entry["value"], entry["source"], entry["in_file"]) == ("single", "environment", "multi")


def test_a_variable_exported_empty_is_in_force_as_empty(tmp_path):
    """The file does not override what the environment has, an empty value
    included: saying "file" here would name a value the server never read."""
    out = _server(tmp_path, file="GRIOT_LOG_QUESTIONS=true\n", GRIOT_LOG_QUESTIONS="")
    entry = out["by_name"]["log-questions"]
    assert (entry["value"], entry["source"], entry["in_file"]) == ("", "environment", "true")


# --- the file changed after the server started ----------------------------------------------

_REWRITE = "common.ENV_PATH.write_text({text!r})"


def test_a_file_changed_since_the_server_started_asks_for_a_restart(tmp_path):
    """The server keeps the value it started with. Reporting the file's new
    value as in force is the lie this tool exists to avoid."""
    out = _server(tmp_path, file="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=7\n",
                  after_start=_REWRITE.format(text="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=9\n"))
    entry = out["by_name"]["max-failed-batches"]
    assert (entry["value"], entry["source"], entry["in_file"]) == ("7", "file", "9")
    assert entry["restart_needed"] is True and out["restart_needed"] is True
    assert [e["name"] for e in out["settings"] if e["restart_needed"]] == ["max-failed-batches"]


def test_a_line_removed_since_the_start_asks_for_a_restart_too(tmp_path):
    out = _server(tmp_path, file="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=7\n", after_start=_REWRITE.format(text=""))
    entry = out["by_name"]["max-failed-batches"]
    assert (entry["value"], entry["source"], entry["in_file"]) == ("7", "file", None)
    assert entry["restart_needed"] is True


def test_a_line_added_since_the_start_asks_for_a_restart_too(tmp_path):
    out = _server(tmp_path, after_start=_REWRITE.format(text="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=9\n"))
    entry = out["by_name"]["max-failed-batches"]
    assert (entry["source"], entry["in_file"], entry["restart_needed"]) == ("default", "9", True)
    assert entry["value"] == entry["default"]


def test_a_file_changed_under_an_exported_variable_changes_nothing(tmp_path):
    """The environment wins before and after: a restart would not apply the
    file's value, so asking for one would be wrong."""
    out = _server(tmp_path, file="GRIOT_MCP_CONCURRENCY_MODE=multi\n", GRIOT_MCP_CONCURRENCY_MODE="single",
                  after_start=_REWRITE.format(text="GRIOT_MCP_CONCURRENCY_MODE=single\n"))
    assert out["by_name"]["mcp-concurrency"]["restart_needed"] is False and out["restart_needed"] is False


def test_a_line_rewritten_to_the_same_value_changes_nothing(tmp_path):
    out = _server(tmp_path, file="GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES=7\n",
                  after_start=_REWRITE.format(text="# a comment\nGRIOT_MAX_CONSECUTIVE_FAILED_BATCHES='7'\n"))
    assert out["restart_needed"] is False


def test_an_empty_line_removed_is_not_a_change_when_the_default_is_empty_too(tmp_path):
    """`GRIOT_PROJECT=` and no line at all are read the same way: asking for
    a restart would send someone to restart a server for nothing."""
    out = _server(tmp_path, file="GRIOT_PROJECT=\n", after_start=_REWRITE.format(text=""))
    entry = out["by_name"]["project"]
    assert entry["default"] is None and entry["restart_needed"] is False


def test_an_empty_line_removed_is_a_change_when_there_is_a_default(tmp_path):
    out = _server(tmp_path, file="GRIOT_CHAT_MODEL=\n", after_start=_REWRITE.format(text=""))
    entry = out["by_name"]["chat-model"]
    assert (entry["value"], entry["source"]) == ("", "file") and entry["default"]
    assert entry["restart_needed"] is True, "a new process would use the default, this one runs with nothing"


def test_an_empty_line_added_is_a_change_when_there_is_a_default(tmp_path):
    """`VAR=` is read as an empty value, not as the default: a new process
    would run with nothing where this one runs with the default."""
    out = _server(tmp_path, after_start=_REWRITE.format(text="GRIOT_CHAT_MODEL=\n"))
    entry = out["by_name"]["chat-model"]
    assert (entry["source"], entry["in_file"], entry["restart_needed"]) == ("default", "", True)


# --- the value shown is the value used ----------------------------------------------------------


@pytest.mark.parametrize("word", config._TRUE + config._FALSE + ("YES", " Off "))
def test_a_yes_or_no_is_read_the_way_the_command_that_sets_it_reads_it(word, monkeypatch):
    """`griot config set` takes yes/no/on/off and writes true/false, but the
    file is also edited by hand and the variable exported. The readers knew
    two spellings each: `GRIOT_LOG_QUESTIONS=no` kept logging questions, and
    the list above showed "no"."""
    setting = config.find("log-questions")
    monkeypatch.setenv("GRIOT_LOG_QUESTIONS", word)
    assert common.log_questions_enabled() is (config.normalized(setting, word) == "true")


@pytest.mark.parametrize("word,registered", [("yes", True), ("on", True), ("true", True), ("1", True),
                                             ("no", False), ("", False)])
def test_indexing_through_mcp_is_enabled_by_every_spelling_of_yes(tmp_path, word, registered):
    out = _server(tmp_path, GRIOT_MCP_ENABLE_INDEX=word)
    assert ("griot_index_repo" in out["tools"]) is registered
    assert out["by_name"]["mcp-index"]["value"] == word


# --- what it shows, and what it never shows -------------------------------------------------


@pytest.mark.anyio
async def test_it_agrees_with_the_profile_list_about_the_active_profile():
    out = await _list()
    async with Client(mcp_server.mcp) as client:
        active = (await client.call_tool("griot_profiles_list", {})).structured_content["active"]
    assert out["by_name"]["embed-profile"]["value"] == active


@pytest.mark.anyio
async def test_no_credential_is_a_setting():
    out = await _list()
    listed = {entry["variable"] for entry in out["settings"]}
    assert not listed & set(common.credential_env_vars())
    assert not [v for v in listed if v.endswith(("_TOKEN", "_API_KEY", "_KEY", "_SECRET", "_PASSWORD"))]


@pytest.mark.anyio
async def test_a_credential_written_inside_a_value_is_not_shown(monkeypatch):
    """A URL can carry one. The value is shown the way anything griot prints
    is: the credential-shaped part replaced."""
    monkeypatch.setitem(common.ENVIRONMENT_BEFORE_ENV_FILE, "GRIOT_GITLAB_API_BASE",
                        "https://deploy:not-a-real-password@git.example.com/api/v4")
    common.env_file_set("GRIOT_GITLAB_API_BASE", "https://deploy:another-fake-password@git.example.com/api/v4")
    entry = (await _list())["by_name"]["gitlab-api-base"]
    assert "not-a-real-password" not in json.dumps(entry) and "another-fake-password" not in json.dumps(entry)
    assert "git.example.com" in entry["value"] and "git.example.com" in entry["in_file"]
    assert "deploy" not in entry["value"], "the whole part before the host: a token can be the user"


@pytest.mark.parametrize("url", [
    "https://deploy:p@ss-not-real@git.example.com/api/v4",     # an @ inside the password
    "https://not-real-token@git.example.com/api/v4",           # a token as the user
    "https://a%40b:not-real@git.example.com/api/v4",
])
@pytest.mark.anyio
async def test_everything_before_the_host_is_replaced(monkeypatch, url):
    monkeypatch.setitem(common.ENVIRONMENT_BEFORE_ENV_FILE, "GRIOT_GITLAB_API_BASE", url)
    value = (await _list())["by_name"]["gitlab-api-base"]["value"]
    assert "not-real" not in value and "ss-" not in value and "deploy" not in value
    assert value.endswith("@git.example.com/api/v4")


@pytest.mark.anyio
async def test_a_value_cannot_carry_control_characters_into_the_answer(monkeypatch):
    monkeypatch.setitem(common.ENVIRONMENT_BEFORE_ENV_FILE, "GRIOT_PROJECT", "name\x1b[31m‮evil")
    value = (await _list())["by_name"]["project"]["value"]
    assert "\x1b" not in value and "‮" not in value and "name" in value


@pytest.mark.anyio
async def test_a_file_that_cannot_be_read_is_an_error_that_names_it(monkeypatch):
    def unreadable(setting):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(config, "_in_file", unreadable)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_config_list", {})
    assert result.is_error is True and common.ENV_PATH.name in str(result.content)


@pytest.mark.anyio
async def test_a_file_that_is_not_text_is_an_error_that_names_it_too():
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_bytes(b"GRIOT_PROJECT=\xff\xfe\n")
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_config_list", {})
    assert result.is_error is True and "Could not read" in str(result.content)


# --- the surface ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_is_read_only_and_says_how_a_setting_is_changed():
    async with Client(mcp_server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "griot_config_list")
        out = (await client.call_tool("griot_config_list", {})).structured_content
    assert tool.annotations.read_only_hint is True
    assert "griot config set" in out["note"] and "restart" in out["note"].lower()
    assert "--profile" in out["note"], "a profile given on the server's command line also reads as environment"
    assert "griot_config_list" in mcp_server.tools_safe_to_preapprove(), "it costs nothing and changes nothing"


@pytest.mark.anyio
async def test_no_mcp_tool_changes_a_setting():
    """Raising a ceiling, enabling indexing, pointing a token at a host: each
    is a question for a person at a terminal."""
    async with Client(mcp_server.mcp) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert [name for name in names if "config" in name] == ["griot_config_list"]


def test_the_installer_says_that_settings_are_among_what_the_rules_show(monkeypatch, tmp_path, capsys):
    """The question that offers to pre-approve the read-only tools lists
    what they show: this one is now among them."""
    from pathlib import Path

    from griot import harnesses
    claude = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    harnesses.offer_tool_approval(claude, "global", home=tmp_path)
    out = " ".join(capsys.readouterr().out.split())
    assert "mcp__griot__griot_config_list" in out and "settings" in out
