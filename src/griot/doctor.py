"""`griot doctor` — one command that checks what the scattered error messages
check one at a time.

A new installation fails in one of a dozen small ways, each reported by the
command that happened to hit it: a setting the file holds that griot cannot
start with, a paid profile without its credential, a repository that moved,
a server registered for a griot that is gone, read-only tools that still ask
before every search. This runs every check, says ok / warn / FAIL / skip for
each with what to do, and changes nothing. The exit status is 1 only for a
FAIL: a warning is something to know, not something broken.

It reads. It changes no setting, no index and no file of yours. Two things
happen on the way, and both are said: the configuration loading closes a
configuration file left open to other users, as every griot command does
(reported under settings); and asking a harness which server it has
registered may start that server for a moment, as `griot assist install`
does when it asks the same.

Loaded BEFORE the configuration (cli.py runs it without importing common
first): the check that matters most is the one for a file griot cannot
start with, and it has to run when nothing else can.
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from griot import ConfigurationError, UnknownEmbedProfile

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
# The name of the check on git itself (a label: the one git call here goes
# through common.run_git like every other).
GIT = "git"
# What the files and directories griot owns must be closed to.
OTHERS = 0o077


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"check": name, "status": status, "detail": detail, "fix": fix}


def _env_path_without_the_configuration() -> Path:
    """Where the configuration file is, computed as common.py computes it,
    for the one check that must run when common.py cannot load."""
    config_home = os.getenv("GRIOT_CONFIG_DIR", os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return Path(config_home) / "griot" / ".env"


def _settings_in_file(env_path: Path) -> list[dict]:
    """The settings the file holds that griot cannot start with."""
    from dotenv import dotenv_values

    from griot import config

    if not env_path.exists():
        return []
    values = dotenv_values(env_path)
    broken = []
    for setting in config.SETTINGS:
        raw = values.get(setting.variable)
        if raw is None or setting.elsewhere:
            continue
        try:
            config.normalized(setting, raw)
        except config._NotValid as e:
            broken.append({"variable": setting.variable, "name": setting.name, "why": str(e)})
    return broken


def check_settings(env_path: Path, error: Exception | None = None, mode_before: int | None = None) -> dict:
    """The file: there, closed to others, and every value one griot can
    start with. `error` is what loading the configuration raised, when it
    did: said here, since this check is what names the broken line.
    `mode_before` is the file's mode before the configuration loaded: every
    griot command closes an open file as it loads, so a look afterwards
    finds it closed and would say nothing."""
    if isinstance(error, UnknownEmbedProfile):
        return _check("settings", FAIL, f"GRIOT_EMBED_PROFILE names a profile that does not exist: {error.name!r} "
                                        f"(set in the environment, or in {env_path})",
                      f"griot profiles use <name>   # one of: {', '.join(error.options)}; or unset the variable")
    broken = _settings_in_file(env_path)
    if broken:
        return _check("settings", FAIL,
                      "; ".join(f"{b['variable']}: {b['why']}" for b in broken),
                      "; ".join(f"griot config set {b['name']} <value>" for b in broken) + "   (or `griot config unset <name>`)")
    if error is not None:
        return _check("settings", FAIL, f"the configuration did not load: {error}", None)
    if not env_path.exists():
        return _check("settings", OK, f"{env_path} does not exist yet: defaults in force (it is written on first use)")
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode_before is not None and mode_before & OTHERS:
        return _check("settings", WARN, f"{env_path} was mode {mode_before:04o}, readable by other users; it holds "
                                        f"credentials. griot closed it to 0600 just now, as every command does",
                      "nothing more: it is 0600 now; whatever opened it may do so again")
    if mode & OTHERS:
        return _check("settings", WARN, f"{env_path} is mode {mode:04o}; it holds credentials and should be 0600",
                      f"chmod 600 {env_path}")
    return _check("settings", OK, f"{env_path}: every value is one griot can start with, mode 0600")


def check_directories(common) -> dict:
    there = [d for d in (common.CONFIG_DIR, common.DATA_DIR) if d.is_dir()]
    open_ones = [str(d) for d in there if stat.S_IMODE(d.stat().st_mode) & OTHERS]
    if open_ones:
        return _check("directories", WARN, "readable by other users of this machine: " + ", ".join(open_ones),
                      "chmod 700 " + " ".join(open_ones))
    missing = [str(d) for d in (common.CONFIG_DIR, common.DATA_DIR) if d not in there]
    said = []
    if there:
        said.append(", ".join(str(d) for d in there) + (" are" if len(there) > 1 else " is") + " private (0700)")
    if missing:
        said.append(", ".join(missing) + " not created yet (made private on first use)")
    return _check("directories", OK, "; ".join(said))


def check_profile(common) -> dict:
    from griot import auth

    name, profile = common.ACTIVE_PROFILE_NAME, common.ACTIVE_PROFILE
    key_env = common.credential_env_for_profile(name, profile)
    if key_env is None:
        cache = common.DATA_DIR / "models"
        where = "model downloaded" if cache.is_dir() and any(cache.iterdir()) else "model downloads on first use"
        return _check("profile", OK, f"{name} (local, {profile['model']}): no credential needed; {where}")
    status = {s["env_var"]: s for s in auth.provider_status()}
    entry = status.get(key_env)
    if entry is None or not entry.get("configured"):
        provider = (entry or {}).get("provider") or key_env
        return _check("profile", FAIL, f"{name} calls an API and its credential ({key_env}) is not configured",
                      f"griot auth set {provider}")
    return _check("profile", OK, f"{name} (API): credential {key_env} is configured")


def check_credentials(common) -> dict:
    """A credential exported in the shell overrides the one griot stores:
    `griot auth set` then changes nothing in that shell, and an API that
    refuses the key looks like a bad new key. Places, never values."""
    overridden, exported = [], []
    for provider, env_var in sorted(common.credential_env_vars().items()):
        origin = common.credential_origin(env_var)
        if origin["source"] != "environment":
            continue
        where = ", ".join(origin["exported_in"]) or "this shell"
        (overridden if origin["shadows_stored"] else exported).append((env_var, where))
    if overridden:
        return _check("credentials", WARN,
                      "exported in the shell with a value other than the one griot stores, which it hides: "
                      + "; ".join(f"{var} ({where})" for var, where in overridden),
                      "remove the export from " + "; ".join(where for _, where in overridden)
                      + ", and `unset` it in terminals already open")
    if exported:
        return _check("credentials", OK, "from the shell, and griot stores no other value: "
                      + "; ".join(f"{var} ({where})" for var, where in exported))
    return _check("credentials", OK, "none is overridden by the shell")


def check_index(common) -> dict:
    status = common.get_index_status(reuse_active_handle=False)
    error = status.get("points_error")
    last = status.get("last_indexed") or {}
    if error and str(error).startswith("unreadable"):
        return _check("index", FAIL, f"the collection could not be read: {str(error)[len('unreadable: '):]}", None)
    if last.get("error"):
        # A run with no counts died; one with counts finished and could not
        # do its job (the platform refused every fetch, say).
        what = "did not finish" if last.get("indexed") is None else "failed"
        return _check("index", WARN, f"the last indexing run {what}: {last['error']}",
                      "griot index all   # and read what it says")
    if error:
        return _check("index", OK, "the collection is held by another process (a running server or index run): "
                                   "count not read")
    count = status.get("points_count") or 0
    if not count:
        return _check("index", WARN, "nothing indexed yet in this profile's collection", "griot index all")
    return _check("index", OK, f"{count} points in {status.get('collection')}"
                               + (f", last indexed {last.get('timestamp')}" if last.get("timestamp") else ""))


def check_repositories(common) -> dict:
    from griot import freshness

    try:
        paths = common.load_repos()
    except FileNotFoundError:
        paths = []  # nothing registered yet: the file is written by the first `griot repos add`
    except (OSError, ValueError) as e:
        return _check("repositories", FAIL, f"repos.json could not be read: {e}", None)
    if not paths:
        return _check("repositories", WARN, "no repository registered: nothing to index",
                      "griot repos add <path>")
    gone = [p for p in paths if not Path(p).is_dir()]
    if gone:
        return _check("repositories", FAIL, "registered but not there: " + ", ".join(gone),
                      "; ".join(f"griot repos remove {p}" for p in gone))
    reports = freshness.repository_freshness()
    behind = [r for r in reports if r.get("behind")]
    without_code = [r["repo"] for r in reports if "code" in (r.get("missing_sources") or [])]
    if behind or without_code:
        parts = []
        if behind:
            from griot import stats
            parts.append("index behind in " + ", ".join(stats._behind_phrase(r) for r in behind))
        if without_code:
            parts.append("code never indexed in " + ", ".join(without_code))
        return _check("repositories", WARN, f"{len(paths)} registered; " + "; ".join(parts),
                      "griot index all   # or one with --repo <name>")
    return _check("repositories", OK, f"{len(paths)} registered, all present"
                                       + (", none behind its repository" if reports and all(r.get("behind") is False for r in reports) else ""))


def check_spend(common) -> dict:
    today, ceiling = common.get_spend_today(), common.SPEND_CEILING_USD
    if not today < ceiling:
        return _check("spend", WARN, f"today's estimated spend ${today:.2f} reached the ${ceiling:.2f} daily ceiling: "
                                     f"paid calls are refused until tomorrow", "griot config set spend-ceiling <usd>   # asks first")
    return _check("spend", OK, f"${today:.2f} today of a ${ceiling:.2f} daily ceiling")


def _present_harnesses():
    from griot import harnesses

    return [h for h in harnesses.HARNESSES if h.detect()]


def check_mcp_registration() -> dict:
    from griot import harnesses

    present = _present_harnesses()
    if not present:
        return _check("mcp registration", SKIP, "no supported agent harness found on this machine")
    details, worst = [], OK
    for harness in present:
        registration = harnesses.mcp_registration(harness)
        if registration is None:
            details.append(f"{harness.display_name}: cannot be asked which servers it has"
                           + ("" if harness.mcp_inspect else " (it has no command for that)"))
            continue
        if not registration:
            details.append(f"{harness.display_name}: no server named griot")
            worst = WARN if worst == OK else worst
            continue
        command = registration.get("command") or ""
        if harnesses._command_is_gone(command):
            details.append(f"{harness.display_name}: registered ({registration.get('scope')}) but its command is gone: {command}")
            worst = FAIL
            continue
        details.append(f"{harness.display_name}: registered, scope {registration.get('scope')}, runs `{command} "
                       f"{registration.get('args', '')}`".rstrip("` ") + "`")
    if all("cannot be asked" in d for d in details):
        return _check("mcp registration", SKIP, "; ".join(details))
    fix = "griot assist install" if worst != OK else None
    return _check("mcp registration", worst, "; ".join(details), fix)


def check_tool_approval(home: Path | None) -> dict:
    from griot import harnesses

    detected = _present_harnesses()
    present = [h for h in detected if h.global_settings_file]
    if not detected:
        return _check("tool approval", SKIP, "no supported agent harness found on this machine")
    if not present:
        return _check("tool approval", SKIP, "no settings file griot knows for: " + ", ".join(h.display_name for h in detected))
    rules_offered = harnesses.tool_rules(present[0])
    details, worst, to_repair = [], OK, False
    for harness in detected:
        if not harness.global_settings_file:
            details.append(f"{harness.display_name}: no settings file griot knows")
            continue
        path = harnesses.settings_file(harness, "global", home=home)
        problem = harness.user_dir_problem() if harness.user_dir_problem else None
        if problem:
            details.append(f"{harness.display_name}: {problem}")
            worst, to_repair = WARN, True
            continue
        settings, why_not, _ = harnesses._read_settings(path) if path else (None, "unknown", "")
        if settings is None:
            details.append(f"{harness.display_name}: {path} cannot be used as settings: {why_not}")
            worst, to_repair = WARN, True
            continue
        to_add, left_out = harnesses._approval_plan(settings, rules_offered)
        if to_add:
            details.append(f"{harness.display_name}: {len(to_add)} of {len(rules_offered)} read-only tools still ask "
                           f"before every call")
            worst = WARN
        elif left_out:
            details.append(f"{harness.display_name}: every read-only tool is allowed or decided by you "
                           f"({', '.join(sorted(set(left_out.values())))})")
        else:
            details.append(f"{harness.display_name}: every read-only tool is allowed without asking")
    if worst == OK:
        fix = None
    elif to_repair:
        fix = ("fix the file or CLAUDE_CONFIG_DIR first (griot assist install refuses them as they are); then "
               "`griot assist install` offers the rules")
    else:
        fix = "griot assist install   # offers the rules, asks first"
    return _check("tool approval", worst, "; ".join(details), fix)


def check_server_environment(common) -> dict:
    """What a server started with THIS environment would ignore: the
    variables that would widen the configuration (see griot.ENVIRONMENT_ONLY_NARROWS)."""
    from griot import config

    ignored = []
    for setting in config.SETTINGS:
        raw = common.ENVIRONMENT_BEFORE_ENV_FILE.get(setting.variable)
        if raw is None:
            continue
        reason, _ = config.measured_from_environment(setting, raw)
        if reason:
            ignored.append(f"{setting.variable} ({reason})")
    if ignored:
        return _check("server environment", WARN, "a server started with this environment would ignore: " + ", ".join(ignored),
                      "griot config set <name> <value>   # or griot profiles use; the environment only narrows")
    exported = sorted(v for v in common.ENVIRONMENT_BEFORE_ENV_FILE if any(s.variable == v for s in config.SETTINGS))
    return _check("server environment", OK, "exported and obeyed by a server too: " + ", ".join(exported) if exported
                  else "no griot setting is exported in this environment")


def check_git(common=None) -> dict:
    found = shutil.which(GIT)
    if not found:
        return _check(GIT, FAIL, "git is not on the PATH; every indexer and the freshness report need it", "install git")
    if common is None:
        return _check(GIT, OK, f"found at {found}")
    try:
        version = common.run_git(str(Path.cwd()), ["--version"], timeout=10, check=False).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return _check(GIT, FAIL, f"{found} could not be run: {e}", None)
    return _check(GIT, OK, f"{version} at {found}")


def check_log(common) -> dict:
    from griot import logdb

    if not common.LOG_DIR.exists():
        return _check("log", OK, f"{common.LOG_DIR} does not exist yet: nothing has run")
    try:
        runs = logdb.read_recent(common.LOG_DIR, "runs", limit=1)
    except Exception as e:  # sqlite3 errors, permissions: whatever keeps the log from being read
        return _check("log", FAIL, f"the log could not be read ({common.LOG_DIR / logdb.DB_FILENAME}): {e}", None)
    return _check("log", OK, f"readable; last run {runs[0].get('timestamp')}" if runs else "readable; no run yet")


def run_checks(*, home: Path | None = None) -> list[dict]:
    """Every check, in the order a person reads them. The configuration is
    loaded here, not before: a file griot cannot start with is the first
    thing to report, and then only the checks that need nothing of it run."""
    env_path = _env_path_without_the_configuration()
    mode_before = stat.S_IMODE(env_path.stat().st_mode) if env_path.exists() else None
    try:
        from griot import common
    except ConfigurationError as error:
        checks = [check_settings(env_path, error, mode_before)]
        checks.append(check_git())  # without the configuration: found or not
        skipped = "skipped: the configuration did not load"
        checks.extend(_check(name, SKIP, skipped) for name in
                      ("directories", "profile", "credentials", "index", "repositories", "spend", "mcp registration",
                       "tool approval", "server environment", "log"))
        return checks
    checks = [
        ("settings", lambda: check_settings(common.ENV_PATH, None, mode_before)),
        ("directories", lambda: check_directories(common)),
        ("profile", lambda: check_profile(common)),
        ("credentials", lambda: check_credentials(common)),
        ("index", lambda: check_index(common)),
        ("repositories", lambda: check_repositories(common)),
        ("spend", lambda: check_spend(common)),
        ("mcp registration", check_mcp_registration),
        ("tool approval", lambda: check_tool_approval(home)),
        ("server environment", lambda: check_server_environment(common)),
        (GIT, lambda: check_git(common)),
        ("log", lambda: check_log(common)),
    ]
    return [_guarded(name, check) for name, check in checks]


def _guarded(name: str, check) -> dict:
    """A check that raises is a failed check, not a dead doctor: a corrupt
    log database, say, must be reported where the person reads it, and the
    other checks must still run."""
    try:
        return check()
    except Exception as e:  # whatever one check could not foresee
        return _check(name, FAIL, f"the check itself failed: {type(e).__name__}: {e}", None)


def exit_status(checks: list[dict]) -> int:
    return 1 if any(check["status"] == FAIL for check in checks) else 0


def format_report(checks: list[dict]) -> str:
    lines = []
    for check in checks:
        lines.append(f"{check['status']:<5} {check['check']:<19} {check['detail']}")
        if check.get("fix"):
            lines.append(f"{'':<5} {'':<19} → {check['fix']}")
    failures = sum(check["status"] == FAIL for check in checks)
    warnings = sum(check["status"] == WARN for check in checks)
    lines.append("")
    lines.append(f"{failures} failure{'s' if failures != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}"
                 + (": fix the failures first" if failures else "" if warnings else ": griot is ready"))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot doctor",
        description="Checks griot's setup: settings, profile and credential, index, repositories, spend, the MCP "
                    "registration and tool approval, the environment a server would obey, git, the log. Reads only "
                    "(changes no setting, index or file of yours). Exit status 1 only when a check fails.")
    parser.add_argument("--json", action="store_true", help="One JSON document instead of the report")
    args = parser.parse_args(argv)
    checks = run_checks()
    print(json.dumps(checks, indent=2) if args.json else format_report(checks))
    return exit_status(checks)


if __name__ == "__main__":
    sys.exit(main())
