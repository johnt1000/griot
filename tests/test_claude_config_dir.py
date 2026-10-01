"""Claude Code keeps its user files where CLAUDE_CONFIG_DIR says, and in
`~/.claude` only when the variable is not set. The installer wrote to
`~/.claude` regardless: with the variable set, the skills, the agent, the
instructions block and the tool rules all went to a directory the harness
does not read, and the install reported success."""

import json
import os
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import harnesses, mcp_server


@pytest.fixture
def claude():
    return next(h for h in harnesses.HARNESSES if h.id == "claude-code")


@pytest.fixture
def relocated(tmp_path, monkeypatch):
    """A user whose Claude Code lives outside ~/.claude."""
    config = tmp_path / "elsewhere" / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_the_suite_never_runs_with_the_variable_of_whoever_runs_it():
    assert "CLAUDE_CONFIG_DIR" not in os.environ


def test_without_the_variable_the_user_directory_is_dot_claude(claude, tmp_path):
    assert claude.global_skills_dir(tmp_path) == tmp_path / ".claude" / "skills"
    assert claude.global_settings_file(tmp_path) == tmp_path / ".claude" / "settings.json"


def test_with_the_variable_every_user_level_path_moves(claude, tmp_path, relocated):
    home = tmp_path / "home"
    assert claude.global_skills_dir(home) == relocated / "skills"
    assert claude.global_agents_dir(home) == relocated / "agents"
    assert claude.global_instructions_file(home) == relocated / "CLAUDE.md"
    assert claude.global_settings_file(home) == relocated / "settings.json"


def test_a_project_is_not_moved_by_it(claude, tmp_path, relocated):
    project = tmp_path / "project"
    assert claude.local_skills_dir(project) == project / ".claude" / "skills"
    assert claude.local_settings_file(project) == project / ".claude" / "settings.local.json"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_variable_is_no_variable(claude, tmp_path, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    assert claude.global_skills_dir(tmp_path) == tmp_path / ".claude" / "skills"


def test_a_tilde_in_the_variable_is_the_home_directory(claude, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "me"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/cfg")
    assert claude.global_skills_dir(tmp_path / "ignored") == tmp_path / "me" / "cfg" / "skills"


def test_a_global_install_lands_where_the_harness_reads(claude, tmp_path, relocated):
    home = tmp_path / "home"
    home.mkdir()
    result = harnesses.install(claude, "global", home=home)
    assert result["skills_target"] == str(relocated / "skills")
    assert any(name.startswith("skills/griot-") for name in _files(relocated))
    assert any(name.startswith("agents/griot-") for name in _files(relocated))
    assert _files(home) == [], "nothing in the directory the harness does not read"


def test_the_instructions_block_and_the_tool_rules_follow(claude, tmp_path, relocated, monkeypatch):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    home = tmp_path / "home"

    assert harnesses.offer_instructions(claude, "global", home=home) == "created"
    assert harnesses.offer_tool_approval(claude, "global", home=home, cwd=tmp_path) == "created"

    assert "griot:begin" in (relocated / "CLAUDE.md").read_text()
    assert json.loads((relocated / "settings.json").read_text())["permissions"]["allow"]
    assert _files(home) == []


def test_the_harness_is_detected_by_its_relocated_directory(claude, tmp_path, relocated, monkeypatch):
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home-without-dot-claude"))
    assert claude.detect() is False
    relocated.mkdir(parents=True)
    assert claude.detect() is True


def test_the_command_says_where_it_wrote(claude, tmp_path, relocated, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    assert harnesses.cmd_install("global", "claude-code", mcp="no", home=tmp_path / "home") == 0
    assert str(relocated / "skills") in capsys.readouterr().out


# --- the MCP tool: whoever starts the server chooses its environment --------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("scope", ["global", "local"])
async def test_the_question_names_the_directories_that_will_be_written(tmp_path, relocated, monkeypatch, scope):
    """The server's environment comes from whoever configured it, which can
    be a project's `.mcp.json`. The variable decides where a GLOBAL install
    writes, so the person asked has to see the place, not only "global"."""
    from mcp_types import ElicitResult
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    asked = []

    async def decline(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="decline", content={})

    async with Client(mcp_server.mcp, elicitation_callback=decline) as client:
        await client.call_tool("griot_assist_install", {"harness": "claude-code", "scope": scope})

    assert len(asked) == 1
    expected = relocated if scope == "global" else Path(os.path.realpath(project)) / ".claude"
    assert str(expected / "skills") in asked[0] and str(expected / "agents") in asked[0]
    assert _files(relocated) == [] and _files(project) == [], "declined: nothing written"


def test_the_question_for_every_harness_names_each_destination(tmp_path, relocated, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda: list(harnesses.HARNESSES))
    question = mcp_server._assist_install_question("all", "global")
    assert str(relocated / "skills") in question
    assert str(tmp_path / "home" / ".config" / "opencode" / "skills") in question


# --- a value that cannot be used, and a place that is not what it looks like -------------------
# The variable is read from the environment of whoever started the process.
# For the MCP server that can be a project's `.mcp.json`, so its value is
# input, not the user's own choice.


def _asked_and_answered(monkeypatch):
    """A client that can ask, and says yes. Returns what it was asked."""
    from mcp_types import ElicitResult
    asked = []

    async def accept(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={})

    return asked, accept


@pytest.mark.parametrize("value,why", [("cfg", "absolute"), (".", "absolute"), ("~nosuchuser-griot/x", "home directory")])
def test_a_value_that_does_not_name_one_place_is_refused(claude, tmp_path, monkeypatch, value, why):
    """A relative value would be read against whatever directory the process
    happens to be in: a project's, for an MCP server. `~user` for a user
    that does not exist used to be an exception."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)
    monkeypatch.chdir(tmp_path)
    refusal = harnesses.install_refusal([claude], "global", home=tmp_path / "home")
    assert refusal and "CLAUDE_CONFIG_DIR" in refusal and why in refusal
    with pytest.raises(harnesses.UnsafeDestination):
        harnesses.install(claude, "global", home=tmp_path / "home")
    assert claude.detect() in (True, False), "detection does not raise on it either"
    assert not (tmp_path / "cfg").exists() and _files(tmp_path / "home") == [] and not (tmp_path / "skills").exists()


def test_a_bad_value_does_not_stop_a_local_install(claude, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "cfg")
    project = tmp_path / "project"
    project.mkdir()
    assert harnesses.install_refusal([claude], "local", cwd=project) is None


def test_with_a_bad_value_nothing_falls_back_to_dot_claude(claude, tmp_path, monkeypatch, capsys):
    """Falling back would be the old bug: files where the harness does not look."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "cfg")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    home = tmp_path / "home"
    assert harnesses.cmd_install("global", "claude-code", mcp="no", home=home) == 1
    assert "CLAUDE_CONFIG_DIR" in capsys.readouterr().err
    assert harnesses.offer_instructions(claude, "global", home=home) == "unsafe"
    assert harnesses.offer_tool_approval(claude, "global", home=home, cwd=tmp_path) == "unsafe"
    assert not (tmp_path / "cfg").exists() and _files(home) == []


def test_a_value_that_names_a_file_is_refused_before_anything(claude, tmp_path, monkeypatch):
    target = tmp_path / "a-file"
    target.write_text("not a directory")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    refusal = harnesses.install_refusal([claude], "global", home=tmp_path / "home")
    assert refusal and "not a directory" in refusal
    assert target.read_text() == "not a directory"


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["cfg", "FILE"])
async def test_the_tool_refuses_a_bad_place_without_asking(tmp_path, monkeypatch, value):
    (tmp_path / "a-file").write_text("x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a-file") if value == "FILE" else value)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    asked, accept = _asked_and_answered(monkeypatch)
    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "claude-code", "scope": "global"})
    assert asked == [], "a yes that cannot change the outcome is not asked for"
    assert result.is_error is False and result.structured_content["changed"] is False
    expected = "not a directory" if value == "FILE" else "CLAUDE_CONFIG_DIR"
    assert expected in result.structured_content["message"]


@pytest.mark.anyio
async def test_the_question_gives_the_place_the_files_really_land_in(tmp_path, monkeypatch):
    """A directory that is a link to another: named by the link, the
    question would show a path that is not where the write goes."""
    victim = tmp_path / "home" / "private"
    victim.mkdir(parents=True)
    link = tmp_path / "looks-harmless"
    link.symlink_to(victim, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    from mcp_types import ElicitResult
    asked = []

    async def decline(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="decline", content={})

    async with Client(mcp_server.mcp, elicitation_callback=decline) as client:
        await client.call_tool("griot_assist_install", {"harness": "claude-code", "scope": "global"})

    assert len(asked) == 1
    assert str(Path(os.path.realpath(victim)) / "skills") in asked[0], "the resolved place"
    assert "CLAUDE_CONFIG_DIR" in asked[0], "and that the environment chose it"
    assert _files(victim) == []


def test_a_question_without_the_variable_does_not_mention_it(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert "CLAUDE_CONFIG_DIR" not in mcp_server._assist_install_question("claude-code", "global")
    assert "CLAUDE_CONFIG_DIR" not in mcp_server._assist_install_question("claude-code", "local")


def test_installing_does_not_change_the_mode_of_a_directory_that_was_already_there(claude, tmp_path, relocated):
    """It tightened every directory it wrote into to 0700, the user's own
    `agents/` included. Only what griot creates is griot's to set."""
    (relocated / "agents").mkdir(parents=True)
    (relocated / "agents").chmod(0o755)
    relocated.chmod(0o755)
    harnesses.install(claude, "global", home=tmp_path / "home")
    assert (relocated / "agents").stat().st_mode & 0o777 == 0o755
    assert relocated.stat().st_mode & 0o777 == 0o755
    created = relocated / "skills" / "griot-onboarding"
    assert created.stat().st_mode & 0o777 == 0o700 and (relocated / "skills").stat().st_mode & 0o777 == 0o700


def test_the_command_says_the_variable_decided_the_place(claude, tmp_path, relocated, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    harnesses.cmd_install("global", "claude-code", mcp="no", home=tmp_path / "home")
    assert "CLAUDE_CONFIG_DIR" in capsys.readouterr().out
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    harnesses.cmd_install("global", "claude-code", mcp="no", home=tmp_path / "home")
    assert "CLAUDE_CONFIG_DIR" not in capsys.readouterr().out


def test_the_undo_of_a_registration_says_it_needs_the_same_variable(claude, tmp_path, relocated, monkeypatch, capsys):
    import subprocess
    monkeypatch.setattr(harnesses, "_which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(harnesses, "_griot_command", lambda: "/usr/local/bin/griot")
    monkeypatch.setattr(harnesses, "_run_harness_command",
                        lambda argv: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
    assert harnesses.offer_mcp_server(claude, "global", mode="yes") == "registered"
    out = " ".join(capsys.readouterr().out.split())
    assert "To undo" in out and "CLAUDE_CONFIG_DIR" in out
