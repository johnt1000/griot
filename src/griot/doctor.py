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
does when it asks the same. One goes to the network: the release check asks
PyPI for the newest version (GRIOT_UPDATE_CHECK=false turns it off); the only
other griot command that makes that request is `griot update`, which a person
runs to upgrade and which that setting does not govern.

Loaded BEFORE the configuration (cli.py runs it without importing common
first): the check that matters most is the one for a file griot cannot
start with, and it has to run when nothing else can.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from griot import ConfigurationError, UnknownEmbedProfile

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
# The name of the check on git itself (a label: the one git call here goes
# through common.run_git like every other).
GIT = "git"
# The name of the check on a newer release, and where it asks.
RELEASE = "release"
DISTRIBUTION = "griot-rag"
PYPI_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
# What the files and directories griot owns must be closed to.
OTHERS = 0o077
# How many of the entries open to others a report names (it counts them all).
OPEN_ENTRIES_NAMED = 3


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
        if not raw.strip() and setting.kind in config.EMPTY_IS_DEFAULT:
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
                                        f"(set in the environment, or in {env_path}).{error.reason}",
                      f"griot profiles use <name>   # one of: {', '.join(error.options)}; or unset the variable")
    broken = _settings_in_file(env_path)
    if broken:
        return _check("settings", FAIL,
                      "; ".join(f"{b['variable']}: {b['why']}" for b in broken),
                      "; ".join(f"griot config set {b['name']} <value>" for b in broken) + "   (or `griot config unset <name>`)")
    if error is not None:
        return _check("settings", FAIL, f"the configuration did not load: {error}", None)
    if not env_path.exists() and env_path.is_symlink():
        # Every command writes the template through a link whose target can
        # be created, so one still dangling here leads nowhere griot can
        # write: "written on first use" would be false.
        return _check("settings", WARN, f"{env_path} is a symbolic link to {os.path.realpath(env_path)}, which does "
                                        f"not exist and cannot be created: defaults in force, and writing a setting fails",
                      f"point the link at a file, or remove it: rm {env_path}")
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


def _open_entries(data_dir: Path) -> list[str]:
    """What under the data directory other users can read or enter. The
    repair that closes the index's files is best-effort (a chmod that fails
    twice is logged, not raised, so it never fails a search or an index
    run): this is where what it left open shows up. Read only: lstat, so a
    link is not followed out of the directory. The model cache is skipped:
    the embedding library writes the downloaded weights with its own modes,
    and they are public, not anything indexed."""
    found = []
    for root, dirs, files in os.walk(data_dir):
        if Path(root) == data_dir:
            dirs[:] = [d for d in dirs if d != "models"]
        for name in dirs + files:
            entry = os.path.join(root, name)
            try:
                st = os.lstat(entry)
            except OSError:
                continue  # gone while walking
            if not stat.S_ISLNK(st.st_mode) and stat.S_IMODE(st.st_mode) & OTHERS:
                found.append(entry)
    return found


def check_directories(common) -> dict:
    there = [d for d in (common.CONFIG_DIR, common.DATA_DIR) if d.is_dir()]
    open_ones = [str(d) for d in there if stat.S_IMODE(d.stat().st_mode) & OTHERS]
    if open_ones:
        return _check("directories", WARN, "readable by other users of this machine: " + ", ".join(open_ones),
                      "chmod 700 " + " ".join(open_ones))
    inside = _open_entries(common.DATA_DIR) if common.DATA_DIR.is_dir() else []
    if inside:
        named = ", ".join(inside[:OPEN_ENTRIES_NAMED]) + (", ..." if len(inside) > OPEN_ENTRIES_NAMED else "")
        return _check("directories", WARN,
                      f"{len(inside)} entr{'y' if len(inside) == 1 else 'ies'} in {common.DATA_DIR} readable by other "
                      f"users (a permission repair that failed, or a copy that reset modes): {named}",
                      f"chmod -R go-rwx {common.DATA_DIR}")
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
    """Where each credential is kept, and what hides or exposes it. Names and
    places, never values, and it moves nothing: a secret is moved only when
    the person asks (`griot auth migrate`).

    A credential exported in the shell overrides the one griot stores:
    `griot auth set` then changes nothing in that shell, and an API that
    refuses the key looks like a bad new key. A credential still in the
    plaintext file while a keychain is reachable is one `griot auth migrate`
    would protect (a warning); with no keychain reachable the file is griot's
    fallback, which is said here instead of happening silently (a note: the
    check stays OK, since nothing may be able to clear it)."""
    from griot import auth

    keychain = common.keychain_status()
    overridden, exported, in_file, in_keychain = [], [], [], []
    for provider, env_var in sorted(common.credential_env_vars().items()):
        origin = common.credential_origin(env_var)
        {"file": in_file, "keychain": in_keychain}.get(origin["stored"], []).append(env_var)
        if origin["source"] != "environment":
            continue
        (overridden if origin["shadows_stored"] else exported).append((env_var, origin))

    details, fixes = [], []
    if overridden:
        details.append("exported in the shell with a value other than the one griot stores, which it hides: "
                       + "; ".join(f"{var} ({common.export_places(origin)})" for var, origin in overridden))
        fixes.append("; ".join(f"{var}: {common.export_advice(var, origin)}" for var, origin in overridden))
    if in_file:
        details.append(f"in plaintext in {common.ENV_PATH}: {', '.join(in_file)}")
        if keychain["available"]:
            fixes.append("`griot auth migrate` moves them into the OS keychain")
        else:
            # A note, not a warning: with no backend the file is where keys
            # belong, and a container or headless server may never have one.
            # A warning nobody can clear teaches people to skip the doctor.
            details.append("once an OS keychain is reachable, `griot auth migrate` moves them there")
    if in_keychain:
        details.append(f"in the OS keychain: {', '.join(in_keychain)}")
    if exported:
        details.append("from the environment, and griot stores no other value: "
                       + "; ".join(f"{var} ({common.export_places(origin)})" for var, origin in exported))
    if not overridden:
        details.append("none is overridden by the shell")
    details.append(auth.keychain_phrase(keychain))
    return _check("credentials", WARN if fixes else OK, "; ".join(details), "; ".join(fixes) or None)


def check_index(common) -> dict:
    status = common.get_index_status(reuse_active_handle=False)
    error = status.get("points_error")
    last = status.get("last_indexed") or {}
    if error and str(error).startswith("unreadable"):
        return _check("index", FAIL, f"the collection could not be read: {str(error)[len('unreadable: '):]}", None)
    from griot import stats

    # A refusal the repositories check names (with the repositories, which
    # this line cannot) is said there only: the same failure said twice.
    if last.get("error") and not stats.refusal_names_last_run(last, status.get("repositories") or []):
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
    # A last run that failed (a refusal said by the repositories check)
    # wrote nothing: it was an attempt, as `griot stats` says it.
    wrote = "last indexing attempt" if last.get("error") else "last indexed"
    detail = f"{count} points in {status.get('collection')}" + (
        f", {wrote} {last.get('timestamp')}" if last.get("timestamp") else "")
    if status.get("keyword_search") is False:
        # Not a warning: every search still answers, the default by meaning
        # alone instead of hybrid (common.search_mode_for), as before keyword
        # search existed. Said so that a person learns what the default is
        # missing here, and the one command that gives it back.
        return _check("index", OK, detail + "; keyword search is not built for it yet, so searches run by "
                      "meaning only instead of the default hybrid",
                      "griot index keywords   # local, embeds nothing")
    return _check("index", OK, detail)


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
    from griot import stats
    refused = stats.platform_refused_names(reports)
    if behind or without_code or refused:
        parts = []
        if behind:
            parts.append("index behind in " + ", ".join(stats._behind_phrase(r) for r in behind))
        if without_code:
            parts.append("code never indexed in " + ", ".join(without_code))
        if refused:
            parts.append(stats.platform_refused_phrase(refused))
        # A refusal is fixed with the token first: indexing again before
        # that is refused again.
        fix = "griot index all   # or one with --repo <name>"
        if refused:
            fix = "griot auth list   # check the platform's token, then: " + fix
        return _check("repositories", WARN, f"{len(paths)} registered; " + "; ".join(parts), fix)
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


# Public, with upgrade_argvs, upgrade_commands and development_install: the
# release check's pieces that `griot update` (update.py) reuses, so that
# asking PyPI, ordering versions and detecting the installer each have one
# implementation, and a change here is a change to a published name.
def latest_release() -> str:
    """The newest version on PyPI, or an exception (requests.RequestException,
    ValueError) when there is no answer to trust.

    Through the session common.py keeps, which stores no cookie. Short timeout:
    a doctor run offline must not hang on this. No redirect: the address is
    PyPI's own, and an answer from anywhere else is not PyPI's."""
    from griot import common

    response = common.http_session().get(PYPI_URL, timeout=3, allow_redirects=False)
    response.raise_for_status()
    info = response.json()
    info = info.get("info") if isinstance(info, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str):
        raise ValueError("no version in PyPI's answer")
    return version


def release_numbers(version: str) -> tuple[int, ...] | None:
    """`0.10.1` as (0, 10, 1), with trailing zeros dropped so that 0.2 and
    0.2.0 are the same release; None for anything else. A pre-release, a
    development or a local version orders against a release by PEP 440
    rules this does not carry (`packaging` is not a dependency of griot),
    and comparing one by its numbers alone would be a guess."""
    # [0-9], not \d: \d also takes digits of other scripts, which int() reads.
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", version):
        return None
    numbers = [int(part) for part in version.split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def upgrade_argvs(prefix: str, base_prefix: str, executable: str) -> list[list[str]]:
    """The command that upgrades griot where it runs, as argument lists,
    guessed from the environment's location (pipx and `uv tool` each keep one
    per tool under a directory of their own); every likely one when that says
    nothing. One answer is as sure as this gets: `griot update` runs it only
    then, and the doctor prints it either way (see upgrade_commands)."""
    parts = Path(prefix).parts
    if "pipx" in parts and "venvs" in parts:
        return [["pipx", "upgrade", DISTRIBUTION]]
    if "uv" in parts and "tools" in parts:
        return [["uv", "tool", "upgrade", DISTRIBUTION]]
    if prefix != base_prefix:
        return [[executable, "-m", "pip", "install", "--upgrade", DISTRIBUTION]]
    return [["pipx", "upgrade", DISTRIBUTION], ["uv", "tool", "upgrade", DISTRIBUTION],
            ["python3", "-m", "pip", "install", "--upgrade", DISTRIBUTION]]


def upgrade_commands(prefix: str, base_prefix: str, executable: str) -> list[str]:
    """upgrade_argvs() as a person types them, quoted for a shell: what the
    doctor prints and `griot update` shows (the same lists it runs)."""
    return [shlex.join(argv) for argv in upgrade_argvs(prefix, base_prefix, executable)]


def development_install() -> str | None:
    """Why this griot is a development install (a phrase naming where it
    comes from), or None for an install an installer can upgrade.

    PEP 610's direct_url.json is what pip, uv and pipx write for an install
    from a directory; `dir_info.editable` marks an editable one. A file that
    cannot be read is refused rather than taken as a regular install: a
    wrong guess here replaces someone's checkout with a release. A legacy
    `setup.py develop` install writes no direct_url.json and so reads as an
    install from an index: a known gap, left because that way of installing
    predates PEP 610 and griot never documented it.

    Here and not in update.py: the doctor prints an upgrade command too, and
    must not print one that would replace a checkout."""
    try:
        distribution = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return f"griot runs from a source tree ({DISTRIBUTION} is not installed as a package)"
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        return None  # installed from an index: the ordinary case
    try:
        direct = json.loads(raw)
        editable = direct.get("dir_info", {}).get("editable") is True
        url = direct.get("url")
    except (ValueError, AttributeError):
        return f"griot's install record (direct_url.json) could not be read: {raw[:80]!r}"
    if editable:
        return f"an editable install of {url}"
    return None


def _upgrade_fix(commands: list[str]) -> str:
    if len(commands) == 1:
        return commands[0]
    return " or ".join(commands) + "   # whichever installed griot"


def check_release(common) -> dict:
    """Whether a newer griot was released. The one check that goes to the
    network: on by default because nothing else tells a person a release
    came out, and turned off with update-check (SECURITY.md has the row).

    Never a failure: PyPI out of reach, or a version that cannot be compared
    exactly, says nothing about this installation, so it is a skip that says
    it could not tell (an ok would claim an answer it does not have)."""
    import griot

    if not common.update_check_enabled():
        return _check(RELEASE, SKIP, "turned off (update-check is false): PyPI was not asked")
    installed = griot.__version__
    try:
        newest = latest_release()
    # Every exception, not the ones requests documents: whatever stopped the
    # answer, there is none, and the doctor's guard would turn the rest into
    # a FAIL about an installation that has nothing wrong with it.
    except Exception as e:
        return _check(RELEASE, SKIP, f"could not ask PyPI for the newest release ({type(e).__name__}); "
                                     f"this is {installed}")
    mine, theirs = release_numbers(installed), release_numbers(newest)
    if mine is None or theirs is None:
        return _check(RELEASE, SKIP, f"could not compare {installed} (installed) with {newest!r} (newest on PyPI)")
    if mine < theirs:
        detail = f"{installed} is installed; {newest} is the newest release on PyPI"
        development = development_install()
        if development is not None:
            # An installer would replace the checkout with the release: the
            # same refusal `griot update` makes, so no command is printed.
            return _check(RELEASE, WARN, detail, f"update the checkout (git pull): this is a development install, "
                                                 f"{development}")
        return _check(RELEASE, WARN, detail,
                      _upgrade_fix(upgrade_commands(sys.prefix, sys.base_prefix, sys.executable)))
    if mine > theirs:
        return _check(RELEASE, OK, f"{installed} is newer than the newest release on PyPI ({newest}): "
                                   f"a version not released yet")
    return _check(RELEASE, OK, f"{installed}, the newest release on PyPI")


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
                       "tool approval", "server environment", "log", RELEASE))
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
        (RELEASE, lambda: check_release(common)),
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
                    "registration and tool approval, the environment a server would obey, git, the log, and whether "
                    "a newer griot was released (asks PyPI; `griot config set update-check false` turns that off). "
                    "Reads only (changes no setting, index or file of yours). Exit status 1 only when a check fails.")
    parser.add_argument("--json", action="store_true", help="One JSON document instead of the report")
    args = parser.parse_args(argv)
    checks = run_checks()
    print(json.dumps(checks, indent=2) if args.json else format_report(checks))
    return exit_status(checks)


if __name__ == "__main__":
    sys.exit(main())
