"""The command is the last line, where a reader pastes it from.

Two places the sweep left with a command that was not last or not where a
reader of fixes looks: the MCP confirmation fallback put a parenthetical note
after the command, and `griot doctor`'s settings row put an exported setting's
`unset VAR` inside its detail, leaving `fix` empty for every reader of fixes
(an agent, `griot update`'s after-upgrade reader, the doctor-fix guard)."""

import json
import os
import subprocess
import sys

import pytest

from griot import common, mcp_server

from test_cli_hints import _NoElicitCtx


def _last_line(text: str) -> str:
    return [line for line in text.splitlines() if line.strip()][-1].strip()


@pytest.mark.anyio
async def test_the_golden_set_fallback_ends_on_its_command_after_its_note():
    out = await mcp_server.griot_golden_set_add("how is x done", must_include=[{"repo": "r"}], confirm=False,
                                                 ctx=_NoElicitCtx())
    message = out["message"]
    assert "interactive" in message  # the note is still said
    assert _last_line(message).startswith("griot golden-set add"), message


@pytest.mark.anyio
async def test_the_repos_remove_fallback_ends_on_its_command(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(repo)]))
    out = await mcp_server.griot_repos_remove(str(repo), confirm=False, ctx=_NoElicitCtx())
    assert _last_line(out["message"]).startswith("griot repos remove"), out["message"]


def test_an_exported_invalid_setting_puts_its_command_in_the_fix(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
               GRIOT_SEARCH_TIMEOUT_SECONDS="abc")
    done = subprocess.run([sys.executable, "-m", "griot.cli", "doctor", "--json"], env=env, capture_output=True,
                          text=True, timeout=180, stdin=subprocess.DEVNULL)
    report = json.loads(done.stdout)
    rows = report["checks"] if isinstance(report, dict) else report
    settings = next(row for row in rows if row["check"] == "settings")
    assert settings["fix"] and "unset GRIOT_SEARCH_TIMEOUT_SECONDS" in settings["fix"], settings
    assert "unset GRIOT_SEARCH_TIMEOUT_SECONDS" not in settings["detail"], settings
