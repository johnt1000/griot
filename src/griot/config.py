"""`griot config` — reads and changes griot's settings without an editor.

Every setting is a variable in `<config>/.env` (see the template in
common.py). This command gives each one a name, shows the value in force and
where it comes from (the environment, the file, or the default), checks a
value before writing it, and asks a person before a change that widens what
griot may do, spend, or send a credential to, or that deletes history.

Classified by effect, like the rest of the CLI: raising a spend ceiling,
turning on indexing through MCP, widening the directories an agent may
index, pointing a platform token at another host, and shortening the log
retention (the next search then deletes older history) are answered at an
interactive terminal, with no flag that answers instead. Everything else is
written as soon as it is valid.

Loaded BEFORE the configuration (cli.py calls before_configuration_loads),
so nothing here imports common at module level: a file with a value griot
cannot start with must not be able to stop the command that repairs it.
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from griot import FALSE_WORDS, TRUE_WORDS  # the package itself: not the configuration


class _NotValid(ValueError):
    """A value that is not valid for the setting it was given to."""


@dataclass(frozen=True)
class Setting:
    name: str
    variable: str
    # amount | ceiling | count | flag | enable | choice | model | url | hosts | roots | text
    kind: str
    choices: tuple[str, ...] = ()
    # Why `set`/`unset` do not apply to it here, when they do not.
    elsewhere: str | None = None


# The chat profiles, as common.CHAT_PROFILES names them. A copy, because this
# module must work before the configuration loads; tests/test_config_command.py
# holds it to the original.
CHAT_PROFILES = ("gemini", "openai", "deepseek", "groq")

SETTINGS = [
    Setting("embed-profile", "GRIOT_EMBED_PROFILE", "text",
            elsewhere="Change it with `griot profiles use <name>`: a profile has its own index and may call an API, "
                      "and that command says what follows."),
    Setting("chat-profile", "GRIOT_CHAT_PROFILE", "choice", CHAT_PROFILES),
    Setting("chat-model", "GRIOT_CHAT_MODEL", "model"),
    Setting("openai-chat-model", "GRIOT_OPENAI_CHAT_MODEL", "model"),
    Setting("deepseek-chat-model", "GRIOT_DEEPSEEK_CHAT_MODEL", "model"),
    Setting("groq-chat-model", "GRIOT_GROQ_CHAT_MODEL", "model"),
    Setting("chat-price", "GRIOT_CHAT_PRICE_PER_1M_TOKENS", "amount"),
    Setting("openai-chat-price", "GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS", "amount"),
    Setting("deepseek-chat-price", "GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS", "amount"),
    Setting("groq-chat-price", "GRIOT_GROQ_CHAT_PRICE_PER_1M_TOKENS", "amount"),
    Setting("spend-ceiling", "GRIOT_SPEND_CEILING_USD", "ceiling"),
    Setting("spend-velocity-ceiling", "GRIOT_SPEND_VELOCITY_CEILING_USD", "ceiling"),
    Setting("max-failed-batches", "GRIOT_MAX_CONSECUTIVE_FAILED_BATCHES", "count"),
    Setting("log-questions", "GRIOT_LOG_QUESTIONS", "flag"),
    Setting("update-check", "GRIOT_UPDATE_CHECK", "flag"),
    Setting("log-retention-days", "GRIOT_LOG_RETENTION_DAYS", "count"),
    Setting("project", "GRIOT_PROJECT", "text",
            elsewhere="It is per project: set GRIOT_PROJECT in the `env` of that project's MCP server entry. In this "
                      "file it would give every project the same name."),
    Setting("mcp-index", "GRIOT_MCP_ENABLE_INDEX", "enable"),
    Setting("mcp-index-roots", "GRIOT_MCP_INDEX_ROOTS", "roots"),
    Setting("mcp-concurrency", "GRIOT_MCP_CONCURRENCY_MODE", "choice", ("multi", "single")),
    Setting("mcp-idle-release", "GRIOT_MCP_IDLE_RELEASE_SECONDS", "amount"),
    Setting("gitlab-api-base", "GRIOT_GITLAB_API_BASE", "url"),
    Setting("gitea-hosts", "GRIOT_GITEA_HOSTS", "hosts"),
]

_TRUE, _FALSE = TRUE_WORDS, FALSE_WORDS
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def find(name: str) -> Setting | None:
    """A setting by its name or by its variable, whichever was typed."""
    wanted = name.strip()
    return next((s for s in SETTINGS if wanted.lower() == s.name or wanted.upper() == s.variable), None)


def _split(value: str | None, separator: str) -> list[str]:
    return [part.strip() for part in (value or "").split(separator) if part.strip()]


# The kinds whose readers take an empty value as their default: a flag or an
# enable reads "" as neither true nor false and keeps the default, and a list
# reads it as no entries. `set` still refuses an empty value (`unset` is how
# one is cleared), but a file that holds one is a file griot starts with, and
# the template writes the two lists empty.
EMPTY_IS_DEFAULT = frozenset({"flag", "enable", "roots", "hosts"})


def normalized(setting: Setting, raw: str) -> str:
    """`raw` as it is written to the file, or _NotValid saying what a valid
    value looks like. The same rules the readers apply when griot starts:
    a value accepted here is one the next run can start with."""
    text = raw.strip()
    kind = setting.kind
    if kind in ("amount", "ceiling"):
        try:
            number = float(text)
        except ValueError:
            number = math.nan
        if not math.isfinite(number) or number < 0:
            raise _NotValid(f"{setting.name} takes a finite number, zero or more (got {raw!r}).")
        return text
    if kind == "count":
        # isascii: "²" and "٣" are digits to isdigit() and an error to int().
        if not (text.isascii() and text.isdigit()) or int(text) < 1:
            raise _NotValid(f"{setting.name} takes a whole number from 1 up (got {raw!r}).")
        return str(int(text))
    if kind in ("flag", "enable"):
        if text.lower() not in _TRUE + _FALSE:
            raise _NotValid(f"{setting.name} takes true or false (got {raw!r}).")
        return "true" if text.lower() in _TRUE else "false"
    if kind == "choice":
        if text not in setting.choices:
            raise _NotValid(f"{setting.name} takes one of: {', '.join(setting.choices)} (got {raw!r}).")
        return text
    if kind == "model":
        if not text or len(text) > 100 or not text.isprintable() or any(ch.isspace() for ch in text):
            raise _NotValid(f"{setting.name} takes a model name, one word (got {raw!r}).")
        return text
    if kind == "url":
        parts = urlsplit(text)
        host = parts.hostname
        if not host or parts.username or parts.password or parts.query or parts.fragment or not text.isprintable():
            raise _NotValid(f"{setting.name} takes the base URL of the API, such as https://gitlab.example.com/api/v4, "
                            f"with no credentials in it (got {raw!r}).")
        if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOCAL_HOSTS):
            raise _NotValid(f"{setting.name} takes an https URL: the platform token is sent to it (got {raw!r}).")
        return text.rstrip("/")
    if kind == "hosts":
        # As the reader compares them (platforms._remote_host): the host of
        # a remote, in lower case and without a port. Written any other way
        # a host would be accepted here and never match anything.
        hosts = [host.lower() for host in _split(text, ",")]
        if any(re.fullmatch(r"[^:]+:\d+", host) for host in hosts):
            raise _NotValid(f"{setting.name} takes host names without a port: a remote is matched by its host "
                            f"alone (got {raw!r}).")
        if not hosts or not all(_HOST.fullmatch(host) for host in hosts):
            raise _NotValid(f"{setting.name} takes host names separated by commas, such as git.example.com "
                            f"(no scheme, no path; got {raw!r}).")
        return ",".join(dict.fromkeys(hosts))
    if kind == "roots":
        roots = []
        for part in _split(text, ":"):
            path = os.path.abspath(os.path.expanduser(part))
            if not os.path.isabs(os.path.expanduser(part)):
                raise _NotValid(f"{setting.name} takes absolute directories separated by ':' ({part!r} is relative).")
            if path == os.path.abspath(os.sep):
                raise _NotValid(f"{setting.name} cannot be the whole filesystem ({part!r}).")
            if not os.path.isdir(path):
                raise _NotValid(f"{setting.name}: {part!r} is not a directory.")
            roots.append(path)
        if not roots:
            raise _NotValid(f"{setting.name} takes absolute directories separated by ':' (got {raw!r}).")
        return ":".join(dict.fromkeys(roots))
    raise _NotValid(f"{setting.name} is not changed with this command.")


# --- before the configuration loads ---------------------------------------------------------

# By kind: a value every reader accepts, for the kinds whose readers refuse
# to start on a bad one (see before_configuration_loads, `unset`).
_STAND_IN = {"amount": "0", "ceiling": "0", "count": "1", "choice": "first choice"}

# What the REAL environment said for each setting, taken before common.py
# loaded the file into the process (and before `set` put its value in force,
# see below). Without it, a value that only came from the file is still in
# os.environ after the line is removed, and reads as "exported". Empty when
# main() is called without going through the CLI: then os.environ is asked.
_EXPORTED: dict[str, str | None] = {}


def before_configuration_loads(argv: list[str]) -> None:
    """For `set <name> <value>` with a valid value: puts that value in force
    for this process BEFORE common.py reads the file. The file is loaded
    without overriding the environment, so a value in it that griot cannot
    start with (a ceiling that is not a number, a profile that no longer
    exists) is shadowed, and the command that repairs it can run. A value
    that is not valid is left for main() to refuse."""
    _EXPORTED.clear()
    _EXPORTED.update({setting.variable: os.environ.get(setting.variable) for setting in SETTINGS})
    if len(argv) < 2 or argv[0] not in ("set", "unset"):
        return
    setting = find(argv[1])
    if setting is None or setting.elsewhere:
        return
    if argv[0] == "unset":
        # Removing the line is the other repair. What stands in for the bad
        # value while this process loads is only something the readers
        # accept; nothing here spends, searches or indexes with it.
        stand_in = _STAND_IN.get(setting.kind)
        if stand_in is not None:
            os.environ[setting.variable] = setting.choices[0] if stand_in == "first choice" else stand_in
        return
    if len(argv) < 3:
        return
    try:
        value = normalized(setting, argv[2])
    except _NotValid:
        return
    os.environ[setting.variable] = value


# --- what is in force -----------------------------------------------------------------------


def _template() -> dict[str, tuple[str | None, str]]:
    from griot import common

    return {variable: (default or None, description)
            for variable, default, description, _ in common.ENV_TEMPLATE_SETTINGS}


def default_of(setting: Setting) -> str | None:
    return _template().get(setting.variable, (None, ""))[0]


def _in_file(setting: Setting) -> str | None:
    from dotenv import dotenv_values

    from griot import common

    if not common.ENV_PATH.exists():
        return None
    # "" for a line that is there and empty (`VAR=`), which is not the same
    # as no line: griot reads it as an empty value, not as the default.
    return dotenv_values(common.ENV_PATH).get(setting.variable)


def _exported(setting: Setting) -> str | None:
    """The variable as the real environment has it, when it differs from the
    file: that is the value in force, whatever the file says."""
    in_environment = (_EXPORTED[setting.variable] if setting.variable in _EXPORTED
                      else os.environ.get(setting.variable)) or None
    return in_environment if in_environment != _in_file(setting) else None


def in_force(setting: Setting) -> tuple[str | None, str]:
    """(value, where it comes from: environment | file | default)."""
    exported = _exported(setting)
    if exported is not None:
        return exported, "environment"
    in_file = _in_file(setting)
    if in_file is not None:
        return in_file, "file"
    return default_of(setting), "default"


def running_with(setting: Setting) -> dict:
    """What THIS process started with, for one that outlives edits to the
    file (the MCP server): {value, source, in_file, restart_needed}.

    in_force() answers for a process that has just read the file. A server
    read it once: the value it runs with is the one in its environment
    (exported where it was started, or put there by the file at that
    moment), and the file may say something else by now. `restart_needed`
    is that difference, when a restart would apply it: under an exported
    variable the file's value is not used before or after."""
    from griot import common

    in_file = _in_file(setting)
    default = default_of(setting)
    if setting.variable in common.ENVIRONMENT_BEFORE_ENV_FILE:
        # Present, even empty: the file does not override what is there.
        value, source = common.ENVIRONMENT_BEFORE_ENV_FILE[setting.variable], "environment"
    elif setting.variable in os.environ:
        value, source = os.environ[setting.variable], "file"
    else:
        value, source = default, "default"
    a_new_process = in_file if in_file is not None else default
    ignored = common.ENVIRONMENT_IGNORED.get(setting.variable)
    # An empty line and no line are the same thing when the default is empty
    # too (`GRIOT_PROJECT=`): "" and None are compared as one.
    return {"value": value, "source": source, "in_file": in_file,
            "restart_needed": source != "environment" and (a_new_process or None) != (value or None),
            "environment_ignored": ignored_sentence(setting.variable, ignored) if ignored else None}


def persisted(setting: Setting) -> str | None:
    """What stays when the shell is gone: the file's value, else the default.
    A widening change is measured against THIS, not against what is in
    force: the environment wins while it is set, but it is the caller's own,
    and measured against it, exporting a wide value first got a wide value
    written to the file with no question."""
    in_file = _in_file(setting)
    return in_file if in_file else default_of(setting)


def _amount(text: str | None) -> float | None:
    """A finite number, or None for anything else (a value griot could not
    have started with)."""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# --- a change that widens something ---------------------------------------------------------


# Flags on by default that a person turns off to keep something from leaving
# their hands (question text into the log, a request to PyPI): a server's
# environment may turn one off, never back on over the file. Each maps to why
# the environment's "on" was not taken.
_OFF_STAYS_OFF = {
    "GRIOT_LOG_QUESTIONS": "the file turns the logging of questions off",
    "GRIOT_UPDATE_CHECK": "the file turns the check with PyPI for a newer release off",
}


def measured_from_environment(setting: Setting, raw: str) -> tuple[str | None, str]:
    """For a server: (why `raw`, the value its environment gives `setting`,
    is not taken, or None when it may be; the value in the form it was
    measured in).

    It may be taken when it widens nothing next to what the person's own
    file says (or the default, where the file is silent): the measure
    `griot config set` asks a person about, for the same reason. Each of
    these lets griot spend more, lets an agent do more, or sends something
    somewhere else.

    The second part matters as much as the first. A value measured in one
    form and left in the environment in another is read by the code that
    uses it in its own way: `~/code` was measured expanded and read as a
    folder named `~`, and `work/link/..` was measured as `work` and read
    as wherever the link leads, one level up. What a server obeys is what
    was measured, so the caller puts THIS form in force.

    Called while common.py is loading (see ENVIRONMENT_ONLY_NARROWS), so
    only what common has defined by then is used."""
    from griot import common

    old = persisted(setting)
    if setting.variable == "GRIOT_EMBED_PROFILE":
        name = raw.strip()
        chosen = common.EMBED_PROFILES.get(name)
        if chosen is None:
            return None, raw  # refused where profiles are read, with the list of the ones there are
        if name == (old or "").strip() or chosen["backend"] == "local":
            return None, name  # the person's own choice, or nothing leaves the machine
        return "that profile sends what is indexed and searched to an API", raw
    if setting.kind not in ("ceiling", "enable", "roots", "url", "hosts", "count") \
            and setting.variable not in _OFF_STAYS_OFF:
        return None, raw
    try:
        return _measured(setting, raw, old)
    except ValueError:
        # _NotValid is one, and so is whatever else a value nobody thought of
        # raises on the way: what cannot be measured is not known to be narrow.
        return "it is not a value this setting takes", raw


def _measured(setting: Setting, raw: str, old: str | None) -> tuple[str | None, str]:
    new = normalized(setting, raw)
    if setting.kind == "roots":
        # As the code that uses them finds them: `~` expanded, links
        # followed, and only then `..` (normalized() took `..` out as
        # text, which is what the file gets and is then read as such).
        new = ":".join(dict.fromkeys(os.path.realpath(os.path.expanduser(part)) for part in _split(raw, ":")))
    try:
        # As the readers take it ("yes" in the file is on).
        old = normalized(setting, old) if old else old
    except _NotValid:
        pass  # measured against what the file says, as it says it
    if setting.variable in _OFF_STAYS_OFF:
        turned_off = (old or "").strip().lower() in _FALSE
        return (_OFF_STAYS_OFF[setting.variable] if turned_off and new == "true" else None), new
    if setting.kind == "count":
        before = old if old and old.isascii() and old.isdigit() else default_of(setting)
        if setting.variable == "GRIOT_LOG_RETENTION_DAYS":
            # The other way round: a shorter window deletes history, of every
            # project, and a project's .mcp.json is not the person's own file.
            return ("it deletes usage history your configuration keeps" if int(new) < int(before or 0) else None), new
        return ("it lets an index run go on failing for longer" if int(new) > int(before or 0) else None), new
    if setting.kind == "url":
        # Any host but the one in force: back to the default is another host
        # too, when the file names your own.
        return ("it sends the GitLab token to another host" if new != (old or default_of(setting)) else None), new
    if _question(setting, old, new) is None:
        return None, new
    return {"ceiling": "it raises a spend ceiling", "enable": "it turns on indexing through MCP",
            "roots": "it adds a directory an agent may index",
            "hosts": "it sends the Gitea token to another host"}[setting.kind], new


def ignored_sentence(variable: str, ignored: dict) -> str:
    """What a person reads about a value the server did not take."""
    setting = next(s for s in SETTINGS if s.variable == variable)
    how = (f"`griot profiles use <name>`" if variable == "GRIOT_EMBED_PROFILE"
           else f"`griot config set {setting.name} <value>`")
    return (f"{variable} in the environment this server was started with was ignored: {ignored['reason']}, and a "
            f"server takes from its environment only what narrows your own configuration. To set it for real, run "
            f"{how} in a terminal.")


def _question(setting: Setting, old: str | None, new: str | None) -> str | None:
    """What to ask a person before `setting` goes from `old` to `new`, or
    None when the change widens or deletes nothing. Asked with no flag that
    answers: each of these lets griot spend more, lets an agent do more,
    sends a credential somewhere else, or deletes usage history."""
    if new is None or new == old:
        return None
    if setting.kind == "ceiling":
        # When what is in the file is not a number griot could not even
        # start, and this is the repair: measured against the default then,
        # so that back to it or below is not a raise.
        before = _amount(old)
        if before is None:
            before = _amount(default_of(setting)) or 0.0
        raised = float(new) > before
        window = "in any 5 minutes" if "VELOCITY" in setting.variable else "in a day"
        return (f"Raise {setting.name} from {old} to {new}? Up to ${new} can then be spent {window} before griot "
                f"stops paid calls.") if raised else None
    if setting.kind == "enable":
        if (old or "").lower() in ("1", "true"):
            return None  # already on, as the reader takes it (mcp_server: `in ("1", "true")`)
        return ("Turn on indexing through the MCP server? An agent can then start an index run of a registered "
                "repository, or of a directory under mcp-index-roots: on a paid profile that sends its content to "
                "the embedding API and is billed.") if new == "true" else None
    if setting.kind == "roots":
        had = [os.path.realpath(os.path.expanduser(root)) for root in _split(old, ":")]
        # A directory inside one already allowed widens nothing; compared
        # resolved, as the reader does (jobs.py).
        added = [root for root in _split(new, ":")
                 if not any(os.path.commonpath([os.path.realpath(root), parent]) == parent for parent in had)]
        return (f"Let the MCP indexing tool index anything under {', '.join(added)}? An agent can then have what is "
                f"in there embedded, which on a paid profile sends it to the embedding API.") if added else None
    if setting.variable == "GRIOT_LOG_RETENTION_DAYS":
        return _shorter_retention_question(setting, old, new)
    if setting.kind == "url":
        if new == default_of(setting):
            return None
        return (f"Send GitLab API calls to {new}? The GitLab token griot holds is sent to that address with "
                f"every call.")
    if setting.kind == "hosts":
        added = [host for host in _split(new, ",") if host not in _split(old, ",")]
        return (f"Treat {', '.join(added)} as Gitea/Forgejo? The Gitea token griot holds is sent there when a "
                f"repository's remote points to it.") if added else None
    return None


def _shorter_retention_question(setting: Setting, old: str | None, new: str) -> str | None:
    """A shorter log retention is the one change here that destroys rather
    than widens: the next search or tool call of ANY project deletes every
    older search and tool call from logs.db, for good. So it is asked about
    like a raised ceiling, with how many rows that prune takes counted now,
    read-only. Nothing is pruned here: the prune stays the next write's."""
    from griot import common, logdb

    # What persists, as _measured() reads it: a file value griot could not
    # start with is measured against the default it repairs to.
    before = old if old and old.isascii() and old.isdigit() else default_of(setting)
    if int(new) >= int(before or 0):
        return None
    try:
        counted = logdb.count_older_than(common.LOG_DIR, int(new))
    except (OSError, sqlite3.Error) as e:
        # Not knowing how many is no reason to skip the question.
        what = f"every search and MCP tool call older than {new} days (griot could not count them: {e})"
    else:
        searches, calls = counted["queries"], counted["tool_calls"]
        what = (f"{searches} {'search' if searches == 1 else 'searches'} and {calls} MCP tool "
                f"{'call' if calls == 1 else 'calls'} older than {new} days")
    return (f"Shorten {setting.name} from {before} to {new} days? The next search or MCP tool call, from any "
            f"project, deletes {what} from logs.db, and they cannot be recovered.")


# --- commands -------------------------------------------------------------------------------


def _unknown(name: str) -> int:
    from griot import common

    print(f"Error: no setting named {common.printable(name)[:80]!r}. The settings are: "
          f"{', '.join(s.name for s in SETTINGS)}.", file=sys.stderr)
    return 2


def _after_a_change(setting: Setting) -> None:
    from griot import common

    print("  It applies to griot processes started from now on. An MCP server that is already running keeps the "
          "value it started with: restart open sessions.")
    exported = _exported(setting)
    if exported is not None:
        print(f"  {setting.variable} is also set in the environment (to '{common.printable(exported)[:80]}'), and "
              f"the environment wins over the file: this shell, and what is started from it, keeps that value "
              f"until the variable is unset.")


def cmd_list(as_json: bool) -> int:
    from griot import common

    rows = []
    for setting in SETTINGS:
        value, source = in_force(setting)
        rows.append({"name": setting.name, "variable": setting.variable, "value": value, "source": source,
                     "default": default_of(setting), "description": _template().get(setting.variable, (None, ""))[1]})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    print(f"griot settings — {common.ENV_PATH}\n")
    for row in rows:
        value = "(not set)" if row["value"] is None else common.printable(row["value"]) or "(empty)"
        print(f"  {row['name']:<24}{value[:38]:<40}{row['source']:<13}{row['variable']}")
    print("\n  griot config set <name> <value>   ·   griot config unset <name>   ·   griot config get <name>")
    return 0


def cmd_get(name: str) -> int:
    setting = find(name)
    if setting is None:
        return _unknown(name)
    value, _ = in_force(setting)
    print(value if value is not None else "")
    return 0


def cmd_set(name: str, raw: str) -> int:
    from griot import auth, common

    setting = find(name)
    if setting is None:
        return _unknown(name)
    if setting.elsewhere:
        print(f"Error: {setting.name} is not set with this command. {setting.elsewhere}", file=sys.stderr)
        return 2
    try:
        value = normalized(setting, raw)
    except _NotValid as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    old = persisted(setting)
    if _in_file(setting) == value:
        print(f"{setting.name} is already {value} in {common.ENV_PATH}.")
        if _exported(setting) is not None:
            _after_a_change(setting)
        return 0
    question = _question(setting, old, value)
    if question:
        refused = common.confirm(question, yes=None)
        if refused:
            return refused
    common.env_file_set(setting.variable, value)
    was = f" (was {old})" if old is not None else ""
    print(f"{setting.name} = {value}{was}. Written to {common.ENV_PATH}.")

    if setting.name == "chat-profile":
        price = find(f"{value}-chat-price")
        if price is not None and in_force(price)[0] is None and value != "groq":
            print(f"  `griot ask` refuses to run with it until its price is set: griot config set {price.name} "
                  f"<USD per 1M tokens>")
        credential = common.CHAT_PROFILES[value].get("api_key_env") or ("GEMINI_TOKEN" if value == "gemini" else None)
        if credential and not os.getenv(credential):
            provider = auth._provider_label(credential) if credential.startswith("GRIOT_") else value
            print(f"  It needs a credential that is not set ({credential}): griot auth set {provider}")
    _after_a_change(setting)
    return 0


def cmd_unset(name: str) -> int:
    from griot import common

    setting = find(name)
    if setting is None:
        return _unknown(name)
    if setting.elsewhere:
        print(f"Error: {setting.name} is not changed with this command. {setting.elsewhere}", file=sys.stderr)
        return 2
    default = default_of(setting)
    if _in_file(setting) is None:
        print(f"{setting.name} is not set in {common.ENV_PATH}: the default applies"
              f"{' (' + default + ')' if default else ''}.")
        return 0
    old = persisted(setting)
    # Going back to the default can be the widening change: a ceiling
    # lowered to 1 and then unset is a ceiling raised to 3.
    question = _question(setting, old, default)
    if question:
        refused = common.confirm(question, yes=None)
        if refused:
            return refused
    common.env_file_unset(setting.variable)
    print(f"{setting.name} removed from {common.ENV_PATH}: back to the default"
          f"{' (' + default + ')' if default else ' (not set)'}.")
    _after_a_change(setting)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot config",
        description="Shows and changes griot's settings (the variables in <config>/.env). A change that widens "
                    "what griot may spend, what an agent may index, or where a platform token is sent, or that "
                    "shortens how long usage history is kept, is asked about at an interactive terminal; there is "
                    "no flag that answers.",
        # Here, and not only in `list`: with a file griot cannot start with,
        # `list` cannot run, and the names are what the repair needs.
        epilog="settings: " + ", ".join(setting.name for setting in SETTINGS)
               + ". A setting can also be named by its variable (GRIOT_...).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    p_list = sub.add_parser("list", help="Every setting, the value in force and where it comes from")
    p_list.add_argument("--json", action="store_true", help="One JSON document instead of the table")
    p_get = sub.add_parser("get", help="Prints the value in force of one setting")
    p_get.add_argument("name")
    p_set = sub.add_parser(
        "set", help="Checks a value and writes it to the config .env",
        description="Checks a value and writes it to the config .env. A change that raises a spend ceiling, turns "
                    "on indexing through MCP, adds a directory an agent may index, points a platform token at "
                    "another host, or shortens log-retention-days (saying how many searches and tool calls the "
                    "next prune deletes) is asked about at an interactive terminal, and there is no flag that "
                    "answers: "
                    "run from a script or an agent's shell, with no terminal, it changes nothing.")
    p_set.add_argument("name")
    p_set.add_argument("value")
    p_unset = sub.add_parser("unset", help="Removes a setting from the config .env, back to its default")
    p_unset.add_argument("name")
    args = parser.parse_args(argv)

    if not _EXPORTED:
        # Not through the CLI (a test, another caller): the configuration is
        # already loaded and with it the file's values, which sit in
        # os.environ next to anything really exported. Taken apart here,
        # before anything is written: what equals the file's value came from
        # the file.
        for setting in SETTINGS:
            in_environment = os.environ.get(setting.variable)
            _EXPORTED[setting.variable] = in_environment if in_environment != _in_file(setting) else None
    try:
        if args.action == "list":
            return cmd_list(args.json)
        if args.action == "get":
            return cmd_get(args.name)
        if args.action == "set":
            return cmd_set(args.name, args.value)
        return cmd_unset(args.name)
    finally:
        _EXPORTED.clear()  # it describes one start of the process, not the next caller of main()


if __name__ == "__main__":
    raise SystemExit(main())
