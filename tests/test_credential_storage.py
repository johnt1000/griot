"""Where each credential is stored, and whether an OS keychain is there at all.

Two things were silent. A credential set before the keychain support stays in
plaintext in <config>/.env until `griot auth migrate` moves it, and nothing
said it was still there. And with no keychain backend reachable (the `keyring`
extra not installed, headless Linux, a container) griot falls back to the
plaintext file, which is right, and nothing said so either.

`griot auth list` and `griot doctor` now say where each credential is stored
(the keychain, the .env file, the environment), whether a keychain backend is
reachable, and, when one is and a credential is still in the file, the command
that moves it. Names only, never a value. Nothing is moved without being
asked, and the MCP paths read no keychain (a keychain read can ask the person
on macOS, and those tools run on every call).

The keyring is faked in-process (conftest makes `import keyring` fail by
default); the real OS keychain is never touched."""

import sys

import pytest
from dotenv import dotenv_values

from griot import auth, common, doctor

SECRET = "sk-" + "s" * 40


class _Backend:
    def __init__(self, name, priority):
        self.name = name
        self.priority = priority


class _FakeKeyring:
    """Stands in for the `keyring` module: a backend that answers, and a store."""

    def __init__(self, backend=None):
        self.store = {}
        self.backend = backend or _Backend("Fake Keyring", 5)

    def get_keyring(self):
        return self.backend

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        del self.store[(service, username)]


class _Untouchable:
    """A keyring that fails the test if anything asks it anything."""

    def __getattr__(self, name):
        raise AssertionError(f"keyring.{name} was reached")


@pytest.fixture(autouse=True)
def _no_exports(monkeypatch):
    """Nothing in these tests is exported in the shell, whatever the machine has."""
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {})
    for env_var in common.credential_env_vars().values():
        monkeypatch.delenv(env_var, raising=False)
    common.secure_mkdir(common.CONFIG_DIR)


@pytest.fixture
def keychain(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return fake


def _credentials():
    return next(c for c in doctor.run_checks() if c["check"] == "credentials")


def _no_secret_in(text):
    assert SECRET not in text and SECRET[-4:] not in text


# --- common.keychain_status ------------------------------------------------------------------------


def test_no_keyring_installed_is_no_backend():
    assert common.keychain_status() == {"available": False, "backend": None, "installed": False}


def test_a_reachable_backend_is_named(keychain):
    assert common.keychain_status() == {"available": True, "backend": "Fake Keyring", "installed": True}


@pytest.mark.parametrize("priority", [0, -1])
def test_the_fail_and_null_backends_are_no_backend(monkeypatch, priority):
    """What keyring picks when nothing is reachable (fail, priority 0) or when
    it was turned off (null, priority -1): installed, but nowhere to store."""
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring(_Backend("fail Keyring", priority)))

    assert common.keychain_status() == {"available": False, "backend": None, "installed": True}


def test_a_backend_that_raises_is_no_backend(monkeypatch):
    class _Broken(_FakeKeyring):
        def get_keyring(self):
            raise RuntimeError("no Secret Service")

    monkeypatch.setitem(sys.modules, "keyring", _Broken())

    assert common.keychain_status() == {"available": False, "backend": None, "installed": True}


def test_asking_whether_a_backend_exists_reads_no_credential(monkeypatch):
    class _OnlyTheBackend(_FakeKeyring):
        def get_password(self, *a):
            raise AssertionError("a credential was read")

    monkeypatch.setitem(sys.modules, "keyring", _OnlyTheBackend())

    assert common.keychain_status()["available"] is True


# --- griot auth list -------------------------------------------------------------------------------


def test_list_says_there_is_no_keychain_and_the_file_is_plaintext(capsys):
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    auth.cmd_list()

    out = capsys.readouterr().out
    assert "no OS keychain backend" in out and "griot[keychain]" in out
    openai = next(line for line in out.splitlines() if line.strip().startswith("openai"))
    assert "plaintext" in openai and str(common.ENV_PATH) in openai
    assert SECRET not in out


def test_list_with_keyring_installed_but_no_backend_does_not_say_install_it(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring(_Backend("fail Keyring", 0)))

    auth.cmd_list()

    out = capsys.readouterr().out
    assert "no OS keychain backend" in out and "griot[keychain]" not in out


def test_list_names_the_keychain_and_where_each_one_is(keychain, monkeypatch, capsys):
    keychain.store[("griot", "GRIOT_OPENAI_API_KEY")] = SECRET
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", SECRET)  # what the injection at import does
    common.env_file_set("GITLAB_PERSONAL_ACCESS_TOKEN", "glpat-" + "g" * 20)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "h" * 36)
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {"GITHUB_TOKEN": "0" * 64})

    auth.cmd_list()

    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
    assert "Fake Keyring" in out
    assert "OS keychain" in lines["openai"] and "plaintext" not in lines["openai"]
    assert "plaintext" in lines["gitlab"] and "griot auth migrate" in lines["gitlab"]
    assert "environment" in lines["github"]


def test_list_says_a_stored_key_is_also_exported(tmp_path, monkeypatch, capsys):
    """The same value in the file and in the shell: both places are named,
    since removing the stored one leaves the export in use."""
    import hashlib

    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text(f"export GRIOT_OPENAI_API_KEY={SECRET}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE",
                        {"GRIOT_OPENAI_API_KEY": hashlib.sha256(SECRET.encode()).hexdigest()})
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    auth.cmd_list()

    openai = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("openai"))
    assert "plaintext" in openai and "also exported in ~/.zshrc:1" in openai


def test_list_does_not_suggest_migrate_when_there_is_no_backend(capsys):
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    auth.cmd_list()

    assert "griot auth migrate" not in capsys.readouterr().out


# --- griot doctor ----------------------------------------------------------------------------------


def test_doctor_names_credentials_still_in_plaintext_and_the_command_that_moves_them(keychain):
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)
    keychain.store[("griot", "GITLAB_PERSONAL_ACCESS_TOKEN")] = "glpat-" + "g" * 20

    check = _credentials()

    assert check["status"] == "warn"
    assert "GRIOT_OPENAI_API_KEY" in check["detail"] and str(common.ENV_PATH) in check["detail"]
    assert "Fake Keyring" in check["detail"] and "GITLAB_PERSONAL_ACCESS_TOKEN" in check["detail"]
    assert "griot auth migrate" in check["fix"]
    _no_secret_in(check["detail"] + check["fix"])


def test_doctor_moves_nothing(keychain):
    """Never move a secret without being asked: the doctor reads."""
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    _credentials()

    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == SECRET
    assert keychain.store == {}


def test_doctor_says_the_fallback_when_no_backend_is_reachable():
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    check = _credentials()

    assert check["status"] == "warn"
    assert "no OS keychain backend" in check["detail"] and "plaintext" in check["detail"]
    assert "GRIOT_OPENAI_API_KEY" in check["detail"]
    assert "griot[keychain]" in check["fix"]
    _no_secret_in(check["detail"] + check["fix"])


def test_doctor_does_not_say_install_it_when_keyring_is_there_but_finds_no_backend(monkeypatch):
    """Installing the extra again fixes nothing on headless Linux: the advice
    is a backend, then the move."""
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring(_Backend("fail Keyring", 0)))
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    check = _credentials()

    assert check["status"] == "warn" and "griot[keychain]" not in check["detail"] + check["fix"]
    assert "Secret Service" in check["fix"] and "griot auth migrate" in check["fix"]


def test_doctor_is_ok_when_everything_is_in_the_keychain(keychain):
    keychain.store[("griot", "GRIOT_OPENAI_API_KEY")] = SECRET

    check = _credentials()

    assert check["status"] == "ok"
    assert "GRIOT_OPENAI_API_KEY" in check["detail"] and "OS keychain" in check["detail"]
    assert "plaintext" not in check["detail"]


def test_doctor_says_there_is_no_keychain_even_with_nothing_stored():
    check = _credentials()

    assert check["status"] == "ok" and "no OS keychain backend" in check["detail"]


def test_doctor_names_a_credential_from_the_environment_only(keychain, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "h" * 36)
    monkeypatch.setattr(common, "EXPORTED_BEFORE_ENV_FILE", {"GITHUB_TOKEN": "0" * 64})

    check = _credentials()

    assert check["status"] == "ok" and "GITHUB_TOKEN" in check["detail"] and "environment" in check["detail"]


# --- the MCP paths read no keychain ----------------------------------------------------------------


def test_provider_status_reads_no_keychain(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _Untouchable())
    common.env_file_set("GRIOT_OPENAI_API_KEY", SECRET)

    assert any(s["configured"] for s in auth.provider_status())


@pytest.mark.anyio
async def test_the_auth_tool_reads_no_keychain_and_does_not_promise_one(monkeypatch):
    from mcp.client.client import Client

    from griot import mcp_server

    monkeypatch.setitem(sys.modules, "keyring", _Untouchable())
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_auth_guidance", {})
        profiles = await client.call_tool("griot_profiles_list", {})

    assert not result.is_error and not profiles.is_error
    how = result.structured_content["how_to_set"]
    # It cannot know without reading the keychain, so it must not say the
    # keychain unconditionally: a reader with no backend would believe it.
    assert "OS keychain" in how and ".env" in how and "griot auth list" in how
