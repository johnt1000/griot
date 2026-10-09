"""Tests for `griot auth` — safe editor for the .env
file for paid embedding providers and code platform credentials
(GitHub/GitLab/Bitbucket/Azure DevOps/Gitea, see platforms.py). Never calls
getpass for real (mocked), never lets the full key appear in captured
stdout/stderr.
"""

import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from griot import auth, common


class _FakeKeyring:
    """Minimal stand-in for the `keyring` module's public API, same shape
    as tests/test_keychain.py's helper — enough to exercise the "available"
    path without ever touching a real OS keychain."""

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


def test_providers_derives_from_embed_profiles():
    providers = auth._providers()
    assert providers["gemini"] == "GEMINI_TOKEN"
    assert providers["openai"] == "GRIOT_OPENAI_API_KEY"


@pytest.mark.parametrize("provider,env_var", [
    ("gitlab", "GITLAB_PERSONAL_ACCESS_TOKEN"),
    ("github", "GITHUB_TOKEN"),
    ("bitbucket", "BITBUCKET_ACCESS_TOKEN"),
    ("azure_devops", "AZURE_DEVOPS_PAT"),
    ("gitea", "GITEA_TOKEN"),
])
def test_providers_includes_platform_credentials(provider, env_var):
    """None of these 5 is an embedding profile credential (none is in
    EMBED_PROFILES) — they're used only by `griot index platform`. Still,
    they need to be manageable by griot auth, like any other external griot
    credential (same reasoning that used to apply only to GitLab)."""
    assert auth._providers()[provider] == env_var


@pytest.mark.parametrize("provider,env_var,fake_value", [
    ("gitlab", "GITLAB_PERSONAL_ACCESS_TOKEN", "glpat-fake-token-9999"),
    ("github", "GITHUB_TOKEN", "ghp_fake-token-9999"),
    ("bitbucket", "BITBUCKET_ACCESS_TOKEN", "bb-fake-token-9999"),
    ("azure_devops", "AZURE_DEVOPS_PAT", "azdo-fake-token-9999"),
    ("gitea", "GITEA_TOKEN", "gitea-fake-token-9999"),
])
def test_cmd_set_platform_credential_writes_to_env_file(monkeypatch, provider, env_var, fake_value):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: fake_value)

    assert auth.cmd_set(provider) == 0

    assert dotenv_values(common.ENV_PATH)[env_var] == fake_value


def test_provider_label_strips_griot_prefix_and_api_key_suffix():
    assert auth._provider_label("GRIOT_OPENAI_API_KEY") == "openai"
    assert auth._provider_label("GRIOT_VOYAGE_API_KEY") == "voyage"


def test_mask_shows_only_last_four_chars():
    assert auth._mask("sk-proj-abcdefgh1234") == "...1234"
    assert auth._mask("ab") == "***"


def test_cmd_set_writes_key_to_env_file_with_0600(monkeypatch):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-fake-key-1234")

    rc = auth.cmd_set("openai")

    assert rc == 0
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-fake-key-1234"
    mode = stat.S_IMODE(common.ENV_PATH.stat().st_mode)
    assert mode == 0o600


def test_cmd_set_never_echoes_full_key(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-super-secret-does-not-leak")

    auth.cmd_set("openai")

    out = capsys.readouterr().out
    assert "sk-super-secret-does-not-leak" not in out


def test_cmd_set_rejects_unknown_provider(capsys):
    rc = auth.cmd_set("unknown-provider")
    assert rc != 0
    assert "unknown" in capsys.readouterr().err


def test_cmd_set_rejects_empty_key(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "   ")
    rc = auth.cmd_set("openai")
    assert rc != 0
    assert not common.ENV_PATH.exists() or "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_cmd_set_warns_when_overwriting_existing_key(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "key-one")
    auth.cmd_set("openai")
    capsys.readouterr()

    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "key-two")
    auth.cmd_set("openai")

    out = capsys.readouterr().out
    assert "restart" in out.lower()


def test_cmd_list_shows_configured_and_missing(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-fake-key-9999")
    auth.cmd_set("openai")
    capsys.readouterr()

    rc = auth.cmd_list()
    out = capsys.readouterr().out

    assert rc == 0
    assert "openai" in out and "9999" in out and "sk-fake-key-9999" not in out
    assert "gemini" in out and ("missing" in out or "griot auth set gemini" in out)


def test_cmd_remove_deletes_key(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-fake-key-0000")
    auth.cmd_set("openai")

    rc = auth.cmd_remove("openai")

    assert rc == 0
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_cmd_remove_on_unconfigured_provider_is_a_noop(capsys):
    rc = auth.cmd_remove("openai")
    assert rc == 0
    assert "was not configured" in capsys.readouterr().out


def test_cmd_remove_rejects_unknown_provider(capsys):
    rc = auth.cmd_remove("unknown-provider")
    assert rc != 0
    assert "unknown" in capsys.readouterr().err


def test_cmd_set_gemini_special_case_uses_gemini_token(monkeypatch):
    """gemini is the only profile without api_key_env (it uses the historical
    env var GEMINI_TOKEN, not GRIOT_*) — review finding: only _providers()
    (a pure function) was tested for this case, never cmd_set/cmd_list/cmd_remove
    end-to-end with the project's oldest/most-used provider."""
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "gm-fake-token-5678")

    assert auth.cmd_set("gemini") == 0
    assert dotenv_values(common.ENV_PATH)["GEMINI_TOKEN"] == "gm-fake-token-5678"

    out_list = auth.cmd_list()
    assert out_list == 0

    assert auth.cmd_remove("gemini") == 0
    assert "GEMINI_TOKEN" not in dotenv_values(common.ENV_PATH)


def test_cmd_list_on_fresh_install_no_env_file(capsys):
    """Day 1 (review finding): no .env exists yet — cmd_list() must not
    break, it should just report everything as missing."""
    assert not common.ENV_PATH.exists()

    rc = auth.cmd_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "gemini" in out and "missing" in out
    assert "openai" in out and "missing" in out


def test_cmd_set_preserves_other_existing_keys_in_env_file(monkeypatch):
    """Review finding: python-dotenv's set_key()/unset_key() rewrite the
    entire file — confirms that unrelated keys (GEMINI_TOKEN,
    GITLAB_PERSONAL_ACCESS_TOKEN) survive intact through a `griot auth set
    openai` and a subsequent `griot auth remove openai`."""
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text(
        'GEMINI_TOKEN=gm-already-configured-before\n'
        'GITLAB_PERSONAL_ACCESS_TOKEN=glpat-also-already-configured\n'
    )

    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-openai-new-1234")
    auth.cmd_set("openai")

    values = dotenv_values(common.ENV_PATH)
    assert values["GEMINI_TOKEN"] == "gm-already-configured-before"
    assert values["GITLAB_PERSONAL_ACCESS_TOKEN"] == "glpat-also-already-configured"
    assert values["GRIOT_OPENAI_API_KEY"] == "sk-openai-new-1234"

    auth.cmd_remove("openai")
    values = dotenv_values(common.ENV_PATH)
    assert values["GEMINI_TOKEN"] == "gm-already-configured-before"
    assert values["GITLAB_PERSONAL_ACCESS_TOKEN"] == "glpat-also-already-configured"
    assert "GRIOT_OPENAI_API_KEY" not in values


# --- provider_status()/set_provider_key()/remove_provider_key():
# same data/behavior as cmd_list()/cmd_set()/cmd_remove(), split from
# getpass/print so a non-terminal front end can reuse it. ---

def test_provider_status_reports_missing_and_configured():
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-fake-1234")

    statuses = {s["provider"]: s for s in auth.provider_status()}

    assert statuses["openai"]["configured"] is True
    assert statuses["openai"]["file_masked"] == "...1234"
    assert "sk-fake-1234" not in str(statuses["openai"])  # never the raw value
    assert statuses["gemini"]["configured"] is False
    assert statuses["gemini"]["file_masked"] is None


def test_provider_status_file_masked_reflects_the_file_not_process_env(monkeypatch):
    """[real finding] common.py resolves .env into os.environ ONCE at
    import — a long-lived process (the UI) that just wrote a new key via
    set_provider_key() would show the OLD value if it read os.getenv()
    first, same precedence bug cmd_list() has always had (env-first). file_masked
    must always reflect the FILE, so a UI showing it right after a write is
    correct without needing to restart the process."""
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-stale-from-shell-0000")
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-fresh-in-file-9999")

    statuses = {s["provider"]: s for s in auth.provider_status()}

    assert statuses["openai"]["file_masked"] == "...9999"
    assert statuses["openai"]["env_masked"] == "...0000"  # what the process env still has
    assert statuses["openai"]["shadowed_by_env"] is True


def test_provider_status_not_shadowed_when_file_and_env_agree(monkeypatch):
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-same-1234")
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-same-1234")

    statuses = {s["provider"]: s for s in auth.provider_status()}
    assert statuses["openai"]["shadowed_by_env"] is False


def test_set_provider_key_writes_and_returns_whether_it_replaced():
    replaced_first = auth.set_provider_key("openai", "sk-first-1234")
    assert replaced_first is False
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-first-1234"

    replaced_second = auth.set_provider_key("openai", "sk-second-5678")
    assert replaced_second is True
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-second-5678"


def test_set_provider_key_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown-provider"):
        auth.set_provider_key("unknown-provider", "x")


def test_set_provider_key_rejects_empty_key():
    with pytest.raises(ValueError):
        auth.set_provider_key("openai", "   ")


def test_remove_provider_key_deletes_and_returns_whether_it_existed():
    assert auth.remove_provider_key("openai").removed is False  # nothing to remove
    auth.set_provider_key("openai", "sk-fake-0000")
    assert auth.remove_provider_key("openai").removed is True
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


def test_remove_provider_key_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown-provider"):
        auth.remove_provider_key("unknown-provider")


def test_cmd_set_still_uses_set_provider_key_precedence_preserved(monkeypatch, capsys):
    """cmd_list() keeps its EXISTING env-first precedence (os.getenv() or
    file) after the refactor — provider_status() is additive, not a
    behavior change for the CLI."""
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-from-shell-1111")
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-from-file-2222")

    auth.cmd_list()
    out = capsys.readouterr().out
    assert "1111" in out
    assert "2222" not in out


def test_main_dispatches_set_list_remove(monkeypatch):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-via-main-1234")
    assert auth.main(["set", "openai"]) == 0
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-via-main-1234"
    assert auth.main(["list"]) == 0
    assert auth.main(["remove", "--yes", "openai"]) == 0
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)


# --- cmd_migrate(): bulk-move every plaintext .env credential to the OS
# keychain, the batched equivalent of running `griot auth set` N times. ---


def test_cmd_migrate_moves_file_credential_to_keychain_when_available(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-plaintext-1234")

    rc = auth.cmd_migrate()

    assert rc == 0
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)
    assert common.keychain_get("GRIOT_OPENAI_API_KEY") == "sk-plaintext-1234"


def test_cmd_migrate_leaves_file_untouched_without_keychain_backend(capsys):
    # No monkeypatch needed — conftest's autouse fixture already forces
    # sys.modules["keyring"] = None, the default "no backend" state.
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-plaintext-5678")

    rc = auth.cmd_migrate()

    assert rc == 0
    assert dotenv_values(common.ENV_PATH)["GRIOT_OPENAI_API_KEY"] == "sk-plaintext-5678"
    out = capsys.readouterr().out
    assert "reinstall griot-rag" in out


def test_cmd_migrate_skips_unconfigured_provider(capsys):
    assert not common.ENV_PATH.exists()

    rc = auth.cmd_migrate()

    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to migrate" in out.lower()


def test_main_dispatches_migrate(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-via-main-migrate")

    assert auth.main(["migrate"]) == 0

    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(common.ENV_PATH)
    assert common.keychain_get("GRIOT_OPENAI_API_KEY") == "sk-via-main-migrate"


# --- running MCP servers keep what they read (debt 71) -----------------------
# A server reads <config_dir>/.env into its environment at start and asks the
# keychain for a credential once, keeping the answer (an absence included)
# for its whole life, so a key stored or removed later reaches it only after
# a restart. `griot auth` has to say so whenever it changes a key, wherever
# the key went, and never when it changed nothing.


def _says_restart(out: str) -> bool:
    return "running griot MCP servers" in out and "/mcp" in out


def _cli_auth(*argv) -> int:
    from griot import cli
    return cli.main(["auth", *argv])


def _fresh_auth(tmp_path, *argv):
    """A real `griot auth` process under the suite's keyring `fail` backend
    (conftest sets PYTHON_KEYRING_BACKEND for every child)."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))
    return subprocess.run([sys.executable, "-m", "griot.cli", "auth", *argv], env=env, capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)


def test_auth_set_of_a_new_key_in_the_file_says_to_restart_running_servers(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-new-key-1111")

    assert _cli_auth("set", "openai") == 0

    out = capsys.readouterr().out
    assert _says_restart(out)
    # The old reason named only .env and import time; a keychain read is
    # kept for the process's life too.
    assert "only at import time" not in out
    note = next(line for line in out.splitlines() if "running griot MCP servers" in line)
    assert "keychain" in note and ".env" in note


def test_auth_set_replacing_a_file_key_says_to_restart_running_servers(monkeypatch, capsys):
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-old-key-0000")
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-new-key-1111")

    assert _cli_auth("set", "openai") == 0

    assert _says_restart(capsys.readouterr().out)


def test_auth_set_of_a_new_key_in_the_keychain_says_to_restart_running_servers(monkeypatch, capsys):
    fake = _FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-new-key-1111")

    assert _cli_auth("set", "openai") == 0

    out = capsys.readouterr().out
    assert fake.store[("griot", "GRIOT_OPENAI_API_KEY")] == "sk-new-key-1111"
    assert _says_restart(out)


def test_auth_set_replacing_a_keychain_key_says_to_restart_running_servers(monkeypatch, capsys):
    fake = _FakeKeyring()
    fake.store[("griot", "GRIOT_OPENAI_API_KEY")] = "sk-old-key-0000"
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "sk-new-key-1111")

    assert _cli_auth("set", "openai") == 0

    out = capsys.readouterr().out
    assert fake.store[("griot", "GRIOT_OPENAI_API_KEY")] == "sk-new-key-1111"
    assert _says_restart(out)


def test_auth_set_that_writes_nothing_does_not_mention_restart(monkeypatch, capsys):
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: "   ")

    assert _cli_auth("set", "openai") != 0
    assert _cli_auth("set", "no-such-provider") != 0

    captured = capsys.readouterr()
    assert "restart" not in (captured.out + captured.err).lower()


def test_auth_remove_of_a_keychain_key_says_to_restart_running_servers(monkeypatch, capsys):
    fake = _FakeKeyring()
    fake.store[("griot", "GRIOT_OPENAI_API_KEY")] = "sk-old-key-0000"
    monkeypatch.setitem(sys.modules, "keyring", fake)

    assert _cli_auth("remove", "openai", "--yes") == 0

    out = capsys.readouterr().out
    assert ("griot", "GRIOT_OPENAI_API_KEY") not in fake.store
    assert _says_restart(out)


def test_auth_remove_of_a_file_key_says_to_restart_running_servers_in_a_real_process(tmp_path):
    env_path = tmp_path / "config" / "griot" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("GRIOT_OPENAI_API_KEY=sk-old-key-0000\n")

    done = _fresh_auth(tmp_path, "remove", "openai", "--yes")

    # Exit 1: the fail backend cannot confirm the keychain held no copy. The
    # file's copy is gone all the same, and that is a change a running
    # server holds on to.
    assert done.returncode == 1, done.stderr
    assert "keychain could not be reached" in done.stderr
    assert "GRIOT_OPENAI_API_KEY" not in dotenv_values(env_path)
    assert _says_restart(done.stdout)


def test_auth_remove_with_nothing_stored_does_not_mention_restart_in_a_real_process(tmp_path):
    done = _fresh_auth(tmp_path, "remove", "openai", "--yes")

    # Under the fail backend nothing was removed and the keychain could not
    # be asked: a warning, but no change for a server to miss.
    assert done.returncode == 1, done.stderr
    assert "removed from" not in done.stdout
    assert "restart" not in (done.stdout + done.stderr).lower()


def test_auth_migrate_does_not_mention_restart(monkeypatch, capsys):
    """Migrate moves a value from the file to the keychain unchanged: a
    running server already holds that value from the file, and after a
    restart it reads the same one from the keychain, so nothing it would use
    changes, and a note asking for a restart would only teach people to
    ignore it."""
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-old-key-0000")
    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())

    assert _cli_auth("migrate") == 0

    out = capsys.readouterr().out
    assert "Migrated" in out
    assert "restart" not in out.lower()


def test_auth_remove_on_a_fresh_template_removes_nothing_and_keeps_the_placeholder(capsys):
    """The template every config starts with lists `GRIOT_OPENAI_API_KEY=`
    empty: that is no key, so removing reports nothing, says nothing about
    restarting, and leaves the placeholder that makes the variable visible
    in the template."""
    common.ensure_env_template()
    assert dotenv_values(common.ENV_PATH).get("GRIOT_OPENAI_API_KEY") == ""

    assert _cli_auth("remove", "openai", "--yes") == 0

    out = capsys.readouterr().out
    assert "was not configured" in out
    assert "restart" not in out.lower()
    assert "GRIOT_OPENAI_API_KEY" in dotenv_values(common.ENV_PATH)
