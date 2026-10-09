"""Tests for OS keychain credential storage (macOS Keychain / Linux Secret
Service / Windows Credential Manager) — a security-review finding: griot's
credentials (`<config_dir>/.env`) sit in plaintext on disk, protected only
by file permissions (0600) + whatever full-disk encryption the OS
provides. A leaked/misconfigured backup, or another process/user reading
the file directly, gets the raw value. The `keyring` package (optional
dependency, see pyproject.toml's `keychain` extra) is the well-precedented
fix real CLI tools use (gh, docker, aws-cli, 1Password CLI) — this is an
ADDITIVE, best-effort layer: griot's file-based storage stays the fallback
whenever no keychain backend is reachable (headless Linux, a container, no
`keyring` installed at all), never a hard requirement.

conftest.py's autouse fixture forces `sys.modules["keyring"] = None`
before every test — Python's import machinery treats that as "explicitly
unavailable" and raises ImportError on `import keyring`, which
common.py's wrapper functions already catch and degrade from — so by
default every test sees the exact same "no keychain" behavior regardless
of what's actually installed on the machine running the suite, and the
REAL OS keychain is never touched. Tests in this file that need the
"available" path override `sys.modules["keyring"]` with a fake module
scoped to just that test.
"""

import sys

import pytest
from dotenv import dotenv_values

from griot import auth, common


class _FakeKeyring:
    """Minimal stand-in for the `keyring` module's public API — enough to
    exercise common.py's wrapper functions without ever touching a real
    OS keychain."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.store:
            raise Exception(f"no such credential: {service}/{username}")
        del self.store[key]


# --- keychain_get/_set/_delete (wiring against the `keyring` API) --------


def test_keychain_unavailable_by_default_in_tests():
    """Sanity check on the autouse fixture itself: without any per-test
    override, the wrapper functions must already report 'unavailable'."""
    assert common.keychain_get("GRIOT_TEST_KEY") is None
    assert common.keychain_set("GRIOT_TEST_KEY", "value") is False
    assert common.keychain_delete("GRIOT_TEST_KEY") == common.KEYCHAIN_NOT_INSTALLED


def test_keychain_set_and_get_round_trip(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())

    assert common.keychain_set("GRIOT_TEST_KEY", "sk-fake-1234") is True
    assert common.keychain_get("GRIOT_TEST_KEY") == "sk-fake-1234"


def test_keychain_get_returns_none_when_backend_raises(monkeypatch):
    class _BrokenKeyring:
        def get_password(self, *a, **kw):
            raise Exception("no backend available (e.g. headless Linux, no Secret Service)")
    monkeypatch.setitem(sys.modules, "keyring", _BrokenKeyring())
    assert common.keychain_get("GRIOT_TEST_KEY") is None


def test_keychain_set_returns_false_when_backend_raises(monkeypatch):
    class _BrokenKeyring:
        def set_password(self, *a, **kw):
            raise Exception("no backend available")
    monkeypatch.setitem(sys.modules, "keyring", _BrokenKeyring())
    assert common.keychain_set("GRIOT_TEST_KEY", "value") is False


class _PasswordDeleteError(Exception):
    """Stands in for keyring.errors.PasswordDeleteError, which the real
    backends raise both for "nothing to delete" (Secret Service, KWallet,
    Windows, macOS item-not-found) AND for real failures (macOS wraps an
    access denial in the same class; KWallet raises it when the user
    cancels the unlock prompt) — so the class alone cannot tell the two
    cases apart, and keychain_delete() must not try to."""


class _DeniedOnDeleteKeyring(_FakeKeyring):
    """A backend that finds the item but refuses to delete it (macOS:
    KeychainDenied wrapped in PasswordDeleteError)."""

    def delete_password(self, service, username):
        raise _PasswordDeleteError("Can't delete password in keychain: Keychain Access Denied")


class _UnreachableKeyring:
    """keyring.backends.fail.Keyring: every call raises NoKeyringError —
    what headless Linux, or an SSH session with no D-Bus, gets while the
    credential may still sit in the user's real Secret Service."""

    def get_password(self, service, username):
        raise RuntimeError("No recommended backend was available.")

    def delete_password(self, service, username):
        raise RuntimeError("No recommended backend was available.")


class _SilentOnMissingKeyring(_FakeKeyring):
    """keyring.backends.libsecret: delete_password() of an item that does
    not exist returns quietly instead of raising, so "the call did not
    raise" does not mean "something was deleted"."""

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


def test_keychain_delete_says_nothing_stored(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    assert common.keychain_delete("GRIOT_NEVER_STORED") == common.KEYCHAIN_NOTHING_STORED


def test_keychain_delete_says_nothing_stored_on_a_backend_that_does_not_raise(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _SilentOnMissingKeyring())
    assert common.keychain_delete("GRIOT_NEVER_STORED") == common.KEYCHAIN_NOTHING_STORED


def test_keychain_delete_says_deleted_after_removing(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    common.keychain_set("GRIOT_TEST_KEY", "value")
    assert common.keychain_delete("GRIOT_TEST_KEY") == common.KEYCHAIN_DELETED
    assert common.keychain_get("GRIOT_TEST_KEY") is None


def test_keychain_delete_says_unreachable_when_no_backend_answers(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _UnreachableKeyring())
    assert common.keychain_delete("GRIOT_TEST_KEY") == common.KEYCHAIN_UNREACHABLE


def test_keychain_delete_says_unreachable_when_the_delete_itself_is_refused(monkeypatch):
    """The item is there and the backend refuses to delete it: an error,
    not "nothing stored", although the exception class is the one keyring
    also uses for "nothing to delete"."""
    fake = _DeniedOnDeleteKeyring()
    fake.store[(common._KEYCHAIN_SERVICE, "GRIOT_TEST_KEY")] = "value"
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert common.keychain_delete("GRIOT_TEST_KEY") == common.KEYCHAIN_UNREACHABLE


def test_keychain_delete_says_unreachable_when_keyring_is_installed_but_breaks_on_import(monkeypatch):
    """Only a missing package means griot never stored anything there. A
    package that is installed but fails while importing (a broken backend
    plugin, say) may be hiding a stored key, so it is not "not installed"."""
    import builtins
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "keyring":
            raise RuntimeError("backend plugin failed to load")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert common.keychain_delete("GRIOT_TEST_KEY") == common.KEYCHAIN_UNREACHABLE


# --- credential_env_vars() (moved from auth._providers(), single source) --


def test_credential_env_vars_matches_auth_providers():
    """auth._providers() becomes a thin wrapper around this — same data,
    single source of truth in common.py (this module already owns
    EMBED_PROFILES/CHAT_PROFILES, which the provider list is derived
    from)."""
    assert common.credential_env_vars() == auth._providers()
    assert common.credential_env_vars()["gemini"] == "GEMINI_TOKEN"
    assert common.credential_env_vars()["openai"] == "GRIOT_OPENAI_API_KEY"


# --- auth.py: keychain-preferred write path --------------------------------


def test_set_provider_key_prefers_keychain_when_available(monkeypatch):
    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: True)

    replaced = auth.set_provider_key("openai", "sk-fake-1234")

    assert replaced is False
    # [security] the whole point — a credential that goes into the
    # keychain must NOT also land in the plaintext .env file.
    assert not common.ENV_PATH.exists() or "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_set_provider_key_falls_back_to_file_when_keychain_unavailable(monkeypatch):
    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: False)

    auth.set_provider_key("openai", "sk-fake-1234")

    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-fake-1234"


def test_set_provider_key_removes_stale_env_file_entry_when_moved_to_keychain(monkeypatch):
    """[security review, real gap found] A credential that already exists
    in plaintext .env (pre-dating this feature, or set on a machine
    without a keychain available at the time) must have that stale entry
    REMOVED once it's successfully re-set into the keychain — otherwise
    re-running `griot auth set` to "move it to the keychain" gives a false
    sense of improved security while the old plaintext copy sits there
    untouched at 0600, fully readable, right next to the new keychain
    entry."""
    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: False)
    auth.set_provider_key("openai", "sk-old-plaintext-value")
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-old-plaintext-value"

    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: True)
    auth.set_provider_key("openai", "sk-new-keychain-value")

    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_remove_provider_key_removes_from_keychain_too(monkeypatch):
    deleted = []
    monkeypatch.setattr(common, "keychain_delete",
                        lambda env_var: deleted.append(env_var) or common.KEYCHAIN_DELETED)

    result = auth.remove_provider_key("openai")

    assert result.removed is True and result.keychain == common.KEYCHAIN_DELETED
    assert deleted == ["GRIOT_OPENAI_API_KEY"]


def test_remove_provider_key_reports_removed_when_only_in_keychain(monkeypatch):
    """The file has nothing to remove (never written there, keychain-only
    credential) — must still report removed, not silently claim 'nothing to
    remove' just because the FILE-based check alone would say so."""
    monkeypatch.setattr(common, "keychain_delete", lambda env_var: common.KEYCHAIN_DELETED)
    assert not common.ENV_PATH.exists()

    assert auth.remove_provider_key("openai").removed is True


def test_remove_provider_key_still_removes_from_the_file_when_the_keychain_is_unreachable(monkeypatch):
    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: False)
    auth.set_provider_key("openai", "sk-fake-1234")
    monkeypatch.setattr(common, "keychain_delete", lambda env_var: common.KEYCHAIN_UNREACHABLE)

    result = auth.remove_provider_key("openai")

    assert result.removed is True and result.keychain == common.KEYCHAIN_UNREACHABLE
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_auth_remove_says_the_keychain_could_not_be_reached(monkeypatch, capsys):
    """Nothing in the file and an unreachable keychain is NOT "nothing to
    remove": a key griot put there earlier may still be there. Exit 1,
    because what was asked (the key gone) could not be confirmed."""
    monkeypatch.setitem(sys.modules, "keyring", _UnreachableKeyring())

    assert auth.cmd_remove("openai") == 1

    out = capsys.readouterr()
    assert "nothing to remove" not in out.out
    assert "keychain could not be reached" in out.err and "GRIOT_OPENAI_API_KEY" in out.err


def test_auth_remove_removes_the_file_copy_and_still_warns_when_the_keychain_is_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(common, "keychain_set", lambda env_var, value: False)
    auth.set_provider_key("openai", "sk-fake-1234")
    monkeypatch.setitem(sys.modules, "keyring", _UnreachableKeyring())

    assert auth.cmd_remove("openai") == 1

    out = capsys.readouterr()
    assert "removed from" in out.out
    assert "keychain could not be reached" in out.err
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_auth_remove_without_keyring_installed_stays_quiet(capsys):
    """No `keyring` package (the autouse default): griot never stored
    anything in a keychain on this install, so nothing to warn about."""
    assert auth.cmd_remove("openai") == 0
    out = capsys.readouterr()
    assert "nothing to remove" in out.out and out.err == ""


def test_auth_remove_of_a_keychain_only_key_says_so(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    common.keychain_set("GRIOT_OPENAI_API_KEY", "sk-fake-1234")

    assert auth.cmd_remove("openai") == 0

    assert common.keychain_get("GRIOT_OPENAI_API_KEY") is None
    assert capsys.readouterr().err == ""


