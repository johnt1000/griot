"""Where griot's text goes and who can silence it (policy: stdout is the result,
stderr is everything else; --quiet silences notices, warnings and progress and
never errors, prompts or the result; a repeated warning is said once per
state; incorrect use exits 2).

The behaviour is checked through the real CLI in a subprocess, with stdout and
stderr captured separately: a unit test that patches the function it tests
cannot see a warning land in the data of `griot stats --json`.
"""
import io
import json
import os
import subprocess
import sys

import pytest

from griot import common, cli, platforms, repos

OLD_VARIABLE = "RAG_EMBED_PROFILE"
# What the CLI says of a renamed variable since the notices ledger: one footer
# line, the sentence itself being listed by `griot doctor` and `griot stats`.
FOOTER = "1 notice: griot doctor"
OLD_LINE = f'{OLD_VARIABLE} was renamed and griot ignores it. — export GRIOT_EMBED_PROFILE="${OLD_VARIABLE}"'


def _run(tmp_path, *args, extra_env=None, stdin=subprocess.DEVNULL):
    """The real CLI in a subprocess with its own directories. Never the
    user's: GRIOT_* and RAG_* are dropped from what the suite inherited."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               GRIOT_UPDATE_CHECK="false")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=stdin)


WARNING_ACTIVE = {OLD_VARIABLE: "jina-code"}


# --- stdout is the result, stderr everything else -----------------------------------------------------------

def test_a_warning_does_not_enter_the_json_of_stats(tmp_path):
    done = _run(tmp_path, "stats", "--json", extra_env=WARNING_ACTIVE)

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)  # valid: nothing else is in it
    assert OLD_LINE in report["attention"]  # listed by the report, not printed beside it
    assert FOOTER not in done.stderr


@pytest.mark.parametrize("argv", [["repos", "list"], ["auth", "list"], ["profiles", "list"]])
def test_a_warning_stays_out_of_the_result_of_other_commands(tmp_path, argv):
    done = _run(tmp_path, *argv, extra_env=WARNING_ACTIVE)

    assert done.returncode == 0, done.stderr
    assert "Warning" not in done.stdout and "renamed" not in done.stdout
    assert done.stdout.strip(), "the result is still printed"
    assert "notice" not in done.stdout
    assert done.stderr.rstrip().splitlines()[-1] == FOOTER


def test_log_and_print_writes_to_stderr_looked_up_at_the_call(capsys):
    common.log_and_print("a notice")

    out, err = capsys.readouterr()
    assert out == ""
    assert err == "a notice\n"


def test_echo_false_still_logs_and_prints_nothing(capsys, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="griot"):
        common.log_and_print("only in the log", echo=False)

    assert capsys.readouterr() == ("", "")
    assert "only in the log" in caplog.text


def test_the_stream_the_mcp_server_sets_still_wins(capsys):
    stream = io.StringIO()
    common.set_echo_stream(stream)
    try:
        common.log_and_print("to the server's stream")
    finally:
        common.set_echo_stream(None)

    assert stream.getvalue() == "to the server's stream\n"
    assert capsys.readouterr() == ("", "")


# --- --quiet ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["--quiet", "-q", "env"])
def test_quiet_silences_the_warning_in_every_spelling(tmp_path, how):
    argv = ["repos", "list"] + ([how] if how != "env" else [])
    done = _run(tmp_path, *argv, extra_env={**WARNING_ACTIVE, **({"GRIOT_QUIET": "1"} if how == "env" else {})})

    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert "No repo configured" in done.stdout  # the result is not silenced


def test_quiet_anywhere_on_the_command_line_and_the_flag_is_not_forwarded(tmp_path):
    done = _run(tmp_path, "index", "code", "--quiet", "--path", str(tmp_path / "missing"), extra_env=WARNING_ACTIVE)

    assert "unrecognized" not in done.stderr
    assert "renamed" not in done.stderr


def test_quiet_never_silences_an_error(tmp_path):
    done = _run(tmp_path, "profiles", "use", "nonesuch", "--quiet", extra_env={**WARNING_ACTIVE, "GRIOT_QUIET": "1"})

    assert done.returncode == 2
    assert "Error: unknown profile 'nonesuch'" in done.stderr
    assert "renamed" not in done.stderr  # the warning was silenced, the error was not


def test_quiet_never_silences_a_prompt_refusal(tmp_path):
    here = tmp_path / "project"
    here.mkdir()
    done = _run(tmp_path, "repos", "add", str(here), "-q")

    assert done.returncode == 2
    assert "Nothing was changed" in done.stderr


def test_quiet_silences_notices_but_not_a_failure_report(capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    common.log_and_print("a notice")
    common.log_and_print("a batch was lost", level="warning", silenceable=False)
    common.progress_note("progress")

    assert capsys.readouterr().err == "a batch was lost\n"


def test_quiet_says_nothing_and_remembers_nothing(tmp_path, capsys, monkeypatch):
    """Silenced is not said: when the person stops being quiet the warning
    still has to come."""
    from griot import notices

    condition = notices.keyword_vectors_missing("c")
    monkeypatch.setenv("GRIOT_QUIET", "1")
    notices.say_once(condition, notices.KEYWORD_VECTORS_MISSING)
    monkeypatch.delenv("GRIOT_QUIET")
    notices.say_once(condition, notices.KEYWORD_VECTORS_MISSING)

    assert capsys.readouterr().err == notices.prose(condition) + "\n"


@pytest.mark.parametrize("argv", [["search"], ["ask"], ["index", "code"], ["index", "all"], ["stats"], ["doctor"],
                                  ["quality-check"], ["profiles"], ["profiles", "use"], ["profiles", "delete"], ["config"], ["auth"],
                                  ["assist"], ["repos"], ["update"], ["golden-set"], ["mcp"], []])
def test_every_help_names_the_quiet_flag(capsys, argv):
    with pytest.raises(SystemExit) as stop:
        cli.main([*argv, "--help"])

    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "--quiet" in out and "GRIOT_QUIET=1" in out
    assert "--quiet QUIET" not in out and "--quiet NAME" not in out  # a switch: no value


# --- a repeated warning is said once per state -----------------------------------------------------------

def test_the_warning_is_said_once_not_on_every_run(tmp_path):
    first = _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)
    second = _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)

    assert first.stderr == FOOTER + "\n"
    assert second.stderr == ""
    assert second.stdout == first.stdout


def test_a_changed_state_speaks_again(tmp_path):
    _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)
    more = _run(tmp_path, "repos", "list", extra_env={**WARNING_ACTIVE, "RAG_CHAT_MODEL": "x"})

    assert more.stderr == FOOTER + "\n"  # one condition (renamed variables), a new fingerprint


def test_a_condition_that_went_away_speaks_again_when_it_returns(tmp_path):
    _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)
    gone = _run(tmp_path, "repos", "list")
    back = _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)

    assert gone.stderr == ""
    assert back.stderr == FOOTER + "\n"


def test_a_state_file_that_cannot_be_used_does_not_stop_the_command(tmp_path):
    # A directory where the file should be: reading and writing it both fail.
    (tmp_path / "data" / "griot" / "announced.json").mkdir(parents=True)
    done = _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)

    assert done.returncode == 0, done.stderr
    assert "No repo configured" in done.stdout
    assert done.stderr == FOOTER + "\n"  # said, since it cannot be remembered


@pytest.mark.parametrize("garbage", ["[not json", "[]", "7", '"text"'])
def test_a_state_file_with_garbage_is_started_over(tmp_path, garbage):
    state = tmp_path / "data" / "griot"
    state.mkdir(parents=True, exist_ok=True)
    (state / "announced.json").write_text(garbage)
    done = _run(tmp_path, "repos", "list", extra_env=WARNING_ACTIVE)

    assert done.returncode == 0
    assert done.stderr == FOOTER + "\n"


def test_doctor_lists_the_renamed_variable_every_time(tmp_path):
    for _ in range(2):
        done = _run(tmp_path, "doctor", "--json", extra_env=WARNING_ACTIVE)
        checks = {c["check"]: c for c in json.loads(done.stdout)}

        assert checks["old settings"]["status"] == "warn"
        assert checks["old settings"]["detail"] == f"{OLD_VARIABLE} was renamed and griot ignores it."
        assert checks["old settings"]["fix"] == f'export GRIOT_EMBED_PROFILE="${OLD_VARIABLE}"'


def test_doctor_old_settings_is_ok_when_there_are_none(tmp_path):
    done = _run(tmp_path, "doctor", "--json")

    assert {c["check"]: c["status"] for c in json.loads(done.stdout)}["old settings"] == "ok"


def test_the_old_layout_is_one_condition_whose_fingerprint_is_its_paths(tmp_path):
    from griot import notices

    root = tmp_path / "old-checkout"
    root.mkdir()
    (root / "repos.json").write_text("[]")

    found = notices.legacy_layout(common.legacy_layout_findings(root))
    assert found.sentence == (f"{root / 'repos.json'} is in the old layout and griot ignores it (griot never moves "
                              f"it on its own).")
    assert found.action == f"mv {root / 'repos.json'} {common.CONFIG_DIR / 'repos.json'}"
    (root / "qdrant_data").mkdir()
    more = notices.legacy_layout(common.legacy_layout_findings(root))
    assert more.id == found.id and more.fingerprint != found.fingerprint
    assert "qdrant_data" in more.sentence and f"mv {root / 'qdrant_data'}" in more.action


# --- messages -----------------------------------------------------------------------------------------------

def test_the_env_file_closed_by_griot_is_a_note_not_a_warning(tmp_path, capsys):
    env = tmp_path / "env"
    env.write_text("A=1\n")
    env.chmod(0o644)

    common._check_env_file_permissions(env)

    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"Note: {env} was readable by others (0o644); griot set it to 0600.\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_the_env_file_that_could_not_be_closed_ends_on_the_command(tmp_path, capsys, monkeypatch):
    env = tmp_path / "env"
    env.write_text("A=1\n")
    env.chmod(0o644)
    monkeypatch.setenv("GRIOT_QUIET", "1")

    def refuse(self, mode):
        raise PermissionError("not yours")
    monkeypatch.setattr(type(env), "chmod", refuse)

    common._check_env_file_permissions(env)

    err = capsys.readouterr().err
    assert err.splitlines()[-1] == f"  chmod 600 {env}"  # still said under --quiet: it is open and holds secrets
    assert err.startswith("Warning:")


def test_a_path_that_is_not_a_directory_is_said_the_same_way_everywhere(capsys):
    common.warn_not_a_directory("/Users/you/gone")

    out, err = capsys.readouterr()
    assert out == ""
    assert err == ("Warning: /Users/you/gone is not a directory, skipped. Update the list:\n"
                   "  griot repos remove /Users/you/gone\n")


@pytest.mark.parametrize("module", ["index_code", "index_tags", "index_commits", "index_branches", "index_platform"])
def test_every_indexer_uses_that_one_sentence(module):
    import importlib
    import inspect
    source = inspect.getsource(importlib.import_module(f"griot.{module}"))

    assert "warn_not_a_directory" in source
    assert "is not a valid directory" not in source


def test_the_capped_listing_names_what_is_lost_without_the_token(capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")  # an incomplete index is not a notice
    platforms._warn_pages_capped("https://user:s3cret@git.example.com/api/v4/projects/1/issues?private_token=s3cret")

    err = capsys.readouterr().err
    assert err.startswith("Warning: the listing at https://git.example.com/api/v4/projects/1/issues was cut at")
    assert "s3cret" not in err


def test_a_network_failure_is_named_by_its_cause_not_its_class():
    import requests
    assert common.network_cause(requests.Timeout()) == "timeout"
    assert common.network_cause(requests.ConnectionError()) == "network unreachable"


def test_a_lost_batch_says_it_will_be_retried_and_survives_quiet(capsys, monkeypatch):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"model": "m", "endpoint_url": "https://example.test"})
    monkeypatch.setattr(common, "credential_env_for_profile", lambda *a: "GRIOT_X_KEY")

    def refuse(*a, **k):
        raise common.DirectAPIUnavailable("HTTP error 500 on https://example.test")
    monkeypatch.setattr(common, "_openai_compatible_post_with_retry", refuse)
    monkeypatch.setattr(common, "_note_embedding_failure", lambda text: None)

    common._embed_one_openai_compatible_request(["a"], "key")

    err = capsys.readouterr().err
    assert err.startswith("Warning: an embedding batch failed")
    assert err.splitlines()[-1] == "  griot index all"


def test_a_points_count_that_cannot_be_read_ends_on_the_command(capsys):
    result = common._points_error("c", RuntimeError("damaged segment"))

    err = capsys.readouterr().err
    assert result == "unreadable: damaged segment"
    assert err.startswith("Warning: the point count of 'c' is not shown")
    assert err.splitlines()[-1] == "  griot doctor"


def test_a_busy_collection_is_a_note_and_goes_to_stderr(capsys, tmp_path):
    assert common._points_error("c", common.CollectionBusyError("c", tmp_path, "held")) == "busy"

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Note: the point count of 'c' is not shown: the collection is")
    assert err.splitlines()[-1] == "  griot doctor"


# --- NO_COLOR and not a terminal -----------------------------------------------------------------------------

class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_no_color_and_not_a_terminal_turn_the_animation_off(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GRIOT_QUIET", raising=False)
    assert common.plain_output(_Terminal()) is False
    assert common.plain_output(io.StringIO()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert common.plain_output(_Terminal()) is True
    monkeypatch.setenv("NO_COLOR", "")  # no-color.org: present and not empty
    assert common.plain_output(_Terminal()) is False


def _reading_output(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("print('x')\n" * 5)
    from griot import index_code
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    index_code.process_repository(repo)
    return terminal.getvalue()


def test_the_reading_bar_is_drawn_on_a_terminal(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GRIOT_QUIET", raising=False)

    assert "Reading repo" in _reading_output(tmp_path, monkeypatch)


def test_the_reading_bar_is_not_drawn_under_no_color(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    assert "Reading repo" not in _reading_output(tmp_path, monkeypatch)


def test_the_reading_bar_is_not_drawn_under_quiet(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("GRIOT_QUIET", "1")

    assert "Reading repo" not in _reading_output(tmp_path, monkeypatch)


def test_no_color_in_a_subprocess_leaves_no_bar_and_no_escape_codes(tmp_path):
    here = tmp_path / "project"
    here.mkdir()
    (here / "a.py").write_text("x = 1\n")
    done = _run(tmp_path, "index", "code", "--dry-run", "--path", str(here), extra_env={"NO_COLOR": "1"})

    assert "\x1b" not in done.stdout + done.stderr
    assert "Reading" not in done.stderr


# --- the result stays on stdout --------------------------------------------------------------------------

# --- incorrect use exits 2 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("argv, message", [
    (["auth", "set", "nonesuch"], "Error: unknown provider 'nonesuch'"),
    (["auth", "remove", "nonesuch"], "Error: unknown provider 'nonesuch'"),
    (["repos", "add", "/Users/you/not-here"], "doesn't exist or isn't a directory"),
    (["repos", "remove", "/Users/you/not-here"], "doesn't exist or is empty"),
    (["profiles", "use", "nonesuch"], "Error: unknown profile 'nonesuch'"),
    (["profiles", "delete", "nonesuch"], "Error: unknown profile 'nonesuch'"),
    (["--profile"], "Error: --profile requires a value. Example:"),
    (["--profile="], "Error: --profile requires a value. Example:"),
    (["ask", "--chat-profile"], "Error: --chat-profile requires a value. Example:"),
    (["index", "all", "--sources"], "Error: --sources requires a value. Example:"),
])
def test_incorrect_use_exits_two_with_an_error_line(tmp_path, argv, message):
    done = _run(tmp_path, *argv)

    assert done.returncode == 2, (done.stdout, done.stderr)
    assert message in done.stderr
    assert done.stdout == ""


def test_the_refusal_ends_on_the_command_that_helps(tmp_path):
    assert _run(tmp_path, "auth", "set", "nonesuch").stderr.splitlines()[-1] == "  griot auth list"
    assert _run(tmp_path, "auth", "remove", "nonesuch").stderr.splitlines()[-1] == "  griot auth list"
    assert _run(tmp_path, "repos", "remove", "/Users/you/x").stderr.splitlines()[-1] == "  griot repos list"
    assert _run(tmp_path, "profiles", "use", "nonesuch").stderr.splitlines()[-1] == "  griot profiles list"
    assert _run(tmp_path, "profiles", "delete", "nonesuch").stderr.splitlines()[-1] == "  griot profiles list"


def test_the_same_unknown_profile_is_worded_once():
    assert "unknown profile 'x'." in cli.unknown_profile_sentence("x", "", ["a", "b"])
    assert cli.unknown_profile_sentence("x", "", ["a", "b"]).endswith("Options: a, b.\n  griot profiles list")


def test_a_repos_json_that_does_not_parse_is_a_failure_not_incorrect_use(tmp_path, monkeypatch, capsys):
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text("{broken")

    assert repos.cmd_remove("/Users/you/x") == 1
    assert "Error:" in capsys.readouterr().err


def test_a_failed_operation_keeps_exit_one(tmp_path, monkeypatch, capsys):
    """The operation ran and failed: `griot index keywords` while a run holds
    the lock is not a wrong request."""
    monkeypatch.setattr(common, "build_keyword_index", lambda: (_ for _ in ()).throw(RuntimeError("an index run holds the lock")))

    assert cli.main(["index", "keywords"]) == 1
    assert "Error: an index run holds the lock" in capsys.readouterr().err


def test_the_main_help_states_the_exit_status(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = " ".join(capsys.readouterr().out.split())
    assert "Exit status: 0 done, 1 the operation ran and failed, 2 incorrect use" in out


def test_a_refusal_because_a_run_holds_the_lock_is_a_failure_not_incorrect_use(monkeypatch, capsys):
    """`profiles delete` refused for a state (an indexing run is going) keeps
    exit 1; refused for the request itself (unknown, active) is 2."""
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setattr(common, "collection_exists", lambda name: True)
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": True})

    assert cli.main(["profiles", "delete", "gemini"]) == 1
    assert "in progress" in capsys.readouterr().err
