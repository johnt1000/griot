"""The CLI command in every refusal is meant to be copy-pasted into a shell.
Anything the agent controls (a path, a query, a profile name) is embedded in
it, so it must survive the shell unchanged: a hint that a shell splits or
expands is a command injection the person pastes themselves."""

import shlex

import pytest

from griot import cli, golden_set, mcp_server, repos

HOSTILE = [
    "/tmp/my repo",
    "/tmp/it's",
    "$HOME/x",
    "/tmp/a;touch pwned",
    "/tmp/a\nb",
    "/tmp/`id`",
    "/tmp/$(id)",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_cli_command_round_trips_through_a_shell(value):
    hint = mcp_server._cli_command("repos", "add", positional=[value])
    assert shlex.split(hint) == ["griot", "repos", "add", "--", value]


def test_cli_command_leaves_plain_words_unquoted():
    assert mcp_server._cli_command("repos", "list") == "griot repos list"


class _NoElicitCtx:
    client_capabilities = None


@pytest.mark.anyio
@pytest.mark.parametrize("value", HOSTILE)
async def test_repos_add_refusal_hint_survives_a_shell(monkeypatch, value):
    monkeypatch.setattr(repos, "add_repo", lambda p: p)
    result = await mcp_server.griot_repos_add(value, confirm=False, ctx=_NoElicitCtx())
    hint = result["message"].split("run it yourself: ", 1)[1]
    assert shlex.split(hint) == ["griot", "repos", "add", "--", value]


@pytest.mark.anyio
@pytest.mark.parametrize("value", HOSTILE)
async def test_golden_set_add_refusal_hint_survives_a_shell(monkeypatch, value):
    result = await mcp_server.griot_golden_set_add(value, must_include=[{"repo": "r"}],
                                                   confirm=False, ctx=_NoElicitCtx())
    hint = result["message"].split("or run it yourself: ", 1)[1]
    assert shlex.split(hint) == ["griot", "golden-set", "add", "--", value]


OPTION_LIKE = ["--limit", "-x", "--profile=evil"]


@pytest.mark.parametrize("value", OPTION_LIKE)
def test_repos_add_hint_reads_an_option_like_path_as_a_path(monkeypatch, value):
    """`--` ends option parsing, so the real `griot repos add` sees data."""
    seen = []
    monkeypatch.setattr(repos, "cmd_add", lambda path: seen.append(path) or 0)
    argv = shlex.split(mcp_server._cli_command("repos", "add", positional=[value]))[2:]
    assert repos.main(argv) == 0
    assert seen == [value]


@pytest.mark.parametrize("value", OPTION_LIKE)
def test_golden_set_add_hint_reads_an_option_like_query_as_a_query(monkeypatch, value):
    seen = []
    monkeypatch.setattr(golden_set, "cmd_add", lambda query, limit: seen.append(query) or 0)
    argv = shlex.split(mcp_server._cli_command("golden-set", "add", positional=[value]))[2:]
    assert golden_set.main(argv) == 0
    assert seen == [value]


@pytest.mark.parametrize("value", OPTION_LIKE)
def test_profiles_delete_hint_reads_an_option_like_name_as_a_name(value):
    argv = shlex.split(mcp_server._cli_command("profiles", "delete", positional=[value]))[1:]
    assert cli.build_parser().parse_args(argv).profile == value


@pytest.mark.parametrize("value", OPTION_LIKE)
def test_option_value_is_glued_so_it_cannot_be_read_as_a_flag(value):
    """`--path X` with X starting with `-` makes argparse fail or swallow the
    next word; `--path=X` is always read as the option's value."""
    hint = mcp_server._cli_command("index", "all", option=("--path", value))
    assert shlex.split(hint)[1:] == ["index", "all", f"--path={value}"]
