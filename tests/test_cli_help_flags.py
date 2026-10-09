"""Every command's --help names every flag the command takes.

`--profile`, `--chat-profile` and `--sources` are taken out of the command
line by cli.py before any parser sees it (cli._extract_flag), so no parser
declared them and no --help showed them: `--sources` was documented only in
the README, `--profile` nowhere a --help reaches, `--chat-profile` only
inside a sentence of `ask`'s description. `griot index all --help` printed
the help of each of the five sources in turn and said the pipeline
completed. `griot mcp --help` started the server. And the parsers griot
forwards to (`ask`, `quality-check`, `index <source>`) wrote their usage
line as `usage: griot [-h] ...` (or `cli.py`), without the command.

Asked through the CLI itself, in this process (cli.main), and through the
real entry points in a subprocess where the program name or a server start
is what is being tested."""

import os
import subprocess
import sys

import pytest

from griot import cli, common, config

# The commands that read the embedding profile, so --profile changes what
# they do: they index, search, report on or serve the active collection.
PROFILE_COMMANDS = [
    [],
    ["index"],
    ["index", "all"],
    ["index", "keywords"],
    *[["index", source] for source in cli.INDEX_SOURCES],
    ["search"],
    ["ask"],
    ["quality-check"],
    ["stats"],
    ["golden-set"],
    # suggest and add resolve a new case's mode against the active
    # collection (golden_set.case_mode_for), and add searches it.
    ["golden-set", "suggest"],
    ["golden-set", "add"],
    ["audit"],
    ["doctor"],
]


def _help(capsys, argv) -> str:
    with pytest.raises(SystemExit) as exited:
        cli.main([*argv, "--help"])
    assert exited.value.code in (0, None)
    return capsys.readouterr().out


@pytest.mark.parametrize("argv", PROFILE_COMMANDS, ids=lambda argv: " ".join(argv) or "griot")
def test_help_names_profile_flag(capsys, argv):
    out = _help(capsys, argv)
    assert "--profile NAME" in out


# The golden-set actions that never open the collection nor resolve the
# profile: the golden set is one file for every profile, and review reads the
# query log of every collection. --profile changes nothing they do, so their
# help does not offer it.
PROFILE_BLIND_GOLDEN_SET_ACTIONS = ["list", "remove", "review"]


@pytest.mark.parametrize("action", PROFILE_BLIND_GOLDEN_SET_ACTIONS)
def test_golden_set_actions_the_profile_does_not_change_do_not_offer_it(capsys, action):
    out = _help(capsys, ["golden-set", action])
    assert out.startswith(f"usage: griot golden-set {action}")
    assert "--profile" not in out


@pytest.mark.parametrize("argv", [[], ["index"]], ids=["griot", "index"])
def test_help_names_sources_flag(capsys, argv):
    assert "--sources LIST" in _help(capsys, argv)


def test_top_level_help_names_chat_profile_flag(capsys):
    assert "--chat-profile NAME" in _help(capsys, [])


def test_ask_help_names_chat_profile_flag_and_every_chat_profile(capsys):
    out = _help(capsys, ["ask"])
    assert "--chat-profile NAME" in out
    for name in common.CHAT_PROFILES:
        assert name in out


def test_index_all_help_is_one_help_naming_sources_and_runs_nothing(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: ran.append(source) or 0)

    out = _help(capsys, ["index", "all"])

    assert ran == []
    assert "usage: griot index all" in out
    assert "--sources LIST" in out
    for source in cli.INDEX_SOURCES:
        assert source in out
    assert "=== griot index" not in out


@pytest.mark.parametrize("spelling", ["-h", "--hel"])
def test_index_all_help_any_spelling_argparse_takes(monkeypatch, capsys, spelling):
    """argparse takes any unambiguous prefix of --help, and each source's
    parser would have taken it: the help of `all` must too, or the five
    source helps come back."""
    ran = []
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: ran.append(source) or 0)
    with pytest.raises(SystemExit):
        cli.main(["index", "all", "--repo", "x", spelling])
    assert ran == []
    assert "usage: griot index all" in capsys.readouterr().out


def test_index_all_still_forwards_every_flag_untouched(monkeypatch):
    """The help check must not eat what `all` forwards to each source."""
    calls = []
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: calls.append((source, rest)) or 0)
    rest = ["--repo", "my-repo", "--dry-run", "--", "--prune"]
    assert cli.main(["index", "all", "--sources", "code,tags", *rest]) == 0
    assert calls == [("code", rest), ("tags", rest)]


@pytest.mark.parametrize("argv, prog", [
    (["ask"], "griot ask"),
    (["quality-check"], "griot quality-check"),
    *[(["index", source], f"griot index {source}") for source in cli.INDEX_SOURCES],
])
def test_forwarded_parsers_name_the_command_in_usage(capsys, argv, prog):
    out = _help(capsys, argv)
    assert out.startswith(f"usage: {prog} ")


def _env(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))
    return env


def test_usage_names_the_command_through_the_real_entry(tmp_path):
    done = subprocess.run([sys.executable, "-m", "griot.cli", "ask", "--help"], env=_env(tmp_path),
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("usage: griot ask ")


MCP_ENTRIES = {"griot mcp": ["-m", "griot.cli", "mcp"], "python -m griot.mcp_server": ["-m", "griot.mcp_server"]}


@pytest.mark.parametrize("entry", MCP_ENTRIES)
@pytest.mark.parametrize("flag", ["--help", "-h", "--hel"])
def test_mcp_help_prints_usage_and_does_not_serve(tmp_path, entry, flag):
    """A server that started anyway exits 0 too, at the end of its input,
    with nothing on stdout: the usage line is what says it did not serve."""
    process = subprocess.Popen([sys.executable, *MCP_ENTRIES[entry], flag], env=_env(tmp_path),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail(f"`{entry} {flag}` started the server instead of printing its help")
    assert process.returncode == 0, err
    assert out.startswith("usage: griot mcp")
    assert "--profile NAME" in out
    mcp_settings = [s for s in config.SETTINGS if s.variable.startswith("GRIOT_MCP_")]
    assert mcp_settings
    for setting in mcp_settings:
        assert setting.variable in out
        assert f"griot config set {setting.name}" in out


def test_mcp_other_arguments_still_start_the_server(monkeypatch):
    """Only a help is new: an argument the server never took is still left
    alone, as before, rather than refused by the help parser."""
    import griot.mcp_server as mcp_server_module

    calls = []
    monkeypatch.setattr(mcp_server_module.mcp, "run", lambda *a, **kw: calls.append((a, kw)))
    # See tests/test_cli.py::test_mcp_subcommand_runs_the_real_server.
    monkeypatch.setattr(mcp_server_module.common, "ENVIRONMENT_WAS_NARROWED", True)

    assert cli.main(["mcp", "--verbose", "stray"]) == 0
    assert calls
