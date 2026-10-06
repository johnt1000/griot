"""Tests for the config/data migration to CONFIG_DIR/DATA_DIR (XDG, section 7
of the MCP plan) and the checklist step 1 safeguards: warning for legacy
RAG_* env vars, automatic migration of .spend_state.json (an earlier decision
exception), <config_dir>/.env permission, and override via
GRIOT_CONFIG_DIR/GRIOT_DATA_DIR."""

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from griot import common

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

_ALL_LEGACY_VARS = list(common._LEGACY_ENV_RENAMES)


@pytest.fixture
def no_legacy_env(monkeypatch):
    """Ensures no legacy RAG_* leaked from the real shell into the test — the
    warning tests control exactly which ones are set."""
    for var in _ALL_LEGACY_VARS:
        monkeypatch.delenv(var, raising=False)


# --- warning for legacy RAG_* env vars ---

def test_legacy_env_var_warns_with_new_name(monkeypatch, caplog, no_legacy_env):
    monkeypatch.setenv("RAG_SPEND_CEILING_USD", "3.0")
    with caplog.at_level(logging.WARNING, logger="griot"):
        common.warn_legacy_env_vars()
    assert "RAG_SPEND_CEILING_USD" in caplog.text
    assert "GRIOT_SPEND_CEILING_USD" in caplog.text


def test_legacy_env_var_warns_once_per_var_set(monkeypatch, caplog, no_legacy_env):
    monkeypatch.setenv("RAG_EMBED_PROFILE", "gemini")
    monkeypatch.setenv("RAG_CHAT_MODEL", "gemini-2.5-flash")
    with caplog.at_level(logging.WARNING, logger="griot"):
        common.warn_legacy_env_vars()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "GRIOT_EMBED_PROFILE" in caplog.text
    assert "GRIOT_CHAT_MODEL" in caplog.text


def test_no_warning_when_no_legacy_env_var(caplog, no_legacy_env):
    with caplog.at_level(logging.WARNING, logger="griot"):
        common.warn_legacy_env_vars()
    assert caplog.records == []


# --- automatic migration of .spend_state.json (an earlier decision, exception to "warning only") ---

def test_spend_state_migrates_automatically_from_legacy_root(tmp_path):
    """Real finding (2026-08-14): the previous version of this test hardcoded
    "date": "2026-08-13" — get_spend_today() resets daily (by design), so
    the test would break on its own once the real clock rolled over to the next day, even
    without any code change. Uses common._today() (LOCAL date, not UTC — see
    common.py) to match exactly what get_spend_today() compares against."""
    legacy_root = tmp_path / "old-checkout"
    legacy_root.mkdir()
    state = {"date": common._today(), "spend_usd": 1.23, "recent_events": []}
    (legacy_root / ".spend_state.json").write_text(json.dumps(state))

    assert not common.SPEND_STATE_PATH.exists()
    common.migrate_legacy_spend_state(legacy_root=legacy_root)

    # copied (not moved) — the day's counter survives the layout change,
    # instead of silently resetting (which would defeat the circuit breaker)
    assert json.loads(common.SPEND_STATE_PATH.read_text()) == state
    assert (legacy_root / ".spend_state.json").exists()
    assert common.get_spend_today() == pytest.approx(1.23)


def test_spend_state_migration_never_overwrites_existing_new_state(tmp_path):
    legacy_root = tmp_path / "old-checkout"
    legacy_root.mkdir()
    (legacy_root / ".spend_state.json").write_text(json.dumps({"date": "2026-08-13", "spend_usd": 9.99, "recent_events": []}))

    common.SPEND_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_state = {"date": "2026-08-13", "spend_usd": 0.5, "recent_events": []}
    common.SPEND_STATE_PATH.write_text(json.dumps(new_state))

    common.migrate_legacy_spend_state(legacy_root=legacy_root)
    assert json.loads(common.SPEND_STATE_PATH.read_text()) == new_state


def test_spend_state_migration_noop_without_legacy_file(tmp_path):
    legacy_root = tmp_path / "old-checkout"
    legacy_root.mkdir()
    common.migrate_legacy_spend_state(legacy_root=legacy_root)
    assert not common.SPEND_STATE_PATH.exists()


# --- warning (and ONLY warning) for the old layout (section 7.2) ---

def test_legacy_layout_warns_and_never_moves(tmp_path, caplog):
    legacy_root = tmp_path / "old-checkout"
    legacy_root.mkdir()
    (legacy_root / "repos.json").write_text("[]")
    (legacy_root / "qdrant_data").mkdir()

    with caplog.at_level(logging.WARNING, logger="griot"):
        common.warn_legacy_layout(legacy_root=legacy_root)

    assert str(legacy_root / "repos.json") in caplog.text
    assert str(common.CONFIG_DIR / "repos.json") in caplog.text
    assert str(common.DATA_DIR / "qdrant_data") in caplog.text
    # nothing was moved or created in the new location
    assert (legacy_root / "repos.json").exists()
    assert not (common.CONFIG_DIR / "repos.json").exists()
    assert not (common.DATA_DIR / "qdrant_data").exists()


def test_legacy_layout_silent_when_root_clean_or_missing(tmp_path, caplog):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with caplog.at_level(logging.WARNING, logger="griot"):
        common.warn_legacy_layout(legacy_root=empty_root)
        common.warn_legacy_layout(legacy_root=tmp_path / "does-not-exist")
    assert caplog.records == []


# --- <config_dir>/.env permission (secrets → 0600) ---

def _env_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_env_file_open_permissions_warns_and_chmods(caplog):
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text("GEMINI_TOKEN=fake\n")
    common.ENV_PATH.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="griot"):
        common._check_env_file_permissions(common.ENV_PATH)

    assert "0600" in caplog.text
    assert _env_mode(common.ENV_PATH) == 0o600


def test_env_file_0600_is_silent(caplog):
    common.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.ENV_PATH.write_text("GEMINI_TOKEN=fake\n")
    common.ENV_PATH.chmod(0o600)

    with caplog.at_level(logging.WARNING, logger="griot"):
        common._check_env_file_permissions(common.ENV_PATH)

    assert caplog.records == []
    assert _env_mode(common.ENV_PATH) == 0o600


def test_env_file_missing_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="griot"):
        common._check_env_file_permissions(common.ENV_PATH)
    assert caplog.records == []


# --- override via GRIOT_CONFIG_DIR/GRIOT_DATA_DIR and XDG fallback ---
# The constants are resolved on import of common.py, so the honest way
# to test the resolution is a fresh interpreter per scenario (subprocess) —
# an in-process reload would leave the shared module in an inconsistent
# state for the other tests.

_PRINT_DIRS = "from griot import common; print('CONFIG=' + str(common.CONFIG_DIR)); print('DATA=' + str(common.DATA_DIR))"


def _resolve_dirs_in_subprocess(tmp_path: Path, extra_env: dict) -> tuple[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        # minimal, fully controlled environment: no GRIOT_*/XDG_*/RAG_* from
        # the real shell, and a disposable HOME — even the side effects of
        # import (old-layout warnings → log) stay contained within tmp_path
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(SRC_DIR),
        # Importing griot.common reads the keychain: keep the child off the
        # real one, as conftest.py does for every inherited environment.
        "PYTHON_KEYRING_BACKEND": os.environ["PYTHON_KEYRING_BACKEND"],
        **extra_env,
    }
    out = subprocess.run(
        [sys.executable, "-c", _PRINT_DIRS],
        capture_output=True, text=True, check=True, timeout=120, env=env,
    ).stdout
    config = next(line for line in out.splitlines() if line.startswith("CONFIG=")).removeprefix("CONFIG=")
    data = next(line for line in out.splitlines() if line.startswith("DATA=")).removeprefix("DATA=")
    return config, data


def test_griot_env_vars_override_dirs(tmp_path):
    config, data = _resolve_dirs_in_subprocess(tmp_path, {
        "GRIOT_CONFIG_DIR": str(tmp_path / "cfg"),
        "GRIOT_DATA_DIR": str(tmp_path / "dat"),
    })
    assert config == str(tmp_path / "cfg" / "griot")
    assert data == str(tmp_path / "dat" / "griot")


def test_xdg_fallback_when_griot_vars_unset(tmp_path):
    config, data = _resolve_dirs_in_subprocess(tmp_path, {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })
    assert config == str(tmp_path / "xdg-config" / "griot")
    assert data == str(tmp_path / "xdg-data" / "griot")


def test_home_default_when_nothing_set(tmp_path):
    config, data = _resolve_dirs_in_subprocess(tmp_path, {})
    home = tmp_path / "home"
    assert config == str(home / ".config" / "griot")
    assert data == str(home / ".local" / "share" / "griot")
