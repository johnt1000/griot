"""`griot assist install` can register griot's MCP server with the harness.

Registration used to be documented per project only, because one server held
the index for the whole time it ran. With the index released when idle,
several sessions share it, and a project without the server simply never uses
griot. So the installer offers it: for every project (user scope) at
`--scope global`, for this project only otherwise.

It is done by the harness's own command line, never by editing its config
file, and only after a typed yes (or `--mcp`). Without the harness CLI or
without a terminal, the exact command is printed instead."""

import shlex

import pytest

from griot import common, harnesses

CLAUDE = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
OPENCODE = next(h for h in harnesses.HARNESSES if h.id == "opencode")


class _Run:
    """Stands in for the harness command and records what would have been run."""

    def __init__(self, returncode=0, output=""):
        self.calls, self.returncode, self.output = [], returncode, output

    def __call__(self, argv):
        self.calls.append(list(argv))
        return type("Done", (), {"returncode": self.returncode, "stdout": self.output, "stderr": ""})()


@pytest.fixture
def env(monkeypatch):
    run = _Run()
    monkeypatch.setattr(harnesses, "_run_harness_command", run)
    monkeypatch.setattr(harnesses, "_which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(harnesses, "_griot_command", lambda: "/opt/tools/griot")
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["jina-code"])
    return run


def _answer(monkeypatch, text):
    shown = []
    monkeypatch.setattr("builtins.input", lambda prompt="": shown.append(prompt) or text)
    return shown


# --- what is run ---------------------------------------------------------------


def test_global_scope_registers_for_every_project_with_an_absolute_command(env, monkeypatch):
    _answer(monkeypatch, "y")
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "registered"
    assert env.calls == [["claude", "mcp", "add", "--scope", "user", "griot", "--", "/opt/tools/griot", "mcp"]]


def test_local_scope_registers_for_this_project_only_and_privately(env, monkeypatch):
    """Claude Code's `local` scope, not `project`: the latter is a shared,
    committed `.mcp.json`, and a machine-specific path does not belong there."""
    _answer(monkeypatch, "yes")
    assert harnesses.offer_mcp_server(CLAUDE, "local") == "registered"
    assert env.calls[0][:5] == ["claude", "mcp", "add", "--scope", "local"]


@pytest.mark.parametrize("answer", ["", "n", "no", "sim", "yep"])
def test_anything_but_a_typed_yes_registers_nothing(env, monkeypatch, answer):
    _answer(monkeypatch, answer)
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "declined"
    assert env.calls == []


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
def test_an_interrupted_question_registers_nothing(env, monkeypatch, error):
    def raises(prompt=""):
        raise error

    monkeypatch.setattr("builtins.input", raises)
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "declined"
    assert env.calls == []


# --- what the person is told before answering ---------------------------------------


def test_the_question_shows_the_exact_command_and_what_it_means(env, monkeypatch, capsys):
    shown = _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out + "".join(shown)
    assert "claude mcp add --scope user griot -- /opt/tools/griot mcp" in out
    assert "every project" in out
    assert "its own griot server" in out
    assert shown[-1].rstrip().endswith("[y/N]")


def test_the_local_question_does_not_talk_about_every_project(env, monkeypatch, capsys):
    shown = _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "local")
    out = capsys.readouterr().out + "".join(shown)
    assert "this project only" in out and "every project" not in out


def test_single_mode_is_called_out_before_registering_for_every_project(env, monkeypatch, capsys):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out
    assert "single" in out and "block each other" in out
    assert "In your griot configuration" in out, "it knows its own .env, not what a harness or a project file sets"


def test_multi_mode_says_nothing_about_blocking(env, monkeypatch, capsys):
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    assert "block each other" not in capsys.readouterr().out


# --- when it cannot or should not ask --------------------------------------------------


def test_without_a_terminal_the_command_is_printed_and_nothing_runs(env, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "not-interactive"
    assert env.calls == []
    assert "claude mcp add --scope user griot -- /opt/tools/griot mcp" in capsys.readouterr().out


def test_without_the_harness_cli_the_command_is_printed_and_nothing_runs(env, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_which", lambda name: None)
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "no-cli"
    assert env.calls == []
    assert "claude mcp add" in capsys.readouterr().out


def test_a_harness_griot_cannot_register_with_is_left_to_the_person(env, capsys):
    assert harnesses.offer_mcp_server(OPENCODE, "global") == "n/a"
    assert env.calls == []
    out = capsys.readouterr().out
    assert "opencode" in out and "/opt/tools/griot mcp" in out


def test_ask_false_skips_it_silently(env, capsys):
    assert harnesses.offer_mcp_server(CLAUDE, "global", mode="no") == "skipped"
    assert env.calls == [] and capsys.readouterr().out == ""


def test_the_flag_registers_without_asking_even_without_a_terminal(env, monkeypatch):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although told to go ahead"))
    assert harnesses.offer_mcp_server(CLAUDE, "global", mode="yes") == "registered"
    assert len(env.calls) == 1


# --- what the harness answers -------------------------------------------------------------


def test_an_existing_registration_is_reported_as_such_not_as_a_failure(env, monkeypatch, capsys):
    env.returncode, env.output = 1, "MCP server griot already exists in user config"
    _answer(monkeypatch, "y")
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "exists"
    assert "already registered" in capsys.readouterr().out


def test_a_failed_registration_shows_what_the_harness_said_and_the_command(env, monkeypatch, capsys):
    env.returncode, env.output = 2, "something went wrong"
    _answer(monkeypatch, "y")
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "failed"
    out = capsys.readouterr().out
    assert "something went wrong" in out and "claude mcp add" in out


def test_a_missing_executable_at_run_time_is_a_failure_not_a_crash(env, monkeypatch, capsys):
    def boom(argv):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(harnesses, "_run_harness_command", boom)
    _answer(monkeypatch, "y")
    assert harnesses.offer_mcp_server(CLAUDE, "global") == "failed"


# --- the command that is registered ------------------------------------------------------


def test_a_griot_path_with_spaces_is_shown_so_that_it_can_be_pasted(env, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_griot_command", lambda: "/Users/some one/bin/griot")
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    harnesses.offer_mcp_server(CLAUDE, "global")
    line = next(l for l in capsys.readouterr().out.splitlines() if "claude mcp add" in l)
    assert shlex.split(line.strip()) == ["claude", "mcp", "add", "--scope", "user", "griot", "--", "/Users/some one/bin/griot", "mcp"]


def test_the_running_griot_is_registered_by_its_own_absolute_path(monkeypatch, tmp_path):
    binary = tmp_path / "bin" / "griot"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(harnesses.sys, "argv", [str(binary), "assist", "install"])
    assert harnesses._griot_command() == str(binary)


def test_run_another_way_it_falls_back_to_what_is_on_the_path(monkeypatch):
    monkeypatch.setattr(harnesses.sys, "argv", ["/somewhere/pytest"])
    monkeypatch.setattr(harnesses, "_which", lambda name: "/usr/local/bin/griot" if name == "griot" else None)
    assert harnesses._griot_command() == "/usr/local/bin/griot"


# --- through the command ---------------------------------------------------------------------


def test_install_offers_the_server_for_each_target(env, monkeypatch, tmp_path):
    monkeypatch.setattr(harnesses, "install", lambda harness, scope, home=None, cwd=None: {
        "harness": harness.id, "scope": scope, "skills_target": "s", "agents_target": "a",
        "created": [], "updated": [], "unchanged": []})
    monkeypatch.setattr(harnesses, "offer_instructions", lambda *a, **k: "n/a")
    monkeypatch.setattr(harnesses, "offer_tool_approval", lambda *a, **k: "n/a")  # tests/test_tool_approval.py
    _answer(monkeypatch, "y")
    assert harnesses.main(["install", "--scope", "global", "--harness", "claude-code"]) == 0
    assert len(env.calls) == 1


@pytest.mark.parametrize("flag,expected_calls", [("--no-mcp", 0), ("--mcp", 1)])
def test_the_flags_skip_or_answer_the_question(env, monkeypatch, flag, expected_calls):
    monkeypatch.setattr(harnesses, "install", lambda harness, scope, home=None, cwd=None: {
        "harness": harness.id, "scope": scope, "skills_target": "s", "agents_target": "a",
        "created": [], "updated": [], "unchanged": []})
    monkeypatch.setattr(harnesses, "offer_instructions", lambda *a, **k: "n/a")
    monkeypatch.setattr(harnesses, "offer_tool_approval", lambda *a, **k: "n/a")  # tests/test_tool_approval.py
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although a flag answered"))
    assert harnesses.main(["install", "--scope", "global", "--harness", "claude-code", flag]) == 0
    assert len(env.calls) == expected_calls


def test_the_two_flags_cannot_be_given_together():
    with pytest.raises(SystemExit):
        harnesses.main(["install", "--mcp", "--no-mcp"])


def test_the_mcp_tool_never_registers_a_server():
    """An agent installing skills must not also wire a server into every
    project: that is offered to a person at a terminal, like the global
    instructions block."""
    from pathlib import Path
    source = (Path(harnesses.__file__).parent / "mcp_server.py").read_text()
    assert "offer_mcp_server" not in source and "cmd_install" not in source


def test_the_suite_itself_cannot_run_a_harness_command():
    """The guard in conftest.py: outside the fixture above, running one fails."""
    with pytest.raises(AssertionError, match="for real"):
        harnesses._run_harness_command(["claude", "mcp", "list"])


# --- what it costs, said for the profile that is actually active --------------------------


def test_a_local_profile_is_told_its_own_memory_estimate(env, monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "bge-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["bge-small"])
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out
    low, high = common.EMBED_PROFILES["bge-small"]["rss_estimate_mb"]
    assert "bge-small" in out and f"{low} to {high} MB" in out and "estimate" in out


def test_a_paid_profile_is_told_no_model_is_loaded(env, monkeypatch, capsys):
    paid = next(name for name, profile in common.EMBED_PROFILES.items() if profile["backend"] != "local")
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", paid)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES[paid])
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out
    assert paid in out and "loads no model" in out and " MB" not in out


# --- a registration that would not work, or would stop working ---------------------------


def test_griot_that_cannot_be_found_is_never_registered_by_a_bare_name(env, monkeypatch, capsys):
    """Run as `python -m griot...` with no `griot` on the PATH: the only
    command left is the bare word, and a harness started from a desktop
    launcher would never find it. Registering that is worse than nothing."""
    monkeypatch.setattr(harnesses, "_griot_command", lambda: "griot")
    assert harnesses.offer_mcp_server(CLAUDE, "global", mode="yes") == "no-path"
    assert env.calls == []
    assert "absolute path" in capsys.readouterr().out


def test_a_griot_inside_a_virtual_environment_is_called_out(env, monkeypatch, capsys, tmp_path):
    """A checkout's .venv, `uvx`, `pipx run`: the path disappears with the
    environment and the registration silently stops working."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(harnesses.sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.setattr(harnesses, "_griot_command", lambda: str(venv_bin / "griot"))
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out
    assert "virtual environment" in out and "pipx" in out


def test_a_griot_installed_as_a_tool_gets_no_such_warning(env, monkeypatch, capsys):
    _answer(monkeypatch, "n")
    harnesses.offer_mcp_server(CLAUDE, "global")
    assert "virtual environment" not in capsys.readouterr().out


def test_a_relative_way_of_running_griot_is_registered_as_an_absolute_path(monkeypatch, tmp_path):
    (tmp_path / "griot").write_text("#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(harnesses.sys, "argv", ["./griot", "assist", "install"])
    command = harnesses._griot_command()
    assert command == str(tmp_path / "griot") or command == str((tmp_path / "griot").absolute())
    assert command.startswith("/")


# --- what it says afterwards -----------------------------------------------------------------


def test_after_registering_it_says_how_to_undo_exactly_that(env, monkeypatch, capsys):
    _answer(monkeypatch, "y")
    harnesses.offer_mcp_server(CLAUDE, "global")
    assert "claude mcp remove --scope user griot" in capsys.readouterr().out
    _answer(monkeypatch, "y")
    harnesses.offer_mcp_server(CLAUDE, "local")
    assert "claude mcp remove --scope local griot" in capsys.readouterr().out


def test_an_existing_registration_is_not_vouched_for(env, monkeypatch, capsys):
    """The harness only says a server of that name exists. It may point at a
    griot that was moved or removed."""
    env.returncode, env.output = 1, "MCP server griot already exists in user config"
    _answer(monkeypatch, "y")
    harnesses.offer_mcp_server(CLAUDE, "global")
    out = capsys.readouterr().out
    assert "was not checked" in out and "claude mcp remove --scope user griot" in out


# --- the exit status, for whoever asked with --mcp ------------------------------------------


def _main(monkeypatch, *argv):
    monkeypatch.setattr(harnesses, "install", lambda harness, scope, home=None, cwd=None: {
        "harness": harness.id, "scope": scope, "skills_target": "s", "agents_target": "a",
        "created": [], "updated": [], "unchanged": []})
    monkeypatch.setattr(harnesses, "offer_instructions", lambda *a, **k: "n/a")
    monkeypatch.setattr(harnesses, "offer_tool_approval", lambda *a, **k: "n/a")  # tests/test_tool_approval.py
    return harnesses.main(["install", "--harness", "claude-code", *argv])


def test_asked_with_the_flag_a_failed_registration_is_a_failed_command(env, monkeypatch):
    env.returncode, env.output = 3, "boom"
    assert _main(monkeypatch, "--mcp") == 1


def test_asked_with_the_flag_a_missing_harness_cli_is_a_failed_command(env, monkeypatch):
    monkeypatch.setattr(harnesses, "_which", lambda name: None)
    assert _main(monkeypatch, "--mcp") == 1


def test_asked_with_the_flag_an_unfindable_griot_is_a_failed_command(env, monkeypatch):
    monkeypatch.setattr(harnesses, "_griot_command", lambda: "griot")
    assert _main(monkeypatch, "--mcp") == 1


@pytest.mark.parametrize("returncode,output", [(0, ""), (1, "MCP server griot already exists in user config")])
def test_asked_with_the_flag_registered_or_already_there_is_a_success(env, monkeypatch, returncode, output):
    env.returncode, env.output = returncode, output
    assert _main(monkeypatch, "--mcp") == 0


def test_without_the_flag_a_failed_registration_does_not_fail_the_install(env, monkeypatch):
    """The skills were installed. The offer is a courtesy on top."""
    env.returncode, env.output = 3, "boom"
    _answer(monkeypatch, "y")
    assert _main(monkeypatch) == 0


def test_with_every_harness_the_flag_registers_where_it_can_and_explains_elsewhere(env, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "install", lambda harness, scope, home=None, cwd=None: {
        "harness": harness.id, "scope": scope, "skills_target": "s", "agents_target": "a",
        "created": [], "updated": [], "unchanged": []})
    monkeypatch.setattr(harnesses, "offer_instructions", lambda *a, **k: "n/a")
    monkeypatch.setattr(harnesses, "offer_tool_approval", lambda *a, **k: "n/a")  # tests/test_tool_approval.py
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: [CLAUDE, OPENCODE])
    assert harnesses.main(["install", "--scope", "global", "--mcp"]) == 0
    assert len(env.calls) == 1 and "opencode" in capsys.readouterr().out
