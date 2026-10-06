"""The CLI confirmations that have no flag that answers say what they need.

`griot repos add`, `griot profiles delete`, raising a ceiling with
`griot config set` and the installer's questions are answered by a person at
an interactive terminal. That is a guard against the plain command an agent
runs from a shell with no terminal, not a security boundary (SECURITY.md,
debt 13 in docs/lessons-and-debts.md). What holds it in place for a person
is the wording: the refusal and the --help both name the terminal, so
nobody reads "refused" as "forbidden" and nobody looks for a flag that is
not there."""

import dataclasses
import re

import pytest

from griot import cli, common, config, harnesses, repos


def _flat(text: str) -> str:
    """argparse wraps lines wherever the width falls."""
    return " ".join(text.split())


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))


@pytest.fixture
def deletable_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    name = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    path = common._collection_path(common.collection_name_for(name))
    path.mkdir(parents=True, exist_ok=True)
    (path / common._EDGE_CONFIG_MARKER).write_text("{}")
    return name


def _refusal(capsys) -> str:
    return _flat(capsys.readouterr().err)


# --- the refusal names the terminal and that no flag answers -------------------------------


def test_repos_add_refusal_names_the_terminal(no_terminal, tmp_path, capsys):
    assert repos.main(["add", str(tmp_path)]) == 2
    err = _refusal(capsys)
    assert "interactive terminal" in err and "no flag" in err


def test_profiles_delete_refusal_names_the_terminal(no_terminal, deletable_profile, capsys):
    assert cli.main(["profiles", "delete", deletable_profile]) == 2
    err = _refusal(capsys)
    assert "interactive terminal" in err and "no flag" in err


def test_raising_a_ceiling_refusal_names_the_terminal(no_terminal, capsys):
    assert config.main(["set", "spend-ceiling", "10"]) == 2
    err = _refusal(capsys)
    assert "interactive terminal" in err and "no flag" in err


@pytest.fixture
def claude(monkeypatch, tmp_path):
    """The real harness, its user directory moved under tmp_path."""
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    real = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
    return dataclasses.replace(
        real, user_dir_problem=None,
        global_instructions_file=lambda home: tmp_path / "CLAUDE.md",
        global_settings_file=lambda home: tmp_path / "settings.json",
    )


# The installer's last two questions have no flag that answers (--mcp answers
# the first one, so its message rightly offers that instead).
def test_installer_instructions_refusal_names_the_terminal(no_terminal, claude, tmp_path, capsys):
    assert harnesses.offer_instructions(claude, "global", home=tmp_path) == "not-interactive"
    out = _flat(capsys.readouterr().out)
    assert "in a terminal" in out and "no flag" in out


def test_installer_tool_approval_refusal_names_the_terminal(no_terminal, claude, tmp_path, capsys):
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "not-interactive"
    out = _flat(capsys.readouterr().out)
    assert "in a terminal" in out and "no flag" in out


# --- the --help of each says it before anyone runs it ----------------------------------------


def _help(capsys, main, argv) -> str:
    with pytest.raises(SystemExit) as exit_:
        main([*argv, "--help"])
    assert exit_.value.code == 0
    return _flat(capsys.readouterr().out)


@pytest.mark.parametrize("main,argv", [
    (repos.main, ["add"]),
    (cli.main, ["profiles", "delete"]),
    (config.main, ["set"]),
    (harnesses.main, ["install"]),
], ids=["repos-add", "profiles-delete", "config-set", "assist-install"])
def test_help_says_it_asks_at_an_interactive_terminal(capsys, main, argv):
    # The description, not the whole help: an option's own help can say "no
    # flag" about one question (assist install's --no-instructions does) and
    # stand in for a description that no longer says it about the command.
    out = re.split(r" (?:positional arguments|options):", _help(capsys, main, argv))[0]
    assert "interactive terminal" in out
    assert "no flag" in out
