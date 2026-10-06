"""Where a credential direnv exported comes from.

`credential_origin()` names the shell file and line that exports a
credential. A value set by direnv was reported as exported in "this shell
(no shell file griot knows sets it)", though direnv says which file it
loaded: it puts DIRENV_FILE (the .envrc it evaluated) and DIRENV_DIR ("-"
followed by that file's directory) in the environment it exports
(internal/cmd/rc.go, RC.Load). The .envrc is evaluated by bash, and only
what it EXPORTS reaches the environment: a plain `VAR=x` stays a shell
variable. A `.env` it loads with direnv's `dotenv`/`dotenv_if_exists`
(no argument: `.env` next to the .envrc, since source_env evaluates from
that directory) exports every key, with or
without `export`. A directory argument loads nothing: stdlib's dotenv
checks for `<dir>/.env` but hands `direnv dotenv` the directory itself,
whose os.ReadFile fails. direnv itself is not needed: the tests set what it
would have set."""

import pytest

from griot import auth, common

from test_credential_origin import NEW, OLD, VAR, _digest, _no_secret_in, shell  # noqa: F401


@pytest.fixture
def project(shell, monkeypatch, tmp_path):
    """A direnv-managed project outside HOME, whose .envrc direnv loaded;
    no shell file exports the token."""
    (shell / ".zshrc").write_text("")
    root = tmp_path / "work" / "proj"
    root.mkdir(parents=True)
    monkeypatch.setenv("DIRENV_FILE", str(root / ".envrc"))
    monkeypatch.setenv("DIRENV_DIR", "-" + str(root))
    return root


def _envrc(root, text):
    (root / ".envrc").write_text(text)


# --- the .envrc -----------------------------------------------------------------------------------


def test_an_export_in_the_envrc_is_named_with_its_line(project):
    _envrc(project, f"# project\nexport {VAR}={OLD}\n")

    origin = common.credential_origin(VAR)

    assert origin["exported_in"] == [f"{project}/.envrc:2"]
    _no_secret_in(repr(origin))


def test_an_envrc_under_home_is_shown_from_home(shell, monkeypatch):
    (shell / ".zshrc").write_text("")
    root = shell / "code" / "proj"
    root.mkdir(parents=True)
    _envrc(root, f"export {VAR}={OLD}\n")
    monkeypatch.setenv("DIRENV_FILE", str(root / ".envrc"))

    assert common.credential_origin(VAR)["exported_in"] == ["~/code/proj/.envrc:1"]


@pytest.mark.parametrize("line,found", [
    (f"export {VAR}=x", True), (f"  export {VAR}=\"$(cat secret)\"", True), (f"export {VAR}", True),
    (f"export OTHER=1 {VAR}=x", True), (f"declare -x {VAR}=x", True), (f"typeset -x {VAR}=x", True),
    # bash evaluates the .envrc: an assignment without export never reaches the environment
    (f"{VAR}=x", False), (f"{VAR}=x some-command", False),
    (f"# export {VAR}=x", False), (f"export {VAR}_OTHER=x", False), (f"export MY_{VAR}=x", False),
])
def test_what_in_an_envrc_counts_as_an_export(project, line, found):
    _envrc(project, line + "\n")

    assert (common.credential_origin(VAR)["exported_in"] == [f"{project}/.envrc:1"]) is found


def test_both_a_shell_file_and_the_envrc_are_named(shell, project):
    (shell / ".zshrc").write_text(f"export {VAR}=x\n")
    _envrc(project, f"export {VAR}=y\n")

    assert common.credential_origin(VAR)["exported_in"] == ["~/.zshrc:1", f"{project}/.envrc:1"]


def test_without_direnv_active_no_envrc_is_read(project, monkeypatch):
    _envrc(project, f"export {VAR}=x\n")
    monkeypatch.delenv("DIRENV_FILE")
    monkeypatch.delenv("DIRENV_DIR")
    monkeypatch.chdir(project)

    assert common.credential_origin(VAR)["exported_in"] == []


def test_a_direnv_that_only_sets_direnv_dir_points_at_its_envrc(project, monkeypatch):
    """direnv before DIRENV_FILE existed exported only DIRENV_DIR."""
    _envrc(project, f"export {VAR}=x\n")
    monkeypatch.delenv("DIRENV_FILE")

    assert common.credential_origin(VAR)["exported_in"] == [f"{project}/.envrc:1"]


def test_direnv_dir_without_its_leading_dash_is_not_trusted(project, monkeypatch):
    _envrc(project, f"export {VAR}=x\n")
    monkeypatch.delenv("DIRENV_FILE")
    # "//path" is the same directory: only the missing dash makes it untrusted
    monkeypatch.setenv("DIRENV_DIR", "/" + str(project))

    assert common.credential_origin(VAR)["exported_in"] == []


def test_the_envrc_is_read_only_for_an_exported_credential(project, monkeypatch):
    _envrc(project, f"export {VAR}=x\n")
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.delenv(VAR)
    common.env_file_set(VAR, NEW)

    assert common.credential_origin(VAR)["exported_in"] == []


@pytest.mark.parametrize("broken", ["missing", "directory", "unreadable", "binary", "empty"])
def test_a_direnv_file_that_cannot_be_read_names_nothing_and_raises_nothing(project, monkeypatch, broken):
    envrc = project / ".envrc"
    if broken == "directory":
        envrc.mkdir()
    elif broken == "unreadable":
        _envrc(project, f"export {VAR}=x\n")
        envrc.chmod(0o000)
    elif broken == "binary":
        envrc.write_bytes(b"\xff\xfe\x00 not text")
    elif broken == "empty":
        # nothing names a directory: the .envrc of the current one is not guessed
        _envrc(project, f"export {VAR}=x\n")
        monkeypatch.chdir(project)
        monkeypatch.setenv("DIRENV_FILE", "")
        monkeypatch.setenv("DIRENV_DIR", "-")
    try:
        assert common.credential_origin(VAR)["exported_in"] == []
    finally:
        if broken == "unreadable":
            envrc.chmod(0o600)


# --- a .env the .envrc loads ----------------------------------------------------------------------


@pytest.mark.parametrize("call,dotenv_path", [
    ("dotenv", ".env"), ("dotenv_if_exists", ".env"), ("dotenv .env.local", ".env.local"),
    ("dotenv_if_exists 'secrets/.env.dev'", "secrets/.env.dev"), ("  dotenv  # load the keys", ".env"),
])
def test_a_dotenv_the_envrc_loads_is_named_with_its_line(project, call, dotenv_path):
    _envrc(project, f"use nix\n{call}\n")
    target = project / dotenv_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"OTHER=1\n{VAR}={OLD}\n")

    origin = common.credential_origin(VAR)

    assert origin["exported_in"] == [f"{project}/{dotenv_path}:2"]
    _no_secret_in(repr(origin))


@pytest.mark.parametrize("line,found", [
    (f"{VAR}=x", True), (f"export {VAR}=x", True), (f"  {VAR} = 'x'", True), (f"{VAR}: x", True),
    (f"{VAR}=", True),
    (f"# {VAR}=x", False), (f"{VAR}_OTHER=x", False), (f"MY_{VAR}=x", False),
])
def test_in_a_loaded_dotenv_every_key_is_exported(project, line, found):
    _envrc(project, "dotenv\n")
    (project / ".env").write_text(line + "\n")

    assert (common.credential_origin(VAR)["exported_in"] == [f"{project}/.env:1"]) is found


def test_a_dotenv_the_envrc_does_not_load_is_not_read(project):
    _envrc(project, "# dotenv\nexport OTHER=1\n")
    (project / ".env").write_text(f"{VAR}=x\n")

    assert common.credential_origin(VAR)["exported_in"] == []


@pytest.mark.parametrize("call", ["dotenv config", "dotenv_if_exists config"])
def test_a_dotenv_given_a_directory_loads_nothing_so_names_nothing(project, call):
    """stdlib's dotenv finds `config/.env` but runs `direnv dotenv bash
    config`, and reading a directory fails: no key of it is exported."""
    _envrc(project, call + "\n")
    (project / "config").mkdir()
    (project / "config" / ".env").write_text(f"{VAR}=x\n")

    assert common.credential_origin(VAR)["exported_in"] == []


def test_a_dotenv_given_an_absolute_path_is_named(project, tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"{VAR}=x\n")
    _envrc(project, f"dotenv {secrets}\n")

    assert common.credential_origin(VAR)["exported_in"] == [f"{secrets}:1"]


def test_an_unquoted_tilde_in_a_dotenv_path_is_the_home_directory(shell, project):
    """bash expands an unquoted leading ~; quoted, it stays a literal name."""
    (shell / "keys.env").write_text(f"{VAR}=x\n")
    _envrc(project, "dotenv ~/keys.env\n")

    assert common.credential_origin(VAR)["exported_in"] == ["~/keys.env:1"]


def test_a_quoted_tilde_in_a_dotenv_path_is_a_literal_name(shell, project):
    (shell / "keys.env").write_text(f"{VAR}=x\n")
    (project / "~").mkdir()
    (project / "~" / "keys.env").write_text(f"OTHER=1\n{VAR}=x\n")
    _envrc(project, "dotenv '~/keys.env'\n")

    assert common.credential_origin(VAR)["exported_in"] == [f"{project}/~/keys.env:2"]


def test_a_dotenv_path_built_from_a_variable_is_not_guessed(project):
    """bash expands the variable; a file named like the unexpanded text is
    not the one it loads."""
    _envrc(project, "dotenv \"$SECRETS\"\n")
    (project / "$SECRETS").write_text(f"{VAR}=x\n")

    assert common.credential_origin(VAR)["exported_in"] == []


def test_a_dotenv_that_is_missing_names_nothing_and_raises_nothing(project):
    _envrc(project, "dotenv_if_exists\n")

    assert common.credential_origin(VAR)["exported_in"] == []


def test_direnv_loading_a_dotenv_directly_reads_it_as_one(project, monkeypatch):
    """With load_dotenv in direnv.toml, DIRENV_FILE names a .env and direnv
    loads it with `dotenv`: every key is exported."""
    (project / ".env").write_text(f"{VAR}=x\n")
    monkeypatch.setenv("DIRENV_FILE", str(project / ".env"))

    assert common.credential_origin(VAR)["exported_in"] == [f"{project}/.env:1"]


# --- what each command says -----------------------------------------------------------------------


def test_the_hint_names_the_envrc(project):
    _envrc(project, f"export {VAR}={OLD}\n")

    hint = common.credential_hint(VAR)

    assert f"{project}/.envrc:1" in hint
    _no_secret_in(hint)


def test_the_hint_without_a_place_says_direnv_was_looked_at_too(project):
    _envrc(project, "")

    assert "no shell file or direnv file griot knows sets it" in common.credential_hint(VAR)


def test_auth_set_list_and_remove_name_the_envrc(project, monkeypatch, capsys):
    _envrc(project, f"export {VAR}={OLD}\n")
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: NEW)

    auth.cmd_set("github")
    auth.cmd_list()
    auth.cmd_remove("github")

    out = capsys.readouterr().out
    assert out.count(f"{project}/.envrc:1") == 3
    _no_secret_in(out)


def test_auth_set_without_a_place_says_direnv_was_looked_at_too(project, monkeypatch, capsys):
    _envrc(project, "")
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: NEW)

    auth.cmd_set("github")

    assert "no shell file or direnv file griot knows sets it" in capsys.readouterr().out


def test_doctor_names_the_envrc(project):
    from griot import doctor
    _envrc(project, f"export {VAR}={OLD}\n")
    common.env_file_set(VAR, NEW)

    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")

    assert f"{project}/.envrc:1" in check["detail"] and f"{project}/.envrc:1" in (check["fix"] or "")
    _no_secret_in(repr(check))


def test_a_dotenv_path_that_cannot_be_looked_at_raises_nothing(project):
    """Opening a file under a directory that cannot be searched raises
    PermissionError."""
    _envrc(project, "dotenv locked/.env\n")
    (project / "locked").mkdir()
    (project / "locked" / ".env").write_text(f"{VAR}=x\n")
    (project / "locked").chmod(0o000)
    try:
        assert common.credential_origin(VAR)["exported_in"] == []
    finally:
        (project / "locked").chmod(0o700)


def test_a_dotenv_path_with_an_unknown_user_raises_nothing(project):
    """bash leaves `~nosuchuser/...` as it is when no such user exists;
    Path.expanduser() raises RuntimeError there instead. The lookup only
    explains where a credential came from, so it names nothing."""
    _envrc(project, "dotenv ~griotnosuchuserxyz/keys.env\n")

    assert common.credential_origin(VAR)["exported_in"] == []


def test_a_dotenv_path_with_a_nul_byte_raises_nothing(project):
    """Reading a path with a NUL byte raises ValueError, not OSError; a
    crafted .envrc can put one there."""
    _envrc(project, "dotenv keys\x00.env\n")

    assert common.credential_origin(VAR)["exported_in"] == []


def test_an_unquoted_tilde_and_nul_in_a_dotenv_path_raises_nothing(project):
    _envrc(project, "dotenv ~griot\x00x/keys.env\n")

    assert common.credential_origin(VAR)["exported_in"] == []
