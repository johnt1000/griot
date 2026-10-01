import hashlib
import http.cookiejar
import json
import logging
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil
import qdrant_edge as qe
import requests
from dotenv import load_dotenv, set_key, unset_key
from tqdm import tqdm

from griot import FALSE_WORDS, ConfigurationError, UnknownEmbedProfile, logdb, redaction

# Where user config and data live: explicit
# override via GRIOT_CONFIG_DIR/GRIOT_DATA_DIR (also useful for tests),
# otherwise honors XDG_CONFIG_HOME/XDG_DATA_HOME, otherwise falls back to
# ~/.config and ~/.local/share — even on macOS (departs from the "correct"
# ~/Library/Application Support, but it's the convention most dev CLIs
# follow today regardless of OS). No platformdirs on purpose: it would be
# one more dependency just for this. No directory is created here at
# import time — whoever writes to them creates them on demand
# (mkdir parents=True exist_ok=True).
CONFIG_DIR = Path(os.getenv("GRIOT_CONFIG_DIR", os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")))) / "griot"
DATA_DIR = Path(os.getenv("GRIOT_DATA_DIR", os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))) / "griot"

# User-editable config — lives in CONFIG_DIR, never inside the installed
# code (site-packages is immutable/shared). The indexers and quality_check
# resolve repos.json/quality_golden_set.json from HERE now, instead of each
# one doing its own Path(__file__).parent.
ENV_PATH = CONFIG_DIR / ".env"
REPOS_JSON_PATH = CONFIG_DIR / "repos.json"
GOLDEN_SET_PATH = CONFIG_DIR / "quality_golden_set.json"

# Persistent execution log — print()/log_and_print() disappear once the
# terminal session ends. Writes to a file (plain text, for debugging)
# WITHOUT duplicating to the console — tqdm already handles showing it on
# screen without breaking the progress bar; log_and_print() just mirrors the
# same message to the file.
LOG_DIR = DATA_DIR / "logs"

_logger = logging.getLogger("griot")
_logger.setLevel(logging.INFO)


# [findings M2/L4 from the 2026-08-19 security audit] Only .env had
# permission handling — logs (the user's question, source labels),
# .spend_state.json, repos.json, quality_golden_set.json and the whole
# qdrant_data/ (a recoverable plaintext copy of everything indexed) were
# born with the default umask (0644/0755, readable by any local user).
# These helpers are the ONE path through which griot writes data: files
# 0600, directories 0700 — and writing to a pre-existing file repairs its
# permission (an old install inherits the fix on its first write).

def secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def secure_append_line(path: Path, line: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)  # O_CREAT only applies the mode on creation — repairs legacy files
        with os.fdopen(fd, "a") as f:
            fd = None  # fdopen takes ownership of the fd; avoid a double close
            f.write(line)
    finally:
        if fd is not None:
            os.close(fd)


def secure_write_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            fd = None
            f.write(text)
    finally:
        if fd is not None:
            os.close(fd)


def secure_write_text_atomic(path: Path, text: str) -> None:
    """Same 0600 contract as secure_write_text(), but crash-safe: writes to
    a sibling .tmp file first and os.replace()s it into place, so a crash
    mid-write (Ctrl-C, OOM kill) can never leave `path` truncated/corrupted
    — the previous version (or its absence) survives intact. Extracted
    from the spend-state writer (which had this exact tmp+rename pattern
    inline) so any other writer of a small, easily-corrupted state file
    (repos.json, the same file every registration path writes to)
    gets the identical guarantee instead of reimplementing it. Caller is
    responsible for secure_mkdir()'ing the parent first, same as
    secure_write_text().

    [real bug, reproduced with 6 concurrent processes] The scratch file is
    named per-writer (pid suffix), NOT a fixed `<target>.tmp`: with a
    shared name, two processes writing the same target raced on the same
    scratch path — one os.replace()d it out from under the other, and the
    loser died with FileNotFoundError mid-write, crashing a real griot
    process during a paid indexing run. os.replace() is still atomic, so
    the last writer wins cleanly instead of both corrupting each other."""
    tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        secure_write_text(tmp_path, text)  # already 0600 on the tmp file — the rename preserves it
        os.replace(tmp_path, path)
    finally:
        # A failure between write and replace would otherwise leave this
        # process's scratch file behind forever (its name is unique, so
        # nothing else would ever reclaim it).
        tmp_path.unlink(missing_ok=True)


# Provider -> one-line description for the credentials section of the
# generated .env template (ensure_env_template() below). Not the source of
# truth for WHICH providers exist — that's auth._providers() (derived from
# EMBED_PROFILES/CHAT_PROFILES + the 5 platform tokens); this is only extra
# prose for a provider auth._providers() already knows about. A provider
# missing here just gets a generic fallback comment, never breaks.
_ENV_TEMPLATE_PROVIDER_NOTES = {
    "gemini": "default chat profile; not the default embed profile (jina-code, local, is)",
    "openai": "shared by the 'openai-small' embed profile and the 'openai' chat profile — same account/key",
    "deepseek": "chat profile only",
    "groq": "chat profile only, free tier by default (see GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS below)",
    "github": "for `griot index platform` against GitHub repos",
    "gitlab": "for `griot index platform` against GitLab repos (incl. self-hosted, see GRIOT_GITLAB_API_BASE)",
    "bitbucket": "for `griot index platform` against Bitbucket Cloud repos",
    "azure_devops": "for `griot index platform` against Azure Repos",
    "gitea": "for `griot index platform` against Gitea/Forgejo (also needs GRIOT_GITEA_HOSTS)",
}

# (var, default, comment, leave_commented) — operational settings that
# already have a literal default in code (os.getenv(name, default)
# elsewhere in this file/mcp_server.py/platforms.py). Writing the same
# default here explicitly is behaviorally IDENTICAL to leaving the var
# unset — pure documentation, zero behavior change. Keep this in sync if a
# new such var is added.
#
# leave_commented=True is REQUIRED (not cosmetic) for any var whose
# consuming code distinguishes "absent" from "present but empty" via an
# `is not None` check rather than a falsy check — e.g. _optional_float_env()
# below, used by the openai/deepseek/groq chat price vars. [real bug found
# via smoke test] writing `VAR=` (present, empty) makes os.getenv(name)
# return "", which passes the `is not None` check and then crashes on
# float(""). Only a genuinely absent (commented-out) line reproduces "not
# configured" for these. The credential vars in _providers() don't have
# this problem — they're all checked with `if not token`, where "" and
# None behave identically — so they're written empty, not commented.
_ENV_TEMPLATE_SETTINGS = [
    ("GRIOT_EMBED_PROFILE", "jina-code", "active embedding profile — see `griot profiles list`", False),
    ("GRIOT_CHAT_PROFILE", "gemini", "active chat profile for `griot ask` — gemini/openai/deepseek/groq", False),
    ("GRIOT_CHAT_MODEL", "gemini-2.5-flash", "model override within the gemini chat profile", False),
    ("GRIOT_OPENAI_CHAT_MODEL", "gpt-4o-mini", "model override within the openai chat profile", False),
    ("GRIOT_DEEPSEEK_CHAT_MODEL", "deepseek-chat", "model override within the deepseek chat profile", False),
    ("GRIOT_GROQ_CHAT_MODEL", "llama-3.3-70b-versatile", "model override within the groq chat profile", False),
    ("GRIOT_CHAT_PRICE_PER_1M_TOKENS", "2.50", "USD/1M tokens for the gemini chat profile (confirmed price, override if it changes)", False),
    ("GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS", "", "REQUIRED (USD/1M tokens) before using --chat-profile openai — griot never guesses a paid price. Set it with `griot config set openai-chat-price <value>`, or uncomment and set a real value.", True),
    ("GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS", "", "REQUIRED (USD/1M tokens) before using --chat-profile deepseek — same reason. Set it with `griot config set deepseek-chat-price <value>`, or uncomment and set a real value.", True),
    ("GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS", "", "optional — defaults to $0 (free tier) if left commented out; uncomment only if that changes", True),
    ("GRIOT_SPEND_CEILING_USD", "3.0", "daily spend ceiling for the local circuit breaker", False),
    ("GRIOT_SPEND_VELOCITY_CEILING_USD", "1.0", "5-minute window spend ceiling (catches burst spend before the daily one would)", False),
    ("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "5", "abort indexing after this many fully-failed batches in a row", False),
    ("GRIOT_LOG_QUESTIONS", "true", "set to false to omit question text from the query log (metrics are kept either way)", False),
    # Per project, not global: put it in the `env` of a project's .mcp.json. Set in this file
    # it would name EVERY project the same, so it stays commented out here.
    ("GRIOT_PROJECT", "", "name recorded with each search and tool call in the usage logs; defaults to the folder griot runs in", True),
    ("GRIOT_MCP_ENABLE_INDEX", "false", "set to true to enable the griot_index_repo MCP tool (can spend money on a paid profile)", False),
    ("GRIOT_MCP_INDEX_ROOTS", "", "':'-separated directory prefixes allowed for MCP indexing, e.g. /Users/you/code — empty means only repos.json entries are allowed", False),
    # Commented on purpose: a default written out explicitly is an override in
    # disguise, so a .env generated under the old default (`single`) kept
    # pinning it after the default became `multi`.
    ("GRIOT_MCP_CONCURRENCY_MODE", "multi", "multi (default): griot mcp releases the collection after the idle window, so other sessions and shell indexing can use it; single keeps it open for the server's whole life (no reopen cost, but nothing else can use that profile meanwhile)", True),
    ("GRIOT_MCP_IDLE_RELEASE_SECONDS", "30", "idle window before releasing the collection handle in multi mode", False),
    ("GRIOT_GITLAB_API_BASE", "https://gitlab.com/api/v4", "self-hosted GitLab instance API base, if not gitlab.com", False),
    ("GRIOT_GITEA_HOSTS", "", "comma-separated Gitea/Forgejo hostnames to recognize, e.g. git.example.com — required, Gitea has no fixed host to detect", False),
]


def ensure_env_template() -> None:
    """Creates <config_dir>/.env pre-populated with every environment
    variable griot supports, each on its own line with an explanatory
    comment — real defaults written out explicitly (behaviorally identical
    to leaving them unset, pure documentation), credentials as empty
    placeholders (never a fake value). No-op if the file already exists —
    never overwrites real configuration.

    This is the closest thing to "on install" a plain pip/pipx package can
    hook into (no reliable post-install hook exists) — called from
    cli.main() on every invocation (cheap no-op after the first) and from
    auth._ensure_env_file() (so `griot auth set` alone triggers it too,
    even if some other command never ran first)."""
    if ENV_PATH.exists():
        return
    from griot import auth  # lazy: auth.py imports common.py at module load, avoid the cycle

    lines = [
        "# griot configuration — generated on first run.",
        "# `griot config list` shows these, `griot config set <name> <value>` changes one",
        "# with the value checked first. Or edit here: every setting below already carries",
        "# griot's built-in default written out explicitly, so nothing here changes",
        "# behavior until you actually change a value.",
        "#",
        "# GRIOT_CONFIG_DIR / GRIOT_DATA_DIR are NOT listed here on purpose — they",
        "# decide WHERE this file lives, so they must be real shell/session",
        "# environment variables, never a line inside it.",
        "",
        "# --- Embedding & chat credentials ---",
    ]
    for provider, env_var in sorted(auth._providers().items()):
        note = _ENV_TEMPLATE_PROVIDER_NOTES.get(provider, f"credential for the '{provider}' provider")
        lines.append(f"# {provider} — {note}")
        lines.append(f"{env_var}=")
    lines.append("")
    lines.append("# --- Profiles, pricing, spend limits, logging, MCP server, platforms ---")
    for var, default, comment, leave_commented in _ENV_TEMPLATE_SETTINGS:
        lines.append(f"# {comment}")
        prefix = "#" if leave_commented else ""
        lines.append(f"{prefix}{var}={default}")

    secure_mkdir(ENV_PATH.parent)
    secure_write_text(ENV_PATH, "\n".join(lines) + "\n")


def env_file_set(var: str, value: str) -> None:
    """The single write path INTO <config_dir>/.env for an arbitrary var
    (credential or operational setting). Ensures the template exists first
    (so this works even on a fresh install), writes via dotenv's set_key(),
    and re-applies 0600 — set_key() rewrites the WHOLE file, so the
    permission guaranteed at creation may not survive the rewrite depending
    on umask. Extracted from auth.cmd_set() (which used to do this inline)
    so any other writer reuses the exact same
    discipline instead of duplicating it and risking a missed chmod.

    [review] Known cosmetic wart, consciously accepted: dotenv's set_key()
    doesn't recognize an existing COMMENTED-OUT line for `var` as "already
    present" — setting a var that ships pre-commented in the template (the
    3 leave_commented price vars) leaves the stale `#VAR=` line in place
    and appends a new active `VAR=value` line below it. Harmless
    functionally (dotenv_values()/os.getenv() only ever see the active
    line, never the commented one), just a redundant leftover a hand-edit
    of .env would find odd. Not worth the complexity of post-processing
    the file to strip it."""
    ensure_env_template()
    set_key(ENV_PATH, var, value)
    ENV_PATH.chmod(0o600)


def env_file_unset(var: str) -> None:
    """Removes one var's line from .env entirely — no-op if it's already
    absent. Clearing a `leave_commented=True` setting must go through here:
    a removed line, never a written-but-empty `VAR=`, which several readers
    would treat as "present and empty" rather than "unset"."""
    if not ENV_PATH.exists():
        return
    unset_key(ENV_PATH, var)


def _ensure_log_handler() -> None:
    """FileHandler created on demand (not at import time): LOG_DIR lives in
    DATA_DIR, and the directory is only created by whoever actually writes
    to it. Reads LOG_DIR as a module attribute at call time — tests swap
    LOG_DIR (and the logger's handlers) via monkeypatch without touching the
    real one."""
    if not _logger.handlers:
        secure_mkdir(LOG_DIR)
        log_path = LOG_DIR / "griot.log"
        _handler = logging.FileHandler(log_path)
        os.chmod(log_path, 0o600)  # FileHandler creates with the default umask
        _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _logger.addHandler(_handler)


# A repository being indexed is untrusted input, and so is its git config:
# several keys name a PROGRAM git runs during a read. With log.showSignature
# set, `git log` runs gpg.program for every signed commit; core.fsmonitor and
# hooks are the same kind. Command-line config outranks the repository's, so
# each of those is overridden here rather than trusted. gpg.program itself is
# overridden too, not just the switch: a `%G` format placeholder verifies
# signatures whatever log.showSignature says. safe.bareRepository=explicit
# refuses a bare repository that was not named on purpose: one can sit in a
# project's tracked files and carry a config of its own.
_GIT_NEUTRAL_CONFIG = (
    "log.showSignature=false",
    "gpg.program=false",
    "gpg.ssh.program=false",
    "gpg.x509.program=false",
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "safe.bareRepository=explicit",
)

# What git needs from the environment to find itself and the user's own
# config. Everything else is dropped: the credentials griot loads into its
# environment have no business in a child process, and inherited GIT_*
# variables (GIT_DIR, when griot is started from a hook) would point git at a
# different repository than the one asked for.
_GIT_ENV_ALLOWED = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                    "XDG_CONFIG_HOME", "SYSTEMROOT", "USERPROFILE")


def run_git(repo_path, args: list[str], *, timeout: float, check: bool = True) -> subprocess.CompletedProcess:
    """The only place the package runs git. Reads `repo_path` with the
    repository's program-running config neutralized and without the user's
    credentials in the environment (see the two constants above).

    Output is decoded as UTF-8 with replacement: a commit written in a legacy
    encoding must cost one odd character, not the whole source."""
    argv = ["git", "--no-pager"]
    for setting in _GIT_NEUTRAL_CONFIG:
        argv += ["-c", setting]
    argv += ["-C", str(repo_path), *args]
    env = {name: os.environ[name] for name in _GIT_ENV_ALLOWED if name in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Reading must never turn into fetching. A repository that claims to be
    # a partial clone makes git fetch a missing object on demand, from a
    # "remote" and over a transport the repository picks: an `ext::` URL is
    # a command line, core.sshCommand is the program run for ssh. The first
    # variable stops the fetch (git 2.44+). The second allows NO transport
    # protocol at all and, unlike `-c protocol.allow=never`, cannot be
    # outranked by a `protocol.<name>.allow=always` in the repository's config.
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = ""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check, timeout=timeout, env=env)


def git_format(*placeholders: str, nul: str = "%x00") -> str:
    """A git format string that ends every field with NUL. `git log` writes
    it `%x00`, `git for-each-ref` writes it `%00`."""
    return "".join(placeholder + nul for placeholder in placeholders)


_GIT_HASH = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def git_records(output: str, fields: int) -> list[list[str]]:
    """What git printed for a git_format() of `fields` fields, as one list
    per record.

    NUL is the one character git refuses in a commit message, a tag message
    and a name. The sources used to separate fields and records with the
    control characters 0x1f and 0x1e, which git accepts in all three: one
    commit holding either made a split come out with the wrong number of
    fields, a ValueError that ended the commits source on every run.

    The line break git puts between two records lands at the start of the
    next record's first field and is removed. Output that does not divide
    into whole records (git cut off, or a NUL where none can be) leaves the
    incomplete tail out and says so in the log: a source never ends in a
    traceback over what a repository holds."""
    parts = output.split("\x00")
    parts.pop()  # after the last terminator: nothing, or the break before a record that never came
    whole = len(parts) - len(parts) % fields
    if whole != len(parts):
        log_and_print(f"Warning: what git printed does not divide into records of {fields} fields; the last "
                      f"{len(parts) - whole} field(s) were left out.", level="warning", echo=False)
    records = []
    for start in range(0, whole, fields):
        record = parts[start:start + fields]
        record[0] = record[0].lstrip("\n")
        records.append(record)
    return records


def is_git_hash(value: str) -> bool:
    """A full object name, SHA-1 or SHA-256: what marks a record as read
    from where it begins."""
    return bool(_GIT_HASH.fullmatch(value))


def is_interactive() -> bool:
    """True when a person can be asked: both ends are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str, *, yes: bool | None = False) -> int:
    """Asks a yes/no question before a CLI command destroys or widens
    something. Returns 0 to proceed, otherwise the exit code the command
    should end with: 1 when the person did not say yes, 2 when nobody could
    be asked, 130 on Ctrl-C.

    Only a typed "y" or "yes" proceeds; the default is no. `yes=True` is the
    command's own --yes flag and skips the question. `yes=None` means the
    command has no such flag on purpose: an operation that widens what may be
    indexed, or destroys data for good, is answered by a person at a terminal,
    so the plain command run from an agent's shell (which has no terminal)
    changes nothing. That is a guard against the easy path, not a boundary: a
    process that can run arbitrary commands can also fake a terminal or edit
    the files directly."""
    if yes:
        return 0
    if not is_interactive():
        how = ("Run it in an interactive terminal: there is no flag that answers for you." if yes is None
               else "Run it in an interactive terminal, or pass --yes.")
        print(f"Error: this needs a confirmation and there is no terminal to ask on. "
              f"Nothing was changed. {how}", file=sys.stderr)
        return 2
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        answer = ""
        print(file=sys.stderr)
    except KeyboardInterrupt:
        print("\nAborted, nothing changed.", file=sys.stderr)
        return 130
    if answer.strip().lower() in ("y", "yes"):
        return 0
    print("Aborted, nothing changed.", file=sys.stderr)
    return 1


# Where log_and_print() echoes. None is the terminal's stdout. The MCP server
# points it at stderr for its whole life: there, stdout IS the JSON-RPC
# stream, and one stray line in it corrupts what the client is reading.
_echo_stream = None


def log_and_print(msg: str, level: str = "info", echo: bool = True) -> None:
    from tqdm import tqdm as _tqdm
    _ensure_log_handler()
    # [shared config path] echo=False keeps the persistent log entry (audit trail
    # intact) while omitting stdout — needed by `griot quality-check
    # --json`, whose stdout contract is "JSON payload only" (jobs.py's
    # subprocess caller parses it as such; any stray line would break that).
    if echo:
        _tqdm.write(msg, file=_echo_stream)
    getattr(_logger, level)(msg)


def log_run_summary(**fields) -> None:
    """Records a structured record per indexer run — script, profile, repo,
    indexed/skipped/failed counts, duration, estimated spend for the day.
    Queryable history afterward (cost + execution), without depending on
    the terminal it ran in. Stored in logs/logs.db (SQLite, see logdb.py —
    replaced the old logs/runs.jsonl in 2026-08-21: that file had to be
    read and re-parsed in full on every griot stats call, with
    no rotation, so the cost grew unboundedly with history)."""
    from datetime import datetime, timezone
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "profile": ACTIVE_PROFILE_NAME,
        "collection": COLLECTION_NAME,
        **fields,
    }
    secure_mkdir(LOG_DIR)
    logdb.write_run(LOG_DIR, record)


def log_questions_enabled() -> bool:
    """[finding M1 from the audit, 2026-08-19] queries.jsonl stores the full
    question forever — GRIOT_LOG_QUESTIONS=false allows turning off just the
    question text without losing the metrics (latency, sources, spend).
    Read at call time (not at import time) so it can be controlled
    per-invocation."""
    return os.getenv("GRIOT_LOG_QUESTIONS", "true").strip().lower() not in FALSE_WORDS


def _clean_project(name: str) -> str | None:
    """A project name fit for a log line: printable characters only (a directory
    name can hold a newline or an escape), at most 100 of them, None if nothing is left."""
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    return cleaned[:100] or None


def current_project() -> str | None:
    """The project this process is serving, for the usage logs: GRIOT_PROJECT if set,
    else the folder named by CLAUDE_PROJECT_DIR, else the working directory's name.
    Only the folder NAME is used, never the path (GRIOT_PROJECT is the exception: it
    is logged exactly as you set it). None when it cannot be worked out,
    including when the directory is the home directory: running the CLI from ~ would
    otherwise write the account's user name into the log.

    Without this the logs cannot say who used griot: usage per project could only be
    guessed by matching timestamps against session transcripts that get deleted."""
    explicit = os.getenv("GRIOT_PROJECT", "").strip()
    if explicit:
        return _clean_project(explicit)
    directory = os.getenv("CLAUDE_PROJECT_DIR", "").strip()
    if not directory:
        try:
            directory = os.getcwd()
        except OSError:  # the working directory was deleted under us
            return None
    path = Path(directory)
    home = Path.home()
    try:
        if os.path.samefile(path, home):  # identity: holds for symlinks and for case-insensitive filesystems
            return None
    except OSError:  # one of them does not exist
        try:
            if path.resolve() == home.resolve():
                return None
        except (OSError, RuntimeError):
            pass
    return _clean_project(path.name)


def log_query(**fields) -> None:
    """Same idea as log_run_summary(), but for ask.py queries (not
    indexing) — a separate table (logs/logs.db's `queries` table) so as
    not to mix the two record types in the same analysis."""
    from datetime import datetime, timezone
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "profile": ACTIVE_PROFILE_NAME,
        "collection": COLLECTION_NAME,
        "project": current_project(),
        **fields,
    }
    secure_mkdir(LOG_DIR)
    logdb.write_query(LOG_DIR, record)


def _check_env_file_permissions(env_path: Path) -> None:
    """[review] <config_dir>/.env holds secrets (GEMINI_TOKEN,
    GITLAB_PERSONAL_ACCESS_TOKEN) — if it exists with a permission more open
    than 0600 (readable by group/others), warns and tries to fix it with
    chmod 0600. If the chmod fails (e.g. file owned by someone else), only
    the warning remains."""
    if not env_path.is_file():
        return
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        log_and_print(
            f"Warning: {env_path} has permission {oct(mode)} (more open than 0600) "
            f"and holds secrets — fixing to 0600.",
            level="warning",
        )
        try:
            env_path.chmod(0o600)
        except OSError as e:
            log_and_print(f"Warning: could not fix the permission of {env_path}: {e}", level="warning")


# griot reads only its own <config_dir>/.env, never another project's. If it
# exists, loads from there; otherwise, the variables (GEMINI_TOKEN,
# GITLAB_PERSONAL_ACCESS_TOKEN) already come from the shell (~/.bashrc /
# ~/.zshrc).
_check_env_file_permissions(ENV_PATH)
# What the environment itself said, before the file is read into it. A
# process that outlives edits to the file (the MCP server) has no other way
# to tell a variable that was exported from one the file gave it at start.
ENVIRONMENT_BEFORE_ENV_FILE = {name: value for name, value in os.environ.items() if name.startswith("GRIOT_")}
load_dotenv(dotenv_path=ENV_PATH)

# Rename RAG_* -> GRIOT_*: values under the
# old names are IGNORED — better to warn loudly than to let, say, a
# configured spend ceiling under the old name silently fall back to the
# default.
_LEGACY_ENV_RENAMES = {
    "RAG_EMBED_PROFILE": "GRIOT_EMBED_PROFILE",
    "RAG_SPEND_CEILING_USD": "GRIOT_SPEND_CEILING_USD",
    "RAG_SPEND_VELOCITY_CEILING_USD": "GRIOT_SPEND_VELOCITY_CEILING_USD",
    "RAG_CHAT_MODEL": "GRIOT_CHAT_MODEL",
    "RAG_CHAT_PRICE_PER_1M_TOKENS": "GRIOT_CHAT_PRICE_PER_1M_TOKENS",
    "RAG_MAX_CONSECUTIVE_FAILED_BATCHES": "GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES",
}


def warn_legacy_env_vars() -> None:
    for old, new in _LEGACY_ENV_RENAMES.items():
        if os.getenv(old) is not None:
            log_and_print(
                f"Warning: the env var {old} was renamed and is IGNORED — use {new}.",
                level="warning",
            )


warn_legacy_env_vars()


def load_repos() -> list[str]:
    """List of repo paths to index — CONFIG_DIR/repos.json (user config,
    the design notes). Centralized here so the five indexers don't
    each resolve the path their own way."""
    with open(REPOS_JSON_PATH, "r") as f:
        return json.load(f)


# griot doesn't depend on LiteLLM/Docker at any point — indexing and
# search are local (embedded Qdrant + fastembed), and ask.py's chat call
# goes straight to Google's API (GEMINI_TOKEN), no proxy in between.
# GEMINI_TOKEN is only required at the moment of use (the "gemini" embedding
# profile, or the chat answer) — it doesn't fail here at import time, so as
# not to break users of local-only profiles (jina-code/bge-m3), which need
# no credential at all.
GEMINI_TOKEN = os.getenv("GEMINI_TOKEN")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

QDRANT_PATH = DATA_DIR / "qdrant_data"

# Migration to Qdrant Edge (that decision— validated in an
# isolated prototype before the migration): EdgeShard is a shard per
# DIRECTORY (a real in-process Qdrant engine), unlike the old QdrantClient,
# which multiplexed several collections into a single path. Each collection
# (one per embedding profile, see COLLECTION_NAME below) gets its own
# subdirectory inside QDRANT_PATH.
_COLLECTION_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def _collection_path(collection: str) -> Path:
    """Where a collection lives. `collection` is a NAME, and it is checked
    here because this is where a name becomes a path: joined as it came, an
    absolute path replaces the data directory and `..` walks out of it, and
    the name can come from an agent (griot_index_status). Every reader and
    writer of a collection goes through this function."""
    if not isinstance(collection, str) or not _COLLECTION_NAME.fullmatch(collection):
        raise ValueError(
            f"Not a collection name: {printable(repr(collection))[:80]}. A collection is named after an "
            f"embedding profile ({collection_name_for('<profile>')}); it is never a path.")
    return QDRANT_PATH / collection


# Marker that a subdirectory already contains an Edge shard — always
# written on create(), so its presence distinguishes "create from scratch"
# from "load existing" without trial-and-error (EdgeShard.create() fails
# explicitly if the path already has segment data).
_EDGE_CONFIG_MARKER = "edge_config.json"

# Same HnswIndexConfig validated in docs/benchmarks/bench_edge.py and
# replicated in the migration prototype (m=16, ef_construct=100) — a
# reasonable starting point; full_scan_threshold high enough not to affect
# small corpora (tests, a fresh install), which fall back to brute force,
# which is correct by definition.
_HNSW_CONFIG = qe.HnswIndexConfig(m=16, ef_construct=100, full_scan_threshold=10000)

# Available embedding profiles. Switching profile = switching collection
# (name derived from the profile) — vectors from different models aren't
# comparable with each other, so each profile is isolated by design, with
# no risk of mixing incompatible vector spaces in the same search.
#
# "direct": embeds by calling the Google API directly (batchEmbedContents),
#           with GEMINI_TOKEN — no LiteLLM, no Docker, API cost.
# "local":  embeds locally via fastembed/ONNX (slow on CPU, but offline —
#           no network dependency at all for indexing or searching).
# "price_per_1m_tokens" (USD) only exists on paid profiles — feeds the
# local circuit breaker below. Local/no-cost profiles simply omit the field.
# "ram_tier"/"onnx_size_mb"/"rss_estimate_mb" (only on "local" profiles):
# RAM metadata from the design notes, used by `griot profiles
# list` (section 9.3) to group by tier and flag what comfortably fits the
# machine detected via psutil. rss_estimate_mb is an engineering ESTIMATE
# (1.3-1.8x factor over onnx_size_mb — memory arena + activation tensors +
# tokenizer), not a measurement — document it as such, never as a
# benchmark.
EMBED_PROFILES = {
    "gemini": {
        "backend": "direct",
        "model": "gemini-embedding-2",
        "dim": 768,
        "price_per_1m_tokens": 0.20,  # confirmed 2026-08 (same price as before — same model, only the call path changed)
    },
    "openai-small": {
        "backend": "direct",
        "request_style": "openai_compatible",  # generic adapter — see _embed_texts_openai_compatible()
        "endpoint_url": "https://api.openai.com/v1/embeddings",
        "model": "text-embedding-3-small",
        "dim": 1536,
        "price_per_1m_tokens": 0.02,
        "index_batch_size": 128,  # texts per request: see batch_sizes()
        "request_token_limit": 300_000,  # what the endpoint takes in one request
        "api_key_env": "GRIOT_OPENAI_API_KEY",  # griot's own name, never the generic "OPENAI_API_KEY" — a scoped key avoids any other local tool inheriting it
    },
    "bge-small": {
        "backend": "local", "model": "BAAI/bge-small-en-v1.5", "dim": 384,
        "ram_tier": "light", "onnx_size_mb": 67, "rss_estimate_mb": (90, 120),
    },
    "nomic-q": {
        "backend": "local", "model": "nomic-ai/nomic-embed-text-v1.5-Q", "dim": 768,
        "ram_tier": "light", "onnx_size_mb": 130, "rss_estimate_mb": (170, 235),
    },
    "jina-code": {   # default — unchanged, just gains ram_tier (it was implicitly "light")
        "backend": "local", "model": "jinaai/jina-embeddings-v2-base-code", "dim": 768,
        "ram_tier": "medium", "onnx_size_mb": 640, "rss_estimate_mb": (830, 1150),
    },
    "mxbai-large": {
        "backend": "local", "model": "mixedbread-ai/mxbai-embed-large-v1", "dim": 1024,
        "ram_tier": "medium", "onnx_size_mb": 640, "rss_estimate_mb": (830, 1150),
    },
    "bge-m3": {   # unchanged, only ram_tier/onnx_size_mb corrected (was ~1GB, is 2.27GB confirmed in fastembed#602)
        "backend": "local", "model": "BAAI/bge-m3", "dim": 1024,
        "ram_tier": "heavy", "onnx_size_mb": 2270, "rss_estimate_mb": (2950, 4100),
    },
    "bge-large-en": {
        "backend": "local", "model": "BAAI/bge-large-en-v1.5", "dim": 1024,
        "ram_tier": "heavy", "onnx_size_mb": 1200, "rss_estimate_mb": (1560, 2160),
    },
}


def _resolve_profile(name: str) -> dict:
    """Validates a profile name against EMBED_PROFILES — extracted from the
    import-time resolution so it can be reused by set_active_profile()
    (override via the CLI's --profile, the design notes) without
    duplicating the error message."""
    if name not in EMBED_PROFILES:
        raise ValueError(
            f"Unknown embedding profile {name!r}. "
            f"Options: {', '.join(EMBED_PROFILES)} (or add a new one to EMBED_PROFILES, common.py)"
        )
    return EMBED_PROFILES[name]


ACTIVE_PROFILE_NAME = os.getenv("GRIOT_EMBED_PROFILE", "jina-code")
if ACTIVE_PROFILE_NAME not in EMBED_PROFILES:
    # A setting griot cannot start with, like a ceiling that is not a number:
    # one line that names it and says how to fix it, not a traceback.
    raise UnknownEmbedProfile(
        f"Unknown GRIOT_EMBED_PROFILE={ACTIVE_PROFILE_NAME!r}. Options: {', '.join(EMBED_PROFILES)}. "
        f"If the variable is exported in this environment, unset it or fix it there (the environment wins "
        f"over the file); otherwise pick one with `griot profiles use <name>`, which writes {ENV_PATH}.",
        ACTIVE_PROFILE_NAME, list(EMBED_PROFILES),
    )
ACTIVE_PROFILE = EMBED_PROFILES[ACTIVE_PROFILE_NAME]


def collection_name_for(profile_name: str) -> str:
    """The "codebase__{profile}" naming pattern, formalized — used to only
    exist inline for the active profile (COLLECTION_NAME below). Callers that
    report status per profile — `griot profiles list`, griot_profiles_list —
    need the collection name for every embed profile, not just the active
    one."""
    return f"codebase__{profile_name}"


COLLECTION_NAME = collection_name_for(ACTIVE_PROFILE_NAME)
EMBED_DIM = ACTIVE_PROFILE["dim"]
# How many documents one round of index_documents() takes, and how many texts
# a local model is handed at once. Per profile, because the right numbers
# have nothing in common:
#
# - An API profile sends one round as one request, so the round is as large
#   as the API takes safely. Gemini's batchEmbedContents accepts at most 100
#   requests per call and 50 is what it was tuned for. An OpenAI-compatible
#   endpoint accepts 2048 inputs and 300,000 tokens per request: 128 chunks
#   of code or English stay well under that, and a round of text that costs
#   more than a token per character is cut into requests by its bytes (see
#   _requests_within_the_limit). With one size for both, an
#   OpenAI-compatible run made two and a half times the requests it needed.
# - A local profile takes rounds of 128 (a large round sorts more texts by
#   length together, so each batch pads less) and hands the model 8 texts
#   at a time. Measured on chunks of this repository, as cut for indexing
#   (up to 1500 characters): with bge-small, 4.2 chunks/s and 0.6 GB at 8
#   against 2.9 chunks/s and 3.0 GB at 128; with jina-code, the default,
#   2.0 chunks/s and 1.4 GB at 8, 0.35 chunks/s and 2.8 GB at 64, and no
#   answer in fifteen minutes at 128. Throughput is flat from 4 to 16 and
#   falls after that; memory grows with every step. The 128 that stood here
#   came from a rule of thumb about ONNX batches that does not hold for
#   texts this long.
_DEFAULT_INDEX_BATCH_SIZE = {"local": 128, "direct": 50}
_DEFAULT_EMBED_BATCH_SIZE = 8


def batch_sizes(profile: dict) -> dict:
    """{"index": documents per round of index_documents(), "embed": texts a
    local model is handed at once} for an embedding profile. A profile says
    its own (`index_batch_size`, `embed_batch_size`) where it differs."""
    return {
        "index": profile.get("index_batch_size", _DEFAULT_INDEX_BATCH_SIZE.get(profile["backend"], 50)),
        "embed": profile.get("embed_batch_size", _DEFAULT_EMBED_BATCH_SIZE),
    }


INDEX_BATCH_SIZE = batch_sizes(ACTIVE_PROFILE)["index"]
# Passed to fastembed's .embed() (the "local" branch of embed_texts).
EMBED_CALL_BATCH_SIZE = batch_sizes(ACTIVE_PROFILE)["embed"]

# ask.py's chat model — a direct call to generateContent (Google's API),
# no LiteLLM. "-latest" (gemini-flash-latest) was tried first but rejected
# thinkingConfig.thinkingBudget=0 on that version; gemini-2.5-flash accepts
# explicitly turning off "thinking", which is what we want here — RAG is
# simple synthesis over already-retrieved context, it doesn't need extended
# reasoning (tested: 213 "thinking" tokens billed on a trivial question
# without thinkingBudget=0, ~0 with it).
CHAT_MODEL = os.getenv("GRIOT_CHAT_MODEL", "gemini-2.5-flash")
# The API doesn't expose a separate prompt/completion price in the
# response — we use the more expensive ceiling (output tokens) as a
# conservative estimate, so as to never UNDERestimate real spend against the
# circuit breaker (the opposite would be dangerous). Adjust via env if you
# know the exact contracted price.
def _where_to_fix() -> str:
    """The second half of every "this setting is not valid" error."""
    return (f"It is set in the environment or in {ENV_PATH}: `griot config set <name> <value>` writes the file "
            f"and `griot config unset <name>` removes the line, back to the default (<name> can be the "
            f"variable itself; `griot config --help` lists the short names).")


def _amount_env(name: str, default: str | None) -> float | None:
    """An amount of money from the environment: a finite number, zero or
    more, or None when the variable is not set and there is no default.
    Every ceiling and every price is read through here, because each of the
    other values quietly turns the circuit breaker off: `spend >= nan` is
    False for every spend, an infinite ceiling is never reached, and a
    negative price makes each paid call record as free."""
    raw = os.getenv(name)
    if raw is None:
        raw = default
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ConfigurationError(f"{name} must be a finite number, zero or more (got {raw!r}). {_where_to_fix()}")
    return value


def _count_env(name: str, default: str) -> int:
    """A whole number from the environment, reported like the amounts are:
    one line that names the variable."""
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a whole number (got {raw!r}). {_where_to_fix()}") from None


CHAT_PRICE_PER_1M_TOKENS = _amount_env("GRIOT_CHAT_PRICE_PER_1M_TOKENS", "2.50")


def _optional_float_env(name: str) -> float | None:
    """None when the env var isn't set — used by chat profiles without a
    confirmed price (openai/deepseek, 2026-08-13): griot never guesses an
    unverified price (same discipline as an earlier decision/24, about
    not including an unconfirmed embedding price). chat_completion() refuses
    to call a profile with price_per_1m_tokens=None, forcing the user to
    confirm before any real spend."""
    return _amount_env(name, None)


# Chat profiles (2026-08-13) — generalizes what used to be Gemini-only to
# any OpenAI-compatible provider (the same generic adapter as the
# openai-small embedding profile, reused here:
# _openai_compatible_post_with_retry()). 'gemini' is the only
# "gemini_native" backend — the other three (openai/deepseek/groq) document
# their own APIs as compatible with OpenAI's chat completions format, so a
# single adapter covers all three.
#
# Prices: openai/deepseek do NOT have a hardcoded price_per_1m_tokens (None
# by default) — model names and prices for these providers change too fast
# to guess a number here with confidence (same reason
# jina-code-embeddings/codestral-embed were left out of section 9 of the
# plan without a confirmed source). groq is the exception: it has a
# well-known free tier (rate-limited, but genuinely $0) — it's not a
# guess, it's a reasonably stable fact; still, confirm current limits on
# their console before relying on this for heavy use.
CHAT_PROFILES = {
    "gemini": {
        "backend": "gemini_native",
        "model": CHAT_MODEL,
        "price_per_1m_tokens": CHAT_PRICE_PER_1M_TOKENS,
    },
    "openai": {
        "backend": "openai_compatible_chat",
        "endpoint_url": "https://api.openai.com/v1/chat/completions",
        "model": os.getenv("GRIOT_OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        "api_key_env": "GRIOT_OPENAI_API_KEY",
        "price_per_1m_tokens": _optional_float_env("GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS"),
    },
    "deepseek": {
        "backend": "openai_compatible_chat",
        "endpoint_url": "https://api.deepseek.com/v1/chat/completions",
        "model": os.getenv("GRIOT_DEEPSEEK_CHAT_MODEL", "deepseek-chat"),
        "api_key_env": "GRIOT_DEEPSEEK_API_KEY",
        "price_per_1m_tokens": _optional_float_env("GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS"),
    },
    "groq": {
        "backend": "openai_compatible_chat",
        "endpoint_url": "https://api.groq.com/openai/v1/chat/completions",
        "model": os.getenv("GRIOT_GROQ_CHAT_MODEL", "llama-3.3-70b-versatile"),
        "api_key_env": "GRIOT_GROQ_API_KEY",
        "price_per_1m_tokens": _optional_float_env("GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS") or 0.0,
    },
}

ACTIVE_CHAT_PROFILE_NAME = os.getenv("GRIOT_CHAT_PROFILE", "gemini")
if ACTIVE_CHAT_PROFILE_NAME not in CHAT_PROFILES:
    raise ConfigurationError(
        f"Unknown GRIOT_CHAT_PROFILE={ACTIVE_CHAT_PROFILE_NAME!r}. Options: {', '.join(CHAT_PROFILES)}. "
        f"{_where_to_fix()}"
    )
ACTIVE_CHAT_PROFILE = CHAT_PROFILES[ACTIVE_CHAT_PROFILE_NAME]


def credential_env_vars() -> dict[str, str]:
    """[security review] provider -> env var name, for every external
    credential griot manages (paid embedding/chat providers + the 5 code
    platform tokens). Single source of truth, deduplicated between
    EMBED_PROFILES and CHAT_PROFILES (e.g. openai's GRIOT_OPENAI_API_KEY
    covers both the openai-small embedding profile and the openai chat
    profile — same real account/key). 'gemini' is a special case in BOTH
    profile dicts — it has no api_key_env field, it uses the historical
    GEMINI_TOKEN var (no GRIOT_ prefix). Lives here (not in auth.py, which
    used to own this exact logic as `_providers()`) so common.py's own
    keychain-injection code below can use it without auth.py importing
    common.py the other way around and creating a cycle — auth._providers()
    is now a one-line wrapper around this."""
    providers: dict[str, str] = {
        "gitlab": "GITLAB_PERSONAL_ACCESS_TOKEN",
        "github": "GITHUB_TOKEN",
        "bitbucket": "BITBUCKET_ACCESS_TOKEN",
        "azure_devops": "AZURE_DEVOPS_PAT",
        "gitea": "GITEA_TOKEN",
    }
    for name, profile in {**EMBED_PROFILES, **CHAT_PROFILES}.items():
        api_key_env = credential_env_for_profile(name, profile)
        if api_key_env is None:
            continue
        if profile.get("api_key_env") is None:
            # The special-cased profile: its label IS the profile name, since
            # GEMINI_TOKEN carries no GRIOT_ prefix for the rule below to strip.
            providers[name] = api_key_env
            continue
        label = api_key_env.removeprefix("GRIOT_").removesuffix("_API_KEY").removesuffix("_EMBED").lower()
        providers[label] = api_key_env
    return providers


def credential_env_for_profile(name: str, profile: dict) -> str | None:
    """The env var holding a profile's credential, or None when it needs
    none (a local model).

    Exists so the 'gemini' special case lives in exactly ONE place. That
    profile predates the GRIOT_ prefix and carries no api_key_env field, so
    `profile.get("api_key_env")` reports griot's flagship PAID profile as
    free — a real defect found in griot_profiles_list, pointing in the worst
    direction: it invites a switch to a profile that bills per query."""
    api_key_env = profile.get("api_key_env")
    if api_key_env:
        return api_key_env
    return "GEMINI_TOKEN" if name == "gemini" else None


# [security review] OS keychain storage for credentials — additive,
# best-effort layer on top of the existing <config_dir>/.env file. Real
# finding from a live security review: .env sits in plaintext, protected
# only by file permissions (0600) + whatever full-disk encryption the OS
# provides — a leaked/misconfigured backup, or another local process/user
# reading the file directly, gets the raw value. Unlike the RAG content
# (a copy of what's already unencrypted in the user's own git checkouts —
# see SECURITY.md's "Encryption at rest" position, deliberately NOT
# revisited by this change), a credential is NOT redundant with anything
# else on disk: a leaked API key is a standalone loss. The `keyring`
# package (pyproject.toml's optional `keychain` extra — NOT a core
# dependency) is the same pattern real CLI tools use (gh, docker
# credential helpers, aws-cli, 1Password CLI). `import keyring` happens
# LAZILY inside each wrapper below (never at module import time) so
# griot's core CLI/MCP/UI behavior never depends on it being installed —
# every wrapper catches ANY exception (ImportError when not installed,
# keyring.errors.NoKeyringError when no backend is reachable — e.g.
# headless Linux without a Secret Service provider, or a container — and
# any backend-specific failure) and degrades to "unavailable," never
# raises. Callers (auth.py) fall back to the existing file-based storage
# whenever these return None/False.
_KEYCHAIN_SERVICE = "griot"


def _keychain_get(env_var: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(_KEYCHAIN_SERVICE, env_var)
    except Exception:
        return None


def _keychain_set(env_var: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_KEYCHAIN_SERVICE, env_var, value)
        return True
    except Exception:
        return False


def _keychain_delete(env_var: str) -> bool:
    try:
        import keyring
        keyring.delete_password(_KEYCHAIN_SERVICE, env_var)
        return True
    except Exception:
        return False


def _inject_keychain_credentials() -> None:
    """Best-effort: for every known credential env var NOT already present
    in os.environ (a shell export or .env value already won — same
    override=False precedence load_dotenv() above already applies),
    checks the OS keychain and injects it into os.environ if found. Called
    once below, at import time, right after this function and
    credential_env_vars() exist (EMBED_PROFILES/CHAT_PROFILES must already
    be defined) — keeps every existing os.getenv(...) call site across the
    whole codebase working unchanged regardless of which backend a given
    credential is actually stored in, since almost all of them read
    lazily at call time, well after this has already run.

    GEMINI_TOKEN (common.py's module-level constant above, line ~493) is
    the ONE exception — it's resolved as a plain module global BEFORE
    EMBED_PROFILES/CHAT_PROFILES even exist, so os.environ injection alone
    wouldn't reach it. Patched explicitly via `global` here."""
    global GEMINI_TOKEN
    for env_var in credential_env_vars().values():
        if os.environ.get(env_var):
            continue
        value = _keychain_get(env_var)
        if value:
            os.environ[env_var] = value
            if env_var == "GEMINI_TOKEN":
                GEMINI_TOKEN = value


_inject_keychain_credentials()


def _require_gemini_token() -> str:
    if not GEMINI_TOKEN:
        raise ValueError(
            f"GEMINI_TOKEN not found in the environment — export it in the shell (e.g. ~/.bashrc) "
            f"or place it in {ENV_PATH}. Required for the 'gemini' embedding profile "
            f"and for ask.py's chat answer (direct call to Google's API)."
        )
    return GEMINI_TOKEN

# Local spend circuit breaker — direct calls to Gemini (embedding and chat)
# don't go through any external platform budget, so this is the ONLY
# spend ceiling that exists for these two paths. State persists on disk
# (gitignored) and resets daily — covers both a single expensive run and
# several small runs on the same day adding up to a lot.
SPEND_STATE_PATH = DATA_DIR / ".spend_state.json"
SPEND_CEILING_USD = _amount_env("GRIOT_SPEND_CEILING_USD", "3.0")

# VELOCITY ceiling (spend in the last 5min), separate from the daily
# ceiling. Our code is sequential by design (one batch, wait for the
# response, next one) — spend piling up too fast for that is the most
# direct signal that something is wrong: two processes running at once, a
# retry without real backoff, etc. (sustained bursts of hundreds of
# calls/min from a SINGLE process are incompatible with one call at a time
# waiting for the previous response).
SPEND_VELOCITY_WINDOW_SECONDS = 300
SPEND_VELOCITY_CEILING_USD = _amount_env("GRIOT_SPEND_VELOCITY_CEILING_USD", "1.0")

# Consecutive-failure detector — 5 batches in a row failing completely
# isn't "bad luck", it's a sign that something is systemically broken
# (external API down, invalid credential, etc.) — better to stop early and
# loudly than to spend hours producing only empty batches.
MAX_CONSECUTIVE_FAILED_BATCHES = _count_env("GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "5")

# Single-process lock — besides Qdrant's native lock (which only blocks
# access to the same collection), this one fails fast with a clear message
# as soon as two indexers try to run at the same time in this directory,
# instead of letting both worsen spend/CPU until one of them stumbles into
# an obscure error.
# (renamed from .rag-indexer.lock along with the rest of the rename to griot)
LOCK_PATH = DATA_DIR / ".griot.lock"

# Migration from the old layout (the design notes:
# warn ONLY, never move). The old layout resolved everything relative to
# the code's folder — which, in a source checkout, is parents[2] from here
# (src/griot/common.py -> root). In a site-packages install that path
# doesn't contain any of these files, so the checks simply find nothing.
_LEGACY_ROOT = Path(__file__).resolve().parents[2]


def warn_legacy_layout(legacy_root: Path | None = None) -> None:
    """Warns (and ONLY warns) if files from the old layout are
    still at the root of the code checkout, pointing to the new location."""
    root = _LEGACY_ROOT if legacy_root is None else legacy_root
    if not root.is_dir():
        return
    legacy_items = {
        "repos.json": CONFIG_DIR,
        "quality_golden_set.json": CONFIG_DIR,
        ".env": CONFIG_DIR,
        "qdrant_data": DATA_DIR,
        "logs": DATA_DIR,
    }
    for name, new_dir in legacy_items.items():
        if (root / name).exists():
            log_and_print(
                f"Warning: {root / name} is in the old layout and is IGNORED — the new location is "
                f"{new_dir / name} (move it manually; griot never moves it on its own).",
                level="warning",
            )


def migrate_legacy_spend_state(legacy_root: Path | None = None) -> None:
    """EXCEPTION to the 'warn only' policy: .spend_state.json
    migrates AUTOMATICALLY (copy, with a log entry) if it exists at the old
    location and doesn't yet exist at the new one. It's ephemeral numeric
    state, not user config — treating it as a 'nonexistent file' would
    silently reset the day's spend counter, which would defeat the whole
    point of the circuit breaker. Never overwrites state that already
    exists at the new location."""
    root = _LEGACY_ROOT if legacy_root is None else legacy_root
    old = root / ".spend_state.json"
    if old.is_file() and not SPEND_STATE_PATH.exists():
        secure_mkdir(SPEND_STATE_PATH.parent)
        shutil.copy2(old, SPEND_STATE_PATH)
        log_and_print(
            f"Automatically migrated {old} -> {SPEND_STATE_PATH} "
            f"(the day's spend counter is preserved)."
        )


warn_legacy_layout()
migrate_legacy_spend_state()

_embed_model = None  # a fastembed.TextEmbedding once a local profile is first used
_client: "qe.EdgeShard | None" = None
_client_last_used_at: float | None = None

# [user decision, 2026-08-20; default flipped to 'multi' 2026-09-29] concurrency
# mode for the active collection's handle. 'single' (opt-in) is the
# traditional behavior — memoizes forever, zero overhead, but a second REAL
# griot mcp SESSION (not a subagent/workflow — those reuse the session's MCP
# connection, see the decision) on the same profile hard-fails with a raw
# error. 'multi'
# retries-with-backoff when reopening, and drops the handle once it has gone
# unused for GRIOT_MCP_IDLE_RELEASE_SECONDS, at a reopen cost of ~94ms
# measured on this machine. Dropping it is done by a reaper thread of the MCP
# server (mcp_server._start_idle_reaper), which counts the calls in flight:
# get_client() never closes the handle it memoizes.
CONCURRENCY_MODE = os.getenv("GRIOT_MCP_CONCURRENCY_MODE", "multi")
if CONCURRENCY_MODE not in ("single", "multi"):
    raise ConfigurationError(
        f"Unknown GRIOT_MCP_CONCURRENCY_MODE={CONCURRENCY_MODE!r}. Use 'multi' (default) or 'single'. "
        f"{_where_to_fix()}"
    )
# Finite and zero or more, like an amount: an idle time of `nan` is never
# reached, and the index would be held for as long as the server runs.
IDLE_RELEASE_SECONDS = _amount_env("GRIOT_MCP_IDLE_RELEASE_SECONDS", "30")
# [tuned after empirical validation with a real collision] local DISK
# contention (another process with the WAL open), not network — a much
# smaller budget than the 10-20s used for paid-API retries, but needs to
# survive ONE full tool call from the other session (measured with a real
# embed+query: a 3s collision nearly exhausted a 3.1s budget, with no
# slack left for process import/startup overhead). A ~15s ceiling gives
# real margin.
_LOCK_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0, 4.0, 4.0)


def _read_lock() -> dict | None:
    """Reads and validates the current lock. Returns {"pid", "start_time",
    "label"} if the file exists and is in the new (JSON) format; None if
    the lock doesn't exist OR is corrupted/in the old format (raw PID, from
    before this change) — both cases are treated as "no valid lock" by
    callers, and it's up to the caller to decide whether that means "free
    to acquire" (acquire_lock) or "no indexing running" (get_index_status)."""
    if not LOCK_PATH.exists():
        return None
    try:
        info = json.loads(LOCK_PATH.read_text())
        return {"pid": int(info["pid"]), "start_time": float(info["start_time"]), "label": info.get("label")}
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _lock_owner_is_alive(info: dict) -> bool:
    """[review] A more robust "is the lock owner still alive" check than
    just "does the PID exist?" (vulnerable to PID recycling by the OS —
    after enough process churn, the PID of a dead griot process may have
    been recycled by an unrelated process, making the old check find
    "still running" forever). Also compares the PID's current
    create_time() against the start_time recorded at acquire time — only
    matches if it's the SAME process (~1s tolerance for float rounding
    between the two reads)."""
    try:
        proc = psutil.Process(info["pid"])
    except psutil.NoSuchProcess:
        return False  # dead PID — orphaned lock
    except psutil.AccessDenied:
        # a process with this PID exists but isn't ours — more likely a PID
        # recycled by another program after the original griot process died
        # than a real race (every process here runs as the same user).
        # Treat as orphaned instead of letting the exception propagate.
        log_and_print(f"Warning: lock PID {info['pid']} exists but doesn't belong to this user — treating it as an orphaned lock.", level="warning")
        return False
    if abs(proc.create_time() - info["start_time"]) > 1.0:
        log_and_print(
            f"Warning: lock PID {info['pid']} exists, but its start time doesn't match "
            f"the one recorded in the lock — likely PID recycling by the OS after the original "
            f"griot process died without clearing the lock. Treating it as an orphaned lock.",
            level="warning",
        )
        return False
    return True


def acquire_lock(label: str | None = None) -> None:
    """label: optional description of what's being indexed (e.g. a repo's
    path) — forward-looking for griot_index_repo (section 2.7 of the MCP
    plan); nobody passes this yet, it stays None in current calls to
    index_documents(). Recorded in the lock and returned by
    get_index_status() as the "path" field, to correlate a PID seen there
    with what it's doing."""
    secure_mkdir(LOCK_PATH.parent)  # DATA_DIR created on demand, 0700
    if LOCK_PATH.exists():
        info = _read_lock()
        if info is not None and _lock_owner_is_alive(info):
            raise RuntimeError(
                f"A griot process is already running (PID {info['pid']}). Wait for it to finish, or delete "
                f"{LOCK_PATH} if you're sure the process died without clearing the lock "
                f"(e.g. kill -9 / OOM)."
            )
        # lock without valid info (corrupted/old format) or orphaned (owner
        # dead or PID recycled) — proceed and overwrite it.
        LOCK_PATH.unlink(missing_ok=True)

    # O_CREAT|O_EXCL: atomic creation — if two processes get here at the
    # same time (both see "no lock" or "orphaned lock" before either one
    # writes), only one succeeds in creating the file; the other gets
    # FileExistsError instead of both thinking they own the lock (a TOCTOU
    # between the `exists()` check above and a plain `write_text()` would
    # not be atomic).
    try:
        # 0600 [review]: the lock carries pid/start_time/label (label is
        # designed to receive a repo path) — same M2 contract as the other files
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(
            f"Another process created {LOCK_PATH} at the same instant (a race between two "
            f"indexers starting together). Run it again."
        )
    pid = os.getpid()
    try:
        start_time = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        # defensive — shouldn't happen for our own running process, but a
        # missing start_time would break the comparison in
        # _lock_owner_is_alive forever (it would never match); time.time()
        # here is only slightly imprecise, not incorrect (it still detects
        # PID recycling from another process, just not from our
        # hypothetical failure case).
        start_time = time.time()
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"pid": pid, "start_time": start_time, "label": label}))

    import atexit
    atexit.register(release_lock)


def release_lock() -> None:
    try:
        if not LOCK_PATH.exists():
            return
        info = json.loads(LOCK_PATH.read_text())
        if info.get("pid") == os.getpid():
            LOCK_PATH.unlink()
    except Exception:
        # includes corrupted JSON/old format — not our recognizable lock
        # (or we're no longer the owner), leave it alone. Same defensive
        # stance as before: never let release_lock() (called via atexit)
        # propagate an exception.
        pass


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _ensure_spend_state_migrated() -> None:
    """One-time import of a pre-SQLite .spend_state.json (see
    logdb.migrate_legacy_spend_file). Called from every spend read/write
    rather than at import time so it costs nothing until a paid call
    actually happens; logdb tracks the marker, so it is idempotent."""
    secure_mkdir(LOG_DIR)
    logdb.migrate_legacy_spend_file(LOG_DIR, SPEND_STATE_PATH, _today())


def check_spend_ceiling() -> None:
    """Two independent checks, run before ANY paid call directly to Gemini
    (embedding or chat — no LiteLLM in between, this is the only budget
    that exists for these two paths):
    1. Daily ceiling (GRIOT_SPEND_CEILING_USD) — already existed.
    2. VELOCITY ceiling (GRIOT_SPEND_VELOCITY_CEILING_USD, spend in the last
       5min) — catches a concurrent burst well before the daily ceiling,
       which only kicks in after the full daily amount has already been
       burned.
    It's up to the caller to decide WHETHER a call is paid (local
    profiles/backends simply never call check_spend_ceiling or
    record_spend)."""
    _ensure_spend_state_migrated()
    spend_today = logdb.read_spend_today(LOG_DIR, _today())
    # Written as "not below" on purpose, here and for the velocity: a total
    # that is not a number compares False both ways, and must read as
    # reached, never as under.
    if not spend_today < SPEND_CEILING_USD:
        raise RuntimeError(
            f"Local circuit breaker: today's estimated spend (${spend_today:.4f}) has already reached "
            f"the ${SPEND_CEILING_USD:.2f} ceiling (GRIOT_SPEND_CEILING_USD). Stop and check the "
            f"reason before continuing — if this is expected, raise the ceiling explicitly."
        )

    now = time.time()
    velocity = logdb.read_spend_velocity(LOG_DIR, now - SPEND_VELOCITY_WINDOW_SECONDS)
    if not velocity < SPEND_VELOCITY_CEILING_USD:
        raise RuntimeError(
            f"Local circuit breaker: spend in the last {SPEND_VELOCITY_WINDOW_SECONDS // 60}min "
            f"(${velocity:.4f}) is way above what's expected for sequential use (ceiling: "
            f"${SPEND_VELOCITY_CEILING_USD:.2f}, GRIOT_SPEND_VELOCITY_CEILING_USD). This usually "
            f"indicates two processes running at once or a retry loop out of control "
            f"(a burst of simultaneous calls). Stop and check before continuing."
        )


def get_spend_today() -> float:
    """Estimated spend (USD) accumulated today by the local circuit breaker
    — 0.0 if only local (no-cost) calls have been made."""
    _ensure_spend_state_migrated()
    return logdb.read_spend_today(LOG_DIR, _today())


def record_spend(cost_usd: float) -> None:
    """Adds a real spend amount (USD, already computed by the caller —
    tokens x price of what was actually used) to the day's total and to
    the last-5min velocity window used by check_spend_ceiling(). Local
    (no-cost) calls never call this.

    [real bug, fixed 2026-08-21] The accumulation happens inside SQLite
    (logdb.write_spend), not here — see that function's docstring for why
    the previous read-modify-write over a JSON file silently lost most
    concurrent spend."""
    if not (math.isfinite(cost_usd) and cost_usd >= 0):
        # The same rule as for a price: finite, zero or more. Python reads
        # NaN and Infinity from a JSON body, and nothing stops a provider
        # from reporting a negative token count. There is no amount to add,
        # and adding nothing would let the paid calls go on uncounted (a
        # negative cost used to be dropped as if the call had been free):
        # stop the run.
        raise RuntimeError(
            f"Local circuit breaker: the cost of a paid call came out as {cost_usd!r}, which is not an "
            f"amount (the provider reported a token count that is not a number, or is negative). It was "
            f"not added to today's total; stopping here rather than go on spending uncounted.")
    if cost_usd == 0:
        return
    _ensure_spend_state_migrated()
    logdb.write_spend(LOG_DIR, _today(), cost_usd, time.time(), SPEND_VELOCITY_WINDOW_SECONDS)


# How many bytes of text are counted as one token when a provider did not say
# what it billed (see _billed_tokens). Lower than the 3.5 characters per token
# measured on an index (CHARS_PER_TOKEN_ESTIMATE), on purpose: this number
# feeds the spend ceiling, and a ceiling that is reached late is the one that
# hurts. Dense text (base64, minified code, numbers) and text outside ASCII
# cost more tokens than their size suggests, so this is still an estimate.
FALLBACK_BYTES_PER_TOKEN = 3.0
# Said once per process: see _billed_tokens().
_estimated_spend_was_said = False


def _billed_tokens(answer, path: tuple[str, str], texts: list[str]) -> float:
    """The number of tokens a paid call is counted by: what the provider
    reported at `path` in its `answer`, or, when it reported nothing usable,
    an estimate from the size of the `texts` that went to and came from it.

    The cost used to be the reported count times the price and nothing
    else, so an answer without a count (the field missing, null, zero, not
    a number) cost zero: nothing was added to the day's total, and the
    ceiling, the only budget these calls have, stayed open for as long as
    nothing told it otherwise. Counted in bytes, so that text which costs
    more than a token per character is not counted short, and never as
    nothing: a paid call is at least one token.

    A count that IS a number and makes no sense (negative, not finite) is
    passed on as it is: record_spend() stops the run on it, which is the
    right answer to a provider reporting nonsense."""
    global _estimated_spend_was_said
    reported = answer
    for key in path:
        reported = reported.get(key) if isinstance(reported, dict) else None
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported != 0:
        return reported
    size = sum(len(text.encode("utf-8")) for text in texts if isinstance(text, str))
    if not _estimated_spend_was_said:
        _estimated_spend_was_said = True
        log_and_print("Note: the API did not report how many tokens it billed for this call. Its cost is counted "
                      "from the size of the text instead, which is an estimate: the spend shown and the spend "
                      "ceiling go by it.", level="warning")
    return max(1, math.ceil(size / FALLBACK_BYTES_PER_TOKEN))


def _reply_if_any(answer, *path) -> str:
    """The text at `path` in a chat answer, or "" when it is not there: for
    sizing the call, which must not be what fails."""
    for step in path:
        try:
            answer = answer[step]
        except (KeyError, IndexError, TypeError):
            return ""
    return answer if isinstance(answer, str) else ""


def _text_embedding_class():
    """fastembed's model class, imported on first use. It pulls onnxruntime
    in with it and was most of what starting griot cost (about 0.45 s of
    0.7 s, measured), paid by every command and every MCP server start, the ones
    that never embed locally included: `griot stats`, `griot repos list`,
    any run on an API profile."""
    from fastembed import TextEmbedding
    return TextEmbedding


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        # limited threads: the machine runs several other heavy things in
        # parallel (Docker Desktop, corporate agents) — using all 12 cores
        # for inference already caused an OOM kill in a previous session.
        #
        # explicit cache_dir (real finding, 2026-08-13): without this,
        # fastembed defaults to tempfile.gettempdir()/fastembed_cache — a
        # model of hundreds of MB to a few GB can disappear if the OS
        # cleans up the temp directory, forcing a silent re-download.
        # Placing it inside DATA_DIR keeps the cache alongside the rest of
        # griot's data (qdrant_data/, logs/), persistent and under the
        # same user control.
        _embed_model = _text_embedding_class()(
            model_name=ACTIVE_PROFILE["model"], threads=6,
            cache_dir=str(DATA_DIR / "models"),
        )
    return _embed_model


class CollectionBusyError(RuntimeError):
    """The collection is open in another process. A normal condition on a
    machine running `griot mcp`, so callers can tell it apart from a broken
    collection and explain it instead of showing a traceback. A RuntimeError
    because get_client() used to raise a plain one here."""

    def __init__(self, collection: str, path: Path, message: str, *, in_this_process: bool = False):
        super().__init__(message)
        self.collection = collection
        self.path = path
        # True when it is another call of THIS process that is opening it (a
        # status read does not wait for that): "held by another process"
        # would then name a process that does not exist.
        self.in_this_process = in_this_process


# The engine raises a bare exception for a lock collision, with no type to
# catch (confirmed by introspection, see get_index_status()); the message is
# all there is to recognise it by. If its wording ever changes, a held
# collection goes back to surfacing as the raw error: worse output, not wrong.
_LOCK_COLLISION_SIGNATURE = "WouldBlock"


def _load_shard(path: Path, *, retry: bool) -> "qe.EdgeShard":
    if retry:
        return _load_shard_with_retry(path)
    try:
        return qe.EdgeShard.load(str(path))
    except Exception as e:
        # Only what is recognisably a lock collision: calling a corrupt shard
        # "held by another process" would send the reader the wrong way.
        if _LOCK_COLLISION_SIGNATURE in str(e):
            raise CollectionBusyError(
                COLLECTION_NAME, path,
                f"Could not open collection '{COLLECTION_NAME}': another griot process has it open.",
            ) from e
        raise


def _load_shard_with_retry(path: Path) -> "qe.EdgeShard":
    """[multi mode] EdgeShard.load() can collide with another process that
    hasn't released its handle yet (e.g. another griot mcp session that just
    finished using it and whose idle release hasn't fired yet). The
    exception has no specific 'locked' type (confirmed by introspection,
    same observation already noted in get_index_status()) — retry with a
    short backoff, local disk contention usually resolves in milliseconds."""
    attempts = len(_LOCK_RETRY_DELAYS) + 1
    for i in range(attempts):
        try:
            return qe.EdgeShard.load(str(path))
        except Exception as e:
            # Only what is recognisably a lock collision is worth waiting
            # for. A damaged collection fails the same way on every attempt:
            # retrying it and then saying "another process has it open" sent
            # people waiting for a process that does not exist.
            if _LOCK_COLLISION_SIGNATURE not in str(e):
                raise
            if i == attempts - 1:
                raise CollectionBusyError(
                    COLLECTION_NAME, path,
                    f"Could not open collection '{COLLECTION_NAME}' after {attempts} "
                    f"attempts — another griot process still has it open.",
                ) from e
            time.sleep(_LOCK_RETRY_DELAYS[i])


# Opening and closing the handle of the active collection, one at a time.
# Sync MCP tools run in worker threads: two calls that both found it closed
# both opened it (and the one that lost was never closed), and a release
# that came while another call was opening found nothing to close.
_client_lock = threading.RLock()
# How long a status read (get_client(wait=False)) waits for a call that is
# opening the collection before it answers "busy".
_STATUS_READ_PATIENCE = 0.5


def get_client(*, wait: bool = True) -> "qe.EdgeShard":
    """Memoized handle for the ACTIVE collection's Edge shard
    (COLLECTION_NAME) — only one process can have the directory open at a
    time (Edge's mutual exclusion is per PROCESS — safe within threads of
    the same process, not ACROSS processes). In 'multi' mode (the default)
    it is reopened with retry-with-backoff after it was released. In
    'single' mode (GRIOT_MCP_CONCURRENCY_MODE=single) the handle stays open
    forever, identical to the original behavior.

    It is never closed here. This function used to close and reopen the
    handle once IDLE_RELEASE_SECONDS had passed since its last use, and it
    cannot know who is using it: a call arriving during a long one closed
    the collection under it. Letting go of an idle handle is the job of
    whoever counts the calls in flight (mcp_server's reaper).

    wait=False skips the backoff for a caller for whom "held by someone
    else" is a normal answer rather than a failure to work around (a status
    read): the ~12s budget is for callers that need the shard. Such a
    caller does not wait long behind another call that is opening it,
    either."""
    global _client, _client_last_used_at
    if not _client_lock.acquire(timeout=-1 if wait else _STATUS_READ_PATIENCE):
        path = _collection_path(COLLECTION_NAME)
        raise CollectionBusyError(COLLECTION_NAME, path,
                                  f"Collection '{COLLECTION_NAME}' is being opened by another call right now.",
                                  in_this_process=True)
    try:
        if _client is None:
            path = _collection_path(COLLECTION_NAME)
            if (path / _EDGE_CONFIG_MARKER).exists():
                opened = _load_shard(path, retry=CONCURRENCY_MODE == "multi" and wait)
            else:
                path.mkdir(parents=True, exist_ok=True)
                cfg = qe.EdgeConfig(
                    vectors={"dense": qe.EdgeVectorParams(size=EMBED_DIM, distance=qe.Distance.Cosine, hnsw_config=_HNSW_CONFIG)},
                )
                opened = qe.EdgeShard.create(str(path), cfg)
            _client = opened
            # [M2] qdrant_data is a recoverable plaintext copy (compressed
            # payload, not encrypted) of ALL indexed content — 0700 on the root
            # directory and on the collection's directory closes it off to
            # other local users without depending on what the engine does with
            # the internal files.
            os.chmod(QDRANT_PATH, 0o700)
            os.chmod(path, 0o700)
            # [security review, lower-priority gap] the outer chmod above only
            # covers the directory itself — Edge's own files inside it (WAL,
            # segments, edge_config.json) are written with the process umask
            # regardless of which caller reached this branch (search(),
            # count_pending(), get_index_status() all funnel through here on a
            # first open, not just index_documents()), so the same recursive
            # repair index_documents() already does at the end of a write must
            # also run here.
            _secure_collection_dir(COLLECTION_NAME)
            ensure_collection(_client)
        if CONCURRENCY_MODE == "multi":
            _client_last_used_at = time.time()
        return _client
    finally:
        _client_lock.release()


def release_client() -> None:
    """Closes and releases the memoized handle for the ACTIVE collection, if
    one is open. [real finding, 2026-08-20] `griot mcp` is a long-lived
    process that memoizes get_client() on its first read
    (griot_search/griot_index_status/griot_quality_check) and holds the
    collection open for the rest of the server's life — permanently
    blocking any indexing subprocess later launched via griot_index_repo
    (same directory, Qdrant Edge's mutual exclusion is per PROCESS, not
    just per thread; confirmed empirically: 'failed to open WAL ...
    WouldBlock'). Called by griot_index_repo BEFORE spawning the
    subprocess — the next read reopens on demand, the same get_client() as
    always (_client goes back to None)."""
    global _client, _client_last_used_at
    # Under the lock: a release that came while another call was opening
    # found nothing to close, and the open that finished a moment later left
    # the collection held for the very subprocess this made room for.
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None
        _client_last_used_at = None


# Options of the top-level `griot` command that take a value, so the value is
# not mistaken for the subcommand (`griot --profile x index all`).
_GRIOT_VALUE_OPTIONS = {"--profile", "--chat-profile", "--sources"}


def _griot_role(argv: list[str]) -> str | None:
    """What a process command line is running, as far as griot is concerned:
    "mcp" (the MCP server), "index" (an indexing run), "other" (any other griot
    command), or None when it is not griot at all.

    Decided on WHAT is being run, never on the word "griot" or "mcp" appearing
    somewhere in the arguments: a virtualenv inside a checkout called griot puts
    that word in the interpreter's own path, and `griot index --repo mcp-gateway`
    is an indexing run whatever the repo is called. Unsure means None."""
    if not argv:
        return None
    exe = os.path.basename(argv[0]).lower()
    if exe == "griot":
        rest = argv[1:]
    elif exe.startswith("python"):
        args, rest = argv[1:], None
        skip = False
        for i, arg in enumerate(args):
            if skip:  # the value of -W / -X, not a script
                skip = False
                continue
            if arg in ("-W", "-X"):
                skip = True
                continue
            if arg == "-m":
                module = args[i + 1] if i + 1 < len(args) else ""
                if module == "griot.mcp_server":
                    return "mcp"
                if module.startswith("griot.index_"):
                    return "index"
                if module != "griot" and not module.startswith("griot."):
                    return None
                rest = args[i + 2:]
                break
            if arg == "-c":
                return None
            if not arg.startswith("-"):
                if os.path.basename(arg) != "griot":
                    return None
                rest = args[i + 1:]
                break
        if rest is None:
            return None
    else:
        return None
    tokens = iter(rest)
    for token in tokens:
        if token in _GRIOT_VALUE_OPTIONS:
            next(tokens, None)
        elif not token.startswith("-"):
            return token if token in ("mcp", "index") else "other"
    return "other"


def _is_griot_command(argv: list[str]) -> bool:
    return _griot_role(argv) is not None


def find_collection_holders(path: Path) -> list[dict]:
    """Best-effort: the OTHER processes that have files under `path` open,
    as {"pid", "command", "started", "role"} (role: see _griot_role). Empty when `lsof` is missing, hangs or
    finds nothing — the caller must be able to explain a held collection
    without this.

    A griot process (one with a role) is shown with its full command line,
    anything else by name only: an unrelated process (a backup tool with a password among its
    arguments) must not be echoed into a terminal or an agent's context."""
    try:
        out = subprocess.run(["lsof", "-t", "+D", str(path)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    holders = []
    for pid in sorted({int(t) for t in out.stdout.split() if t.isdigit()} - {os.getpid()}):
        info = {"pid": pid, "command": None, "started": None, "role": None}
        try:
            proc = psutil.Process(pid)
            argv = proc.cmdline()
            info["role"] = _griot_role(argv)
            shown = " ".join([os.path.basename(argv[0]), *argv[1:]]) if info["role"] else proc.name()
            info["command"] = shown[:200] or None
            info["started"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(proc.create_time()))
        except (psutil.Error, OSError):
            pass
        holders.append(info)
    return holders


def ensure_collection(client: "qe.EdgeShard") -> None:
    """With Edge, creating/loading the shard is already a single operation
    (EdgeShard.create/load, done in get_client() before the client exists)
    — unlike the old QdrantClient, which connected first and only then
    checked/created the collection. There's nothing left to "ensure" here
    once the client already exists; kept as a no-op just for the public
    signature (no code outside common.py calls this today, but it's one of
    the functions whose contract this migration deliberately preserves)."""
    pass


class GeminiUnavailable(Exception):
    """The Gemini API became unavailable/rate-limited beyond the configured
    retries — the decision of what to do (give up on the item, propagate
    the error) belongs to whoever called _gemini_post_with_retry, not to
    the helper."""


class _NoCookies(http.cookiejar.DefaultCookiePolicy):
    """A kept session is for the connection, not for state: a cookie an API
    sets is not stored and not sent back."""

    def set_ok(self, cookie, request):
        return False


_http_lock = threading.Lock()
_http_session_kept: "requests.Session | None" = None
# The session that has completed a call, if it is still the kept one: only
# then can one of its connections have gone stale. The session itself, not a
# flag: a slow call finishing on a session that was replaced meanwhile must
# not vouch for the new one.
_http_session_worked: "requests.Session | None" = None
# A connection error that a fresh connection would meet again.
_NOT_A_STALE_CONNECTION = (requests.Timeout, requests.exceptions.SSLError, requests.exceptions.ProxyError)


def _new_http_session() -> "requests.Session":
    session = requests.Session()
    session.cookies.set_policy(_NoCookies())
    return session


def _http_session() -> "requests.Session":
    """The session this process keeps for its calls to embedding and chat
    APIs. Nothing is stored on it between calls but the open connections:
    the credential of a call goes in that call's own headers."""
    global _http_session_kept
    with _http_lock:
        if _http_session_kept is None:
            _http_session_kept = _new_http_session()
        return _http_session_kept


def _drop_http_session() -> None:
    global _http_session_kept, _http_session_worked
    with _http_lock:
        kept, _http_session_kept, _http_session_worked = _http_session_kept, None, None
    if kept is not None:
        kept.close()


def _http_post(url: str, **kwargs) -> "requests.Response":
    """POST over a connection that is kept between calls. Each call used to
    open its own: a TCP and a TLS handshake before every batch of an index
    run and before every search of a long-lived server (about 150 ms of a
    385 ms call, measured against a real endpoint).

    A kept connection can have been closed by the other end while it sat
    idle, and then the first write on it fails. The callers wait ten or
    twenty seconds between attempts when an API cannot be reached, which is
    for a network that is down, not for this: after a call that worked, a
    connection error is tried once more, at once, on a fresh connection. A
    timeout is not (the call already waited its full time), nor an error a
    fresh connection would meet again (a certificate, a proxy), nor a
    failure on a session that never worked (nothing was kept, so nothing
    went stale)."""
    global _http_session_worked
    session = _http_session()
    try:
        response = session.post(url, **kwargs)
    except requests.ConnectionError as e:
        if isinstance(e, _NOT_A_STALE_CONNECTION) or _http_session_worked is not session:
            raise
        _drop_http_session()
        session = _http_session()
        response = session.post(url, **kwargs)
    with _http_lock:
        if _http_session_kept is session:
            _http_session_worked = session
    return response


def _gemini_post_with_retry(path: str, json_body: dict, max_rate_limit_retries: int = 5, max_connection_retries: int = 30) -> dict:
    """POST directly to Google's API (no LiteLLM/proxy in between) with two
    independent retry budgets — used by embed_texts() ("direct" backend)
    and chat_completion():
    - 429 (rate limit): short backoff (up to max_rate_limit_retries).
    - connection error (network down): a much more patient backoff (up to
      max_connection_retries, 20s each — ~10min total), same philosophy as
      when this used to protect against Docker Desktop crashing — now it
      protects against a generic network outage."""
    token = _require_gemini_token()
    rate_limit_attempt = 0
    connection_attempt = 0
    while True:
        try:
            # [finding H1, 2026-08-19 audit] key in a header, NEVER in
            # ?key= in the URL: requests' str() for HTTPError/ConnectionError
            # includes the full URL, so a query param would leak the key
            # into logs/stdout/tracebacks. allow_redirects=False: a
            # cross-host redirect can't carry the credential along with it.
            resp = _http_post(f"{GEMINI_API_BASE}/{path}", headers={"x-goog-api-key": token}, json=json_body, timeout=120, allow_redirects=False)
            if resp.status_code == 429:
                if rate_limit_attempt >= max_rate_limit_retries:
                    raise GeminiUnavailable(f"persistent 429 after {max_rate_limit_retries} attempts")
                wait = 2 ** rate_limit_attempt
                rate_limit_attempt += 1
                log_and_print(f"429 from the Gemini API, waiting {wait}s...")
                time.sleep(wait)
                continue
            if 300 <= resp.status_code < 400:
                # [review] a 3xx sails right through raise_for_status() —
                # turned into a typed error here instead of a raw
                # JSONDecodeError further down.
                raise GeminiUnavailable(f"redirect {resp.status_code} not followed (policy: the credential doesn't follow a redirect)")
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            # only the status code — never str(e), which may contain the
            # request URL (and, in a future regression, an embedded secret)
            status = e.response.status_code if e.response is not None else "?"
            raise GeminiUnavailable(f"HTTP error {status} from the Gemini API") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            if connection_attempt >= max_connection_retries:
                raise GeminiUnavailable(f"Gemini API unreachable after {max_connection_retries} attempts ({e.__class__.__name__})") from e
            connection_attempt += 1
            log_and_print(f"Gemini API unreachable ({e.__class__.__name__}), attempt {connection_attempt}/{max_connection_retries}, waiting 20s...", level="warning")
            time.sleep(20)


class DirectAPIUnavailable(Exception):
    """Same role as GeminiUnavailable, but for the generic HTTP adapter
    reused by any OpenAI-compatible provider — embedding (openai-small
    profile, the design notes) and chat (openai/deepseek/groq
    profiles, see "Chat profiles" above). Renamed from
    DirectEmbedUnavailable in 2026-08-13 when it stopped being
    embedding-exclusive."""


def _openai_compatible_post_with_retry(url: str, headers: dict, json_body: dict, max_rate_limit_retries: int = 5, max_connection_retries: int = 10) -> dict:
    """Generic POST for the {model, input} -> data[].embedding format
    (OpenAI/Voyage/remote — request_style="openai_compatible"). Same
    two-independent-retries philosophy as _gemini_post_with_retry, without
    duplicating the logic for each paid provider."""
    rate_limit_attempt = 0
    connection_attempt = 0
    while True:
        try:
            # [M5] allow_redirects=False: the Authorization header must not
            # follow a cross-host redirect (requests only strips it in some
            # cases; better to never follow one with a credential attached).
            resp = _http_post(url, headers=headers, json=json_body, timeout=120, allow_redirects=False)
            if resp.status_code == 429:
                if rate_limit_attempt >= max_rate_limit_retries:
                    raise DirectAPIUnavailable(f"persistent 429 after {max_rate_limit_retries} attempts on {url}")
                wait = 2 ** rate_limit_attempt
                rate_limit_attempt += 1
                log_and_print(f"429 on {url}, waiting {wait}s...")
                time.sleep(wait)
                continue
            if 300 <= resp.status_code < 400:
                # [review] raise_for_status() ignores 3xx — without this the
                # redirect (never followed, M5) would fall through to
                # resp.json() and die with a raw JSONDecodeError instead of
                # the expected typed error.
                raise DirectAPIUnavailable(f"redirect {resp.status_code} not followed (policy: the credential doesn't follow a redirect) on {url}")
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            # [review, same contract as H1] never interpolate str(e) — the
            # requests library's text may contain the full request URL, and
            # a future provider with a key in a query param would silently
            # regress this.
            status = e.response.status_code if e.response is not None else "?"
            raise DirectAPIUnavailable(f"HTTP error {status} on {url}") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            if connection_attempt >= max_connection_retries:
                raise DirectAPIUnavailable(f"{url} unreachable after {max_connection_retries} attempts ({e.__class__.__name__})") from e
            connection_attempt += 1
            log_and_print(f"{url} unreachable ({e.__class__.__name__}), attempt {connection_attempt}/{max_connection_retries}, waiting 10s...", level="warning")
            time.sleep(10)


# What one request may hold when the profile does not say: the embeddings
# endpoint this adapter was written for takes 300,000 tokens per request.
_DEFAULT_REQUEST_TOKEN_LIMIT = 300_000


def _requests_within_the_limit(texts: list[str], limit: int) -> list[list[int]]:
    """The positions of `texts`, in order, cut into requests whose UTF-8
    bytes add up to at most `limit`. A token is never shorter than one byte,
    so the bytes of a request bound its tokens whatever the text is: counted
    in characters, a round of Chinese or of emoji costs several times what
    the same round of code does, and went over a limit that code stays far
    under. A text larger than the limit on its own goes alone: the endpoint
    answers for that one text, and the rest of the round is not lost to it."""
    requests_, current, size = [], [], 0
    for position, text in enumerate(texts):
        weight = len(text.encode("utf-8"))
        if current and size + weight > limit:
            requests_.append(current)
            current, size = [], 0
        current.append(position)
        size += weight
    if current:
        requests_.append(current)
    return requests_


def _embed_texts_openai_compatible(texts: list[str]) -> list[list[float] | None]:
    """Generic HTTP adapter: only reads what the active
    profile defines (endpoint_url/model/api_key_env/extra_params) — no
    provider-specific logic here, so Voyage/remote can reuse it without
    duplication when they're added. One round is one request, unless its
    texts are too large for one (see _requests_within_the_limit): then each
    request stands or fails on its own."""
    api_key_env = ACTIVE_PROFILE["api_key_env"]
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise ValueError(
            f"{api_key_env} not found in the environment — required for the "
            f"{ACTIVE_PROFILE_NAME!r} embedding profile (backend direct/openai_compatible)."
        )
    result: list[list[float] | None] = [None] * len(texts)
    limit = ACTIVE_PROFILE.get("request_token_limit", _DEFAULT_REQUEST_TOKEN_LIMIT)
    for number, positions in enumerate(_requests_within_the_limit(texts, limit)):
        if number:
            check_spend_ceiling()  # embed_texts() checked before the first one
        vectors = _embed_one_openai_compatible_request([texts[position] for position in positions], api_key)
        for position, vector in zip(positions, vectors):
            result[position] = vector
    return result


def _embed_one_openai_compatible_request(texts: list[str], api_key: str) -> list[list[float] | None]:
    body = {"model": ACTIVE_PROFILE["model"], "input": texts, **ACTIVE_PROFILE.get("extra_params", {})}
    try:
        data = _openai_compatible_post_with_retry(
            ACTIVE_PROFILE["endpoint_url"], {"Authorization": f"Bearer {api_key}"}, body,
        )
    except DirectAPIUnavailable as e:
        log_and_print(f"Error in the embedding batch ({ACTIVE_PROFILE_NAME}): {e}", level="warning")
        _note_embedding_failure(str(e))
        return [None] * len(texts)

    tokens = _billed_tokens(data, ("usage", "total_tokens"), texts)
    record_spend(tokens / 1_000_000 * ACTIVE_PROFILE["price_per_1m_tokens"])
    # index guarantees correspondence with the input even if data[] comes
    # back out of order — unlike Gemini's batchEmbedContents, which has no index.
    by_index = {item["index"]: item["embedding"] for item in data["data"]}
    missing = sum(1 for i in range(len(texts)) if i not in by_index)
    if missing:
        _note_embedding_failure(f"the embedding API returned no vector for {missing} of {len(texts)} text(s)")
    return [by_index.get(i) for i in range(len(texts))]


def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embeds a batch of texts with the active profile (direct to Gemini,
    or local). Returns None for any item that fails (only possible on the
    "direct" backend, due to a network error — the call is all-or-nothing
    per batch, Google's API doesn't expose per-item failure)."""
    # The reason kept for a caller is the reason of THIS call. A server runs
    # for days: yesterday's rate limit must not explain today's failure.
    _note_embedding_failure(None)
    if ACTIVE_PROFILE["backend"] == "local":
        # Sort by length before embedding: fastembed pads each batch up to
        # its longest text (enable_padding() without a length — plan
        # section 10.2, item 3), and griot's corpus mixes long code chunks
        # with short commit messages. Without sorting, a short commit in a
        # mixed batch pays the padding cost of the longest chunk in the
        # SAME batch. The batch size is the profile's (see batch_sizes()). At
        # the end, undoes the sort — the caller (index_documents) does
        # zip(to_embed, vectors) assuming index-to-index correspondence
        # with the INPUT `texts` list, not with the internal processing order.
        if not texts:
            return []
        sorted_idx = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        sorted_texts = [texts[i] for i in sorted_idx]
        sorted_vectors = list(get_embed_model().embed(sorted_texts, batch_size=EMBED_CALL_BATCH_SIZE))
        result: list[list[float] | None] = [None] * len(texts)
        for original_i, vector in zip(sorted_idx, sorted_vectors):
            result[original_i] = vector.tolist()
        return result

    check_spend_ceiling()
    if ACTIVE_PROFILE.get("request_style") == "openai_compatible":
        return _embed_texts_openai_compatible(texts)

    model = ACTIVE_PROFILE["model"]
    try:
        body = _gemini_post_with_retry(
            f"models/{model}:batchEmbedContents",
            {"requests": [
                {"model": f"models/{model}", "content": {"parts": [{"text": t}]}, "outputDimensionality": EMBED_DIM}
                for t in texts
            ]},
        )
    except GeminiUnavailable as e:
        log_and_print(f"Error in the embedding batch: {e}", level="warning")
        _note_embedding_failure(str(e))
        return [None] * len(texts)

    tokens = _billed_tokens(body, ("usageMetadata", "promptTokenCount"), texts)
    record_spend(tokens / 1_000_000 * ACTIVE_PROFILE["price_per_1m_tokens"])
    # batchEmbedContents doesn't return a per-item id/index (unlike the
    # OpenAI-style format the proxy call used to use) — the response order
    # matches the order of the sent request list.
    embeddings = body.get("embeddings") or []
    if len(embeddings) != len(texts):
        # With no index to match on, a short list cannot be lined up with
        # the texts: none of it is trusted.
        _note_embedding_failure(f"the embedding API returned {len(embeddings)} vector(s) for {len(texts)} text(s)")
        return [None] * len(texts)
    return [e["values"] for e in embeddings]


def _chat_completion_openai_compatible(prompt: str, model: str | None = None) -> str:
    """Generic adapter for chat profiles with backend="openai_compatible_chat"
    (openai/deepseek/groq) — same request/response format as the embedding
    adapter (_embed_texts_openai_compatible), reusing
    _openai_compatible_post_with_retry() instead of duplicating retry/backoff logic."""
    profile = ACTIVE_CHAT_PROFILE
    api_key = os.getenv(profile["api_key_env"])
    if not api_key:
        raise ValueError(
            f"{profile['api_key_env']} not found in the environment — required for the "
            f"{ACTIVE_CHAT_PROFILE_NAME!r} chat profile."
        )
    price = profile.get("price_per_1m_tokens")
    if price is None:
        env_var = f"GRIOT_{ACTIVE_CHAT_PROFILE_NAME.upper()}_CHAT_PRICE_PER_1M_TOKENS"
        raise ValueError(
            f"Price per 1M tokens for the {ACTIVE_CHAT_PROFILE_NAME!r} chat profile is not confirmed. "
            f"Set {env_var} (USD) before using this profile — griot never assumes an "
            f"unverified price (protects the spend circuit breaker)."
        )
    check_spend_ceiling()

    chat_model = model or profile["model"]
    body = {"model": chat_model, "messages": [{"role": "user", "content": prompt}]}
    try:
        data = _openai_compatible_post_with_retry(
            profile["endpoint_url"], {"Authorization": f"Bearer {api_key}"}, body,
        )
    except DirectAPIUnavailable as e:
        raise RuntimeError(f"Could not get a response from {ACTIVE_CHAT_PROFILE_NAME}: {e}") from e

    # Counted before the reply is taken out: an answer that cannot be read
    # was billed all the same.
    tokens = _billed_tokens(data, ("usage", "total_tokens"), [prompt, _reply_if_any(data, "choices", 0, "message", "content")])
    record_spend(tokens / 1_000_000 * price)
    return data["choices"][0]["message"]["content"]


def chat_completion(prompt: str, model: str | None = None) -> str:
    """Synthesizes the final answer over the already-retrieved context —
    uses the ACTIVE chat profile (GRIOT_CHAT_PROFILE, default "gemini"; see
    CHAT_PROFILES). 'model' only overrides the model within the active
    profile — to switch PROVIDER, use GRIOT_CHAT_PROFILE or `griot ask
    --chat-profile`. Goes through the same local spend circuit breaker as
    embed_texts()."""
    if ACTIVE_CHAT_PROFILE["backend"] == "openai_compatible_chat":
        return _chat_completion_openai_compatible(prompt, model=model)

    # gemini_native (default) — direct call to generateContent, no
    # LiteLLM/proxy, original behavior preserved.
    model = model or ACTIVE_CHAT_PROFILE["model"]
    check_spend_ceiling()
    try:
        body = _gemini_post_with_retry(
            f"models/{model}:generateContent",
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
            },
        )
    except GeminiUnavailable as e:
        raise RuntimeError(f"Could not get a response from Gemini: {e}") from e

    tokens = _billed_tokens(body, ("usageMetadata", "totalTokenCount"),
                            [prompt, _reply_if_any(body, "candidates", 0, "content", "parts", 0, "text")])
    record_spend(tokens / 1_000_000 * ACTIVE_CHAT_PROFILE["price_per_1m_tokens"])
    return body["candidates"][0]["content"]["parts"][0]["text"]


# What this process replaced since it was last asked: (where, rule). Kept per
# process, not per call, because a source cuts all its text first and indexes
# afterwards; the indexer takes the list when its run ends.
_redactions: list[tuple[str, str]] = []


def _without_credentials(text: str, where: str | None) -> str:
    """`text` with credential-looking values replaced (see redaction.py), and
    each replacement recorded against `where` for the run's report."""
    cleaned, rules = redaction.redact(text)
    _redactions.extend((where or "(unnamed text)", rule) for rule in rules)
    return cleaned


def printable(text: str) -> str:
    """`text` as it may be written to a terminal. File, branch and tag names
    and indexed text come from the repository; a name can hold a line break
    or an escape sequence, which would forge lines or drive the terminal."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def shown(name: str) -> str:
    """A name that comes from a repository (a path, a branch, a tag, a label
    built from them), as the CLI may print it: a credential-shaped part is
    replaced and nothing in it can drive the terminal."""
    return printable(redaction.redact(name)[0])


def take_redactions() -> list[tuple[str, str]]:
    """What was replaced since the last call, and forgets it."""
    taken = list(_redactions)
    _redactions.clear()
    return taken


def report_redactions() -> int:
    """Prints where credential-looking values were replaced in this run and
    returns how many. Locations and rule names only: the point of replacing a
    value is that it is written nowhere, a terminal included."""
    taken = take_redactions()
    if not taken:
        return 0
    places: dict[str, list[str]] = {}
    for where, rule in taken:
        places.setdefault(where, []).append(rule)
    print(f"\nReplaced {len(taken)} credential-looking value(s) before embedding, in {len(places)} place(s):")
    for where, rules in list(places.items())[:20]:
        # The place is named by a path, a branch, a tag: a name can hold the
        # very kind of value this report is about, and control characters.
        print(f"  {shown(where)} ({', '.join(sorted(set(rules)))})")
    if len(places) > 20:
        print(f"  ... and {len(places) - 20} more place(s)")
    print("They are not in the index. If one is real, it is still in that file or history: rotate it.")
    return len(taken)


_last_embedding_failure: str | None = None


def _note_embedding_failure(reason: str | None) -> None:
    global _last_embedding_failure
    _last_embedding_failure = reason


def last_embedding_failure() -> str | None:
    """Why the most recent embedding call gave up, in the provider's words."""
    return _last_embedding_failure


def stored_text(payload: dict | None) -> str:
    """The text of a stored point, as it may leave griot. What an older
    version indexed raw is still in the store until its repository is indexed
    again; every reader goes through here so that it is replaced on the way
    out, to an agent, a terminal, a chat model or an embedding API."""
    return redaction.redact((payload or {}).get("content") or "")[0]


def chunk_text(text: str, max_chars: int = 1500, overlap: int = 200, *, where: str | None = None) -> list[str]:
    """Cuts `text` into overlapping chunks for indexing. Credential-looking
    values are replaced FIRST: cut in two by a chunk boundary, a value would
    match no detector and both halves would be embedded and stored. `where`
    names the source for the run's report."""
    text = _without_credentials(text, where)
    if overlap >= max_chars:
        raise ValueError(f"overlap ({overlap}) must be smaller than max_chars ({max_chars}), otherwise the cursor never advances")
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        if end >= len(text):
            break  # this chunk already reached the end of the text —
                    # without this, the next "start" (end - overlap) could
                    # still be < len(text) and generate a spurious final
                    # chunk, nearly identical to the previous one's overlap
                    # (reproduces whenever the text ends within the last
                    # `overlap` chars of a full chunk)
        start = end - overlap
    return chunks


def stable_id(key: str) -> str:
    """Deterministic UUID derived from a natural key (e.g.
    'repo:commit:hash'). Upserting by this id overwrites instead of
    duplicating — reindexing the same item (same key) doesn't create a new row."""
    digest = hashlib.md5(key.encode()).hexdigest()
    return str(uuid.UUID(hex=digest))


# [user-requested] Cap on how many individual failures one run records. A
# systemic failure (bad credential, API down) fails EVERY document — storing
# thousands of ids would bloat the run summary for no extra insight, since
# they all share the same reason. The count in `failed` stays exact
# regardless; only the itemized list is capped.
MAX_RECORDED_FAILURES = 20

# Which documents failed in the most recent index_documents() call, as
# [{"id", "reason"}]. Module-level rather than returned so index_documents()
# keeps its (indexed, skipped, failed) contract — every index_*.py unpacks
# exactly three values, and widening that tuple would touch all five for a
# diagnostic detail only the run summary needs.
_last_run_failures: list[dict] = []


def _record_failures(docs: list[dict], reason: str) -> None:
    """Appends failures up to the cap. Uses the document's own `id` (the
    natural key: "repo:commit:<hash>", "repo:code:<file>:<chunk>"), not the
    derived point id — the natural key is what a person can act on."""
    room = MAX_RECORDED_FAILURES - len(_last_run_failures)
    for doc in docs[:max(0, room)]:
        _last_run_failures.append({"id": doc.get("id"), "reason": reason})


def last_run_failures() -> list[dict]:
    """The itemized failures from the last index_documents() call in this
    process — id plus reason, capped at MAX_RECORDED_FAILURES.

    [user-requested] `failed=50` is a number, not a diagnosis: learning that
    those 50 were oversized commit bodies meant grepping
    griot.log and correlating by hand. Naming the documents is what makes
    the count actionable."""
    return list(_last_run_failures)


def _split_pending(batch: list[dict], client: "qe.EdgeShard") -> list[dict]:
    """Tags each doc in the batch with _point_id/_content_hash and returns
    only the ones that need to be (re)embedded (new point, or content hash
    changed since last time). Used by index_documents() (actually embeds)
    and count_pending() (only counts, spends nothing) — same logic, no
    duplication.

    shard.retrieve() (validated in the Edge migration prototype) is the
    direct equivalent of the old qdrant-client's client.retrieve(): accepts
    with_payload as a list of fields (only content_hash, without bringing
    the rest of the payload) and omits missing IDs from the result instead
    of raising an error — same contract as before."""
    for doc in batch:
        # The net under chunk_text(): a document that was never chunked (a
        # tag, a branch, a source written later) passes here all the same.
        # Before the hash, so the hash is of what is stored; text that was
        # already replaced is left as it is and recorded once.
        doc["content"] = _without_credentials(doc["content"], doc["id"])
        doc["_point_id"] = stable_id(doc["id"])
        doc["_content_hash"] = hashlib.md5(doc["content"].encode()).hexdigest()

    # The details stored beside the text are read too. What decides whether
    # to embed is the text alone, so a detail that changed while the text
    # stayed (a tag that got the hash of its commit, a pull request that was
    # merged) was never written again: see _stale_details().
    details = sorted({name for doc in batch for name in doc.get("metadata", {})})
    existing = client.retrieve(
        point_ids=[doc["_point_id"] for doc in batch],
        with_payload=["content_hash", *details],
        with_vector=False,
    )
    stored = {str(r.id): (r.payload or {}) for r in existing}

    pending = []
    for doc in batch:
        payload = stored.get(doc["_point_id"])
        if payload is None or payload.get("content_hash") != doc["_content_hash"]:
            pending.append(doc)
            doc["_details_changed"] = False
        else:
            doc["_details_changed"] = any(payload.get(name) != value for name, value in doc.get("metadata", {}).items())
    return pending


def _write_stale_details(batch: list[dict], client: "qe.EdgeShard") -> int:
    """For the documents of `batch` whose text is up to date and whose
    stored details are not: writes the details, without embedding anything.
    Returns how many. After _split_pending(), which marks them."""
    stale = [doc for doc in batch if doc.get("_details_changed")]
    written = 0
    try:
        for doc in stale:
            try:
                # Merges: the text, its hash and the vector stay as they are.
                client.update(qe.UpdateOperation.set_payload([doc["_point_id"]], doc["metadata"]))
                written += 1
            except Exception as e:  # noqa: BLE001 - one point (gone since it was read) must not stop the others
                log_and_print(f"Warning: could not update the stored details of {shown(doc['id'])}: {e}",
                              level="warning", echo=False)
    finally:
        if written:
            client.flush()
    return written


def pending_summary(documents: list[dict], desc: str = "Checking") -> dict:
    """What a run over `documents` would embed, WITHOUT calling embed_texts
    or upsert: zero cost, a local read of the store only. `to_embed` and
    `up_to_date` count documents; `to_embed_chars` is the size of what would
    be sent, which is what a cost is estimated from."""
    client = get_client()
    pending = up_to_date = chars = 0

    for i in tqdm(range(0, len(documents), INDEX_BATCH_SIZE), desc=desc):
        batch = documents[i:i + INDEX_BATCH_SIZE]
        to_embed = _split_pending(batch, client)
        pending += len(to_embed)
        up_to_date += len(batch) - len(to_embed)
        chars += sum(len(doc["content"]) for doc in to_embed)

    return {"to_embed": pending, "up_to_date": up_to_date, "to_embed_chars": chars}


def count_pending(documents: list[dict], desc: str = "Checking") -> tuple[int, int]:
    """--dry-run mode: counts how many documents would need to be
    (re)embedded vs how many are already up to date in Qdrant. Returns
    (pending, up_to_date). Useful for predicting the real size of a rerun
    before committing hours of CPU (local) or money (gateway)."""
    summary = pending_summary(documents, desc=desc)
    return summary["to_embed"], summary["up_to_date"]


# About how many characters of indexed code and prose make one token, for an
# ESTIMATE of what a run would cost (measured on a real index: 3.53). The
# spend that is recorded comes from the provider's own token count; where a
# provider reports none, from FALLBACK_BYTES_PER_TOKEN, not from this.
CHARS_PER_TOKEN_ESTIMATE = 3.5


def dry_run(documents: list[dict], *, source: str, unit: str, desc: str = "Checking",
            prune_scope: dict | None = None) -> dict:
    """`--dry-run` of one source: says what a run would embed and remove,
    and does neither. The five indexers share it so that they report the same
    way, to a person (the printed lines) and to a program: when
    GRIOT_DRY_RUN_REPORT names a file, one JSON line per source is appended
    to it. That is how the MCP preview reads the counts, instead of parsing
    sentences written for a terminal."""
    pruning: dict = {}
    if collection_exists(COLLECTION_NAME):
        summary = pending_summary(documents, desc=desc)
    else:
        # Nothing indexed under this profile yet: everything is to embed and
        # nothing can be stale. Said without opening the collection, because
        # opening one that does not exist CREATES it, and a dry run changes
        # nothing.
        summary = {"to_embed": len(documents), "up_to_date": 0,
                   "to_embed_chars": sum(len(_without_credentials(doc["content"], doc["id"])) for doc in documents)}
    print(f"\n[dry-run] {summary['to_embed']} {unit} would need to be (re)embedded, "
          f"{summary['up_to_date']} are already up to date.")
    report_redactions()
    stale = 0
    if prune_scope is not None and collection_exists(COLLECTION_NAME):
        stale = prune_orphans(documents, dry_run=True, report=pruning, **prune_scope)
    # `stale` is what a plain run would remove; `held_back` what it would
    # leave because it is more than half of the repository (--prune removes it).
    record = {"source": source, **summary, "stale": stale, "held_back": pruning.get("held_back", 0)}
    report_path = os.getenv("GRIOT_DRY_RUN_REPORT")  # internal: set by jobs.run_index_preview, not a setting
    if report_path:
        try:
            with open(report_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            print(f"Warning: could not write the dry-run report to {report_path}: {e}", file=sys.stderr)
    return record


def _secure_collection_dir(collection: str) -> None:
    """[security review] Qdrant Edge's own Rust engine writes its files
    (WAL, segments, payload_storage/*.dat, vector_storage/*) with the
    process umask, not through griot's secure_* helpers — a real gap a
    security review found: those files can end up group/world-readable
    (0644/0755) even though the outer qdrant_data/<collection>/ directory
    is 0700. Harmless TODAY only because directory traversal permission is
    required at every level to reach them — but fragile: a backup tool
    without permission preservation (rsync/tar without -p), a naive cloud
    sync, or restoring from an archive could reset the outer directories
    and leave the actual RAG content world-readable with nothing else
    standing in the way. Called once at the end of every index_documents()
    run (the natural "a write just happened" point) — recursively repairs
    whatever Edge wrote during this run. Best-effort per-file: one file's
    chmod failing (e.g. a transient race with Edge's own I/O) must never
    fail the whole indexing run over a permission repair. Also called on
    every cold open in get_client() (including every multi-mode
    idle-release reopen), so the walk always visits every entry (a new
    file from another process must still be checked) but skips the chmod
    syscall itself when the mode is already correct — a real cost on large
    collections opened repeatedly."""
    path = _collection_path(collection)
    if not path.exists():
        return
    for root, _dirs, files in os.walk(path):
        _repair_mode(root, 0o700)
        for f in files:
            _repair_mode(os.path.join(root, f), 0o600)


def _repair_mode(target: str, mode: int) -> None:
    """Chmods target to mode unless it already is — a failed stat falls
    through to attempting the chmod anyway (never let a failed optimization
    check block the real repair). A chmod failure is logged, not raised:
    this must never fail the indexing run or search that called it over a
    permission race, but silent swallowing left no trace anywhere.
    echo=False is required, not optional — this runs from get_client(),
    reached from the MCP server path where stdout is the JSON-RPC
    transport."""
    try:
        if stat.S_IMODE(os.stat(target).st_mode) == mode:
            return
    except OSError:
        pass
    try:
        os.chmod(target, mode)
    except OSError as exc:
        log_and_print(
            f"permission repair failed for {target} (kept previous mode) — {exc}",
            level="warning", echo=False,
        )


def index_documents(documents: list[dict], desc: str = "Indexing") -> tuple[int, int, int]:
    """Receives documents with 'id' (natural key, string), 'content' and
    'metadata' already prepared, embeds them in batches (active profile)
    and writes them in batches to the embedded Qdrant. Shared by all
    sources (code, commits, tags, branches, GitLab).

    Idempotency of COST, not just of storage: before embedding, checks
    whether the point already exists in Qdrant with the same content hash
    — if so, skips it (doesn't re-embed). Upserting by stable_id already
    prevented duplicate rows, but it didn't prevent paying again for
    embedding something that hadn't changed; running the whole pipeline
    several times in the same day adds up fast without this check. griot
    doesn't depend on LiteLLM/Docker for anything. Returns (indexed,
    skipped, failed)."""
    acquire_lock()
    # [real finding, 2026-08-13] index_documents() only registered the
    # release via atexit — correct for a single-source CLI (process ends,
    # atexit fires), but broke `griot index all`: 5 calls to
    # index_documents() (one per source) in the SAME process, and the 2nd
    # one onward always failed with "a griot process is already running
    # (PID <the same process>)" because the 1st call's lock was never
    # released between them. The try/finally here guarantees release at the
    # END of EACH run (success or exception), not only at the end of the
    # whole process — atexit (acquire_lock) stays registered as a safety
    # net for a crash that skips the finally block.
    try:
        print(f"\nGenerating embeddings ({ACTIVE_PROFILE_NAME}) and indexing {len(documents)} chunks...")
        client = get_client()
        indexed = 0
        skipped = 0
        failed = 0
        refreshed = 0  # among the skipped: text unchanged, stored details brought up to date
        consecutive_failed_batches = 0
        # Reset per run, not per process: a second index_documents() call
        # (griot index all makes five) must not inherit the previous
        # source's failures.
        global _last_run_failures
        _last_run_failures = []

        for i in tqdm(range(0, len(documents), INDEX_BATCH_SIZE), desc=desc):
            batch = documents[i:i + INDEX_BATCH_SIZE]
            to_embed = _split_pending(batch, client)
            skipped += len(batch) - len(to_embed)
            try:
                refreshed += _write_stale_details(batch, client)
            except Exception as e:  # noqa: BLE001 - the text is intact; the details are tried again next run
                log_and_print(f"Warning: could not update the stored details of unchanged points: {e}", level="warning")
            if not to_embed:
                continue

            texts = [doc["content"] for doc in to_embed]
            vectors = embed_texts(texts)

            points = [
                qe.Point(
                    id=doc["_point_id"],
                    vector={"dense": vector},
                    payload={**doc["metadata"], "content": doc["content"], "content_hash": doc["_content_hash"]},
                )
                for doc, vector in zip(to_embed, vectors)
                if vector is not None
            ]
            failed += len(to_embed) - len(points)
            _record_failures(
                [doc for doc, vector in zip(to_embed, vectors) if vector is None],
                "embedding returned no vector",
            )

            if not points:
                consecutive_failed_batches += 1
                if consecutive_failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                    raise RuntimeError(
                        f"{consecutive_failed_batches} consecutive batches failed completely — this isn't "
                        f"bad luck, something is systemically broken (Gemini API down, "
                        f"invalid credential, etc). Stopping instead of continuing to produce only failures. "
                        f"See logs/griot.log for the reason behind each failure."
                    )
                continue
            consecutive_failed_batches = 0

            try:
                client.update(qe.UpdateOperation.upsert_points(points))
                # flush() on every batch (not just at the end, unlike the
                # migration prototype) — index_documents() runs over large
                # corpora (the initial load takes ~5h); losing an
                # entire batch to a mid-run crash is worse here than the
                # small cost of an fsync per batch. flush() is about
                # durability on disk (WAL -> segments), not about read
                # visibility (retrieve()/query() already see points just
                # upserted in the same process without a flush).
                client.flush()
                indexed += len(points)
            except Exception as e:
                log_and_print(f"Error writing batch to Qdrant: {e}", level="warning")
                failed += len(points)
                # A whole batch lost to one write error: the reason is the
                # same for all of them, which is exactly why the itemized
                # list is capped while `failed` stays exact.
                _record_failures([doc for doc, vector in zip(to_embed, vectors) if vector is not None],
                                 f"write to the vector store failed: {e}")

        # [review] shard.optimize() was never called — without this, the
        # HNSW index is never (re)built over the new segments and searches
        # on large corpora (>10k points, above _HNSW_CONFIG's
        # full_scan_threshold) stay permanently in brute-force mode, and the
        # Edge migration's performance gain never materializes. Only at the
        # END of a full run (not per batch): measured cost ~8.8s for 30k
        # points, too expensive to pay on every INDEX_BATCH_SIZE batch; and
        # only when indexed > 0 — nothing new to optimize if the whole run
        # was skipped (reindexing with no changes at all) or failed entirely.
        if indexed > 0:
            client.optimize()

        if refreshed:
            # Said, because nothing else shows it: these count as skipped
            # (nothing was embedded), and something stored did change.
            print(f"Updated the stored details of {refreshed} unchanged point(s) (nothing was embedded).")
        return indexed, skipped, failed
    finally:
        _secure_collection_dir(COLLECTION_NAME)
        release_lock()


def _registration(repo_path) -> tuple[str | None, str | None]:
    """(name, None) when repo_path is registered under a name that belongs to
    it alone, otherwise (None, why). The name is the registered entry's own
    directory name, which is what the indexers write as `payload.repo` and use
    as the id key; two entries are the same repository when they resolve to
    the same directory, so one registered through a symlink still counts."""
    try:
        entries = [Path(p) for p in load_repos()]
    except (OSError, ValueError):
        return None, "repos.json could not be read"
    resolved = Path(repo_path).resolve()
    mine = [entry for entry in entries if entry.resolve() == resolved]
    if not mine:
        return None, "it is not registered in repos.json"
    if len(mine) > 1:
        # By a symlink and by its real path, or the same line twice: each
        # entry indexes under its own name, so no single name stands for it.
        return None, "that directory is registered more than once"
    name = mine[0].name
    if len([entry for entry in entries if entry.name == name]) != 1:
        return None, f"more than one registered repository is named '{name}'"
    return name, None


def registered_repo_name(repo_path: Path) -> str | None:
    """The name a repository's points carry and its ids are keyed on, but
    only when that name leads back to exactly this directory through
    repos.json. None for a path that is not registered and for a name two
    registered repositories share: nothing keyed on the name may act on them."""
    return _registration(repo_path)[0]


_PRUNE_PAGE = 1000
# Above this many points AND this fraction of what a repository has for a
# source, stale points are not removed unless the run was told to (--prune).
# Most of a repository going stale at once is far more often a wrong branch
# checked out or a listing gone wrong than code that really left; a small
# repository losing two of three files is ordinary, hence the floor.
_PRUNE_GUARD_MIN_POINTS = 100
_PRUNE_GUARD_FRACTION = 0.5
# The payload fields each source's id is built from (see the indexers).
_PRUNE_ID_FIELDS = {
    "code": ["file_path", "chunk_index"],
    "commit": ["commit_hash", "chunk_index"],
    "tag": ["tag_name", "chunk_index"],
    "branch": ["branch_name"],
}


def _ids_a_point_could_have(source_type: str, key: str, payload: dict) -> list[str]:
    """The natural ids a point with this payload would have if it had been
    written under `key`. Mirrors the id formats of the four local indexers
    (tests/test_orphans.py holds the two together). A commit has had three
    shapes over time: one point per commit, chunked with an index, and an
    early one that suffixed `:0` to single points."""
    if source_type == "code":
        if payload.get("file_path") is None or payload.get("chunk_index") is None:
            return []
        return [f"{key}:code:{payload['file_path']}:{payload['chunk_index']}"]
    if source_type == "commit":
        if not payload.get("commit_hash"):
            return []
        base = f"{key}:commit:{payload['commit_hash']}"
        return [base, *{f"{base}:{i}" for i in (payload.get("chunk_index"), 0) if i is not None}]
    if source_type == "tag":
        # One point per tag, or one per chunk of a long message. A point
        # written before tags were cut has no chunk number.
        if not payload.get("tag_name"):
            return []
        base = f"{key}:tag:{payload['tag_name']}"
        return [base] if payload.get("chunk_index") is None else [base, f"{base}:{payload['chunk_index']}"]
    return [f"{key}:branch:{payload['branch_name']}"] if payload.get("branch_name") else []


def _point_ids_written_under(client, repo: str, source_type: str, key: str) -> tuple[list, int]:
    """(ids, foreign): the points of `repo` and `source_type` that were
    written under the id key `key`, and how many were not.

    `payload.repo` is a directory name. A --path run of ANOTHER directory
    with the same name writes that name too, under a different key, so the
    name alone does not say whose a point is. A point belongs to `key` when
    its id is the one its own payload produces under that key; anything else
    (another key, or a point missing the fields its id is built from) is
    counted as foreign and never touched."""
    scope = qe.Filter(must=[
        qe.FieldCondition(key="repo", match=qe.MatchValue(value=repo)),
        qe.FieldCondition(key="source_type", match=qe.MatchValue(value=source_type)),
    ])
    ids, foreign, offset = [], 0, None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(
            limit=_PRUNE_PAGE, offset=offset, filter=scope,
            with_payload=_PRUNE_ID_FIELDS[source_type], with_vector=False))
        for point in points:
            candidates = _ids_a_point_could_have(source_type, key, point.payload or {})
            if str(point.id) in {stable_id(candidate) for candidate in candidates}:
                ids.append(point.id)
            else:
                foreign += 1
        if offset is None:
            return ids, foreign


def prune_orphans(documents: list[dict], *, source_type: str, repo_paths: list, used_path: bool = False,
                  incomplete=(), failed: int = 0, dry_run: bool = False, force: bool = False,
                  report: dict | None = None) -> int:
    """Removes the points of `source_type` that this run did not produce, for
    each repository in `repo_paths`. Returns how many were removed (or, with
    dry_run, would be).

    Indexing only ever added or replaced, so a deleted file, a file that
    shrank or became ignored, a deleted branch, all stayed searchable for
    good. This asks the store itself what it holds for the repository and
    compares that with the ids of `documents`: no second record of what was
    indexed is kept anywhere.

    It is the one destructive step of indexing, and re-creating a point
    costs an embedding, so every doubt resolves to "remove nothing":
    - `used_path`: a --path run is not a registered repository's run.
    - a repository that is not registered, or whose name two registered
      repositories share: the name does not identify its points.
    - a point that was not written under this repository's id key (see
      _point_ids_written_under): it is another directory's.
    - no documents for the repository: that is what a failed listing looks
      like, far more often than a repository emptied on purpose.
    - `incomplete`: names of repositories a file of which could not be read.
    - `failed`: documents of this run that were not written.
    - more than half of the repository's points (above a floor) stale at
      once, unless `force` (see _PRUNE_GUARD_*).
    - the index lock is taken: the documents are already written, so this
      says so and leaves the removal to the next run.

    The lock is taken again here, after index_documents() released it. A run
    that slips in between can have a point it just wrote removed by this one;
    the next run puts it back. The window is milliseconds and the cost one
    embedding, which is why the two are not fused into one critical section."""
    if used_path:
        return 0
    produced: dict[str, set[str]] = {}
    for doc in documents:
        produced.setdefault(doc["metadata"]["repo"], set()).add(stable_id(doc["id"]))
    names = []
    for path in repo_paths:
        if not produced.get(Path(path).name):
            continue
        name, why_not = _registration(path)
        if name is None:
            print(f"Stale points of {Path(path).name} ({source_type}) are not removed: {why_not}.")
        elif produced.get(name) and name not in incomplete and name not in names:
            names.append(name)
    if not names:
        return 0
    if failed:
        print(f"Stale points were not removed: {failed} document(s) of this run failed, "
              f"so what the index holds cannot be compared with what was read.")
        return 0

    total = 0
    if not dry_run:
        try:
            acquire_lock()
        except RuntimeError as e:
            print(f"Stale points were not removed: {e} The next run removes them.")
            return 0
    try:
        client = get_client()
        for name in names:
            existing, foreign = _point_ids_written_under(client, name, source_type, name)
            if foreign:
                print(f"{foreign} point(s) named {name} ({source_type}) were written by a run of another "
                      f"directory (--path) and are left alone.")
            stale = [point_id for point_id in existing if str(point_id) not in produced[name]]
            if not stale:
                continue
            if (not force and len(stale) > _PRUNE_GUARD_MIN_POINTS
                    and len(stale) > len(existing) * _PRUNE_GUARD_FRACTION):
                outcome = "they would not be removed" if dry_run else "nothing was removed"
                print(f"{len(stale)} of {len(existing)} indexed points of {name} ({source_type}) are not produced "
                      f"by this run. That is more than half, so {outcome}: it usually means another branch is "
                      f"checked out or files went missing. If it is right, run again with --prune.")
                # Counted for a caller that reads numbers and not this
                # sentence: returned as zero, these looked like a clean index.
                if report is not None:
                    report["held_back"] = report.get("held_back", 0) + len(stale)
                continue
            if dry_run:
                print(f"[dry-run] {len(stale)} stale point(s) of {name} ({source_type}) would be removed: "
                      f"their source is no longer there.")
            else:
                for i in range(0, len(stale), _PRUNE_PAGE):
                    client.update(qe.UpdateOperation.delete_points(stale[i:i + _PRUNE_PAGE]))
                client.flush()
                print(f"Removed {len(stale)} stale point(s) of {name} ({source_type}): their source is no longer there.")
            total += len(stale)
    finally:
        if not dry_run:
            _secure_collection_dir(COLLECTION_NAME)
            release_lock()
    return total


def index_lock_status() -> dict:
    """[shared config path] Just the lock-derived fields of get_index_status()
    (running/pid/path) — zero shard I/O. Extracted for callers that only
    care about "is anything indexing right now" (a job-confirmation
    screen, a status badge rendered on every page load): calling the full
    get_index_status() for that would risk EdgeShard.load()'s warning-per-
    call noise in griot.log against a collection currently held open by an
    indexer (see get_index_status()'s own comment on that exact
    scenario) — this never touches a shard at all, only the lock file."""
    lock_info = _read_lock()
    running = lock_info is not None and _lock_owner_is_alive(lock_info)
    return {
        "running": running,
        "pid": lock_info["pid"] if running else None,
        "path": lock_info["label"] if running else None,
    }


def collection_exists(collection: str) -> bool:
    """True if a collection's Edge shard has actually been created on disk
    — the same marker-file check get_index_status()/quality_check.py
    already do inline to avoid opening/materializing a shard that isn't
    there, factored out for delete_collection() and its callers (`griot
    profiles delete`, griot_profiles_delete)."""
    return (_collection_path(collection) / _EDGE_CONFIG_MARKER).exists()


def delete_collection(collection: str) -> None:
    """Permanently removes a collection's on-disk directory — the only way
    today to reclaim disk space from a profile no longer in use (embedding
    is paid once, at index time, never for storage). Pure filesystem
    operation: never opens an Edge shard, so it can never trip the
    get_client() memoization hazard, whether called from the CLI or a
    long-lived process. Raises ValueError if the collection
    doesn't exist, naming the collection (cli.delete_profile() wraps this
    with a profile-scoped active-profile check of its own before ever
    reaching here).

    [review finding] Also raises if an indexing run currently holds the
    lock: acquire_lock() is process-wide, not per-collection, so a run
    writing to a DIFFERENT collection right now still means THIS one could
    be next in the very same run. rmtree()ing a collection an indexer
    still has a shard open on would race its WAL rather than raise
    cleanly — an unlinked-while-open file just silently vanishes once the
    other process closes it, no crash, no clean error, which is worse than
    refusing up front."""
    if not collection_exists(collection):
        raise ValueError(f"collection '{collection}' does not exist.")
    if index_lock_status()["running"]:
        raise ValueError("an indexing run is currently in progress — wait for it to finish, then try again.")
    shutil.rmtree(_collection_path(collection))


def _points_error(collection: str, error: Exception) -> str:
    """Says which of the two very different things went wrong, in the log and
    to the caller: a collection that is busy is normal and passes; one that
    cannot be read is damage, and calling it busy hides that."""
    if isinstance(error, CollectionBusyError) or _LOCK_COLLISION_SIGNATURE in str(error):
        holder = ("being opened by another call" if getattr(error, "in_this_process", False)
                  else "held by another process")
        log_and_print(f"Warning: could not check points_count for '{collection}': "
                      f"{holder} ({error})", level="warning")
        return "busy"
    log_and_print(f"Warning: could not check points_count for '{collection}': "
                  f"the collection could not be opened ({error})", level="warning")
    return f"unreadable: {error}"


def get_index_status(collection: str | None = None, *, reuse_active_handle: bool = True) -> dict:
    """Snapshot of state for "does this collection have data? when was it
    last indexed? is any indexing running right now?" (section 2.4 of the
    MCP plan) — used by the griot_index_status tool, but it's a pure
    common.py function (no network I/O, only local disk) so it can be
    reused by the CLI too. 100% read-only, zero cost.

    collection: defaults to common.COLLECTION_NAME (the active embedding
    profile).

    reuse_active_handle: True (default) preserves the original
    behavior — the active collection goes through get_client(), which
    memoizes the handle for the rest of the process AND creates the
    collection from scratch if it doesn't exist yet (see get_client()).
    That's fine for a short-lived CLI call or the MCP server (which is
    already meant to hold the collection open to serve griot_search). It is
    NOT fine for a long-lived read-only caller: it
    would permanently block a concurrent `griot index` subprocess (same
    failure mode release_client() documents for the MCP server) and would
    materialize an empty collection on day 1 just from loading a status
    page. Pass False to route the active collection through the SAME
    read-only load-or-doesn't-exist path already used below for non-active
    collections — no handle memoized, nothing created."""
    collection = collection or COLLECTION_NAME

    # [review] day 1 (collection doesn't exist yet): 0 points, not an
    # error — same handling griot_search gives to a fresh install. With
    # Edge, each collection is a shard in its own directory
    # (_collection_path): the active one uses get_client()'s memoized
    # handle; another collection is opened separately, read-only, and NEVER
    # created here — checking status must not have the side effect of
    # materializing an empty collection out of nowhere.
    points_error = None
    if reuse_active_handle and collection == COLLECTION_NAME:
        # [real finding, 2026-08-20] this branch used to let the lock
        # exception propagate raw — unlike the other-collection branch just
        # below, which already handled the same error. Real scenario:
        # griot_index_repo launches an indexing subprocess; if
        # griot_index_status is called while that subprocess is still
        # writing (same active collection), the whole tool would break
        # instead of returning a best-effort result.
        try:
            points_count = get_client(wait=False).info().points_count
        except Exception as e:
            points_count, points_error = None, _points_error(collection, e)
    elif (_collection_path(collection) / _EDGE_CONFIG_MARKER).exists():
        # [review] EdgeShard.load() on a collection that another process
        # has open at this exact moment (e.g. a running indexer) raises a
        # generic runtime exception ("failed to open WAL... WouldBlock",
        # confirmed by introspection — not a specific "locked" type that
        # can be caught precisely), not something specific to "doesn't
        # exist". Without this except, checking the status of a concurrent
        # collection broke this function's "100% read-only, zero cost"
        # promise — it propagated the exception outward instead of
        # returning a dict. Best-effort: points_count comes back as None
        # when it can't be checked, instead of propagating.
        try:
            other_shard = qe.EdgeShard.load(str(_collection_path(collection)))
            try:
                points_count = other_shard.info().points_count
            finally:
                other_shard.close()
        except Exception as e:
            points_count, points_error = None, _points_error(collection, e)
    else:
        points_count = 0

    lock_status = index_lock_status()
    running = lock_status["running"]
    pid = lock_status["pid"]
    path = lock_status["path"]

    # logdb.most_recent_run_for_collection() orders by id (insertion
    # order) — same "last write wins" invariant runs.jsonl's append-only
    # format used to give for free, now backed by logs/logs.db instead.
    last_indexed = None
    record = logdb.most_recent_run_for_collection(LOG_DIR, collection)
    if record is not None:
        last_indexed = {
            "script": record.get("script"),
            "timestamp": record.get("timestamp"),
            "indexed": record.get("indexed"),
            "skipped": record.get("skipped"),
            "failed": record.get("failed"),
            # None on a normal run; set when the run DIED before it could
            # count anything (cli.py::_run_index_source). Passed through so
            # consumers can render a failure as a failure instead of as
            # "None indexed, None skipped, None failed", which reads like a
            # successful no-op — the exact illusion recording died runs
            # exists to remove.
            "error": record.get("error"),
        }

    return {
        "points_count": points_count,
        # Why points_count is None when it is: "busy" (another process holds
        # the collection, routine with `griot mcp` running) or "unreadable:
        # <reason>" (it could not be opened at all). None when there is a count.
        "points_error": points_error,
        "collection": collection,
        "embed_profile": ACTIVE_PROFILE_NAME,
        "running": running,
        "pid": pid,
        "path": path,
        "last_indexed": last_indexed,
        "spend_ceiling_exceeded": not get_spend_today() < SPEND_CEILING_USD,
    }


# What identifies ONE item of each source type, independent of how it was
# chunked. Lives here (not in golden_set.py, which owned it first) so that
# module and search()'s grouping apply the same rule — the mapping is
# knowledge about the payloads the indexers write, which is this layer's.
IDENTIFYING_FIELDS = {
    "code": ["file_path"],
    "commit": ["commit_hash"],
    "tag": ["tag_name"],
    "branch": ["branch_name"],
    "merge_request": ["mr_iid"],
    "release": ["tag_name"],
    "issue": ["issue_iid"],
}


def document_key(payload: dict) -> tuple:
    """Identifies the DOCUMENT a point belongs to — the file, commit, MR or
    release — so several chunks of one document collapse to one thing.

    Includes repo and source_type, not just the identifier: two repositories
    commonly hold a README.md, and a release and a tag share tag_name."""
    source_type = payload.get("source_type", "code")
    identifiers = tuple(payload.get(f) for f in IDENTIFYING_FIELDS.get(source_type, []))
    return (payload.get("repo"), source_type, *identifiers)


def source_label(meta: dict) -> str:
    """How a stored point is named to a person or an agent. The parts come
    from the repository (a path, a branch, a tag), and a name can hold a
    credential-shaped value just as a file can, so the label goes through the
    same replacement as the text. The stored fields themselves stay as they
    are: ids are built from them."""
    return redaction.redact(_label(meta))[0]


def _label(meta: dict) -> str:
    source_type = meta.get("source_type", "code")
    repo = meta.get("repo", "?")
    if source_type == "commit":
        return f"commit {meta.get('commit_hash', '?')[:8]} — {repo}"
    if source_type == "tag":
        return f"tag {meta.get('tag_name', '?')} — {repo}"
    if source_type == "branch":
        return f"branch {meta.get('branch_name', '?')} — {repo}"
    if source_type == "merge_request":
        return f"MR !{meta.get('mr_iid', '?')} ({meta.get('state', '?')}) — {repo}"
    if source_type == "release":
        return f"release {meta.get('tag_name', '?')} — {repo}"
    if source_type == "issue":
        return f"issue #{meta.get('issue_iid', '?')} ({meta.get('state', '?')}) — {repo}"
    return f"{repo}/{meta.get('file_path', '?')}"


# What a reader of results gets at most from ONE document when the search is
# not grouped. Unbounded, a long file took every slot: in real sessions the
# eight results of a search were often four documents, and the agent asked
# again to see what else there was. Three still lets one document answer in
# some depth (4,500 characters of it); the rest is a file the agent can open.
SEARCH_MAX_CHUNKS_PER_DOCUMENT = 3


class SearchHit:
    """One result of a diverse search: what a stored point offers a reader
    (`id`, `score`, `payload`), plus `also_in`, the labels of the other
    places where the same thing was found among the best matches."""

    __slots__ = ("id", "score", "payload", "also_in")

    def __init__(self, hit):
        self.id, self.score, self.payload = hit.id, hit.score, hit.payload or {}
        self.also_in: list[str] = []


def _diversified(hits: list, limit: int, per_document: int) -> list:
    """`hits` (best first) as a reader should get them: at most
    `per_document` from one document, and the same thing indexed in more
    than one place once, the best-placed copy naming the others.

    "The same thing" is the same text AND the same kind of thing. For code
    that is any file with that text (a copied file, a vendored directory).
    For the rest the identifier must match too: a commit stores its message
    and not its hash, so two commits that both say "fix typo" have the same
    text and are two different steps of a history; the same commit hash in
    two repositories (a fork, a mirror) is one. Compared by the text itself,
    not by `content_hash`: the hash covers what was embedded, which may
    include more than the text.

    Up to `limit` results: the store was asked for a multiple of `limit`,
    and when that whole window is chunks of a few long documents there are
    fewer slots to give. For the same reason `also_in` names the copies
    found among the best matches, not every copy in the index."""
    kept, taken, by_text = [], {}, {}
    for hit in hits:
        payload = hit.payload or {}
        text = payload.get("content") or ""
        kind = payload.get("source_type", "code")
        same_thing = (text, kind) if kind == "code" else (text, *document_key(payload)[1:])
        twin = by_text.get(same_thing) if text.strip() else None
        if twin is not None:
            label = source_label(payload)
            if label != source_label(twin.payload) and label not in twin.also_in:
                twin.also_in.append(label)
            continue
        if len(kept) >= limit:
            continue  # still reading on: a copy of something kept may come later
        key = document_key(payload)
        if taken.get(key, 0) >= per_document:
            continue
        taken[key] = taken.get(key, 0) + 1
        kept.append(SearchHit(hit))
        if text.strip():
            by_text[same_thing] = kept[-1]
    return kept


# How many points to ask the store for, per document requested, when
# grouping. The vector search runs in-process and the query embedding (the
# only paid part) happens once either way — measured at 32 points being no
# slower than 8 — so over-fetching costs nothing and a short factor would
# silently return fewer documents than asked for.
_GROUPING_OVERFETCH = 6


# The kinds of source an indexer writes as `source_type`, which is what a
# search can be narrowed to. tests/test_search_filters.py holds this list to
# what the five indexers actually write.
SOURCE_TYPES = ("code", "commit", "tag", "branch", "merge_request", "release", "issue")

# Each repository named in a filter costs one scan of the collection to make
# sure something is indexed for it (there is no payload index to ask).
SEARCH_FILTER_MAX_VALUES = 20


class SearchFilterError(ValueError):
    """A search filter that cannot be used: not a list of names, too many, a
    kind of source that does not exist, a repository with nothing indexed.
    Its own type so that a caller can tell "the arguments are wrong" from
    any other failure of a search (a ValueError still, for those who do not
    care)."""


def _filter_names(name: str, values) -> list[str]:
    """The values of one search filter, or [] for "no filter". A bare string
    is refused rather than read as a list of its letters."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SearchFilterError(f"{name} must be a list of names, not {type(values).__name__}.")
    if len(values) > SEARCH_FILTER_MAX_VALUES:
        raise SearchFilterError(f"{name} takes at most {SEARCH_FILTER_MAX_VALUES} names (got {len(values)}).")
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SearchFilterError(f"{name} must hold names (got {printable(repr(value))[:80]}).")
    return list(dict.fromkeys(values))


def repository_is_indexed(client, repo: str) -> bool:
    """Whether any point carries this repository name. One scan of the
    collection: there is no payload index to ask."""
    one = qe.Filter(must=[qe.FieldCondition(key="repo", match=qe.MatchValue(value=repo))])
    return bool(client.count(qe.CountRequest(exact=True, filter=one)))


def _checked_filters(repos, source_types) -> tuple[list[str], list[str]]:
    """Both filters as lists, after everything that can be checked without
    the index: opening a collection another process holds waits for it, and
    a filter that is wrong on its face should not wait for anything."""
    repos, source_types = _filter_names("repos", repos), _filter_names("source_types", source_types)
    unknown = [kind for kind in source_types if kind not in SOURCE_TYPES]
    if unknown:
        raise SearchFilterError(f"Unknown source type(s): {', '.join(printable(repr(k))[:80] for k in unknown)}. "
                         f"The kinds of source are: {', '.join(SOURCE_TYPES)}.")
    return repos, source_types


def _search_filter(client, repos: list[str], source_types: list[str]):
    """The store filter for a search, or None. A filter that matches nothing
    gives an empty result, and an empty result reads as "nothing was found":
    so a value that CANNOT match (a kind griot does not have, a repository
    with nothing indexed) is an error, and only a combination of values that
    do exist may come back empty.

    A name is matched against what is INDEXED under it (the `repo` every
    point carries: the directory name at the time of the run), not against
    repos.json. A point with no `source_type` at all would be left out by a
    kind filter; every indexer has always written one."""
    for repo in repos:
        if not repository_is_indexed(client, repo):
            try:
                registered = sorted({Path(p).name for p in load_repos()})
            except (OSError, ValueError):
                registered = []
            known = f" Registered repositories: {', '.join(shown(r) for r in registered)}." if registered else ""
            raise SearchFilterError(
                f"Nothing is indexed for a repository named {printable(repr(repo))[:80]} with profile "
                f"'{ACTIVE_PROFILE_NAME}'. Pass the directory name alone, spelled exactly (not a path). If the "
                f"name is right, the repository has not been indexed with this profile yet: index it first."
                f"{known}")
    must = []
    if repos:
        must.append(qe.FieldCondition(key="repo", match=qe.MatchAny(any=repos)))
    if source_types:
        must.append(qe.FieldCondition(key="source_type", match=qe.MatchAny(any=source_types)))
    return qe.Filter(must=must) if must else None


def search(query: str, limit: int = 5, group_by_document: bool = False, *,
           repos: list[str] | None = None, source_types: list[str] | None = None,
           diverse: bool = False) -> list:
    """Local search over Qdrant Edge — embeds the query with the active
    profile and queries the embedded index. Returns a list of ScoredPoint
    (.payload, .score) — shard.query() already returns the list directly
    (unlike the old qdrant-client's query_points(), which wrapped it in a
    QueryResponse object with a .points attribute).

    group_by_document collapses a document's chunks to its best-scoring one,
    making `limit` count DOCUMENTS instead of points. Off by default, and
    deliberately so: repeated hits on one file are not redundancy — they are
    different chunk_index values, 1500 chars each, overlapping by the
    chunker's 200 — so grouping trades depth for breadth rather than
    removing waste. Measured on a real index: a focused query held 4
    documents in 8 slots, and grouping surfaced 4 more at a slightly LOWER
    score than the eighth ungrouped hit. Worth it when the question is "where
    does this live", wrong when one document IS the answer.

    repos / source_types narrow the search to those repositories (the
    `repo` of a point: its directory name) and those kinds of source
    (SOURCE_TYPES); see _search_filter for what is an error and what is an
    empty result.

    diverse is for whoever READS the results (an agent, a person, the chat
    model): see _diversified. It returns SearchHit objects, up to `limit` of
    them. Off by default
    because the quality check and the golden set measure retrieval itself
    and need every point, in the store's order."""
    repos, source_types = _checked_filters(repos, source_types)
    client = get_client()
    # Before the query is embedded: on a paid profile that call costs money,
    # and a search that cannot run should not spend it.
    only = _search_filter(client, repos, source_types)
    query_vector = embed_texts([query])[0]
    if query_vector is None:
        # embed_texts() answers None for what it could not embed, which is
        # right for a batch being indexed (the rest of the batch goes on). A
        # search has one text and nothing to go on with: say what happened,
        # or the None becomes a type error from the vector store.
        raise RuntimeError(
            f"The query could not be embedded with profile '{ACTIVE_PROFILE_NAME}': "
            f"{last_embedding_failure() or 'the embedding call returned nothing'}.")
    fetch = limit * _GROUPING_OVERFETCH if (group_by_document or diverse) else limit
    hits = client.query(
        qe.QueryRequest(query=qe.Query.Nearest(query_vector, using="dense"), limit=fetch, with_payload=True,
                        filter=only)
    )
    if diverse:
        return _diversified(hits, limit, 1 if group_by_document else SEARCH_MAX_CHUNKS_PER_DOCUMENT)
    if not group_by_document:
        return hits

    best, seen = [], set()
    for hit in hits:  # already ordered by score, so the first of a document is its best
        key = document_key(hit.payload or {})
        if key in seen:
            continue
        seen.add(key)
        best.append(hit)
        if len(best) == limit:
            break
    return best


# gitlab_project_path()/gitlab_request() moved to platforms.py in
# 2026-08-13, generalized to cover GitHub/Bitbucket/Azure DevOps/Gitea
# besides GitLab (see griot.platforms — detect_platform()/fetch_*()) — not
# duplicated here, index_platform.py (formerly index_gitlab.py) is the only
# consumer now and imports from there.
