"""Where the credential in force comes from, said wherever it matters.

A token exported in the shell (`export GITHUB_TOKEN=...` in ~/.zshrc) wins
over the one griot stores, in its .env or in the keychain. That is the
right precedence, and it was invisible: `griot auth set github` wrote the
new token and said so, every new terminal kept using the old exported one,
and the platform answered 401 with nothing saying which token it had been
given. `config set` and `profiles use` already said so for a setting; the
credential commands did not.

Now `auth set`, `auth list`, `auth remove`, an API that refuses the key and
`griot doctor` say where the credential in force comes from, and where it is
exported: the shell file and line, never the value."""

import hashlib
import os

import pytest
import requests

from griot import auth, common, platforms

VAR = "GITHUB_TOKEN"
OLD, NEW = "ghp_" + "o" * 36, "ghp_" + "n" * 36


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def shell(tmp_path, monkeypatch):
    """A home whose ~/.zshrc exports the token, and a process that inherited it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A test run started from a direnv-managed directory must not read that
    # directory's .envrc as if it were part of the scenario.
    monkeypatch.delenv("DIRENV_FILE", raising=False)
    monkeypatch.delenv("DIRENV_DIR", raising=False)
    (home / ".zshrc").write_text(f"# settings\nexport PATH=/usr/bin\nexport {VAR}={OLD}\n")
    monkeypatch.setenv(VAR, OLD)
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {VAR: _digest(OLD)})
    monkeypatch.setattr(common, "keychain_get", lambda var: None)
    monkeypatch.setattr(common, "keychain_set", lambda var, value: False)
    common.secure_mkdir(common.CONFIG_DIR)
    return home


def _no_secret_in(text):
    assert OLD not in text and NEW not in text and OLD[-8:] not in text and NEW[-8:] not in text


# --- the origin -----------------------------------------------------------------------------------


def test_the_origin_says_the_environment_and_where_it_is_exported(shell):
    common.env_file_set(VAR, NEW)

    origin = common.credential_origin(VAR)

    assert origin["source"] == "environment" and origin["stored"] == "file"
    assert origin["shadows_stored"] is True
    assert origin["exported_in"] == ["~/.zshrc:3"]


def test_an_export_equal_to_the_stored_key_shadows_nothing(shell, monkeypatch):
    common.env_file_set(VAR, OLD)

    assert common.credential_origin(VAR)["shadows_stored"] is False


def test_a_key_only_in_the_file_comes_from_the_file(shell, monkeypatch):
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.delenv(VAR)
    common.env_file_set(VAR, NEW)

    origin = common.credential_origin(VAR)

    assert origin["source"] == "file" and origin["shadows_stored"] is False and origin["exported_in"] == []


def test_a_key_only_in_the_keychain_comes_from_the_keychain(shell, monkeypatch):
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.setattr(common, "keychain_get", lambda var: NEW if var == VAR else None)
    monkeypatch.setenv(VAR, NEW)  # injected at load time

    assert common.credential_origin(VAR)["source"] == "keychain"


def test_an_export_over_a_different_key_in_the_keychain_shadows_it(shell, monkeypatch):
    monkeypatch.setattr(common, "keychain_get", lambda var: NEW if var == VAR else None)

    origin = common.credential_origin(VAR)

    assert origin["stored"] == "keychain" and origin["shadows_stored"] is True


@pytest.mark.parametrize("line,found", [
    (f"export {VAR}=x", True), (f"  export {VAR}='x'", True), (f"{VAR}=x", True), (f"export {VAR}", True),
    (f"set -gx {VAR} x", True), (f"typeset -x {VAR}=x", True), (f"declare -x {VAR}=x", True),
    (f"export OTHER=1 {VAR}=x", True),
    (f"# export {VAR}=x", False), (f"export {VAR}_OTHER=x", False), (f"export MY_{VAR}=x", False),
    (f"{VAR}=x some-command --flag", False),
])
def test_where_a_shell_file_exports_it(shell, line, found):
    (shell / ".zshrc").write_text(line + "\n")

    assert (common.credential_origin(VAR)["exported_in"] == ["~/.zshrc:1"]) is found


def test_every_usual_shell_file_is_read(shell):
    (shell / ".zshrc").write_text("")
    (shell / ".bash_profile").write_text(f"export {VAR}=x\n")
    (shell / ".config" / "fish").mkdir(parents=True)
    (shell / ".config" / "fish" / "config.fish").write_text(f"set -x {VAR} x\n")

    assert common.credential_origin(VAR)["exported_in"] == ["~/.bash_profile:1", "~/.config/fish/config.fish:1"]


def test_an_export_set_by_something_that_is_no_shell_file_says_so(shell):
    (shell / ".zshrc").write_text("")

    origin = common.credential_origin(VAR)

    assert origin["source"] == "environment" and origin["exported_in"] == []
    assert "exported in this shell" in common.credential_hint(VAR)


# --- what each command says -----------------------------------------------------------------------


def test_auth_set_says_the_new_key_will_not_be_used_while_the_export_is_there(shell, monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: NEW)

    assert auth.cmd_set("github") == 0

    out = capsys.readouterr().out
    assert "~/.zshrc:3" in out and "exported" in out and "unset GITHUB_TOKEN" in out
    _no_secret_in(out)


def test_auth_set_says_nothing_more_when_nothing_is_exported(shell, monkeypatch, capsys):
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.delenv(VAR)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: NEW)

    auth.cmd_set("github")

    assert "exported" not in capsys.readouterr().out


def test_auth_set_says_nothing_more_when_the_export_is_the_same_key(shell, monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: OLD)

    auth.cmd_set("github")

    assert "exported" not in capsys.readouterr().out


def test_auth_list_says_the_key_in_use_comes_from_the_shell(shell, capsys):
    common.env_file_set(VAR, NEW)

    auth.cmd_list()

    line = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("github"))
    assert "~/.zshrc:3" in line and "differs" in line
    assert NEW[-4:] not in line, "the stored key is not what is in use: its mask is not shown as if it were"


def test_auth_remove_says_the_exported_key_is_still_used(shell, capsys):
    common.env_file_set(VAR, NEW)

    auth.cmd_remove("github")

    out = capsys.readouterr().out
    assert "still exported" in out and "~/.zshrc:3" in out
    _no_secret_in(out)


def test_auth_list_compares_with_the_keychain_too(shell, monkeypatch, capsys):
    monkeypatch.setattr(common, "keychain_get", lambda var: NEW if var == VAR else None)

    auth.cmd_list()

    line = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("github"))
    assert "differs" in line


def test_the_status_the_mcp_tools_read_does_not_touch_the_keychain(shell, monkeypatch):
    """`griot_profiles_list` and `griot_auth_guidance` call this on every
    call; on macOS a keychain read from a binary the item does not trust
    asks the person, every time."""
    monkeypatch.setattr(common, "keychain_get", lambda var: pytest.fail("the keychain was read"))

    auth.provider_status()


@pytest.mark.parametrize("broken", ["unreadable", "binary"])
def test_a_settings_file_that_cannot_be_read_costs_the_hint_not_the_error(shell, monkeypatch, broken):
    """The hint is built inside an exception handler: failing there would
    turn a clean API error into a crash."""
    if broken == "unreadable":
        common.env_file_set(VAR, NEW)
        common.ENV_PATH.chmod(0o000)
    else:
        common.ENV_PATH.write_bytes(b"\xff\xfe\x00 not text")
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Refused())

    try:
        with pytest.raises(common.DirectAPIUnavailable) as raised:
            common._openai_compatible_post_with_retry("https://api.example.invalid/v1", {}, {}, credential_env=VAR)
    finally:
        common.ENV_PATH.chmod(0o600)

    assert "401" in str(raised.value)


def test_zsh_files_under_zdotdir_are_read(shell, monkeypatch):
    (shell / ".zshrc").write_text("")
    zdot = shell / ".config" / "zsh"
    zdot.mkdir(parents=True)
    (zdot / ".zshrc").write_text(f"export {VAR}=x\n")
    monkeypatch.setenv("ZDOTDIR", str(zdot))

    assert common.credential_origin(VAR)["exported_in"] == ["~/.config/zsh/.zshrc:1"]


# --- an API that refuses the key ------------------------------------------------------------------


class _Refused:
    status_code = 401
    links = {}

    def raise_for_status(self):
        raise requests.HTTPError("401 Client Error: Unauthorized", response=self)


def test_a_platform_that_refuses_the_token_says_where_it_came_from(shell, monkeypatch, capsys, tmp_path):
    from griot import index_platform

    common.env_file_set(VAR, NEW)
    monkeypatch.setattr(platforms, "_get_with_retry", lambda *a, **k: _Refused())
    monkeypatch.setattr(index_platform, "_remote_url", lambda path: "https://github.com/someone/repo.git")

    index_platform.build_documents(tmp_path)

    out = capsys.readouterr().out
    assert "401" in out and "~/.zshrc:3" in out and "griot auth" in out
    assert out.count("~/.zshrc:3") == 1, "said once per repository, not once per kind of item"
    _no_secret_in(out)


def test_a_platform_that_refuses_a_stored_token_says_to_set_a_new_one(shell, monkeypatch, capsys, tmp_path):
    from griot import index_platform

    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.setenv(VAR, NEW)
    common.env_file_set(VAR, NEW)
    monkeypatch.setattr(platforms, "_get_with_retry", lambda *a, **k: _Refused())
    monkeypatch.setattr(index_platform, "_remote_url", lambda path: "https://github.com/someone/repo.git")

    index_platform.build_documents(tmp_path)

    out = capsys.readouterr().out
    assert "griot auth set github" in out and str(common.ENV_PATH) in out


def test_an_embedding_api_that_refuses_the_key_says_where_it_came_from(shell, monkeypatch):
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {"GRIOT_OPENAI_API_KEY": _digest("sk-" + "o" * 40)})
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-" + "o" * 40)
    (shell / ".zshrc").write_text("export GRIOT_OPENAI_API_KEY=sk-x\n")
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Refused())

    with pytest.raises(common.DirectAPIUnavailable) as raised:
        common._openai_compatible_post_with_retry("https://api.example.invalid/v1/embeddings", {}, {},
                                                  credential_env="GRIOT_OPENAI_API_KEY")

    assert "401" in str(raised.value) and "~/.zshrc:1" in str(raised.value)
    assert "o" * 20 not in str(raised.value)


def test_gemini_refusing_the_key_says_where_it_came_from(shell, monkeypatch):
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {"GEMINI_TOKEN": _digest("g" * 39)})
    (shell / ".zshrc").write_text("export GEMINI_TOKEN=x\n")
    monkeypatch.setattr(common, "GEMINI_TOKEN", "g" * 39)
    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Refused())

    with pytest.raises(common.GeminiUnavailable) as raised:
        common._gemini_post_with_retry("models/m:embedContent", {})

    assert "401" in str(raised.value) and "~/.zshrc:1" in str(raised.value)


def test_an_error_that_is_not_about_the_key_carries_no_hint(shell, monkeypatch):
    class _Broken(_Refused):
        status_code = 500

    monkeypatch.setattr(common, "_http_post", lambda *a, **k: _Broken())

    with pytest.raises(common.DirectAPIUnavailable) as raised:
        common._openai_compatible_post_with_retry("https://api.example.invalid/v1/embeddings", {}, {},
                                                  credential_env="GRIOT_OPENAI_API_KEY")

    assert "500" in str(raised.value) and "GRIOT_OPENAI_API_KEY" not in str(raised.value)


# --- griot doctor ---------------------------------------------------------------------------------


def test_doctor_warns_about_a_credential_the_shell_overrides(shell):
    from griot import doctor

    common.env_file_set(VAR, NEW)

    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")

    assert check["status"] == "warn" and "GITHUB_TOKEN" in check["detail"] and "~/.zshrc:3" in check["detail"]
    assert "~/.zshrc:3" in check["fix"]
    _no_secret_in(check["detail"] + check["fix"])


def test_doctor_is_quiet_about_credentials_nothing_overrides(shell, monkeypatch):
    from griot import doctor

    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    monkeypatch.delenv(VAR)

    check = next(c for c in doctor.run_checks() if c["check"] == "credentials")

    assert check["status"] == "ok"


# --- the snapshot holds no secret ----------------------------------------------------------------


def test_what_was_exported_is_kept_as_digests_not_values():
    for name, digest in common.EXPORTED_BEFORE_ENV_FILE.items():
        value = os.environ.get(name)
        if value is not None and name in os.environ:
            expected = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
            assert digest in (expected, digest) and digest != value, name
        assert len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest), name
    path = os.environ.get("PATH")
    if path is not None and "PATH" in common.EXPORTED_BEFORE_ENV_FILE:
        assert common.EXPORTED_BEFORE_ENV_FILE["PATH"] == hashlib.sha256(path.encode()).hexdigest()


def test_an_unreadable_settings_file_reads_as_nothing_stored(shell):
    common.env_file_set(VAR, NEW)
    common.ENV_PATH.chmod(0o000)
    try:
        origin = common.credential_origin(VAR)
    finally:
        common.ENV_PATH.chmod(0o600)

    assert origin["stored"] is None and origin["source"] == "environment"


def test_whatever_fails_while_building_the_hint_costs_only_the_hint(shell, monkeypatch):
    def broken(env_var):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(common, "credential_origin", broken)

    assert common.credential_hint(VAR) == ""
