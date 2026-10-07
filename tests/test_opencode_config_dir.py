"""opencode reads its user-level skills and agents from
`$XDG_CONFIG_HOME/opencode`, and from `~/.config/opencode` only when that
variable is unset or empty; the same rule on Linux, macOS and Windows. The
installer assumed `~/.config/opencode` regardless: with the variable set, a
global install landed where opencode does not look, and reported success.

Evidence, opencode's own source (anomalyco/opencode, formerly
sst/opencode; read at commit ecc4916b on 2026-10-07):
- packages/core/src/global.ts: `config = path.join(xdgConfig!, "opencode")`,
  with `xdgConfig` from xdg-basedir 5.1.0, which is
  `env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config")`: no platform
  branch, no `~` expansion, a relative value used as it is.
- packages/opencode/src/config/paths.ts, `directories()`: that directory
  ALWAYS, then every `.opencode` up from the project, `~/.opencode`, and
  OPENCODE_CONFIG_DIR when set, as one MORE directory, not instead.
- skills (src/skill/index.ts) and agents (src/config/agent.ts) are scanned
  in every one of those directories.
So OPENCODE_CONFIG_DIR does not move the place griot installs into: the
XDG one is read with or without it."""

import os
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import harnesses, mcp_server


@pytest.fixture
def opencode():
    return next(h for h in harnesses.HARNESSES if h.id == "opencode")


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """A user whose XDG_CONFIG_HOME is not ~/.config."""
    config_home = tmp_path / "elsewhere" / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_the_suite_never_runs_with_the_variables_of_whoever_runs_it():
    assert "XDG_CONFIG_HOME" not in os.environ and "OPENCODE_CONFIG_DIR" not in os.environ


def test_without_the_variable_the_user_directory_is_dot_config_opencode(opencode, tmp_path):
    assert opencode.global_skills_dir(tmp_path) == tmp_path / ".config" / "opencode" / "skills"
    assert opencode.global_agents_dir(tmp_path) == tmp_path / ".config" / "opencode" / "agents"


def test_with_the_variable_the_user_directory_moves(opencode, tmp_path, xdg):
    home = tmp_path / "home"
    assert opencode.global_skills_dir(home) == xdg / "opencode" / "skills"
    assert opencode.global_agents_dir(home) == xdg / "opencode" / "agents"


def test_an_empty_variable_is_no_variable(opencode, tmp_path, monkeypatch):
    """xdg-basedir tests the value with `||`: an empty string is unset."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert opencode.global_skills_dir(tmp_path) == tmp_path / ".config" / "opencode" / "skills"
    assert harnesses.install_refusal([opencode], "global", home=tmp_path) is None
    assert harnesses.user_dir_set_by(opencode, "global") is None


def test_a_project_is_not_moved_by_it(opencode, tmp_path, xdg):
    project = tmp_path / "project"
    assert opencode.local_skills_dir(project) == project / ".opencode" / "skills"
    assert harnesses.destinations(opencode, "local", cwd=project)[0] == project / ".opencode" / "skills"


def test_claude_code_is_not_moved_by_it(tmp_path, xdg):
    """Claude Code keeps `~/.claude` whatever XDG_CONFIG_HOME says."""
    claude = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
    assert claude.global_skills_dir(tmp_path) == tmp_path / ".claude" / "skills"
    assert harnesses.user_dir_set_by(claude, "global") is None


def test_opencode_config_dir_does_not_move_the_install(opencode, tmp_path, monkeypatch):
    """opencode reads OPENCODE_CONFIG_DIR IN ADDITION to its XDG directory.
    Installing into the extra one would leave two copies of each skill for
    anyone who installed before setting it; the XDG one is read either way."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "extra"))
    assert opencode.global_skills_dir(tmp_path / "home") == tmp_path / "home" / ".config" / "opencode" / "skills"
    assert harnesses.user_dir_set_by(opencode, "global") is None


def test_a_global_install_lands_where_opencode_reads(opencode, tmp_path, xdg):
    home = tmp_path / "home"
    home.mkdir()
    result = harnesses.install(opencode, "global", home=home)
    assert result["skills_target"] == str(xdg / "opencode" / "skills")
    assert any(name.startswith("skills/griot-") for name in _files(xdg / "opencode"))
    assert any(name.startswith("agents/griot-") for name in _files(xdg / "opencode"))
    assert _files(home) == [], "nothing in the directory opencode does not read"


@pytest.mark.parametrize("value", ["cfg", ".", "~/cfg", "  "])
def test_a_value_that_does_not_name_one_place_is_refused(opencode, tmp_path, monkeypatch, value):
    """opencode joins a relative value to whatever directory it was started
    in (xdg-basedir neither expands `~` nor checks the value), so griot
    cannot know which place that is: a project's, for an MCP server."""
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    refusal = harnesses.install_refusal([opencode], "global", home=home)
    assert refusal and "XDG_CONFIG_HOME" in refusal and "absolute" in refusal
    with pytest.raises(harnesses.UnsafeDestination):
        harnesses.install(opencode, "global", home=home)
    assert opencode.detect() in (True, False), "detection does not raise on it either"
    assert _files(home) == [], "nothing in the default place either"
    assert not (tmp_path / "cfg").exists() and not (tmp_path / "opencode").exists() and not (tmp_path / "~").exists()


def test_detection_does_not_follow_a_bad_value_into_the_current_directory(opencode, tmp_path, monkeypatch):
    """A relative value read against the directory griot runs in would make
    any project holding `cfg/opencode` look like an opencode installation."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "cfg")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cfg" / "opencode").mkdir(parents=True)
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert opencode.detect() is False
    assert opencode.global_skills_dir(tmp_path / "home") == tmp_path / "home" / ".config" / "opencode" / "skills"


def test_a_bad_value_does_not_stop_a_local_install(opencode, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "cfg")
    project = tmp_path / "project"
    project.mkdir()
    assert harnesses.install_refusal([opencode], "local", cwd=project) is None


def test_the_harness_is_detected_by_its_relocated_directory(opencode, tmp_path, xdg, monkeypatch):
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home" / ".config" / "opencode").mkdir(parents=True)
    assert opencode.detect() is False, "the default place is not where this opencode lives"
    (xdg / "opencode").mkdir(parents=True)
    assert opencode.detect() is True


def test_the_command_says_where_it_wrote_and_which_variable_chose_it(tmp_path, xdg, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    home = tmp_path / "home"
    assert harnesses.cmd_install("global", "opencode", mcp="no", home=home) == 0
    out = capsys.readouterr().out
    assert str(xdg / "opencode" / "skills") in out and "XDG_CONFIG_HOME" in out
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert harnesses.cmd_install("global", "opencode", mcp="no", home=home) == 0
    out = capsys.readouterr().out
    assert str(home / ".config" / "opencode" / "skills") in out and "XDG_CONFIG_HOME" not in out


def test_the_command_refuses_a_bad_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", "cfg")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    assert harnesses.cmd_install("global", "opencode", mcp="no", home=tmp_path / "home") == 1
    assert "XDG_CONFIG_HOME" in capsys.readouterr().err
    assert _files(tmp_path / "home") == [] and not (tmp_path / "cfg").exists()


# --- the MCP tool: whoever starts the server chooses its environment --------------------------


@pytest.mark.anyio
async def test_the_question_names_the_directories_and_the_variable(tmp_path, xdg, monkeypatch):
    from mcp_types import ElicitResult
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    asked = []

    async def decline(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="decline", content={})

    async with Client(mcp_server.mcp, elicitation_callback=decline) as client:
        await client.call_tool("griot_assist_install", {"harness": "opencode", "scope": "global"})

    assert len(asked) == 1
    expected = Path(os.path.realpath(xdg)) / "opencode"
    assert str(expected / "skills") in asked[0] and str(expected / "agents") in asked[0]
    assert "XDG_CONFIG_HOME" in asked[0]
    assert _files(xdg) == [] and _files(tmp_path / "home") == [], "declined: nothing written"


@pytest.mark.anyio
async def test_the_tool_refuses_a_bad_value_without_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "cfg")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    asked = []

    async def accept(ctx, params):
        from mcp_types import ElicitResult
        asked.append(params.message)
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "opencode", "scope": "global"})
    assert asked == [], "a yes that cannot change the outcome is not asked for"
    assert result.structured_content["changed"] is False
    assert "XDG_CONFIG_HOME" in result.structured_content["message"]
