"""Tests for `common.ensure_env_template()` — writes <config_dir>/.env
pre-populated with every environment variable griot supports on first run
(closest thing to "on install" a plain pip package can hook into — no
reliable post-install hook exists, so this runs lazily the first time any
command needs the config dir, via cli.main() and auth._ensure_env_file()).
Real defaults are written out explicitly (behaviorally identical to leaving
them unset); credentials get an empty placeholder line, never a fake value.
"""

import os
import stat

from dotenv import dotenv_values

from griot import auth, common


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_creates_file_with_0600_permissions():
    common.ensure_env_template()
    assert common.ENV_PATH.exists()
    assert _mode(common.ENV_PATH) == 0o600


def test_never_overwrites_an_existing_file():
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text("GEMINI_TOKEN=ja-configurada-de-verdade\n")

    common.ensure_env_template()

    assert common.ENV_PATH.read_text() == "GEMINI_TOKEN=ja-configurada-de-verdade\n"


def test_generated_content_is_valid_dotenv():
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    assert "GEMINI_TOKEN" in values
    assert "GRIOT_EMBED_PROFILE" in values


def test_includes_every_provider_from_auth_providers():
    """Single source of truth: whatever auth._providers() knows about
    (EMBED_PROFILES/CHAT_PROFILES api_key_env + the 5 platform tokens) must
    all show up here — new provider added later gets picked up automatically."""
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    for env_var in auth._providers().values():
        assert env_var in values, f"{env_var} missing from generated .env template"


def test_credential_vars_are_empty_not_fake_placeholders():
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    for env_var in auth._providers().values():
        assert values[env_var] in (None, ""), f"{env_var} should be empty, got {values[env_var]!r}"


def test_operational_vars_match_real_code_defaults():
    """Writing these explicitly must be behaviorally IDENTICAL to leaving
    them unset — same literal default the code already falls back to."""
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    assert values["GRIOT_EMBED_PROFILE"] == "jina-code"
    assert values["GRIOT_CHAT_PROFILE"] == "gemini"
    assert values["GRIOT_SPEND_CEILING_USD"] == "3.0"
    assert values["GRIOT_SPEND_VELOCITY_CEILING_USD"] == "1.0"
    assert values["GRIOT_LOG_QUESTIONS"] == "true"
    assert values["GRIOT_MCP_ENABLE_INDEX"] == "false"
    assert values["GRIOT_MCP_IDLE_RELEASE_SECONDS"] == "30"
    assert values["GRIOT_GITLAB_API_BASE"] == "https://gitlab.com/api/v4"


def test_concurrency_mode_is_commented_out_so_it_follows_the_code_default():
    """A default written out explicitly is an override in disguise: a .env
    generated while the default was `single` kept pinning it after the default
    became `multi`. Left commented, the template documents the value without
    freezing it."""
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    assert "GRIOT_MCP_CONCURRENCY_MODE" not in values
    assert "#GRIOT_MCP_CONCURRENCY_MODE=multi" in common.ENV_PATH.read_text().splitlines()


def test_price_vars_without_a_safe_default_are_commented_out_not_empty():
    """[real bug found via smoke test] _optional_float_env() checks
    `value is not None`, not a falsy check — a dotenv line like `VAR=`
    (present, empty) sets os.environ[VAR] = "", which is NOT None, so
    float("") raises ValueError at import time. Only a genuinely ABSENT key
    (line commented out) is safe here — unlike the credential vars, which
    use `if not token` (falsy) checks and are fine either way."""
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    assert "GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS" not in values
    assert "GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS" not in values


def test_generated_template_never_crashes_common_py_on_import():
    """End-to-end regression for the bug above: load the generated .env for
    real (as griot's own import machinery does) and confirm every
    _optional_float_env()-backed var parses without raising.

    [real finding] load_dotenv(..., override=True) injects
    EVERY var from the generated template into the REAL process os.environ
    — not just the 3 optional price vars this test originally cared about.
    Only popping those 3 in `finally` left ~17 others (GRIOT_EMBED_PROFILE,
    GRIOT_SPEND_CEILING_USD, etc.) permanently polluting os.environ for the
    rest of the pytest session — invisible until a later test happens to
    compare os.environ against a fresh per-test .env file (as
    tests/test_ui_app.py's shadowed-by-shell tests do) and gets a stale
    leaked value instead of a clean one. Full snapshot/restore instead of
    an incomplete allowlist of vars to pop — robust even if the template
    gains new vars later."""
    common.ensure_env_template()
    from dotenv import load_dotenv
    environ_backup = dict(os.environ)
    try:
        load_dotenv(common.ENV_PATH, override=True)
        assert common._optional_float_env("GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS") is None
        assert common._optional_float_env("GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS") is None
        assert common._optional_float_env("GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS") is None
    finally:
        os.environ.clear()
        os.environ.update(environ_backup)


def test_config_dir_data_dir_are_not_written_into_the_file():
    """GRIOT_CONFIG_DIR/GRIOT_DATA_DIR decide WHERE this file lives — putting
    them inside it would be circular, they must stay real shell/session vars."""
    common.ensure_env_template()
    values = dotenv_values(common.ENV_PATH)
    assert "GRIOT_CONFIG_DIR" not in values
    assert "GRIOT_DATA_DIR" not in values


def test_cli_main_triggers_template_creation_on_any_real_subcommand(monkeypatch, capsys):
    """--version/--help exit inside argparse's own parse_args() and never
    reach the hook — by design, so they stay cheap (see cli.py's module
    docstring on lazy dispatch). Any REAL subcommand does reach it."""
    from griot import cli

    calls = []
    monkeypatch.setattr(common, "ensure_env_template", lambda: calls.append(1))

    cli.main(["repos", "list"])

    assert calls == [1]


def test_cli_main_does_not_trigger_template_creation_for_version_flag(monkeypatch):
    from griot import cli

    calls = []
    monkeypatch.setattr(common, "ensure_env_template", lambda: calls.append(1))

    try:
        cli.main(["--version"])
    except SystemExit:
        pass

    assert calls == []


def test_auth_ensure_env_file_delegates_to_ensure_env_template(monkeypatch):
    calls = []
    monkeypatch.setattr(common, "ensure_env_template", lambda: calls.append(1))

    auth._ensure_env_file()

    assert calls == [1]


# --- common.env_file_set()/env_file_unset(): the single write
# path into .env, extracted out of auth.cmd_set() so a future writer (the
# path) doesn't duplicate the "ensure template exists, then
# re-chmod 0600 because set_key() rewrites the whole file" discipline. ---

def test_env_file_set_creates_template_first_if_missing():
    assert not common.ENV_PATH.exists()
    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")
    values = dotenv_values(common.ENV_PATH)
    assert values["GEMINI_TOKEN"] == "gm-fake-1234"
    # template creation still ran — some other var from the template is present
    assert "GRIOT_EMBED_PROFILE" in values


def test_env_file_set_reasserts_0600_after_rewrite():
    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")
    assert stat.S_IMODE(common.ENV_PATH.stat().st_mode) == 0o600


def test_env_file_set_preserves_other_existing_keys():
    common.env_file_set("GEMINI_TOKEN", "gm-first")
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-second")
    values = dotenv_values(common.ENV_PATH)
    assert values["GEMINI_TOKEN"] == "gm-first"
    assert values["GRIOT_OPENAI_API_KEY"] == "sk-second"


def test_env_file_unset_removes_only_the_given_var():
    common.env_file_set("GEMINI_TOKEN", "gm-fake-1234")
    common.env_file_set("GRIOT_OPENAI_API_KEY", "sk-fake-5678")

    common.env_file_unset("GEMINI_TOKEN")

    values = dotenv_values(common.ENV_PATH)
    assert "GEMINI_TOKEN" not in values
    assert values["GRIOT_OPENAI_API_KEY"] == "sk-fake-5678"


def test_env_file_unset_on_absent_var_is_a_noop():
    common.ensure_env_template()
    before = common.ENV_PATH.read_text()
    common.env_file_unset("SOME_VAR_NEVER_SET")
    assert common.ENV_PATH.read_text() == before
