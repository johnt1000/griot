"""One wording for where an exported credential came from, and the right
advice for a file direnv loaded.

The same unknown origin was worded three ways: "this shell", "this shell
(not in a shell file or direnv file griot knows)" and "this shell (no shell
file or direnv file griot knows sets it)", depending on whether `auth set`,
`auth list`, `auth remove`, `griot doctor` or an API refusing the key said
it. And the advice to stop an export ("remove the export, and `unset` it in
terminals already open") was also given for a direnv file, where it is
wrong: direnv reloads its terminals by itself when the file changes, and a
changed .envrc is blocked until `direnv allow`. Now every consumer reads
the place and the advice from one helper in common."""

import pytest

from griot import auth, common, doctor

from test_credential_origin import NEW, OLD, VAR, _digest, _no_secret_in, shell  # noqa: F401
from test_credential_origin_direnv import _envrc, project  # noqa: F401

UNKNOWN = "this shell (no shell file or direnv file griot knows sets it)"


def _every_message(monkeypatch, capsys):
    """What `auth set`, `auth list`, `auth remove`, the API-refusal hint and
    doctor's credentials check say, with a stored key that differs from the
    exported one."""
    common.env_file_set(VAR, NEW)
    hint = common.credential_hint(VAR)
    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")
    auth.cmd_list()
    listed = capsys.readouterr().out
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: NEW)
    auth.cmd_set("github")
    set_out = capsys.readouterr().out
    auth.cmd_remove("github")
    removed = capsys.readouterr().out
    messages = {"hint": hint, "doctor": check["detail"] + " " + (check["fix"] or ""),
                "list": listed, "set": set_out, "remove": removed}
    for text in messages.values():
        _no_secret_in(text)
    return messages


def test_an_export_griot_cannot_place_is_worded_the_same_everywhere(shell, monkeypatch, capsys):
    (shell / ".zshrc").write_text("")

    messages = _every_message(monkeypatch, capsys)

    for consumer, text in messages.items():
        assert UNKNOWN in text, f"{consumer} words the unknown origin differently: {text!r}"


def test_a_shell_file_export_is_advised_to_be_removed_and_unset(shell, monkeypatch, capsys):
    messages = _every_message(monkeypatch, capsys)

    for consumer in ("hint", "doctor", "set"):
        assert "~/.zshrc:3" in messages[consumer] and "unset" in messages[consumer], consumer
        assert "direnv" not in messages[consumer], consumer


def test_a_direnv_export_is_advised_to_be_edited_not_unset(project, monkeypatch, capsys):
    _envrc(project, f"export {VAR}={OLD}\n")

    messages = _every_message(monkeypatch, capsys)

    for consumer in ("hint", "doctor", "set", "remove"):
        text = messages[consumer]
        assert f"{project}/.envrc:1" in text, consumer
        assert "direnv allow" in text and "reloads" in text, f"{consumer}: {text!r}"
        assert "unset" not in text, f"{consumer} advises `unset` for a file direnv reloads: {text!r}"


def test_the_hint_for_a_direnv_export_griot_does_not_store_is_advised_for_direnv(project):
    """No stored key to hide: the hint offers `griot auth set` instead of
    the export, and still does not advise `unset` for a direnv file."""
    _envrc(project, f"export {VAR}={OLD}\n")

    hint = common.credential_hint(VAR)

    assert f"{project}/.envrc:1" in hint and "griot auth set github" in hint
    assert "direnv allow" in hint and "unset" not in hint
    assert hint.count(f"{project}/.envrc:1") == 1, "the place is said once"
    _no_secret_in(hint)


def test_the_hint_for_a_shell_export_griot_does_not_store_says_unset(shell):
    hint = common.credential_hint(VAR)

    assert "~/.zshrc:3" in hint and f"unset {VAR}" in hint and "griot auth set github" in hint
    assert "direnv" not in hint


def test_an_export_in_both_a_shell_file_and_direnv_gets_both_pieces_of_advice(project, monkeypatch, capsys):
    (common.Path.home() / ".zshrc").write_text(f"export {VAR}={OLD}\n")
    _envrc(project, f"export {VAR}={OLD}\n")

    advice = common.export_advice(VAR, common.credential_origin(VAR))

    assert "~/.zshrc:1" in advice and f"unset {VAR}" in advice
    assert f"{project}/.envrc:1" in advice and "direnv allow" in advice
    assert advice.index("~/.zshrc:1") < advice.index("unset") < advice.index(f"{project}/.envrc:1"), \
        "the `unset` belongs to the shell file, not to the direnv file"


def test_a_message_that_named_both_kinds_of_place_still_tells_their_steps_apart(project):
    """A sentence that has just named the places says "there" for its
    advice, but "there" cannot tell a shell file from a direnv file."""
    (common.Path.home() / ".zshrc").write_text(f"export {VAR}={OLD}\n")
    _envrc(project, f"export {VAR}={OLD}\n")
    common.env_file_set(VAR, NEW)

    hint = common.credential_hint(VAR)

    assert f"from ~/.zshrc:1 and run `unset {VAR}`" in hint
    assert f"from {project}/.envrc:1; direnv reloads" in hint


def test_the_origin_says_which_places_direnv_loaded(project):
    (common.Path.home() / ".zshrc").write_text(f"export {VAR}={OLD}\n")
    _envrc(project, f"export {VAR}={OLD}\n")

    origin = common.credential_origin(VAR)

    assert origin["exported_in"] == ["~/.zshrc:1", f"{project}/.envrc:1"]
    assert origin["direnv_in"] == [f"{project}/.envrc:1"]


def test_nothing_exported_has_no_direnv_place(project, monkeypatch):
    """The .envrc mentions the variable, but it is not in this process's
    environment (the .envrc changed and was not reloaded, say)."""
    _envrc(project, f"export {VAR}={OLD}\n")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.delenv(VAR)

    assert common.credential_origin(VAR)["direnv_in"] == []


def test_an_unplaced_export_equal_to_the_stored_key_is_worded_the_same_too(shell, monkeypatch, capsys):
    """Nothing hidden: `auth list` and doctor still say where it is exported,
    in the same words."""
    (shell / ".zshrc").write_text("")
    common.env_file_set(VAR, OLD)

    auth.cmd_list()
    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")

    line = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("github"))
    assert f"also exported in {UNKNOWN}" in line
    assert f"{VAR} ({UNKNOWN})" in check["detail"]


@pytest.mark.parametrize("places", [[], ["~/.zshrc:3"]])
def test_the_place_helper_names_the_places_or_the_one_unknown_wording(places):
    origin = {"exported_in": places, "direnv_in": []}

    assert common.export_places(origin) == (", ".join(places) or UNKNOWN)
