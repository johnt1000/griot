"""`griot profiles use <name>` makes an embedding profile the active one.

The active profile was a line in `<config>/.env` that only an editor could
change. Since the MCP server is registered once for every project, with no
environment of its own, that file is what decides the profile everywhere:
changing it should not take knowing where the file is and what the variable
is called."""

import os
import subprocess
import sys

import pytest
from dotenv import dotenv_values

from griot import cli, common


def _active_in_file() -> str | None:
    return dotenv_values(common.ENV_PATH).get("GRIOT_EMBED_PROFILE") if common.ENV_PATH.exists() else None


def _fresh(tmp_path, *argv, **extra) -> subprocess.CompletedProcess:
    """A new process, the way a real `griot` starts: the profile is read
    when the module loads, so only a fresh one shows what a later run sees."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"), **extra)
    return subprocess.run([sys.executable, "-m", "griot.cli", *argv], env=env, capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: True)
    asked = []

    def answer(text):
        monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or text)
        return asked

    return answer


@pytest.fixture
def no_question(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked: {prompt}"))


# --- a local profile: nothing leaves the machine, nothing to ask --------------------------


def test_a_local_profile_is_written_without_a_question(no_question, capsys):
    assert cli.main(["profiles", "use", "bge-small"]) == 0
    assert _active_in_file() == "bge-small"
    out = capsys.readouterr().out
    assert "bge-small" in out and str(common.ENV_PATH) in out


def test_the_next_run_uses_it(tmp_path):
    assert _fresh(tmp_path, "profiles", "use", "bge-small").returncode == 0
    listed = _fresh(tmp_path, "profiles", "list")
    active = [line for line in listed.stdout.splitlines() if "bge-small" in line]
    assert listed.returncode == 0 and active and "active" in active[0].lower(), listed.stdout


def test_it_says_what_to_do_next(no_question, capsys):
    cli.main(["profiles", "use", "bge-small"])
    out = " ".join(capsys.readouterr().out.split())
    assert "griot index all" in out, "a profile has its own index: nothing is there yet"
    assert "restart" in out.lower(), "a running MCP server keeps the profile it started with"


def test_an_index_that_already_exists_under_it_is_said(no_question, capsys, monkeypatch):
    monkeypatch.setattr(common, "collection_exists", lambda collection: collection == common.collection_name_for("bge-small"))
    cli.main(["profiles", "use", "bge-small"])
    out = " ".join(capsys.readouterr().out.split())
    # "A collection exists", not "it has an index": the check does not open
    # the collection, and one that exists can be empty.
    assert "collection already exists" in out and "griot index all" in out and "Nothing is indexed" not in out


def test_the_index_left_behind_is_mentioned_only_when_there_is_one(no_question, capsys, monkeypatch):
    """A fresh install has `jina-code` in the file and nothing indexed:
    telling the user how to delete an index that does not exist is noise."""
    cli.main(["profiles", "use", "bge-small"])
    assert "profiles delete" not in capsys.readouterr().out
    monkeypatch.setattr(common, "collection_exists", lambda collection: collection == common.collection_name_for("bge-small"))
    cli.main(["profiles", "use", "nomic-q"])
    out = " ".join(capsys.readouterr().out.split())
    assert "griot profiles delete bge-small" in out and "stays on disk" in out


def test_the_rest_of_the_file_is_kept(no_question):
    common.ensure_env_template()
    common.env_file_set("GRIOT_SPEND_CEILING_USD", "1.5")
    before = dotenv_values(common.ENV_PATH)
    cli.main(["profiles", "use", "bge-small"])
    after = dotenv_values(common.ENV_PATH)
    assert after.pop("GRIOT_EMBED_PROFILE") == "bge-small"
    before.pop("GRIOT_EMBED_PROFILE", None)
    assert after == before
    assert common.ENV_PATH.stat().st_mode & 0o777 == 0o600


def test_the_profile_already_active_is_not_written_again(no_question, capsys):
    cli.main(["profiles", "use", "bge-small"])
    stamp = common.ENV_PATH.stat().st_mtime_ns
    capsys.readouterr()
    assert cli.main(["profiles", "use", "bge-small"]) == 0
    assert common.ENV_PATH.stat().st_mtime_ns == stamp
    assert "already" in capsys.readouterr().out


# --- a name that is not a profile ----------------------------------------------------------


def test_an_unknown_name_is_an_error_that_lists_the_profiles(tmp_path):
    _fresh(tmp_path, "profiles", "use", "bge-small")
    done = _fresh(tmp_path, "profiles", "use", "no-such-profile")
    assert done.returncode == 2
    assert done.stderr.startswith("Error: ") and "no-such-profile" in done.stderr and "jina-code" in done.stderr
    assert "Traceback" not in done.stderr
    assert "bge-small" in _fresh(tmp_path, "profiles", "list").stdout and _fresh(tmp_path, "stats").returncode == 0


def test_an_unknown_name_is_refused_by_the_command_itself(no_question, capsys):
    """In a process where the configuration is already loaded (a test, a
    caller of cli.main), the start-up check does not run again."""
    assert cli.main(["profiles", "use", "no-such-profile"]) == 2
    assert "no-such-profile" in capsys.readouterr().err
    assert _active_in_file() != "no-such-profile"


def test_a_file_that_names_a_profile_that_does_not_exist_can_be_repaired_with_it(tmp_path):
    """The profile is validated when griot starts. A bad value in the file
    stopped every command, this one included, and with a traceback."""
    _fresh(tmp_path, "profiles", "use", "bge-small")
    env_path = tmp_path / "config" / "griot" / ".env"
    env_path.write_text(env_path.read_text().replace("bge-small", "left-over-from-an-old-version"))

    broken = _fresh(tmp_path, "stats")
    assert broken.returncode == 2
    assert broken.stderr.startswith("Error: ") and "left-over-from-an-old-version" in broken.stderr
    assert "Traceback" not in broken.stderr and "griot profiles use" in broken.stderr, "it says how to fix it"
    assert "unset it" in broken.stderr, "and what to do when the bad value is exported instead"

    assert _fresh(tmp_path, "profiles", "use", "bge-small").returncode == 0
    assert _fresh(tmp_path, "stats").returncode == 0


# --- a profile that calls an API: a person says yes ---------------------------------------


def test_a_paid_profile_asks_and_says_what_changes(terminal, capsys):
    asked = terminal("y")
    assert cli.main(["profiles", "use", "openai-small"]) == 0
    assert _active_in_file() == "openai-small"
    assert len(asked) == 1
    question = " ".join(asked[0].split())
    assert "openai-small" in question and "API" in question and "billed" in question


@pytest.mark.parametrize("answer", ["n", "", "maybe"])
def test_anything_but_a_yes_changes_nothing(terminal, answer):
    terminal(answer)
    cli.main(["profiles", "use", "bge-small"])
    assert cli.main(["profiles", "use", "openai-small"]) == 1
    assert _active_in_file() == "bge-small", "what was there stays"


def test_without_a_terminal_a_paid_profile_needs_the_flag(monkeypatch, capsys):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    cli.main(["profiles", "use", "bge-small"])
    assert cli.main(["profiles", "use", "openai-small"]) == 2
    assert _active_in_file() == "bge-small" and "--yes" in capsys.readouterr().err
    assert cli.main(["profiles", "use", "openai-small", "--yes"]) == 0
    assert _active_in_file() == "openai-small"


def test_a_paid_profile_already_active_does_not_ask_again(terminal, no_question):
    common.env_file_set("GRIOT_EMBED_PROFILE", "openai-small")
    assert cli.main(["profiles", "use", "openai-small"]) == 0


def test_a_missing_credential_is_said_with_the_command_that_sets_it(terminal, capsys, monkeypatch):
    monkeypatch.delenv("GRIOT_OPENAI_API_KEY", raising=False)
    terminal("y")
    cli.main(["profiles", "use", "openai-small"])
    out = " ".join(capsys.readouterr().out.split())
    assert "GRIOT_OPENAI_API_KEY" in out and "griot auth set openai" in out


def test_a_credential_that_is_there_is_not_mentioned(terminal, capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")
    terminal("y")
    cli.main(["profiles", "use", "openai-small"])
    out = capsys.readouterr().out
    assert "GRIOT_OPENAI_API_KEY" not in out and "not-a-real-key" not in out


# --- the environment wins over the file ----------------------------------------------------


def test_a_variable_set_in_the_environment_is_pointed_out(tmp_path):
    """The file is read without overriding what the environment already
    has, so with the variable exported the file's value is not the one
    used. Writing it and saying nothing would look like the command failed."""
    done = _fresh(tmp_path, "profiles", "use", "bge-small", GRIOT_EMBED_PROFILE="jina-code")
    out = " ".join(done.stdout.split())
    assert done.returncode == 0
    assert "environment" in out and "jina-code" in out and "wins" in out
    assert "bge-small" in (tmp_path / "config" / "griot" / ".env").read_text()


def test_a_variable_exported_with_a_name_that_is_no_profile_is_said_to_break_every_command(tmp_path):
    """The file gets fixed and the command succeeds, and the next command
    still fails, because the environment wins. Without this line the hint
    of that failure ("pick one with `griot profiles use`") goes in circles."""
    done = _fresh(tmp_path, "profiles", "use", "bge-small", GRIOT_EMBED_PROFILE="bogus")
    out = " ".join(done.stdout.split())
    assert done.returncode == 0 and "bogus" in out and "not a profile" in out and "unset" in out
    after = _fresh(tmp_path, "stats", GRIOT_EMBED_PROFILE="bogus")
    assert after.returncode == 2 and "environment" in after.stderr


@pytest.mark.parametrize("argv", [["profiles", "use", "jina-code", "--profile", "bge-small"],
                                  ["--profile", "bge-small", "profiles", "use", "jina-code"]])
def test_the_one_run_flag_is_refused_with_it(tmp_path, argv):
    """`--profile` picks a profile for one run by setting the same variable
    in the process. Taken together with this command it was read back as
    "exported in your environment", and the warning about that was false."""
    _fresh(tmp_path, "profiles", "use", "nomic-q")
    done = _fresh(tmp_path, *argv)
    assert done.returncode == 2 and "--profile" in done.stderr and "Traceback" not in done.stderr
    assert "nomic-q" in (tmp_path / "config" / "griot" / ".env").read_text(), "nothing was written"


def test_without_the_variable_in_the_environment_nothing_is_said_about_it(tmp_path):
    done = _fresh(tmp_path, "profiles", "use", "bge-small")
    assert done.returncode == 0 and "environment" not in done.stdout


# --- the surface -------------------------------------------------------------------------


def test_the_help_lists_the_action(capsys):
    with pytest.raises(SystemExit):
        cli.main(["profiles", "--help"])
    assert "use" in capsys.readouterr().out


def test_the_list_says_how_to_switch(capsys):
    cli.main(["profiles", "list"])
    assert "griot profiles use" in capsys.readouterr().out


@pytest.mark.anyio
async def test_no_mcp_tool_switches_the_profile():
    """Which profile is active decides where everything indexed is sent.
    That is the user's call, at a terminal."""
    from mcp.client.client import Client
    from griot import mcp_server
    async with Client(mcp_server.mcp) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert not [name for name in names if "use" in name.split("_") or "switch" in name]
