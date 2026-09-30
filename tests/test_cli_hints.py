"""The CLI command in every refusal is meant to be copy-pasted into a shell.
Anything the agent controls (a path, a query, a profile name) is embedded in
it, so it must survive the shell unchanged: a hint that a shell splits or
expands is a command injection the person pastes themselves."""

import shlex

import pytest

from griot import cli, common, golden_set, mcp_server, repos

HOSTILE = [
    "/tmp/my repo",
    "/tmp/it's",
    "$HOME/x",
    "/tmp/a;touch pwned",
    "/tmp/`id`",
    "/tmp/$(id)",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_cli_command_round_trips_through_a_shell(value):
    hint = mcp_server._cli_command("repos", "add", positional=[value])
    assert shlex.split(hint) == ["griot", "repos", "add", "--", value]


def test_cli_command_leaves_plain_words_unquoted():
    assert mcp_server._cli_command("repos", "list") == "griot repos list"


def _command_in(message: str) -> str:
    """The command a person is told to run: the line right after the one
    that ends with 'to run:'. The command never contains a line break (a value
    that would need one gets no command at all), and the question above it is
    escaped to one line, so nothing the agent sends can move this marker."""
    lines = message.split("\n")
    return lines[next(i for i, line in enumerate(lines) if line.endswith("to run:")) + 1]


class _NoElicitCtx:
    client_capabilities = None


@pytest.mark.anyio
@pytest.mark.parametrize("value", HOSTILE)
async def test_repos_add_refusal_hint_survives_a_shell(monkeypatch, value):
    monkeypatch.setattr(repos, "add_repo", lambda p: p)
    result = await mcp_server.griot_repos_add(value, confirm=False, ctx=_NoElicitCtx())
    hint = _command_in(result["message"])
    assert shlex.split(hint) == ["griot", "repos", "add", "--", value]


@pytest.mark.anyio
@pytest.mark.parametrize("value", HOSTILE)
async def test_golden_set_add_refusal_hint_survives_a_shell(monkeypatch, value):
    result = await mcp_server.griot_golden_set_add(value, must_include=[{"repo": "r"}],
                                                   confirm=False, ctx=_NoElicitCtx())
    hint = _command_in(result["message"])
    assert shlex.split(hint) == ["griot", "golden-set", "add", "--", value]


OPTION_LIKE = ["--limit", "-x", "--profile=evil"]


@pytest.mark.parametrize("value", OPTION_LIKE)
def test_repos_add_hint_reads_an_option_like_path_as_a_path(monkeypatch, value):
    """`--` ends option parsing, so the real `griot repos add` sees data."""
    seen = []
    monkeypatch.setattr(repos, "cmd_add", lambda path: seen.append(path) or 0)
    # Only the parsing is under test: skip the path check and the question.
    monkeypatch.setattr(repos, "check_add", lambda path: path)
    monkeypatch.setattr(common, "confirm", lambda question, **kw: 0)
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


# Not a list of known-bad characters: the rule is "anything that is not
# printable", which is what makes a pasted command differ from the one read.
# U+2028, U+2029 and U+0085 are line breaks to a terminal without being below
# 32; bidi controls reorder what is displayed; zero-width ones hide in it.
CONTROL = ["/tmp/a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb", "a\x7fb",
           "a\u2028b", "a\u2029b", "a\x85b", "a\u202eb", "a\u2066b", "a\u200bb", "a\ufeffb", "a\u00a0b"]


@pytest.mark.parametrize("value", CONTROL, ids=lambda v: repr(v))
def test_cli_command_refuses_to_build_a_command_around_control_characters(value):
    assert mcp_server._cli_command("repos", "add", positional=[value]) is None
    assert mcp_server._cli_command("index", "all", option=("--path", value)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["run: y", "how (It is interactive: x) run: y", "ends with to run:"])
async def test_text_that_imitates_the_marker_cannot_move_the_command(value):
    out = await mcp_server.griot_golden_set_add(value, must_include=[{"repo": "r"}], confirm=False, ctx=_NoElicitCtx())
    assert shlex.split(_command_in(out["message"])) == ["griot", "golden-set", "add", "--", value]


@pytest.mark.parametrize("value", ["plain", "with space", "it's", "a\"b", "ação", "/tmp/x;y", "$HOME", "日本語"])
def test_cli_command_is_printable_and_one_line_for_every_value_it_accepts(value):
    hint = mcp_server._cli_command("repos", "add", positional=[value])
    assert hint is not None and hint.isprintable()
    assert len(hint.splitlines()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("value", CONTROL, ids=lambda v: repr(v))
async def test_no_refusal_carries_a_non_printable_character_from_the_agent(value):
    """Whole message, not only the command: the question above it is reused
    in the refusal, and `splitlines` is what a terminal does with it."""
    out = await mcp_server.griot_repos_add(value, ctx=_NoElicitCtx())
    message = out["message"]
    assert all(line.isprintable() for line in message.split("\n")), repr(message)
    assert len(message.splitlines()) == len(message.split("\n"))
    assert "griot repos add" not in message
