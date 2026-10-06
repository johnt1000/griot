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
"""

import argparse
import subprocess
import sys

from griot import doctor

# Read in a new interpreter: this process has the old griot loaded, and its
# metadata may be cached; only a fresh one sees what the installer left.
_VERSION_PROGRAM = f"import importlib.metadata as m; print(m.version({doctor.DISTRIBUTION!r}))"


def _installed_version(executable: str) -> str | None:
    try:
        done = subprocess.run([executable, "-c", _VERSION_PROGRAM], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    version = done.stdout.strip()
    return version if done.returncode == 0 and version else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot update",
        description="Upgrades griot to the newest release on PyPI: asks PyPI (whatever update-check says: that "
                    "setting governs only `griot doctor`), works out how griot was installed (pipx, uv tool, or "
                    "pip in a virtual environment), shows the exact command and asks before running it. Refuses "
                    "a development install (update the checkout instead) and an installation whose method it "
                    "cannot tell (it prints the candidates). Running MCP servers keep the old version until "
                    "restarted.")
    parser.add_argument("--yes", action="store_true", help="Run the upgrade without asking")
    args = parser.parse_args(argv)

    import griot
    from griot import common

    installed = griot.__version__
    try:
        newest = doctor.latest_release()
    # Every exception, as in the doctor's check: whatever stopped the answer,
    # there is none, and nothing may run on a missing one.
    except Exception as e:
        print(f"Error: could not ask PyPI for the newest release ({type(e).__name__}); this is {installed}. "
              f"Nothing was run.", file=sys.stderr)
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
    try:
        # An argument list, never a shell string; nothing captured, so the
        # installer's own output reaches the person as it happens.
        done = subprocess.run(argvs[0])
    except OSError as e:
        print(f"Error: could not run {argvs[0][0]}: {e}. Nothing was changed.", file=sys.stderr)
        return 1
    if done.returncode != 0:
        print(f"Error: {commands[0]} exited with {done.returncode}.", file=sys.stderr)
        return done.returncode
    now = _installed_version(sys.executable)
    if now is None:
        print("Done, but griot could not read the version now installed: run `griot --version`.")
    else:
        print(f"griot {now} is now installed (this was {installed}).")
    print("MCP servers already running keep the old version until they are restarted (restart the session "
          "or the client that started them).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
