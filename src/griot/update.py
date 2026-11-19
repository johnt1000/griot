"""`griot update` — runs the upgrade that `griot doctor` prints.

It asks PyPI for the newest griot-rag with the doctor's own query, compares
versions with the doctor's own rules and works out the command with the
doctor's own detection (one implementation of each, in doctor.py). Then it
shows the exact command and asks before running it.

What it refuses, and why:
- a development install (editable, or griot imported from a source tree):
  the installer would replace the checkout with PyPI's release, so the
  checkout is what to update;
- an installation whose method cannot be told: running a guess is a
  different promise from printing one, so it prints the candidates;
- no terminal and no --yes: nobody could be asked. Updating griot widens no
  boundary, so a flag answers (unlike the confirmations with no flag).

It is a command for a person, never an MCP tool: an agent must not upgrade
the tool it is using. GRIOT_UPDATE_CHECK governs the doctor's automatic
check only; asking PyPI is what this explicit command is for.

After a successful upgrade it runs the new version's `griot doctor --json`
and shows each condition that needs an action, with its command last (an
index built before keyword vectors stayed that way for weeks after the
upgrade that made hybrid the default: nothing said so). The check runs the
UPGRADED interpreter as a subprocess because this process still holds the
old modules: an import here would check with the old code. The doctor exits
1 on any failure, which is its report, not a failed check; a check that
cannot run is one message pointing at `griot doctor` (shown under --quiet
too) and never changes the exit status of an upgrade that succeeded. It
only reads: `griot update` never indexes or builds anything itself.

The --json rows (and their `notices` field) are therefore read across
versions: this release's `griot update` parses the next release's doctor,
so changing that shape is a change to a published contract.
"""

import argparse
import json
import subprocess
import sys

from griot import doctor

# Read in a new interpreter: this process has the old griot loaded, and its
# metadata may be cached; only a fresh one sees what the installer left.
_VERSION_PROGRAM = f"import importlib.metadata as m; print(m.version({doctor.DISTRIBUTION!r}))"

# The new version's doctor, run by the interpreter the upgrade went into.
# -I: `-m` would otherwise put the current directory first on the path, and
# a directory named griot there would be checked instead of the install.
DOCTOR_ARGS = ["-I", "-m", "griot.doctor", "--json"]
# Generous: the doctor asks PyPI (unless update-check is off) and may start
# the registered MCP server for a moment to check its registration.
DOCTOR_TIMEOUT_S = 120

# Set once this run said what the new version needs done (or that it could
# not tell): cli.py then ends the command without the notices footer, which
# would repeat, right under it, a count of the same conditions or
# `griot doctor`. Only that path: an `update` that found nothing to upgrade
# may still end with a footer.
CHECKED_AFTER_UPGRADE = False

_SEVERE = ("warn", "FAIL")


class _Unreadable(Exception):
    """The doctor's stdout is not the report this version knows how to read."""


def _str(value) -> bool:
    return isinstance(value, str)


def _report(stdout: str) -> list[dict]:
    """The doctor's rows, checked as far as this code reads them. A shape it
    does not know (a future doctor that changed it) is a check that could
    not be read, never a guess at what the rows say."""
    try:
        rows = json.loads(stdout)
    except ValueError:
        raise _Unreadable() from None
    if not isinstance(rows, list):
        raise _Unreadable()
    for row in rows:
        if not isinstance(row, dict) or not _str(row.get("status")) or not (row.get("fix") is None or _str(row["fix"])):
            raise _Unreadable()
        found = row.get("notices", [])
        if not isinstance(found, list):
            raise _Unreadable()
        for notice in found:
            if not isinstance(notice, dict) or not all(_str(notice.get(k)) for k in ("id", "severity", "sentence",
                                                                                      "action")):
                raise _Unreadable()
    return rows


def _run_new_doctor(executable: str) -> list[dict] | str:
    """The upgraded doctor's rows, or why there are none (a phrase)."""
    try:
        # The environment is inherited: the same configuration and data
        # directories, the same profile, --quiet included.
        done = subprocess.run([executable, *DOCTOR_ARGS], capture_output=True, text=True, timeout=DOCTOR_TIMEOUT_S,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"they took longer than {DOCTOR_TIMEOUT_S} s"
    except (OSError, subprocess.SubprocessError):
        return "it did not start"
    if done.returncode != 0 and not (done.stdout or "").strip():
        # A failure is exit 1 WITH the report; exit non-zero with nothing on
        # stdout is an interpreter that could not import the new griot.
        return "it did not start"
    try:
        return _report(done.stdout or "")
    except _Unreadable:
        return "their report could not be read"


def _covered(row: dict, actionable: list[dict]) -> bool:
    """Whether a row's fix is no more than its listed conditions' commands:
    then the conditions say all of it. A row with a fix of its own besides
    (a run that died, beside the keyword condition) is also counted, so its
    own warning is not lost."""
    actions = {line for notice in actionable for line in notice["action"].splitlines()}
    return bool(actionable) and all(line in actions for line in (row.get("fix") or "").splitlines() if line)


def _after_upgrade(rows: list[dict], now: str) -> tuple[str, bool]:
    """What the new version's checks report: (the message, whether it names
    something to do, which --quiet does not silence)."""
    from griot import common, notices

    listed, failures, warnings = [], 0, 0
    for row in rows:
        actionable = [n for n in row.get("notices", []) if n["severity"] in _SEVERE]
        listed.extend(actionable)
        if not _covered(row, actionable):
            failures += row["status"] == "FAIL"
            warnings += row["status"] == "warn"
    if not listed and not failures and not warnings:
        return (f"Nothing else needs doing: griot {now}'s checks report {doctor.counts_phrase(0, 0)}.", False)
    if not listed:
        # Only the doctor's own rows: one line, not a "1 thing" header over
        # a count of several warnings.
        return (f"After the upgrade, griot {now}'s checks report {doctor.counts_phrase(failures, warnings)}:\n"
                f"  griot doctor", True)
    # Printable line by line: the text came from another process, and a
    # line break inside an action is how several commands are given.
    items = [notices.prose(notices.Notice(id=n["id"], severity=n["severity"], fingerprint="",
                                          sentence=common.printable(n["sentence"]),
                                          action="\n".join(map(common.printable, n["action"].splitlines()))))
             for n in listed]
    if failures or warnings:
        # The doctor's own fixes are prose in places: counted here and read
        # there, not reworded as if they were commands.
        counted = doctor.counts_phrase(failures, warnings)
        items.append(f"griot doctor also reports {counted} besides these:\n  griot doctor")
    # The header counts the conditions; the doctor's other rows are their
    # own count, under them.
    return ("\n".join([f"griot {now} reports {len(listed)} thing{'s' if len(listed) != 1 else ''} to do after the "
                       f"upgrade:", *items]), True)


def _installed_version(executable: str) -> str | None:
    try:
        done = subprocess.run([executable, "-c", _VERSION_PROGRAM], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    version = done.stdout.strip()
    return version if done.returncode == 0 and version else None


def main(argv=None) -> int:
    from griot.cli import show_flags_read_by_griot  # here: cli is imported before these modules at run time
    parser = argparse.ArgumentParser(
        prog="griot update",
        description="Upgrades griot to the newest release on PyPI: asks PyPI (whatever update-check says: that "
                    "setting governs only `griot doctor`), works out how griot was installed (pipx, uv tool, or "
                    "pip in a virtual environment), shows the exact command and asks before running it. Refuses "
                    "a development install (update the checkout instead) and an installation whose method it "
                    "cannot tell (it prints the candidates). After upgrading, it runs the new version's `griot "
                    "doctor` (reads only) and shows what needs doing, each with its command; it never indexes or "
                    "builds anything itself, and --quiet does not hide those. Running MCP servers keep the old "
                    "version until they are restarted.")
    show_flags_read_by_griot(parser)
    parser.add_argument("--yes", action="store_true", help="Run the upgrade without asking")
    args = parser.parse_args(argv)
    global CHECKED_AFTER_UPGRADE
    CHECKED_AFTER_UPGRADE = False

    import griot
    from griot import common

    installed = griot.__version__
    try:
        newest = doctor.latest_release()
    # Every exception, as in the doctor's check: whatever stopped the answer,
    # there is none, and nothing may run on a missing one.
    except Exception as e:
        print(f"Error: could not ask PyPI for the newest release ({doctor.pypi_failure_cause(e)}); this is griot "
              f"{installed}. Nothing was run.\n  griot update", file=sys.stderr)
        return 1
    mine, theirs = doctor.release_numbers(installed), doctor.release_numbers(newest)
    if mine is None or theirs is None:
        print(f"Error: could not compare {installed} (installed) with {newest!r} (newest on PyPI). "
              f"Nothing was run.", file=sys.stderr)
        return 1
    if mine == theirs:
        print(f"griot {installed} is up to date: it is the newest release on PyPI.")
        return 0
    if mine > theirs:
        print(f"griot {installed} is newer than the newest release on PyPI ({newest}): nothing to update.")
        return 0

    development = doctor.development_install()
    if development is not None:
        print(f"Error: griot {newest} is out, but this is a development install: {development}. An installer "
              f"would replace it with the release; update the checkout instead (git pull). Nothing was run.",
              file=sys.stderr)
        return 1
    argvs = doctor.upgrade_argvs(sys.prefix, sys.base_prefix, sys.executable)
    commands = doctor.upgrade_commands(sys.prefix, sys.base_prefix, sys.executable)
    if len(argvs) != 1:
        print(f"Error: griot {newest} is out, but how this griot was installed cannot be told from where it "
              f"runs ({sys.prefix}), and running a guess could upgrade some other copy. Run the one that "
              f"installed it:\n" + "\n".join(f"  {command}" for command in commands), file=sys.stderr)
        return 1

    print(f"griot {installed} is installed; {newest} is the newest release on PyPI.")
    print(f"This runs: {commands[0]}", flush=True)
    status = common.confirm("Run it?", yes=args.yes)
    if status:
        return status
    # Once the installer runs, this is what to run: the command it shows.
    retry = f"Nothing was changed.\n  {commands[0]}"
    try:
        # An argument list, never a shell string; nothing captured, so the
        # installer's own output reaches the person as it happens.
        done = subprocess.run(argvs[0])
    except FileNotFoundError:
        print(f"Error: {argvs[0][0]} is not on the PATH, so the upgrade could not run. {retry}", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"Error: {argvs[0][0]} could not be run (permission denied). {retry}", file=sys.stderr)
        return 1
    except OSError as e:
        # The system's own words for the cause (an exec format error, say),
        # never the exception's class or errno.
        cause = f" ({e.strerror[:1].lower()}{e.strerror[1:]})" if e.strerror else ""
        print(f"Error: {argvs[0][0]} could not be run{cause}. {retry}", file=sys.stderr)
        return 1
    if done.returncode != 0:
        print(f"Error: {commands[0]} exited with {done.returncode}: its output above says why. griot may still be "
              f"{installed}:\n  griot --version", file=sys.stderr)
        return done.returncode
    return _say_what_is_left(installed, _installed_version(sys.executable))


def _say_what_is_left(installed: str, now: str | None) -> int:
    """After an upgrade that succeeded: the version on stdout (the result),
    then on stderr the restart advice and what the new version's checks
    report. Exit 0 whatever the checks could or could not say.

    Under --quiet the conditions that need an action are still said: the
    new doctor records what it found as said (notices.listed), so a later
    footer would not say a condition hidden here, and it is said once per
    upgrade, the upgrade being the change of state. A check that could not
    run is also said: it is data that is missing, and a doctor whose report
    could not be read may already have recorded its conditions as said, so
    `griot doctor` is the only place left that lists them. Silenced: the
    restart advice, the progress line and the "nothing else" line."""
    global CHECKED_AFTER_UPGRADE
    from griot import auth, common

    CHECKED_AFTER_UPGRADE = True
    advice = (f"MCP servers already running still run griot {installed} until they are restarted. "
              f"{auth.RECONNECT_SERVERS}")
    if now is None:
        # The upgraded interpreter did not answer a one-line import: its
        # doctor would not either, and a second failure would say nothing new.
        if not common.quiet():
            print(advice, file=sys.stderr)
        print("The upgrade finished, but the upgraded griot did not answer, so its version and its checks could "
              "not be read:\n  griot --version\n  griot doctor", file=sys.stderr)
        return 0
    print(f"griot {now} is now installed (this was {installed}).", flush=True)
    if not common.quiet():
        print(advice, file=sys.stderr)
        # The doctor takes seconds (up to its timeout): said, so the pause
        # is not a hang.
        print(f"Running griot {now}'s checks (they only read)...", file=sys.stderr, flush=True)
    report = _run_new_doctor(sys.executable)
    if isinstance(report, str):
        message, shown = (f"The upgraded griot's checks could not run ({report}); they only read, so run them "
                          f"yourself:\n  griot doctor"), True
    else:
        message, shown = _after_upgrade(report, now)
    if shown or not common.quiet():
        print(message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
