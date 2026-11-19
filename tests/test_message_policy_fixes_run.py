"""A fix griot prints is a command griot itself accepts.

test_message_policy.py checks the FORM of every last line and every doctor
fix: a command, one per line, no comment, no placeholder. That is how two
fixes that griot refused got through (a review on 2026-11-19): the doctor's
server-environment row told the person to `griot config set embed-profile`,
which `config set` refuses, and to write back a value the row had just
called invalid; the settings row offered the first profile in the list, a
paid one, while the variable that broke it was exported and would win over
the file. Here each such fix is run, in a real process with fresh
directories and no keychain, and must succeed; and the text of the run that
needs one (a platform with no token, a stopped `index all`) is read as a
person and an agent receive it.
"""

import json
import os
import shlex
import subprocess
import sys

import pytest

from griot import cli


def _env(tmp_path, extra=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
           and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
           and k not in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN")}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "isolated" / "config"), GRIOT_DATA_DIR=str(tmp_path / "isolated" / "data"),
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    env.update(extra or {})
    return env


def _griot(tmp_path, *args, extra_env=None):
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=_env(tmp_path, extra_env),
                          capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)


def _env_file(tmp_path, text):
    path = tmp_path / "isolated" / "config" / "griot" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


def _doctor_row(tmp_path, check, extra_env=None):
    done = _griot(tmp_path, "doctor", "--json", extra_env=extra_env)
    rows = {row["check"]: row for row in json.loads(done.stdout)}
    return rows[check]


def _last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _run_fix(tmp_path, line, extra_env=None):
    """A griot fix line, run as a person pastes it."""
    words = shlex.split(line)
    assert words[0] == "griot", line
    return _griot(tmp_path, *words[1:], extra_env=extra_env)


# --- the server-environment row ------------------------------------------------------------------


def test_a_profile_a_server_would_ignore_is_fixed_with_profiles_use(tmp_path):
    row = _doctor_row(tmp_path, "server environment", {"GRIOT_EMBED_PROFILE": "openai-small"})
    assert row["fix"] == "griot profiles use openai-small", row


def test_a_value_a_server_cannot_take_is_unset_not_written_back(tmp_path):
    row = _doctor_row(tmp_path, "server environment", {"GRIOT_MCP_INDEX_ROOTS": "/Users/you/a b"})
    assert row["fix"] == "unset GRIOT_MCP_INDEX_ROOTS", row


def test_a_value_that_widens_is_still_set_in_the_file(tmp_path):
    row = _doctor_row(tmp_path, "server environment", {"GRIOT_SPEND_CEILING_USD": "9"})
    assert row["fix"] == "griot config set spend-ceiling 9", row


# --- the settings row: an unknown profile, and two broken settings at once ----------------------


def test_an_unknown_profile_in_the_file_is_fixed_with_the_default_and_the_fix_runs(tmp_path):
    _env_file(tmp_path, "GRIOT_EMBED_PROFILE=bogus\n")
    row = _doctor_row(tmp_path, "settings")
    assert row["fix"] == "griot profiles use jina-code", row
    assert _run_fix(tmp_path, row["fix"]).returncode == 0
    assert _griot(tmp_path, "stats").returncode == 0


def test_an_unknown_profile_exported_is_fixed_with_unset(tmp_path):
    row = _doctor_row(tmp_path, "settings", {"GRIOT_EMBED_PROFILE": "bogus"})
    assert row["fix"] == "unset GRIOT_EMBED_PROFILE", row


def test_two_broken_settings_are_both_named_and_each_fix_runs_despite_the_other(tmp_path):
    _env_file(tmp_path, "GRIOT_EMBED_PROFILE=bogus\nGRIOT_SPEND_CEILING_USD=abc\n")
    row = _doctor_row(tmp_path, "settings")
    fixes = row["fix"].splitlines()
    assert fixes == ["griot profiles use jina-code", "griot config unset spend-ceiling"], row
    assert "GRIOT_SPEND_CEILING_USD" in row["detail"] and "bogus" in row["detail"]
    # The second fix, first: each one runs whatever state the other is in.
    done = _run_fix(tmp_path, fixes[1])
    assert done.returncode == 0, done.stderr
    done = _run_fix(tmp_path, fixes[0])
    assert done.returncode == 0, done.stderr
    assert _griot(tmp_path, "stats").returncode == 0


def test_the_command_an_error_gives_for_one_broken_setting_runs_while_another_is_broken(tmp_path):
    """The loop a review found: each command, pasted, failed with the other
    setting's error, and that one's command failed with the first's."""
    _env_file(tmp_path, "GRIOT_EMBED_PROFILE=bogus\nGRIOT_SPEND_CEILING_USD=abc\n")
    for _ in range(2):
        done = _griot(tmp_path, "stats")
        if done.returncode == 0:
            break
        assert done.returncode == 2, done.stderr
        fixed = _run_fix(tmp_path, _last_line(done.stderr).strip())
        assert fixed.returncode == 0, fixed.stderr
    assert _griot(tmp_path, "stats").returncode == 0


def test_the_settings_detail_joins_sentences_without_a_stray_semicolon(tmp_path):
    _env_file(tmp_path, "GRIOT_SPEND_CEILING_USD=abc\nGRIOT_MCP_SEARCH_LIMIT=many\n")
    row = _doctor_row(tmp_path, "settings")
    assert ".;" not in row["detail"], row["detail"]


# --- a stopped `griot index all` -----------------------------------------------------------------


def test_the_last_source_failing_reads_as_one_source_to_run_again():
    text = cli._stopped_at("platform", ["code", "platform"], [], "failed (exit 1)")
    assert "no other source did not run" not in text
    assert "run them again" not in text
    assert "run it again" in text
    assert _last_line(text).strip() == "griot index all --sources platform"


def test_an_error_that_ends_on_its_own_command_keeps_it_as_a_line_before_the_resume():
    what = ("failed: paid profile 'x' needs KEY, and it is not set. Nothing was sent. A credential is set in a "
            "terminal:\n  griot auth set openai")
    text = cli._stopped_at("code", ["code", "commits", "platform"], [], what)
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1] == "  griot index all --sources code,commits,platform", text
    assert lines[1] == "  griot auth set openai", text
    assert lines[0].endswith("A credential is set in a terminal:"), text
    assert "commits, platform did not run" in text


def test_an_exported_value_is_never_set_aside_by_a_repair_command(tmp_path):
    """Only `unset` in the shell fixes an exported value (it wins over the
    file): a repair command still refuses to start on one, and says so."""
    _env_file(tmp_path, "GRIOT_SPEND_CEILING_USD=abc\n")
    done = _griot(tmp_path, "config", "unset", "spend-ceiling", extra_env={"GRIOT_EMBED_PROFILE": "bogus"})
    assert done.returncode == 2, done.stderr
    assert _last_line(done.stderr) == "  unset GRIOT_EMBED_PROFILE", done.stderr


def test_a_repair_command_does_not_leave_its_flag_on(monkeypatch):
    """A process that runs more than one command (a host calling _main, the
    suite) must not carry the set-aside into the next: a broken amount is
    refused again afterwards."""
    import griot
    from griot import common
    with cli._repairing(True):
        assert griot.REPAIRING_SETTINGS is True
    assert griot.REPAIRING_SETTINGS is False
    with pytest.raises(griot.UnknownEmbedProfile):
        with cli._repairing(True):
            raise griot.UnknownEmbedProfile("x", "bogus", [])
    assert griot.REPAIRING_SETTINGS is False
    monkeypatch.setenv("GRIOT_SPEND_CEILING_USD", "abc")
    monkeypatch.delitem(common.ENVIRONMENT_BEFORE_ENV_FILE, "GRIOT_SPEND_CEILING_USD", raising=False)
    with pytest.raises(griot.ConfigurationError):
        common._amount_env("GRIOT_SPEND_CEILING_USD", "3.0")


def test_a_broken_setting_has_one_command_on_the_cli_and_in_doctor(tmp_path):
    _env_file(tmp_path, "GRIOT_SPEND_CEILING_USD=abc\n")
    done = _griot(tmp_path, "stats")
    assert done.returncode == 2
    assert _last_line(done.stderr).strip() == _doctor_row(tmp_path, "settings")["fix"] == \
        "griot config unset spend-ceiling"


# --- a platform with no token --------------------------------------------------------------------


def _github_repository(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/you/app.git"], check=True)
    return repo


def test_a_missing_platform_token_is_said_once_and_ends_on_its_commands(tmp_path):
    repo = _github_repository(tmp_path)
    done = _griot(tmp_path, "index", "platform", "--path", str(repo))
    assert done.returncode == 1
    err = done.stderr
    assert err.count("GITHUB_TOKEN") == 1, err
    assert ":." not in err, err
    assert "ValueError" not in err, err
    lines = [line for line in err.splitlines() if line.strip()]
    assert lines[-2:] == ["  griot auth set github", "  griot index platform"], err


def test_stats_and_doctor_name_the_missing_token_as_the_run_did(tmp_path):
    repo = _github_repository(tmp_path)
    config = tmp_path / "isolated" / "config" / "griot"
    config.mkdir(parents=True, exist_ok=True)
    (config / "repos.json").write_text(json.dumps([str(repo)]))
    assert _griot(tmp_path, "index", "platform").returncode == 1
    stats = _griot(tmp_path, "stats")
    # The failures are counted by their cause, never by an exception class.
    assert "3× GITHUB_TOKEN not set" in stats.stdout, stats.stdout
    assert "ValueError" not in stats.stdout and "TokenMissing" not in stats.stdout, stats.stdout
    assert "GITHUB_TOKEN is not set" in stats.stdout, stats.stdout
    assert "griot auth set github" in stats.stdout, stats.stdout
    # Said once, as a missing token: not a second time as a refusal.
    assert "the platform refused every fetch for app" not in stats.stdout, stats.stdout
    row = _doctor_row(tmp_path, "repositories")
    assert "GITHUB_TOKEN is not set" in row["detail"], row
    assert row["fix"] == "griot auth set github\ngriot index platform", row


@pytest.mark.anyio
async def test_the_index_status_tool_carries_the_missing_token(tmp_path):
    from mcp.client.client import Client

    from griot import freshness, mcp_server
    run = {"script": "index_platform.py", "timestamp": "2026-11-19T00:00:00+00:00", "error": "x",
           "refused_repos": ["app"], "refusal_causes": {"app": {"token": ["issues"]}},
           "missing_tokens": {"app": "GITHUB_TOKEN"}, "heads": {}}
    reports = freshness.assess([run], [{"name": "app", "path": str(tmp_path), "head": None}])
    assert reports[0]["platform_missing_token"] == "GITHUB_TOKEN"
    # Anything else recorded there (a record edited by hand) is no token name.
    run["missing_tokens"] = {"app": ["not", "a", "name"]}
    assert freshness.assess([run], [{"name": "app", "path": str(tmp_path), "head": None}])[0][
        "platform_missing_token"] is None
    async with Client(mcp_server.mcp) as client:
        listed = await client.list_tools()
    schema = next(t for t in listed.tools if t.name == "griot_index_status").output_schema
    assert "platform_missing_token" in json.dumps(schema)
    assert "spend_today_usd" in json.dumps(schema)


# --- smaller wordings ----------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["https_proxy", "HTTPS_PROXY", "ALL_PROXY"])
def test_a_proxy_refusal_names_the_variable_it_came_from(monkeypatch, variable):
    import requests

    from griot import doctor
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "http://127.0.0.1:9")
    assert doctor.pypi_failure_cause(requests.exceptions.ProxyError("x")) == \
        f"the proxy in {variable} refused the connection"


def test_a_proxy_refusal_with_no_variable_names_none(monkeypatch):
    import requests

    from griot import doctor
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert doctor.pypi_failure_cause(requests.exceptions.ProxyError("x")) == "the proxy refused the connection"


def test_stats_proposes_a_ceiling_from_the_spend_it_was_given(monkeypatch):
    from datetime import datetime, timezone

    from griot import common, stats

    def no_read():
        raise AssertionError("compute_stats read the spend itself")
    monkeypatch.setattr(common, "get_spend_today", no_read)
    status = {"points_count": 1, "embed_profile": "any", "spend_ceiling_exceeded": True, "spend_today_usd": 4.2,
              "last_indexed": None}
    result = stats.compute_stats([], [], status, now=datetime(2026, 11, 19, tzinfo=timezone.utc))
    line = next(item for item in result["attention"] if "ceiling" in item)
    assert line.endswith("griot config set spend-ceiling 6"), line


def test_the_refusal_before_a_paid_run_says_where_a_credential_is_set(monkeypatch, capsys):
    from griot import common
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(common, "credential", lambda name: None)
    assert common.refuse_missing_credential() == 1
    assert capsys.readouterr().err == ("Error: paid profile 'openai-small' needs GRIOT_OPENAI_API_KEY, and it is not "
                                       "set. Nothing was changed. A credential is set in a terminal:\n"
                                       "  griot auth set openai\n")
