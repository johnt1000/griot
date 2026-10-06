"""`griot auth` — management of griot's external credentials: paid embedding
providers and the GitLab token (`griot index
gitlab`, not an embedding profile, but the same category of "secret in
.env that today can only be edited by hand"). It's a secure editor for
<config_dir>/.env, NOT OAuth/login — none of these credentials use an
interactive flow, so there's no reason to invent that abstraction here.
"""

import argparse
import getpass
import os
import sys
from dataclasses import dataclass

from dotenv import dotenv_values

from griot import common


def _provider_label(api_key_env: str) -> str:
    """Derives a human-readable provider name from the env var name (e.g.
    GRIOT_OPENAI_API_KEY -> "openai") — no duplicated table (the design notes): the provider->env var mapping comes from EMBED_PROFILES/
    CHAT_PROFILES (api_key_env field, the design notes), this just
    formats the name for display/CLI argument."""
    name = api_key_env.removeprefix("GRIOT_").removesuffix("_API_KEY").removesuffix("_EMBED")
    return name.lower()


def _providers() -> dict[str, str]:
    """provider -> env var name. Thin wrapper — the actual logic lives in
    common.credential_env_vars() now (moved there so common.py's own
    keychain-injection code can use it without importing auth.py and
    creating a cycle); kept here as auth.py's own public name so nothing
    else in this module (or any existing caller of auth._providers())
    needs to change."""
    return common.credential_env_vars()


def _mask(value: str) -> str:
    """Never echoes the whole key (section 11.1) — only the last 4
    characters, enough to recognize which key it is without being a usable
    credential if it leaks into an accidental log/print."""
    if len(value) <= 4:
        return "***"
    return f"...{value[-4:]}"


def _ensure_env_file() -> None:
    common.ensure_env_template()


def provider_status() -> list[dict]:
    """Per-provider status — the data behind cmd_list(), split out so a
    non-terminal caller can render it without going through print(). Read
    today by griot_auth_guidance and griot_profiles_list. One dict per provider: provider, env_var, configured,
    file_masked, env_masked, shadowed_by_env.

    file_masked reads <config_dir>/.env directly (dotenv_values) — the
    correct source of truth for a UI that may have just written a new value
    itself: common.py resolves .env into os.environ ONCE, at import time,
    so a long-lived process's own os.getenv() goes stale right after a
    write in that same process. env_masked is what os.getenv() sees right
    now (whatever the process inherited from the shell, if anything).
    shadowed_by_env is True when both are set and differ — that's the case
    where editing the file has NO effect on this running process (a shell
    export always wins) and a UI needs to say so, not silently show a
    change that isn't real."""
    providers = _providers()
    file_values = dotenv_values(common.ENV_PATH) if common.ENV_PATH.exists() else {}
    statuses = []
    for provider in sorted(providers):
        env_var = providers[provider]
        file_value = file_values.get(env_var) or None
        env_value = os.getenv(env_var) or None
        statuses.append({
            "provider": provider,
            "env_var": env_var,
            "configured": bool(file_value or env_value),
            "file_masked": _mask(file_value) if file_value else None,
            "env_masked": _mask(env_value) if env_value else None,
            # The file only, as before: this is read by MCP tools on every
            # call, and reading the keychain there can ask the person on
            # macOS. `griot auth list` and `griot doctor` compare with the
            # keychain too (common.credential_origin).
            "shadowed_by_env": bool(env_value and file_value and env_value != file_value),
        })
    return statuses


def set_provider_key(provider: str, key: str) -> bool:
    """Write half of provider_status() — validates the provider
    and the key. Returns True if it REPLACED an already-configured value
    (the caller may want to warn about a running MCP server not picking
    this up until restart, same as cmd_set() prints). Raises ValueError on
    an unknown provider or an empty key — never on a missing .env (that
    case is just "nothing replaced").

    [security review] Prefers the OS keychain (common._keychain_set()) —
    when that succeeds, the value is deliberately NOT ALSO written to the
    plaintext .env file (storing it in two places would defeat the point).
    Falls back to the existing common.env_file_set() path (same one
    cmd_set() always used) whenever the keychain is unavailable — no
    `keyring` installed, no reachable backend (headless Linux without a
    Secret Service provider, a container), or any other failure. This
    fallback is silent by design: a missing OS keychain is an expected,
    normal environment, not an error condition worth surfacing.

    [real gap, review-caught] A credential that already exists in
    plaintext .env (set before this feature existed, or on a machine that
    didn't have a keychain available at the time) must have that stale
    entry REMOVED once the value is successfully re-set into the
    keychain — otherwise re-running `griot auth set` to "move it to the
    keychain" gives a false sense of improved security while the old
    plaintext copy sits there untouched at 0600, readable, right next to
    the new keychain entry."""
    providers = _providers()
    if provider not in providers:
        raise ValueError(f"Unknown provider {provider!r}. Options: {', '.join(sorted(providers))}")
    key = key.strip()
    if not key:
        raise ValueError("Empty key — nothing to write.")

    env_var = providers[provider]
    existing_in_file = dotenv_values(common.ENV_PATH).get(env_var) if common.ENV_PATH.exists() else None
    if common._keychain_set(env_var, key):
        if existing_in_file:
            common.env_file_unset(env_var)
        return bool(existing_in_file)
    common.env_file_set(env_var, key)
    return bool(existing_in_file)


@dataclass(frozen=True)
class KeyRemoval:
    """What remove_provider_key() did. `removed`: a key existed (in the
    keychain and/or the file) and was removed from wherever it was found.
    `keychain`: the common.KEYCHAIN_* result, so a caller can tell an
    unreachable keychain (a stored copy may remain) from an empty one."""
    removed: bool
    keychain: str


def remove_provider_key(provider: str) -> KeyRemoval:
    """Write half of provider_status() for deletion. Raises ValueError on
    an unknown provider. The file is cleaned even when the keychain cannot
    be reached: one unreachable store must not keep the other's copy.

    [security review] Removes from BOTH the keychain and the file,
    unconditionally — a credential set before this feature existed only
    ever lives in the file; one set after may live only in the keychain;
    either is a real, independent "was this actually configured" signal,
    so neither check alone is sufficient."""
    providers = _providers()
    if provider not in providers:
        raise ValueError(f"Unknown provider {provider!r}. Options: {', '.join(sorted(providers))}")

    env_var = providers[provider]
    keychain = common._keychain_delete(env_var)
    removed_from_file = common.ENV_PATH.exists() and env_var in dotenv_values(common.ENV_PATH)
    if removed_from_file:
        common.env_file_unset(env_var)
    return KeyRemoval(removed=keychain == common.KEYCHAIN_DELETED or removed_from_file, keychain=keychain)


def cmd_set(provider: str) -> int:
    providers = _providers()
    if provider not in providers:
        print(f"Error: unknown provider '{provider}'. Options: {', '.join(sorted(providers))}", file=sys.stderr)
        return 1
    env_var = providers[provider]

    key = getpass.getpass(f"Key for {provider} ({env_var}, hidden input): ")
    if not key.strip():
        print("Error: empty key, nothing was written.", file=sys.stderr)
        return 1

    _ensure_env_file()
    replaced = set_provider_key(provider, key)

    print(f"{env_var} written to {common.ENV_PATH} (permission 0600).")
    _say_if_the_shell_overrides(env_var)
    if replaced:
        # [review] common.py resolves .env once, at import time
        # — a long-lived MCP server already running won't see this change
        # until it restarts. The CLI is a new process on every call, so this
        # doesn't affect `griot auth set` itself, only concurrent MCP sessions.
        print("Warning: an already-running MCP server won't see this change until restarted (common.py resolves .env only at import time).")
    return 0


def _say_if_the_shell_overrides(env_var: str) -> None:
    """After a key was written: an export of a DIFFERENT value in the shell
    wins over it, here and in every terminal that sets it, and nothing else
    would say so (the next API refusal looked like a bad new key)."""
    origin = common.credential_origin(env_var)
    if not origin["shadows_stored"]:
        return
    where = ", ".join(origin["exported_in"]) or "this shell (not in a shell file or direnv file griot knows)"
    print(f"Warning: {env_var} is also exported, with a different value, in {where}. The environment wins, so "
          f"griot keeps using that one: remove the export there, and run `unset {env_var}` in terminals already open.")


def cmd_list() -> int:
    # env-first precedence preserved exactly as before (a shell-exported
    # value always wins over the file for THIS process) — provider_status()
    # exposes both explicitly (file_masked/env_masked) for a UI that needs
    # to show the file's value even when it's currently shadowed; the CLI
    # keeps showing only what's effectively active.
    for status in provider_status():
        masked = status["env_masked"] or status["file_masked"]
        line = f"✓ configured ({masked})" if masked else f"✗ missing — griot auth set {status['provider']}"
        origin = common.credential_origin(status["env_var"])
        if origin["shadows_stored"]:
            where = ", ".join(origin["exported_in"]) or "this shell"
            line += (f" — from the environment ({where}), which overrides the key griot stores; the stored one "
                     f"differs and is not used")
        print(f"  {status['provider']:<10} {line}")
    return 0


def _has_stored_key(provider: str) -> bool:
    """Read-only twin of remove_provider_key()'s return value: whether a key
    is stored in the keychain or in the file, the two places it removes from."""
    env_var = _providers()[provider]
    in_file = common.ENV_PATH.exists() and env_var in dotenv_values(common.ENV_PATH)
    return bool(in_file or common._keychain_get(env_var))


def cmd_remove(provider: str) -> int:
    providers = _providers()
    if provider not in providers:
        print(f"Error: unknown provider '{provider}'. Options: {', '.join(sorted(providers))}", file=sys.stderr)
        return 1
    env_var = providers[provider]
    result = remove_provider_key(provider)
    unreachable = result.keychain == common.KEYCHAIN_UNREACHABLE
    if not result.removed and not unreachable:
        print(f"{env_var} was not configured — nothing to remove.")
        return 0
    if result.removed:
        print(f"{env_var} removed from {common.ENV_PATH}.")
        origin = common.credential_origin(env_var)
        if origin["source"] == "environment":
            where = ", ".join(origin["exported_in"]) or "this shell"
            print(f"Note: {env_var} is still exported in {where}: griot keeps using that value until the export is removed.")
    if unreachable:
        # Not "nothing to remove": a key stored there earlier may still be
        # there, and saying it is gone would be the one wrong answer. Exit 1
        # because the removal that was asked for could not be confirmed.
        print(f"Warning: the OS keychain could not be reached, so a copy of {env_var} stored there, if any, was "
              f"not removed. Run `griot auth remove {provider}` again where the keychain is available.",
              file=sys.stderr)
        return 1
    return 0


def cmd_migrate() -> int:
    """Bulk version of running `griot auth set <provider>` for every
    provider already configured in the plaintext .env file — same
    keychain-write + file-unset sequence set_provider_key() already
    performs for one provider, just batched (docs/lessons-and-debts.md's
    "Open debts" #1: this is an explicit, user-invoked action, the same
    kind as running `set` N times, not the silent/automatic migration
    that debt deliberately avoids)."""
    providers = _providers()
    file_values = dotenv_values(common.ENV_PATH) if common.ENV_PATH.exists() else {}

    migrated = []
    could_not_migrate = []
    for provider in sorted(providers):
        env_var = providers[provider]
        file_value = file_values.get(env_var) or None
        if not file_value:
            continue
        if common._keychain_set(env_var, file_value):
            common.env_file_unset(env_var)
            migrated.append(provider)
        else:
            could_not_migrate.append(provider)

    if not migrated and not could_not_migrate:
        print(f"Nothing to migrate — no credentials found in {common.ENV_PATH}.")
        return 0

    if migrated:
        print(f"Migrated to the OS keychain: {', '.join(migrated)}.")
    if could_not_migrate:
        print(f"Could not migrate (no keychain backend available): {', '.join(could_not_migrate)}.")
        print('Install `pip install "griot[keychain]"` for OS keychain support.')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot auth",
        description="Manages paid embedding provider keys (securely edits <config_dir>/.env).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_set = sub.add_parser("set", help="Sets a provider's key (hidden input)")
    p_set.add_argument("provider")

    sub.add_parser("list", help="Lists status of each provider (key always masked)")

    p_remove = sub.add_parser("remove", help="Removes a provider's key")
    p_remove.add_argument("provider")
    p_remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    sub.add_parser(
        "migrate",
        help="Moves every credential currently in the plaintext .env file into the OS keychain (no-op for any without a reachable backend)",
    )

    args = parser.parse_args(argv)
    if args.action == "set":
        return cmd_set(args.provider)
    if args.action == "list":
        return cmd_list()
    if args.action == "migrate":
        return cmd_migrate()
    # Asked only when there is something to lose: an unknown provider or one
    # with nothing stored goes straight to cmd_remove(), which says so. The
    # question names the provider and the variable, never the key.
    providers = _providers()
    if args.provider in providers and _has_stored_key(args.provider):
        refused = common.confirm(f"Remove the stored key for {args.provider} ({providers[args.provider]})? "
                                 f"You will need the key again to use that provider.", yes=args.yes)
        if refused:
            return refused
    return cmd_remove(args.provider)


if __name__ == "__main__":
    raise SystemExit(main())
