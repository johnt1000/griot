"""Confirmation prompts of the CLI commands that destroy or widen something.

Two classes, decided by effect:

- terminal only (`repos add`, `profiles delete`): a person answers at an
  interactive terminal and there is no flag that answers instead, so the plain
  command run by an agent's shell (which has no terminal) changes nothing;
- recoverable (`repos remove`, `golden-set remove`, `auth remove`): the same
  prompt, plus `--yes` for scripts.

Every command checks what is cheap to check BEFORE asking, and an answer that
is not a yes leaves everything as it was."""

import json

import pytest

from griot import auth, cli, common, golden_set, repos


def _terminal(monkeypatch, answer=None, raises=None):
    """An interactive terminal whose person types `answer` (or whose input()
    raises). Returns the list of prompts shown."""
    shown = []

    def fake_input(prompt=""):
        shown.append(prompt)
        if raises is not None:
            raise raises
        return answer

    monkeypatch.setattr(common, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    return shown


def _no_terminal(monkeypatch):
    def never(prompt=""):
        raise AssertionError("asked without a terminal")

    monkeypatch.setattr(common, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", never)


# --- common.confirm ------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_confirm_proceeds_only_on_a_typed_yes(monkeypatch, answer):
    shown = _terminal(monkeypatch, answer)
    assert common.confirm("Do it?") == 0
    assert shown == ["Do it? [y/N] "]


@pytest.mark.parametrize("answer", ["", "n", "no", "yep", "s", "sim", "1"])
def test_confirm_treats_anything_else_as_no(monkeypatch, capsys, answer):
    _terminal(monkeypatch, answer)
    assert common.confirm("Do it?") == 1
    assert "Aborted, nothing changed." in capsys.readouterr().err


def test_confirm_end_of_input_is_a_no(monkeypatch, capsys):
    _terminal(monkeypatch, raises=EOFError())
    assert common.confirm("Do it?") == 1
    assert "Aborted, nothing changed." in capsys.readouterr().err


def test_confirm_ctrl_c_exits_like_a_shell_interrupt(monkeypatch, capsys):
    _terminal(monkeypatch, raises=KeyboardInterrupt())
    assert common.confirm("Do it?") == 130
    assert "Aborted, nothing changed." in capsys.readouterr().err


def test_confirm_yes_flag_skips_the_question(monkeypatch):
    _no_terminal(monkeypatch)
    assert common.confirm("Do it?", yes=True) == 0


def test_confirm_without_a_terminal_refuses_and_names_the_flag(monkeypatch, capsys):
    _no_terminal(monkeypatch)
    assert common.confirm("Do it?") == 2
    err = capsys.readouterr().err
    assert "Nothing was changed" in err and "--yes" in err


def test_confirm_terminal_only_never_mentions_a_flag(monkeypatch, capsys):
    _no_terminal(monkeypatch)
    assert common.confirm("Do it?", yes=None) == 2
    err = capsys.readouterr().err
    assert "Nothing was changed" in err and "--yes" not in err and "terminal" in err


# --- repos add: terminal only ----------------------------------------------------


def _registered():
    return json.loads(common.REPOS_JSON_PATH.read_text()) if common.REPOS_JSON_PATH.exists() else []


def test_repos_add_changes_nothing_without_a_terminal(monkeypatch, tmp_path):
    _no_terminal(monkeypatch)
    assert repos.main(["add", str(tmp_path)]) == 2
    assert _registered() == []


def test_repos_add_has_no_flag_that_answers_for_the_person(monkeypatch, tmp_path):
    _no_terminal(monkeypatch)
    for flag in ("--yes", "--y", "-y", "--force"):
        with pytest.raises(SystemExit):
            repos.main(["add", flag, str(tmp_path)])
    assert _registered() == []


def test_repos_add_registers_on_a_yes_and_says_what_it_means(monkeypatch, tmp_path):
    shown = _terminal(monkeypatch, "y")
    assert repos.main(["add", str(tmp_path)]) == 0
    assert _registered() == [str(tmp_path.resolve())]
    assert str(tmp_path.resolve()) in shown[0] and "embedding API" in shown[0]


def test_repos_add_does_nothing_on_a_no(monkeypatch, tmp_path):
    _terminal(monkeypatch, "n")
    assert repos.main(["add", str(tmp_path)]) == 1
    assert _registered() == []


def test_repos_add_reports_a_bad_path_without_asking(monkeypatch, tmp_path, capsys):
    shown = _terminal(monkeypatch, "y")
    assert repos.main(["add", str(tmp_path / "missing")]) == 1
    assert shown == [] and "Error" in capsys.readouterr().err


# --- repos remove: recoverable ---------------------------------------------------


def test_repos_remove_needs_an_answer_or_the_flag(monkeypatch, tmp_path):
    repos.add_repo(str(tmp_path))
    _no_terminal(monkeypatch)
    assert repos.main(["remove", str(tmp_path)]) == 2
    assert _registered() == [str(tmp_path.resolve())]
    assert repos.main(["remove", "--yes", str(tmp_path)]) == 0
    assert _registered() == []


def test_repos_remove_asks_at_a_terminal(monkeypatch, tmp_path):
    repos.add_repo(str(tmp_path))
    shown = _terminal(monkeypatch, "n")
    assert repos.main(["remove", str(tmp_path)]) == 1
    assert _registered() == [str(tmp_path.resolve())]
    assert "already indexed is kept" in shown[0]


def test_repos_remove_reports_an_unregistered_path_without_asking(monkeypatch, tmp_path, capsys):
    repos.add_repo(str(tmp_path))
    shown = _terminal(monkeypatch, "y")
    assert repos.main(["remove", "/not/registered"]) == 1
    assert shown == [] and "Error" in capsys.readouterr().err


# --- profiles delete: terminal only ------------------------------------------------


@pytest.fixture
def deletable_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    name = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    path = common.collection_path(common.collection_name_for(name))
    path.mkdir(parents=True, exist_ok=True)
    (path / common.EDGE_CONFIG_MARKER).write_text("{}")
    return name, path


def test_profiles_delete_changes_nothing_without_a_terminal(monkeypatch, deletable_profile):
    name, path = deletable_profile
    _no_terminal(monkeypatch)
    assert cli.main(["profiles", "delete", name]) == 2
    assert path.exists()


def test_profiles_delete_has_no_flag_that_answers_for_the_person(monkeypatch, deletable_profile):
    name, path = deletable_profile
    _no_terminal(monkeypatch)
    for flag in ("--yes", "--y", "-y", "--force"):
        with pytest.raises(SystemExit):
            cli.main(["profiles", "delete", flag, name])
    assert path.exists()


def test_profiles_delete_deletes_on_a_yes_and_says_it_cannot_be_undone(monkeypatch, deletable_profile):
    name, path = deletable_profile
    shown = _terminal(monkeypatch, "yes")
    assert cli.main(["profiles", "delete", name]) == 0
    assert not path.exists()
    assert name in shown[0] and "cannot be undone" in shown[0]


def test_profiles_delete_does_nothing_on_a_no(monkeypatch, deletable_profile):
    name, path = deletable_profile
    _terminal(monkeypatch, "")
    assert cli.main(["profiles", "delete", name]) == 1
    assert path.exists()


@pytest.mark.parametrize("name", ["not-a-real-profile", "jina-code"])
def test_profiles_delete_reports_what_it_would_refuse_without_asking(monkeypatch, capsys, name):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    shown = _terminal(monkeypatch, "y")
    assert cli.main(["profiles", "delete", name]) == 1
    assert shown == [] and "Error" in capsys.readouterr().err


def test_profiles_delete_reports_a_never_indexed_profile_without_asking(monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    name = next(n for n in common.EMBED_PROFILES if n != "jina-code")
    shown = _terminal(monkeypatch, "y")
    assert cli.main(["profiles", "delete", name]) == 1
    assert shown == [] and "does not exist" in capsys.readouterr().err


# --- golden-set remove: recoverable ------------------------------------------------


def test_golden_set_remove_needs_an_answer_or_the_flag(monkeypatch):
    golden_set.add_case(query="how does login work", must_include=[{"repo": "r", "source_type": "code"}])
    _no_terminal(monkeypatch)
    assert golden_set.main(["remove", "1"]) == 2
    assert len(golden_set.list_cases()) == 1
    assert golden_set.main(["remove", "--yes", "1"]) == 0
    assert golden_set.list_cases() == []


def test_golden_set_remove_shows_the_question_it_is_about_to_remove(monkeypatch):
    golden_set.add_case(query="how does login work", must_include=[{"repo": "r", "source_type": "code"}])
    shown = _terminal(monkeypatch, "n")
    assert golden_set.main(["remove", "1"]) == 1
    assert "how does login work" in shown[0]
    assert len(golden_set.list_cases()) == 1


def test_golden_set_remove_reports_a_bad_index_without_asking(monkeypatch, capsys):
    shown = _terminal(monkeypatch, "y")
    assert golden_set.main(["remove", "7"]) == 1
    assert shown == [] and "Error" in capsys.readouterr().err


# --- auth remove: recoverable ------------------------------------------------------


def test_auth_remove_needs_an_answer_or_the_flag(monkeypatch):
    auth.set_provider_key("openai", "sk-test-not-a-real-key")
    _no_terminal(monkeypatch)
    assert auth.main(["remove", "openai"]) == 2
    assert any(s["provider"] == "openai" and s["configured"] for s in auth.provider_status())
    assert auth.main(["remove", "--yes", "openai"]) == 0
    assert not any(s["provider"] == "openai" and s["file_masked"] for s in auth.provider_status())


def test_auth_remove_with_nothing_stored_says_so_without_asking(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    shown = _terminal(monkeypatch, "y")
    assert auth.main(["remove", "openai"]) == 0
    assert shown == [] and "nothing to remove" in capsys.readouterr().out


def test_auth_remove_never_shows_the_key(monkeypatch):
    auth.set_provider_key("openai", "sk-test-not-a-real-key")
    shown = _terminal(monkeypatch, "n")
    assert auth.main(["remove", "openai"]) == 1
    assert "sk-test" not in shown[0] and "openai" in shown[0]


def test_profiles_delete_reports_a_running_index_without_asking(monkeypatch, capsys, deletable_profile):
    """Cheap to know before the question, so a person is not asked to agree
    to something that is then refused. The delete keeps its own check: the
    state can change between the question and the answer."""
    name, path = deletable_profile
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": True})
    shown = _terminal(monkeypatch, "y")
    assert cli.main(["profiles", "delete", name]) == 1
    assert shown == [] and "indexing run is currently in progress" in capsys.readouterr().err
    assert path.exists()
