"""`griot update`: runs the upgrade that `griot doctor` only prints.

It asks PyPI the way the doctor's release check does, works out how griot
was installed the way that check does, shows the exact command and asks
before running it. A development install is refused (the checkout is what
gets updated), and so is an installation whose method cannot be told: a
guess is printed, never run. The suite never touches the network nor runs a
real installer: the PyPI query and every subprocess are stubbed."""

import json
import subprocess
import sys

import pytest

from griot import doctor, update


PIPX_PREFIX = "/Users/you/.local/share/pipx/venvs/griot-rag"
UV_PREFIX = "/Users/you/.local/share/uv/tools/griot-rag"
VENV_PREFIX = "/Users/you/work/venv"


@pytest.fixture
def world(monkeypatch):
    """What `griot update` sees: PyPI's answer, the installed version, where
    it runs, whether it is a development install, whether a person is at a
    terminal, and what each subprocess does. Records every subprocess."""
    import griot

    state = {"newest": "0.3.0", "asked": 0, "calls": [], "installer_rc": 0, "now": "0.3.0",
             "development": None, "interactive": False, "answer": "y", "prompts": []}

    def latest():
        state["asked"] += 1
        if isinstance(state["newest"], BaseException):
            raise state["newest"]
        return state["newest"]

    def run(argv, *args, **kwargs):
        assert isinstance(argv, list), "an argument list, never a shell string"
        assert not kwargs.get("shell"), "never through a shell"
        state["calls"].append(argv)
        if argv[:2] == [sys.executable, "-c"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{state['now']}\n", stderr="")
        return subprocess.CompletedProcess(argv, state["installer_rc"])

    def ask(prompt=""):
        state["prompts"].append(prompt)
        return state["answer"]

    monkeypatch.setattr(doctor, "latest_release", latest)
    monkeypatch.setattr(griot, "__version__", "0.2.1")
    monkeypatch.setattr(doctor, "development_install", lambda: state["development"])
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess.run only"))
    monkeypatch.setattr("griot.common.is_interactive", lambda: state["interactive"])
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr(sys, "prefix", PIPX_PREFIX)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    return state


def _installer_calls(world):
    return [c for c in world["calls"] if c[:2] != [sys.executable, "-c"]]


# --- PyPI -----------------------------------------------------------------------------------------


def test_up_to_date_says_so_and_runs_nothing(world, capsys):
    world["newest"] = "0.2.1"

    assert update.main(["--yes"]) == 0

    assert world["calls"] == []
    out = capsys.readouterr().out
    assert "0.2.1" in out and "up to date" in out


def test_newer_than_pypi_says_so_and_runs_nothing(world, capsys):
    world["newest"] = "0.2.0"

    assert update.main(["--yes"]) == 0

    assert world["calls"] == []
    assert "newer than" in capsys.readouterr().out


def test_pypi_out_of_reach_says_it_could_not_check_and_runs_nothing(world, capsys):
    import requests

    world["newest"] = requests.ConnectionError("offline")

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    assert "could not" in capsys.readouterr().err.lower()


def test_any_failure_of_the_query_is_could_not_check_not_a_traceback(world, capsys):
    world["newest"] = ValueError("no version in PyPI's answer")

    assert update.main(["--yes"]) != 0
    assert world["calls"] == []


def test_a_version_that_cannot_be_compared_runs_nothing(world, capsys):
    world["newest"] = "0.3.0rc1"

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    assert "could not compare" in capsys.readouterr().err


def test_pypi_is_asked_once_through_the_doctor_query(world):
    world["newest"] = "0.2.1"

    update.main([])

    assert world["asked"] == 1


# --- how griot is installed -----------------------------------------------------------------------


@pytest.mark.parametrize("prefix, expected", [
    (PIPX_PREFIX, ["pipx", "upgrade", "griot-rag"]),
    (UV_PREFIX, ["uv", "tool", "upgrade", "griot-rag"]),
    (VENV_PREFIX, [f"{VENV_PREFIX}/bin/python", "-m", "pip", "install", "--upgrade", "griot-rag"]),
])
def test_each_install_method_runs_its_own_upgrade(world, monkeypatch, capsys, prefix, expected):
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", f"{prefix}/bin/python")

    assert update.main(["--yes"]) == 0

    assert _installer_calls(world) == [expected]
    assert " ".join(expected) in capsys.readouterr().out, "the exact command is shown"


def test_a_development_install_is_refused_with_the_reason(world, capsys):
    world["development"] = "an editable install of /Users/you/code/griot"

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    err = capsys.readouterr().err
    assert "/Users/you/code/griot" in err and "checkout" in err


def test_a_development_install_inside_pipx_is_still_refused(world, capsys):
    world["development"] = "an editable install of /Users/you/code/griot"

    assert update.main(["--yes"]) != 0
    assert world["calls"] == []


def test_an_unknown_method_is_refused_and_the_candidates_printed(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    err = capsys.readouterr().err
    for candidate in ("pipx upgrade griot-rag", "uv tool upgrade griot-rag", "pip install --upgrade griot-rag"):
        assert candidate in err


def test_the_commands_are_the_ones_doctor_prints():
    """One detection: the doctor's printed fix is the update's argument list,
    joined."""
    for prefix in (PIPX_PREFIX, UV_PREFIX, VENV_PREFIX, "/usr", "/Users/you/my work/venv"):
        argvs = doctor.upgrade_argvs(prefix, "/usr", f"{prefix}/bin/python")
        assert doctor.upgrade_commands(prefix, "/usr", f"{prefix}/bin/python") == [
            __import__("shlex").join(a) for a in argvs]


class _Distribution:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.direct_url


def test_an_editable_install_is_development(monkeypatch):
    found = _Distribution(json.dumps({"url": "file:///Users/you/code/griot", "dir_info": {"editable": True}}))
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: found)

    reason = doctor.development_install()

    assert reason is not None and "/Users/you/code/griot" in reason


def test_an_install_from_an_index_is_not_development(monkeypatch):
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: _Distribution(None))

    assert doctor.development_install() is None


def test_a_non_editable_install_from_a_directory_is_not_development(monkeypatch):
    found = _Distribution(json.dumps({"url": "file:///Users/you/code/griot", "dir_info": {}}))
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: found)

    assert doctor.development_install() is None


def test_no_installed_distribution_is_development(monkeypatch):
    """griot imported from a source tree on the path, with no package
    installed: there is nothing for an installer to upgrade."""
    def missing(name):
        raise doctor.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(doctor.metadata, "distribution", missing)

    assert doctor.development_install() is not None


def test_an_unreadable_direct_url_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: _Distribution("{not json"))

    assert doctor.development_install() is not None


# --- the confirmation -----------------------------------------------------------------------------


def test_no_terminal_and_no_yes_refuses_saying_so(world, capsys):
    assert update.main([]) == 2

    assert world["calls"] == []
    assert "no terminal" in capsys.readouterr().err


def test_at_a_terminal_it_asks_and_a_yes_runs_it(world):
    world["interactive"] = True

    assert update.main([]) == 0

    assert len(world["prompts"]) == 1
    assert _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


def test_at_a_terminal_anything_but_yes_runs_nothing(world):
    world["interactive"] = True
    world["answer"] = ""

    assert update.main([]) == 1
    assert world["calls"] == []


def test_yes_answers_without_asking(world):
    world["interactive"] = True

    assert update.main(["--yes"]) == 0

    assert world["prompts"] == []
    assert _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


# --- running it -----------------------------------------------------------------------------------


def test_a_failing_installer_s_exit_code_is_returned(world, capsys):
    world["installer_rc"] = 3

    assert update.main(["--yes"]) == 3

    assert "3" in capsys.readouterr().err


def test_an_installer_not_on_the_path_is_said_not_a_traceback(world, monkeypatch, capsys):
    def missing(argv, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", missing)

    assert update.main(["--yes"]) != 0
    assert "pipx" in capsys.readouterr().err


def test_the_installer_s_output_is_streamed_not_captured(world, monkeypatch):
    seen = {}

    def run(argv, *args, **kwargs):
        if argv[:2] != [sys.executable, "-c"]:
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, stdout="0.3.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    update.main(["--yes"])

    assert not seen.get("capture_output") and seen.get("stdout") is None and seen.get("stderr") is None


def test_afterwards_it_prints_the_version_read_by_a_new_process(world, capsys):
    world["now"] = "0.3.0"

    update.main(["--yes"])

    version_calls = [c for c in world["calls"] if c[:2] == [sys.executable, "-c"]]
    assert len(version_calls) == 1 and "griot-rag" in version_calls[0][2]
    out = capsys.readouterr().out
    assert "0.3.0 is now installed" in out


def test_afterwards_it_says_running_servers_keep_the_old_version(world, capsys):
    update.main(["--yes"])

    out = capsys.readouterr().out
    assert "MCP server" in out and "restart" in out


@pytest.mark.parametrize("rc, stdout", [
    (1, "0.3.0\n"),  # output and a failure: a failed read is not a version, whatever it printed
    (0, "\n"),       # success with nothing printed is no version either
])
def test_a_version_that_cannot_be_read_afterwards_is_said(world, monkeypatch, capsys, rc, stdout):
    def run(argv, *args, **kwargs):
        if argv[:2] == [sys.executable, "-c"]:
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="boom")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert update.main(["--yes"]) == 0
    assert "could not read" in capsys.readouterr().out


# --- where it is reachable ------------------------------------------------------------------------


def test_griot_update_dispatches_to_this_module(world, capsys):
    from griot import cli

    world["newest"] = "0.2.1"

    assert cli.main(["update"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_griot_help_lists_update(capsys):
    from griot import cli

    assert "update" in cli.build_parser().format_help()


@pytest.mark.anyio
async def test_no_mcp_tool_updates_griot():
    """An agent must not upgrade the tool it is using: `griot update` is a
    command for a person, never a tool."""
    from mcp.client.client import Client
    from griot import mcp_server

    async with Client(mcp_server.mcp) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert names, "the server lists its tools"
    assert [n for n in names if "update" in n or "upgrade" in n or "install" in n and "assist" not in n] == []


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_update_check_turned_off_does_not_stop_the_explicit_command(world, monkeypatch, value):
    """GRIOT_UPDATE_CHECK governs the doctor's automatic check; a person who
    runs `griot update` asked for PyPI to be asked."""
    monkeypatch.setenv("GRIOT_UPDATE_CHECK", value)

    assert update.main(["--yes"]) == 0

    assert world["asked"] == 1 and _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


def test_update_uses_only_the_doctor_s_public_names():
    """update.py reuses the doctor's release query, version rules and
    upgrade detection (one implementation of each), through names the doctor
    publishes: a private helper renamed or reshaped inside doctor.py would
    otherwise break `griot update` with nothing in doctor.py saying so."""
    import pathlib
    import re

    source = pathlib.Path(update.__file__).read_text()
    assert re.findall(r"\bdoctor\._\w+", source) == []
    for name in ("latest_release", "release_numbers", "upgrade_commands", "upgrade_argvs", "development_install"):
        assert callable(getattr(doctor, name)), name
        assert f"doctor.{name}(" in source, name
