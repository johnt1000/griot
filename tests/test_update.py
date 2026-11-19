"""`griot update`: runs the upgrade that `griot doctor` only prints.

It asks PyPI the way the doctor's release check does, works out how griot
was installed the way that check does, shows the exact command and asks
before running it. A development install is refused (the checkout is what
gets updated), and so is an installation whose method cannot be told: a
guess is printed, never run. The suite never touches the network nor runs a
real installer: the PyPI query and every subprocess are stubbed."""

import json
import subprocess
import sys

import pytest

from griot import doctor, update
from test_keyword_search import fake_embeddings, legacy_index  # noqa: F401


PIPX_PREFIX = "/Users/you/.local/share/pipx/venvs/griot-rag"
UV_PREFIX = "/Users/you/.local/share/uv/tools/griot-rag"
VENV_PREFIX = "/Users/you/work/venv"


# The upgraded installation's doctor: its interpreter (the one the upgrade
# went into), isolated from the current directory, as a new process.
DOCTOR_CALL = [sys.executable, "-I", "-m", "griot.doctor", "--json"]


def _row(check, status, detail, fix=None, notices=()):
    """A row of `griot doctor --json`."""
    return {"check": check, "status": status, "detail": detail, "fix": fix,
            "notices": [{"id": n.id, "severity": n.severity, "sentence": n.sentence, "action": n.action}
                        for n in notices]}


@pytest.fixture
def world(monkeypatch):
    """What `griot update` sees: PyPI's answer, the installed version, where
    it runs, whether it is a development install, whether a person is at a
    terminal, and what each subprocess does. Records every subprocess."""
    import griot

    state = {"newest": "0.3.0", "asked": 0, "calls": [], "installer_rc": 0, "now": "0.3.0",
             "development": None, "interactive": False, "answer": "y", "prompts": [],
             # What the upgraded doctor answers: its --json rows (a list), raw
             # stdout (a str), or an exception its run raises.
             "doctor": [_row("git", "ok", "git version 2.54.0")], "doctor_rc": 0, "doctor_kwargs": None}

    def latest():
        state["asked"] += 1
        if isinstance(state["newest"], BaseException):
            raise state["newest"]
        return state["newest"]

    def run(argv, *args, **kwargs):
        assert isinstance(argv, list), "an argument list, never a shell string"
        assert not kwargs.get("shell"), "never through a shell"
        state["calls"].append(argv)
        if argv[:2] == [sys.executable, "-c"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{state['now']}\n", stderr="")
        if argv == DOCTOR_CALL:
            state["doctor_kwargs"] = kwargs
            answer = state["doctor"]
            if isinstance(answer, BaseException):
                raise answer
            stdout = answer if isinstance(answer, str) else json.dumps(answer)
            return subprocess.CompletedProcess(argv, state["doctor_rc"], stdout=stdout, stderr="")
        return subprocess.CompletedProcess(argv, state["installer_rc"])

    def ask(prompt=""):
        state["prompts"].append(prompt)
        return state["answer"]

    monkeypatch.setattr(doctor, "latest_release", latest)
    monkeypatch.setattr(griot, "__version__", "0.2.1")
    monkeypatch.setattr(doctor, "development_install", lambda: state["development"])
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess.run only"))
    monkeypatch.setattr("griot.common.is_interactive", lambda: state["interactive"])
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr(sys, "prefix", PIPX_PREFIX)
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    return state


def _installer_calls(world):
    return [c for c in world["calls"] if c[:2] != [sys.executable, "-c"] and c[1:] != DOCTOR_CALL[1:]]


def _doctor_calls(world):
    return [c for c in world["calls"] if c == DOCTOR_CALL]


# --- PyPI -----------------------------------------------------------------------------------------


def test_up_to_date_says_so_and_runs_nothing(world, capsys):
    world["newest"] = "0.2.1"

    assert update.main(["--yes"]) == 0

    assert world["calls"] == []
    out = capsys.readouterr().out
    assert "0.2.1" in out and "up to date" in out


def test_newer_than_pypi_says_so_and_runs_nothing(world, capsys):
    world["newest"] = "0.2.0"

    assert update.main(["--yes"]) == 0

    assert world["calls"] == []
    assert "newer than" in capsys.readouterr().out


def test_pypi_out_of_reach_says_it_could_not_check_and_runs_nothing(world, capsys):
    import requests

    world["newest"] = requests.ConnectionError("offline")

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    assert "could not" in capsys.readouterr().err.lower()


def test_any_failure_of_the_query_is_could_not_check_not_a_traceback(world, capsys):
    world["newest"] = ValueError("no version in PyPI's answer")

    assert update.main(["--yes"]) != 0
    assert world["calls"] == []


def test_a_version_that_cannot_be_compared_runs_nothing(world, capsys):
    world["newest"] = "0.3.0rc1"

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    assert "could not compare" in capsys.readouterr().err


def test_pypi_is_asked_once_through_the_doctor_query(world):
    world["newest"] = "0.2.1"

    update.main([])

    assert world["asked"] == 1


# --- how griot is installed -----------------------------------------------------------------------


@pytest.mark.parametrize("prefix, expected", [
    (PIPX_PREFIX, ["pipx", "upgrade", "griot-rag"]),
    (UV_PREFIX, ["uv", "tool", "upgrade", "griot-rag"]),
    (VENV_PREFIX, [f"{VENV_PREFIX}/bin/python", "-m", "pip", "install", "--upgrade", "griot-rag"]),
])
def test_each_install_method_runs_its_own_upgrade(world, monkeypatch, capsys, prefix, expected):
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "executable", f"{prefix}/bin/python")

    assert update.main(["--yes"]) == 0

    assert _installer_calls(world) == [expected]
    assert " ".join(expected) in capsys.readouterr().out, "the exact command is shown"


def test_a_development_install_is_refused_with_the_reason(world, capsys):
    world["development"] = "an editable install of /Users/you/code/griot"

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    err = capsys.readouterr().err
    assert "/Users/you/code/griot" in err and "checkout" in err


def test_a_development_install_inside_pipx_is_still_refused(world, capsys):
    world["development"] = "an editable install of /Users/you/code/griot"

    assert update.main(["--yes"]) != 0
    assert world["calls"] == []


def test_an_unknown_method_is_refused_and_the_candidates_printed(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")

    assert update.main(["--yes"]) != 0

    assert world["calls"] == []
    err = capsys.readouterr().err
    for candidate in ("pipx upgrade griot-rag", "uv tool upgrade griot-rag", "pip install --upgrade griot-rag"):
        assert candidate in err


def test_the_commands_are_the_ones_doctor_prints():
    """One detection: the doctor's printed fix is the update's argument list,
    joined."""
    for prefix in (PIPX_PREFIX, UV_PREFIX, VENV_PREFIX, "/usr", "/Users/you/my work/venv"):
        argvs = doctor.upgrade_argvs(prefix, "/usr", f"{prefix}/bin/python")
        assert doctor.upgrade_commands(prefix, "/usr", f"{prefix}/bin/python") == [
            __import__("shlex").join(a) for a in argvs]


class _Distribution:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.direct_url


def test_an_editable_install_is_development(monkeypatch):
    found = _Distribution(json.dumps({"url": "file:///Users/you/code/griot", "dir_info": {"editable": True}}))
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: found)

    reason = doctor.development_install()

    assert reason is not None and "/Users/you/code/griot" in reason


def test_an_install_from_an_index_is_not_development(monkeypatch):
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: _Distribution(None))

    assert doctor.development_install() is None


def test_a_non_editable_install_from_a_directory_is_not_development(monkeypatch):
    found = _Distribution(json.dumps({"url": "file:///Users/you/code/griot", "dir_info": {}}))
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: found)

    assert doctor.development_install() is None


def test_no_installed_distribution_is_development(monkeypatch):
    """griot imported from a source tree on the path, with no package
    installed: there is nothing for an installer to upgrade."""
    def missing(name):
        raise doctor.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(doctor.metadata, "distribution", missing)

    assert doctor.development_install() is not None


def test_an_unreadable_direct_url_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(doctor.metadata, "distribution", lambda name: _Distribution("{not json"))

    assert doctor.development_install() is not None


# --- the confirmation -----------------------------------------------------------------------------


def test_no_terminal_and_no_yes_refuses_saying_so(world, capsys):
    assert update.main([]) == 2

    assert world["calls"] == []
    assert "no terminal" in capsys.readouterr().err


def test_at_a_terminal_it_asks_and_a_yes_runs_it(world):
    world["interactive"] = True

    assert update.main([]) == 0

    assert len(world["prompts"]) == 1
    assert _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


def test_at_a_terminal_anything_but_yes_runs_nothing(world):
    world["interactive"] = True
    world["answer"] = ""

    assert update.main([]) == 1
    assert world["calls"] == []


def test_yes_answers_without_asking(world):
    world["interactive"] = True

    assert update.main(["--yes"]) == 0

    assert world["prompts"] == []
    assert _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


# --- running it -----------------------------------------------------------------------------------


def test_a_failing_installer_s_exit_code_is_returned(world, capsys):
    world["installer_rc"] = 3

    assert update.main(["--yes"]) == 3

    assert "3" in capsys.readouterr().err


def test_an_installer_not_on_the_path_is_said_not_a_traceback(world, monkeypatch, capsys):
    def missing(argv, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", missing)

    assert update.main(["--yes"]) != 0
    assert "pipx" in capsys.readouterr().err


def test_the_installer_s_output_is_streamed_not_captured(world, monkeypatch):
    seen = {}

    def run(argv, *args, **kwargs):
        if argv == DOCTOR_CALL:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        if argv[:2] != [sys.executable, "-c"]:
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0)
        return subprocess.CompletedProcess(argv, 0, stdout="0.3.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    update.main(["--yes"])

    assert not seen.get("capture_output") and seen.get("stdout") is None and seen.get("stderr") is None


def test_afterwards_it_prints_the_version_read_by_a_new_process(world, capsys):
    world["now"] = "0.3.0"

    update.main(["--yes"])

    version_calls = [c for c in world["calls"] if c[:2] == [sys.executable, "-c"]]
    assert len(version_calls) == 1 and "griot-rag" in version_calls[0][2]
    out = capsys.readouterr().out
    assert "0.3.0 is now installed" in out


def test_afterwards_it_says_running_servers_keep_the_old_version(world, capsys):
    from griot import auth

    update.main(["--yes"])

    captured = capsys.readouterr()
    # Advice, not the result: on stderr, in the block the conditions follow,
    # with the reconnect advice `griot auth` gives, in the same words.
    assert "MCP server" not in captured.out
    assert "MCP servers already running still run griot 0.2.1 until they are restarted." in captured.err
    assert auth.RECONNECT_SERVERS in captured.err and auth.RECONNECT_SERVERS in auth.RUNNING_SERVERS_KEEP_WHAT_THEY_READ


@pytest.mark.parametrize("rc, stdout", [
    (1, "0.3.0\n"),  # output and a failure: a failed read is not a version, whatever it printed
    (0, "\n"),       # success with nothing printed is no version either
])
def test_a_version_that_cannot_be_read_afterwards_is_said(world, monkeypatch, capsys, rc, stdout):
    def run(argv, *args, **kwargs):
        if argv[:2] == [sys.executable, "-c"]:
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="boom")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert update.main(["--yes"]) == 0
    captured = capsys.readouterr()
    assert "is now installed" not in captured.out
    lines = captured.err.rstrip().splitlines()
    # The upgraded interpreter did not answer a one-line import: its checks
    # are not tried (a second failure), and one message says both, the
    # commands last.
    assert any(line.startswith("The upgrade finished, but the upgraded griot did not answer") for line in lines)
    assert lines[-2:] == ["  griot --version", "  griot doctor"]
    assert "Traceback" not in captured.err


# --- what the new version needs done -------------------------------------------------------------
#
# [real use, 2026-11] An index built before keyword vectors stayed that way
# for weeks after the upgrade that made hybrid the default, and agents asking
# for mode=keyword were refused: the upgrade said the version and nothing
# about the index. Now it runs the upgraded doctor and shows each condition
# that needs an action, with its command last.


def _keyword_missing():
    from griot import notices

    return notices.keyword_vectors_missing("griot_jina_code")


def test_the_check_is_the_upgraded_doctor_in_a_new_process(world):
    """This process still holds the old modules: an in-process import would
    check with the old code. The new version's doctor, run by the
    interpreter the upgrade went into, is the one to ask."""
    update.main(["--yes"])

    assert _doctor_calls(world) == [DOCTOR_CALL]
    assert world["calls"].index(DOCTOR_CALL) > world["calls"].index(["pipx", "upgrade", "griot-rag"])
    assert world["doctor_kwargs"].get("capture_output") and world["doctor_kwargs"].get("timeout") >= 120
    assert world["doctor_kwargs"].get("env") is None, "the environment is inherited: same directories and profile"


def test_a_condition_the_new_version_reports_is_said_with_its_command_last(world, capsys):
    from griot import notices

    found = _keyword_missing()
    world["doctor"] = [_row("git", "ok", "git version 2.54.0"),
                       _row("index", "warn", f"12 points in griot_jina_code; {found.sentence}", found.action, [found])]
    # doctor exits 1 on any FAIL: that is its report, not a failed check.
    world["doctor_rc"] = 1

    assert update.main(["--yes"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[-1] == "griot 0.3.0 is now installed (this was 0.2.1).", "stdout: the result"
    err = captured.err
    assert "griot 0.3.0 reports 1 thing to do after the upgrade:\n" in err
    # The ledger's own sentence, in the form every CLI notice has.
    assert notices.prose(found) in err
    assert err.rstrip().splitlines()[-1] == "  griot index keywords"
    assert err.index("MCP servers already running") < err.index("reports 1 thing"), "after the restart advice"
    assert "Nothing else needs doing" not in err and "could not run" not in err


def test_a_condition_already_in_the_rows_prose_is_not_counted_again(world, capsys):
    found = _keyword_missing()
    world["doctor"] = [_row("index", "warn", f"12 points; {found.sentence}", found.action, [found]),
                       _row("quality", "ok", "a note", "griot quality-check")]

    update.main(["--yes"])

    lines = capsys.readouterr().err.rstrip().splitlines()
    assert "griot 0.3.0 reports 1 thing to do after the upgrade:" in lines
    assert "  griot doctor" not in lines and lines[-1] == "  griot index keywords"


@pytest.fixture
def legacy_doctor_rows(legacy_index, monkeypatch):
    """What the real doctor reports on a real index built before keyword
    vectors (test_keyword_search's fixture), as its --json prints it."""
    monkeypatch.setattr(doctor, "latest_release", lambda: "0.3.0")
    return json.loads(json.dumps(doctor.run_checks()))


def test_the_real_doctor_s_report_of_a_legacy_index_says_the_keyword_command(legacy_doctor_rows, world, capsys):
    from griot import common, notices

    world["doctor"] = legacy_doctor_rows

    assert update.main(["--yes"]) == 0

    err = capsys.readouterr().err
    assert notices.prose(notices.keyword_vectors_missing(common.COLLECTION_NAME)) in err
    assert "  griot index keywords\n" in err


def test_other_warnings_and_failures_are_counted_in_one_closing_item(world, capsys):
    """The rows that carry no ledger condition (a credential, a directory
    mode) keep their own prose in the doctor: counted here, not reworded."""
    found = _keyword_missing()
    world["doctor"] = [_row("profile", "FAIL", "openai-small has no credential", "griot auth set openai"),
                       _row("directories", "warn", "readable by others", "chmod 700 /Users/you/.config/griot"),
                       _row("index", "warn", f"12 points; {found.sentence}", found.action, [found]),
                       _row("spend", "ok", "US$ 0.00 today")]
    world["doctor_rc"] = 1

    assert update.main(["--yes"]) == 0

    err = capsys.readouterr().err
    # The header counts the ledger's conditions; the doctor's other rows are
    # one count of their own, not one more "thing".
    assert "griot 0.3.0 reports 1 thing to do after the upgrade:" in err
    assert "griot doctor also reports 1 failure, 1 warning besides these:\n  griot doctor" in err
    assert err.rstrip().splitlines()[-1] == "  griot doctor"
    assert "griot auth set openai" not in err and "chmod" not in err, "the doctor's own fixes stay in the doctor"


def test_a_row_with_a_warning_of_its_own_beside_the_condition_is_counted(world, capsys):
    """A run that died, in the same row as the keyword condition: the
    condition is listed, and the row's own fix is not lost."""
    found = _keyword_missing()
    world["doctor"] = [_row("index", "warn", f"the last indexing run did not finish: boom; {found.sentence}",
                            f"griot index all   # and read what it says\n{found.action}", [found])]

    update.main(["--yes"])

    err = capsys.readouterr().err
    assert "griot 0.3.0 reports 1 thing to do after the upgrade:" in err
    assert "griot doctor also reports 0 failures, 1 warning besides these:" in err


def test_only_other_warnings_are_said_without_also(world, capsys):
    world["doctor"] = [_row("repositories", "warn", "no repository registered", "griot repos add <path>")]

    update.main(["--yes"])

    err = capsys.readouterr().err
    # No condition: no "1 thing" header over a count of 1 warning, one line.
    assert err.rstrip().splitlines()[-2:] == ["After the upgrade, griot 0.3.0's checks report 0 failures, 1 warning:",
                                              "  griot doctor"]
    assert "thing to do" not in err


def test_a_warning_with_no_fix_and_no_condition_is_still_counted(world, capsys):
    world["doctor"] = [_row("settings", "warn", "something the doctor has no command for")]

    update.main(["--yes"])

    assert "griot 0.3.0's checks report 0 failures, 1 warning:\n  griot doctor" in capsys.readouterr().err


def test_text_from_the_other_process_is_printable(world, capsys):
    """A repository name can hold an escape sequence; the doctor's report
    comes from another process, and is cleaned before it reaches a terminal."""
    from griot import notices

    behind = notices.Notice(id="behind:x", severity="warn", fingerprint="", sentence="the index of x\x1b[2J is behind",
                            action="griot index all --repo x\x1b[2J")
    world["doctor"] = [_row("repositories", "warn", behind.sentence, behind.action, [behind])]

    update.main(["--yes"])

    assert "\x1b" not in capsys.readouterr().err


def test_a_multi_command_action_is_one_indented_line_per_command(world, capsys):
    from griot import notices

    renamed = notices.legacy_env([("RAG_CHAT_MODEL", "GRIOT_CHAT_MODEL")])
    two = notices.Notice(id="x", severity="warn", fingerprint="", sentence="two things to run",
                         action="griot index keywords\ngriot index all")
    world["doctor"] = [_row("old settings", "warn", renamed.sentence, renamed.action, [renamed]),
                       _row("index", "warn", two.sentence, two.action, [two])]

    update.main(["--yes"])

    err = capsys.readouterr().err
    assert "Two things to run.\n  griot index keywords\n  griot index all" in err
    assert f"{renamed.sentence}\n  {renamed.action}" in err


def test_a_note_that_needs_no_action_is_not_shown(world, capsys):
    from griot import notices

    note = notices.Notice(id="quality_older_than_index", severity="ok", fingerprint="", sentence="numbers are older",
                          action="griot quality-check")
    world["doctor"] = [_row("quality", "ok", note.sentence, note.action, [note])]

    update.main(["--yes"])

    err = capsys.readouterr().err
    assert "griot quality-check" not in err and "Nothing else needs doing" in err


def test_a_progress_line_says_the_checks_are_running_before_the_pause(world, capsys):
    update.main(["--yes"])

    err = capsys.readouterr().err
    assert "Running griot 0.3.0's checks (they only read)...\n" in err
    assert err.index("MCP servers already running") < err.index("Running griot") < err.index("Nothing else")


def test_none_says_nothing_else_needs_doing_after_the_restart_advice(world, capsys):
    world["doctor"] = [_row("git", "ok", "git version 2.54.0"), _row("release", "skip", "turned off")]

    assert update.main(["--yes"]) == 0

    err = capsys.readouterr().err
    last = err.rstrip().splitlines()[-1]
    # The doctor's own count phrase: one fact, one wording.
    assert last == "Nothing else needs doing: griot 0.3.0's checks report 0 failures, 0 warnings."
    assert err.index("MCP servers already running") < err.index("Nothing else")


@pytest.mark.parametrize("answer, cause", [
    (OSError(8, "Exec format error"), "it did not start"),
    (subprocess.TimeoutExpired(DOCTOR_CALL, 120), "they took longer than 120 s"),
    ("Traceback (most recent call last):\n  ...", "their report could not be read"),
    ("", "their report could not be read"),
    ('{"check": "git"}', "their report could not be read"),
    ('[{"check": "git"}]', "their report could not be read"),
    ('[{"check": "index", "status": "warn", "notices": [{"id": "x"}]}]', "their report could not be read"),
    ('[{"check": "index", "status": "warn", "notices": "griot index keywords"}]', "their report could not be read"),
    ('[{"check": "index", "status": "warn", "fix": 3, "notices": []}]', "their report could not be read"),
    # Empty, of another type: iterating them would read as "no rows" or "no
    # condition", i.e. nothing to do.
    ("{}", "their report could not be read"),
    ('[{"check": "index", "status": "warn", "notices": ""}]', "their report could not be read"),
])
def test_a_check_that_cannot_run_is_one_message_and_the_upgrade_still_succeeds(world, capsys, answer, cause):
    world["doctor"] = answer

    assert update.main(["--yes"]) == 0

    captured = capsys.readouterr()
    assert "0.3.0 is now installed" in captured.out
    lines = captured.err.rstrip().splitlines()
    assert lines[-2:] == [f"The upgraded griot's checks could not run ({cause}); they only read, so run them yourself:",
                          "  griot doctor"]
    assert "Error" not in captured.err and "Traceback" not in captured.err.replace(str(answer), "")
    assert "OSError" not in captured.err and "TimeoutExpired" not in captured.err


def test_a_doctor_that_exits_non_zero_with_no_report_did_not_start(world, capsys):
    """An interpreter that cannot import the new griot exits 1 with a
    traceback on its stderr and nothing on stdout: it did not start."""
    world["doctor"], world["doctor_rc"] = "", 1

    update.main(["--yes"])

    assert "(it did not start)" in capsys.readouterr().err


def test_no_check_runs_when_the_upgrade_failed_or_did_not_run(world, monkeypatch):
    world["installer_rc"] = 3
    update.main(["--yes"])
    world["installer_rc"] = 0
    world["newest"] = "0.2.1"
    update.main(["--yes"])

    assert _doctor_calls(world) == []


def test_no_check_runs_when_the_installer_could_not_start(world, monkeypatch, capsys):
    calls = []

    def missing(argv, *args, **kwargs):
        calls.append(argv)
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", missing)

    assert update.main(["--yes"]) == 1
    assert calls == [["pipx", "upgrade", "griot-rag"]]
    err = capsys.readouterr().err
    # The upgrade command last: what to run once the installer is on the PATH.
    assert err.strip().splitlines() == ["Error: pipx is not on the PATH, so the upgrade could not run. Nothing was "
                                        "changed.", "  pipx upgrade griot-rag"]
    assert "Errno" not in err


def test_an_installer_that_cannot_be_run_says_permission_denied(world, monkeypatch, capsys):
    def denied(argv, *args, **kwargs):
        raise PermissionError(13, "Permission denied", argv[0])

    monkeypatch.setattr(subprocess, "run", denied)

    assert update.main(["--yes"]) == 1
    assert capsys.readouterr().err.strip().splitlines() == [
        "Error: pipx could not be run (permission denied). Nothing was changed.", "  pipx upgrade griot-rag"]


def test_an_installer_that_cannot_be_run_for_another_reason_keeps_the_cause(world, monkeypatch, capsys):
    def broken(argv, *args, **kwargs):
        raise OSError(8, "Exec format error", argv[0])

    monkeypatch.setattr(subprocess, "run", broken)

    assert update.main(["--yes"]) == 1
    err = capsys.readouterr().err
    assert err.strip().splitlines() == [
        "Error: pipx could not be run (exec format error). Nothing was changed.", "  pipx upgrade griot-rag"]
    assert "Errno" not in err and "OSError" not in err


def test_a_failing_installer_says_where_the_reason_is_and_how_to_see_the_version(world, capsys):
    world["installer_rc"] = 3

    assert update.main(["--yes"]) == 3

    lines = capsys.readouterr().err.rstrip().splitlines()
    assert lines == ["Error: pipx upgrade griot-rag exited with 3: its output above says why. "
                     "griot may still be 0.2.1:", "  griot --version"]


@pytest.mark.parametrize("failure, cause", [
    ("connection", "no network answer"),
    ("timeout", "no network answer"),
    ("json", "PyPI's answer could not be read"),
    ("value", "PyPI's answer could not be read"),
])
def test_pypi_that_cannot_be_asked_names_the_cause_not_the_exception(world, capsys, failure, cause):
    import requests

    world["newest"] = {"connection": requests.ConnectionError("offline"), "timeout": requests.Timeout("slow"),
                       "json": requests.JSONDecodeError("Expecting value", "<html>", 0),
                       "value": ValueError("no version in PyPI's answer")}[failure]

    assert update.main(["--yes"]) == 1

    lines = capsys.readouterr().err.rstrip().splitlines()
    assert lines == [f"Error: could not ask PyPI for the newest release ({cause}); this is griot 0.2.1. "
                     f"Nothing was run.", "  griot update"]


def test_the_doctor_s_release_row_names_the_same_cause(monkeypatch):
    import requests

    def offline():
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(doctor, "latest_release", offline)
    monkeypatch.setenv("GRIOT_UPDATE_CHECK", "true")
    from griot import common

    check = doctor.check_release(common)
    # The same fact as `griot update`'s error, in the same words.
    assert check["detail"].startswith("could not ask PyPI for the newest release (no network answer); this is griot ")
    assert "ConnectionError" not in check["detail"]


# --quiet: an action the upgrade left to do is not noise. The upgraded doctor
# records what it found as said (notices.listed), so no later footer would
# say a condition hidden here: silencing it would lose it for good. Said once
# per upgrade, the upgrade being the change of state. A check that could not
# run is data that is missing, not a notice (policy section 5), and the
# doctor may have recorded its conditions before its report turned out
# unreadable: also never silenced. What is silenced is the "nothing else"
# line, the progress line and the restart advice.


def test_quiet_still_shows_a_condition_the_upgrade_left_to_do(world, monkeypatch, capsys):
    from griot import notices

    monkeypatch.setenv("GRIOT_QUIET", "1")
    found = _keyword_missing()
    world["doctor"] = [_row("index", "warn", found.sentence, found.action, [found]),
                       _row("profile", "FAIL", "no credential", "griot auth set openai")]

    assert update.main(["--yes"]) == 0

    err = capsys.readouterr().err
    assert notices.prose(found) in err and "griot doctor also reports 1 failure, 0 warnings" in err
    assert "MCP servers" not in err


def test_quiet_still_shows_the_doctor_s_own_warnings_count(world, monkeypatch, capsys):
    """No ledger condition, but the doctor warns: still something to do
    after the upgrade, pointed at `griot doctor`."""
    monkeypatch.setenv("GRIOT_QUIET", "1")
    world["doctor"] = [_row("repositories", "warn", "no repository registered", "griot repos add <path>")]

    assert update.main(["--yes"]) == 0

    assert capsys.readouterr().err.splitlines() == [
        "After the upgrade, griot 0.3.0's checks report 0 failures, 1 warning:", "  griot doctor"]


def test_quiet_with_nothing_to_do_is_silent(world, monkeypatch, capsys):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    world["doctor"] = [_row("git", "ok", "git")]

    assert update.main(["--yes"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "0.3.0 is now installed" in captured.out, "the result is never silenced"


@pytest.mark.parametrize("doctor_answer, cause", [
    (OSError("no"), "it did not start"),
    # The doctor marks its conditions as said after printing its report:
    # one it printed but that cannot be read must still send the person to
    # `griot doctor`, or those conditions are never seen.
    ("stray line\n[]", "their report could not be read"),
])
def test_quiet_still_says_a_check_that_could_not_run(world, monkeypatch, capsys, doctor_answer, cause):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    world["doctor"] = doctor_answer

    assert update.main(["--yes"]) == 0

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"The upgraded griot's checks could not run ({cause}); they only read, so run them yourself:",
        "  griot doctor"]
    assert "0.3.0 is now installed" in captured.out


def test_quiet_still_says_the_version_could_not_be_read(world, monkeypatch, capsys):
    monkeypatch.setenv("GRIOT_QUIET", "1")
    world["now"] = ""

    update.main(["--yes"])

    assert capsys.readouterr().err.rstrip().splitlines()[-1] == "  griot doctor"


# The notices footer the CLI ends a command with would repeat, right under
# these lines, `griot doctor` or a count of the same conditions.


def test_the_footer_is_not_said_after_the_check_ran(world, monkeypatch, capsys):
    from griot import cli, notices

    monkeypatch.setattr(notices, "footer", lambda *a, **k: "2 notices: run `griot doctor`")
    world["doctor"] = OSError("no")

    assert cli.main(["update", "--yes"]) == 0

    assert "2 notices" not in capsys.readouterr().err


def test_the_footer_is_still_said_when_nothing_was_upgraded(world, monkeypatch, capsys):
    from griot import cli, notices

    monkeypatch.setattr(notices, "footer", lambda *a, **k: "2 notices: run `griot doctor`")
    world["newest"] = "0.2.1"

    assert cli.main(["update", "--yes"]) == 0

    assert "2 notices" in capsys.readouterr().err


def test_the_help_says_it_runs_the_new_version_s_checks(capsys):
    with pytest.raises(SystemExit):
        update.main(["--help"])

    text = " ".join(capsys.readouterr().out.split())
    assert "runs the new version's `griot doctor` (reads only)" in text
    assert "never indexes or builds anything itself" in text
    assert "--quiet does not hide those" in text


# --- the real thing: a griot process upgrading, with the new doctor for real -----------------------

_DRIVER = r'''
import os, subprocess, sys
from griot import doctor
doctor.latest_release = lambda: "999.0.0"
doctor.development_install = lambda: None
real_run = subprocess.run
def run(argv, *args, **kwargs):
    if argv[1:2] in (["-c"], ["-I"]):
        done = real_run(argv, *args, **kwargs)  # the version and the doctor: for real
        if argv[1:2] == ["-I"] and os.environ.get("STRAY_DOCTOR_LINE"):
            # A report the real doctor printed (and recorded as said) but
            # that cannot be read: a stray line in front of its JSON.
            done.stdout = "stray line\n" + done.stdout
        return done
    print("the installer would run: " + " ".join(argv), flush=True)
    return subprocess.CompletedProcess(argv, 0)
subprocess.run = run
sys.prefix, sys.base_prefix = "/Users/you/.local/share/pipx/venvs/griot-rag", "/usr"
from griot import cli
sys.exit(cli.main(["update", *sys.argv[1:]]))
'''


def _real_env(tmp_path, extra=None):
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_", "CLAUDE_"))}
    (tmp_path / "home").mkdir()
    # No harness on PATH (asking one starts the registered server), PyPI not
    # asked by the doctor, no keychain: the suite touches nothing real.
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"),
               HOME=str(tmp_path / "home"), PATH=f"{os.path.dirname(sys.executable)}:/usr/bin:/bin",
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", **(extra or {}))
    return env


def _real_update(tmp_path, *args, extra=None):
    return subprocess.run([sys.executable, "-c", _DRIVER, *args], env=_real_env(tmp_path, extra), capture_output=True,
                          text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=tmp_path)


def _real_update_at_a_terminal(tmp_path, keys):
    """The same, on a pseudo-terminal, answering the question with `keys`."""
    import os
    import pty
    import select

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen([sys.executable, "-c", _DRIVER], stdin=slave, stdout=slave, stderr=slave,
                                env=_real_env(tmp_path), cwd=tmp_path, close_fds=True)
        os.close(slave)
        os.write(master, keys)
        out = b""
        while True:
            ready, _, _ = select.select([master], [], [], 120)
            if not ready:
                proc.kill()
                pytest.fail(f"timed out; so far: {out!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=60)
    finally:
        os.close(master)
    return code, out.decode(errors="replace").replace("\r\n", "\n")


def test_at_a_real_terminal_a_yes_upgrades_and_then_says_what_is_left(tmp_path):
    code, out = _real_update_at_a_terminal(tmp_path, b"y\n")

    assert code == 0, out
    assert "Run it?" in out and "the installer would run: pipx upgrade griot-rag" in out
    assert "After the upgrade, griot " in out and out.rstrip().endswith("  griot doctor")


def test_at_a_real_terminal_a_no_runs_neither_the_upgrade_nor_the_check(tmp_path):
    code, out = _real_update_at_a_terminal(tmp_path, b"n\n")

    assert code == 1, out
    assert "Nothing was changed" in out
    assert "the installer would run" not in out and "to do after the upgrade" not in out


def test_with_no_terminal_and_no_yes_nothing_runs(tmp_path):
    done = _real_update(tmp_path)

    assert done.returncode == 2
    assert "Nothing was changed" in done.stderr and "the installer would run" not in done.stdout
    assert "after the upgrade" not in done.stderr and "Nothing else" not in done.stderr


def test_a_real_update_runs_the_real_new_doctor_and_counts_what_it_reports(tmp_path):
    """A fresh installation: nothing indexed, no repository registered. The
    doctor's own warnings carry no ledger condition, so they are one count
    pointing at `griot doctor`, and the upgrade exits 0."""
    done = _real_update(tmp_path, "--yes")

    assert done.returncode == 0, done.stderr
    assert "the installer would run: pipx upgrade griot-rag" in done.stdout
    assert "is now installed" in done.stdout and "MCP servers" not in done.stdout
    lines = done.stderr.rstrip().splitlines()
    assert lines[0].startswith("MCP servers already running still run griot ")
    assert lines[-2].startswith("After the upgrade, griot ") and "'s checks report 0 failures, " in lines[-2]
    assert lines[-1] == "  griot doctor" and "thing to do" not in done.stderr
    assert "notices: run" not in done.stderr, "no footer under it"
    assert "Traceback" not in done.stderr


def test_a_real_quiet_update_with_a_renamed_variable_still_says_it(tmp_path):
    """--quiet, through the real flag: the ledger condition the new doctor
    found and recorded as said is still shown, with its command."""
    done = _real_update(tmp_path, "--yes", "--quiet", extra={"RAG_CHAT_MODEL": "some-model"})

    assert done.returncode == 0, done.stderr
    assert "RAG_CHAT_MODEL was renamed and griot ignores it.\n  export GRIOT_CHAT_MODEL=\"$RAG_CHAT_MODEL\"" \
        in done.stderr
    assert "MCP servers" not in done.stderr


def test_a_real_quiet_update_whose_doctor_report_cannot_be_read_still_says_so(tmp_path):
    """[review, 2026-11-19] The real doctor records a condition as said and
    then prints a report this update cannot read: under --quiet the person
    was told nothing, and no later footer said the condition. Now the check
    that could not run is said, pointing at `griot doctor`, which lists it."""
    done = _real_update(tmp_path, "--yes", "--quiet", extra={"RAG_CHAT_MODEL": "x", "STRAY_DOCTOR_LINE": "1"})

    assert done.returncode == 0, done.stderr
    assert done.stderr.splitlines()[-2:] == [
        "The upgraded griot's checks could not run (their report could not be read); they only read, so run them "
        "yourself:", "  griot doctor"]


# --- where it is reachable ------------------------------------------------------------------------


def test_griot_update_dispatches_to_this_module(world, capsys):
    from griot import cli

    world["newest"] = "0.2.1"

    assert cli.main(["update"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_griot_help_lists_update(capsys):
    from griot import cli

    assert "update" in cli.build_parser().format_help()


@pytest.mark.anyio
async def test_no_mcp_tool_updates_griot():
    """An agent must not upgrade the tool it is using: `griot update` is a
    command for a person, never a tool."""
    from mcp.client.client import Client
    from griot import mcp_server

    async with Client(mcp_server.mcp) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert names, "the server lists its tools"
    assert [n for n in names if "update" in n or "upgrade" in n or "install" in n and "assist" not in n] == []


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_update_check_turned_off_does_not_stop_the_explicit_command(world, monkeypatch, value):
    """GRIOT_UPDATE_CHECK governs the doctor's automatic check; a person who
    runs `griot update` asked for PyPI to be asked."""
    monkeypatch.setenv("GRIOT_UPDATE_CHECK", value)

    assert update.main(["--yes"]) == 0

    assert world["asked"] == 1 and _installer_calls(world) == [["pipx", "upgrade", "griot-rag"]]


def test_update_uses_only_the_doctor_s_public_names():
    """update.py reuses the doctor's release query, version rules and
    upgrade detection (one implementation of each), through names the doctor
    publishes: a private helper renamed or reshaped inside doctor.py would
    otherwise break `griot update` with nothing in doctor.py saying so."""
    import pathlib
    import re

    source = pathlib.Path(update.__file__).read_text()
    assert re.findall(r"\bdoctor\._\w+", source) == []
    for name in ("latest_release", "release_numbers", "upgrade_commands", "upgrade_argvs", "development_install"):
        assert callable(getattr(doctor, name)), name
        assert f"doctor.{name}(" in source, name
