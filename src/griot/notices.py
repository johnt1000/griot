"""The conditions griot tells a person or an agent about, as data.

Each surface used to build its own sentences for the same fact: a repository
behind its HEAD was worded four ways (doctor, the stats attention list, the
stats index line, the griot_search note), keyword vectors missing five, and
the search note said "1 commits" where stats said "1 commit". Here each
condition is built once, by one function, as a Notice: an id, a severity on
doctor's scale (ok / warn / FAIL), a fingerprint (the state that matters),
one sentence and one action (a command that can be pasted, said last).
`griot doctor` and `griot stats` list every current one, always; the other
surfaces announce one only when its fingerprint is new.

How often (griot-interaction-policy, section 4), one rule on both surfaces:
the prose of a condition is said once per fingerprint, the structured field
an agent reads (`behind`) is always there.
- The CLI keeps the fingerprints in <data_dir>/announced.json (0600, the
  store of #161) and says at most one footer line on stderr, when the set of
  fingerprints changed or seven days after it last said it (footer()).
- The MCP server keeps them in memory, for the life of its process
  (ProcessMemory): an agent reads every result of its session.

Importable without the configuration (doctor loads before it): common is
imported inside the functions that need it, never here.
"""

import json
import shlex
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Decided by the maintainer: a set of notices nobody acted on is said again
# a week later, so that a person who missed the line once is not left
# without it for good.
FOOTER_REPEAT_AFTER = timedelta(days=7)

# The kinds of condition, the part of an id before ":". A caller that looked
# at a kind says so to record(), and what it did not find of that kind is gone.
BEHIND = "behind"
KEYWORD_VECTORS_MISSING = "keyword_vectors_missing"
QUALITY_OLDER_THAN_INDEX = "quality_older_than_index"
LEGACY_ENV = "legacy_env"
LEGACY_LAYOUT = "legacy_layout"

# Said before the keyword sentence when a default search fell back. Not
# "by meaning only": the sentence that follows says that already.
FELL_BACK = "This search ran in mode vector."


@dataclass(frozen=True)
class Notice:
    id: str
    severity: str
    fingerprint: str
    # Starts in lower case and has no final period: doctor and the stats
    # attention list put it inside a line ("...; " + sentence, sentence +
    # " — " + action). prose() makes it a sentence of its own.
    sentence: str
    action: str
    # What a surface needs to name the object or its own action
    # (griot_index_repo takes a path, not a name).
    repo: str | None = None
    path: str | None = None
    # How a repository is behind (behind_detail()), for the one line that
    # lists several.
    detail: str | None = None

    @property
    def kind(self) -> str:
        return self.id.split(":", 1)[0]

    def line(self) -> str:
        """The form doctor's fix arrow and the stats attention list share.
        The legacy sentences keep their own final period (several are joined
        with a space); before the dash it would read '. —'."""
        return f"{self.sentence.removesuffix('.')} — {self.action}"


def capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


def prose(notice: Notice, lead: str = "") -> str:
    """A notice as the CLI prints it on stderr: what, then the command on a
    line of its own, indented as every griot warning puts its command. An
    action of several commands, one per line, gets one indented line each,
    so that each can be pasted on its own."""
    end = "" if notice.sentence.endswith(".") else "."
    commands = "\n".join(f"  {command}" for command in notice.action.splitlines() if command)
    return f"{lead + ' ' if lead else ''}{capitalized(notice.sentence)}{end}\n{commands}"


def _printable(text: str) -> str:
    from griot import common

    return common.printable(text)


# --- the conditions ---------------------------------------------------------------------------------

def commits_band(count: int | None) -> str:
    """The band of commits behind that a fingerprint keeps: the state that
    matters is the order of magnitude, so 3 -> 4 commits is not news while
    9 -> 10 is, and a history rewritten (None: the indexed commit is not in
    it) is news whatever the count was. 0 is a repository behind only
    because tags or branches changed."""
    if count is None:
        return "rewritten"
    if count <= 0:
        return "refs"
    if count < 10:
        return "1-9"
    if count < 100:
        return "10-99"
    if count < 1000:
        return "100-999"
    return "1000+"


def behind_detail(report: dict) -> str:
    """`3 commits; tags changed`: the commits since for the sources that
    follow HEAD, and the refs sources that changed. The one wording of how a
    repository is behind (stats.behind_phrase wraps it)."""
    count = report.get("commits_behind")
    parts = []
    if count is None:
        parts.append("the indexed commit is not in this history")
    elif count:
        parts.append(f"{count} commit{'s' if count != 1 else ''}")
    changed = [source for source in report.get("behind_sources") or [] if source in ("tags", "branches")]
    if changed:
        parts.append(f"{' and '.join(changed)} changed")
    # Not reached by a report of freshness.assess() (behind is a count, a
    # rewritten history or a changed ref), but a bare "behind" would say
    # nothing about how: say what the comparison is against.
    return "; ".join(parts) or "since the last index run"


def index_repo_command(name: str) -> str:
    """`griot index all --repo NAME`, pasteable: quoted where the shell needs
    it, and glued with `=` where argparse would read the name as a flag."""
    if name.startswith("-"):
        return shlex.join(["griot", "index", "all", f"--repo={name}"])
    return shlex.join(["griot", "index", "all", "--repo", name])


INDEX_ALL = "griot index all"


def behind(report: dict) -> Notice | None:
    """A repository whose index is behind it (a report of
    freshness.assess()), or None when it is not."""
    if not report.get("behind"):
        return None
    name = _printable(report["repo"])
    # A name with a character that cannot be printed would paste, printed,
    # as another repository's name: no command names it, and the one that
    # reaches it anyway is indexing everything (search_result.catch_up_commands
    # gives it only when no other command is left).
    action = index_repo_command(name) if report["repo"].isprintable() else INDEX_ALL
    return Notice(id=f"{BEHIND}:{name}", severity=WARN, fingerprint=commits_band(report.get("commits_behind")),
                  sentence=f"the index of {name} is behind it ({behind_detail(report)})",
                  action=action, repo=name, path=report.get("path"), detail=behind_detail(report))


KEYWORD_BUILD_COMMAND = "griot index keywords"


def keyword_vectors_missing(collection: str) -> Notice:
    """A collection made before griot stored keyword vectors (debt 88: one
    wording, where there were two)."""
    return Notice(id=KEYWORD_VECTORS_MISSING, severity=WARN, fingerprint=collection,
                  sentence=(f"keyword search is not built for collection '{collection}' (indexed before griot "
                            f"stored keyword vectors): searches run by meaning only instead of the default hybrid. "
                            f"Building it is local and embeds nothing"),
                  action=KEYWORD_BUILD_COMMAND)


def keyword_fallback_text(notice: Notice) -> str:
    """What a default search that fell back says before the action: what ran,
    then the ledger sentence."""
    return f"{FELL_BACK} {capitalized(notice.sentence)}."


def quality_older_than_index(last_check_at: str | None, last_change_at: str | None,
                             now: datetime | None = None) -> Notice | None:
    """The last quality check was made before the index last changed. `ok`,
    not `warn`: nothing is broken, and a check costs money on a paid profile;
    the numbers are only about an older index. The fingerprint is the change:
    a new index change is a new state."""
    from griot import stats

    checked, changed = stats.when(last_check_at), stats.when(last_change_at)
    if checked is None or changed is None or not changed > checked:
        return None
    return Notice(id=QUALITY_OLDER_THAN_INDEX, severity=OK, fingerprint=str(last_change_at),
                  sentence=(f"the last quality check ({stats.ago(last_check_at, now)}) predates the last index "
                            f"change ({stats.ago(last_change_at, now)}): its numbers describe an older index"),
                  action="griot quality-check")


def legacy_env(renamed: list[tuple[str, str]]) -> Notice | None:
    """Variables renamed by an earlier release, still set and ignored:
    [(old, new)], as common.legacy_env_findings() gives them."""
    if not renamed:
        return None
    pairs = sorted(renamed)
    return Notice(id=LEGACY_ENV, severity=WARN, fingerprint=",".join(old for old, _ in pairs),
                  sentence=" ".join(f"{old} was renamed and griot ignores it." for old, _ in pairs),
                  action="; ".join(f'export {new}="${old}"' for old, new in pairs))


def legacy_layout(moved: list[tuple[str, str]]) -> Notice | None:
    """Files of the old layout still at the root of a checkout:
    [(old path, new path)], as common.legacy_layout_findings() gives them."""
    if not moved:
        return None
    pairs = sorted(moved)
    return Notice(id=LEGACY_LAYOUT, severity=WARN, fingerprint=",".join(old for old, _ in pairs),
                  sentence=" ".join(f"{old} is in the old layout and griot ignores it (griot never moves it on its own)."
                                    for old, _ in pairs),
                  action="; ".join(shlex.join(["mv", old, new]) for old, new in pairs))


def legacy(common) -> list[Notice]:
    """The two conditions of an earlier release, read from the environment
    and the checkout: free to ask, so every CLI command asks."""
    return [n for n in (legacy_env(common.legacy_env_findings()), legacy_layout(common.legacy_layout_findings()))
            if n is not None]


# Past this many repositories behind, the attention list says them in one
# line: one line per repository would push everything else down.
BEHIND_LINES_AT_MOST = 3


def attention_lines(found: list[Notice]) -> list[str]:
    """The stats attention items for `found`, each `sentence — action`; more
    than BEHIND_LINES_AT_MOST repositories behind are one line ending in the
    command that indexes them all."""
    behind_found = [n for n in found if n.kind == BEHIND]
    lines = []
    for notice in found:
        if notice.kind != BEHIND:
            lines.append(notice.line())
        elif len(behind_found) <= BEHIND_LINES_AT_MOST:
            lines.append(notice.line())
        elif notice is behind_found[0]:
            named = ", ".join(f"{n.repo} ({n.detail})" for n in behind_found)
            lines.append(f"the index of these repositories is behind them: {named} — {INDEX_ALL}")
    return lines


# --- the CLI's ledger: <data_dir>/announced.json --------------------------------------------------------

def _store_path():
    from griot import common

    return common.DATA_DIR / "announced.json"


# What this process last wrote, by path, for a file it could not write or
# read back (a directory where the file should be, a full disk): the command
# still says what it found, and the next one says it again.
_UNSTORED: dict[str, dict] = {}


def _load() -> dict:
    """The stored ledger. A file that cannot be read, or holds something
    else, only means it is said again: it must never stop a command.

    Read, changed and written with no lock across processes, on purpose: two
    griot commands ending at the same moment can lose one's update, and the
    cost is a notice said once more. A lock would let a stuck process hold
    every command's last line."""
    path = _store_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        state = _UNSTORED.get(str(path), {})
    except ValueError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    conditions = state.get("conditions")
    footer_state = state.get("footer")
    return {"conditions": conditions if isinstance(conditions, dict) else {},
            "footer": footer_state if isinstance(footer_state, dict) else {}}


def _save(state: dict) -> None:
    from griot import common

    path = _store_path()
    try:
        common.secure_mkdir(path.parent)
        # 0600 like the rest of the data directory: doctor flags anything
        # other users can read, and this file is griot's own.
        common.secure_write_text(path, json.dumps(state, sort_keys=True))
    except OSError:
        # Said again next time: the cost of a data directory that cannot be
        # written. Kept for the rest of this process.
        _UNSTORED[str(path)] = state


def record(kinds: set[str] | frozenset[str], found: list[Notice]) -> None:
    """What a command looked at (`kinds`) and what of it it found. A stored
    condition of a kind looked at and not found is gone; one of a kind not
    looked at stays as it was (the command could not say)."""
    state = _load()
    before = dict(state["conditions"])
    conditions = {cid: entry for cid, entry in before.items() if cid.split(":", 1)[0] not in kinds}
    for notice in found:
        conditions[notice.id] = {"fingerprint": notice.fingerprint, "severity": notice.severity}
    if conditions != before:
        state["conditions"] = conditions
        _save(state)


def stored_fingerprint(notice_id: str) -> str | None:
    entry = _load()["conditions"].get(notice_id)
    return entry.get("fingerprint") if isinstance(entry, dict) else None


def _current(conditions: dict) -> list[str]:
    """The set the footer is about: the conditions that need someone (warn
    and FAIL; an `ok` one is a note, read in doctor and stats)."""
    return sorted(f"{cid}={entry.get('fingerprint')}" for cid, entry in conditions.items()
                  if isinstance(entry, dict) and entry.get("severity") in (WARN, FAIL))


def footer_text(count: int) -> str:
    # The wording the maintainer gave in the brief of this change; the
    # backticks set the command apart on a line that is otherwise a count.
    return f"{count} notice{'s' if count != 1 else ''}: run `griot doctor`"


# The ids of the conditions this process said in prose (say_once()): the
# footer does not count them as news a second time. Kept here, not in the
# command's module: `python -m griot.cli` runs cli.py as __main__, a second
# copy of it beside the griot.cli that ask.py imports.
SPOKEN: set[str] = set()


def footer(now: datetime | None = None, spoken: set[str] | frozenset[str] | None = None) -> str | None:
    """The one line a CLI command ends with, or None. Said when the set of
    fingerprints changed since it was last said, or FOOTER_REPEAT_AFTER
    after that, and only while there is something to say. A change that only
    removed conditions is not news; neither is a new condition this command
    already said in prose (`spoken`, its ids; SPOKEN by default).

    Under --quiet nothing is said and nothing is remembered: it was not
    said. `now` is the clock, for tests."""
    from griot import common

    if common.quiet():
        return None
    spoken = SPOKEN if spoken is None else spoken
    now = now or datetime.now(timezone.utc)
    state = _load()
    current = _current(state["conditions"])
    shown = state["footer"].get("shown")
    shown = shown if isinstance(shown, list) else []
    if not current:
        if state["footer"]:
            state["footer"] = {}
            _save(state)
        return None
    try:
        at = datetime.fromisoformat(state["footer"].get("at"))
    except (TypeError, ValueError):
        at = None
    if at is not None and at.tzinfo is None:
        at = None  # edited by hand: cannot be compared with an aware clock
    new = [entry for entry in current if entry not in shown]
    unspoken = [entry for entry in new if entry.split("=", 1)[0] not in spoken]
    due = at is None or now - at >= FOOTER_REPEAT_AFTER
    if not unspoken and not (due and current == shown):
        if current != shown:
            # Removed, or said in prose: the set moves on without a line,
            # and the week is counted from the last line said.
            state["footer"] = {"shown": current, "at": state["footer"].get("at") or now.isoformat()}
            _save(state)
        return None
    state["footer"] = {"shown": current, "at": now.isoformat()}
    _save(state)
    return footer_text(len(current))


def listed(found: list[Notice], kinds: set[str] | frozenset[str], now: datetime | None = None) -> None:
    """`griot doctor` or `griot stats` listed what they found of the kinds
    they looked at (`kinds`): those are the ledger now, and the footer has
    nothing new to say about them. A kind they could not look at (doctor
    stops before the keyword check on a busy collection, say) stays stored
    and, if the footer never said it, is still news: it was not on screen.
    Only into a data directory that is already there: looking is not
    writing, and a doctor run on a new installation must leave nothing
    behind (the next command that writes says it once, in its footer)."""
    from griot import common

    if not common.DATA_DIR.is_dir():
        return
    record(kinds, found)
    state = _load()
    current = _current(state["conditions"])
    shown_before = state["footer"].get("shown")
    shown_before = shown_before if isinstance(shown_before, list) else []
    on_screen = {notice.id for notice in found}
    shown = [entry for entry in current if entry in shown_before or entry.split("=", 1)[0] in on_screen]
    state["footer"] = {"shown": shown, "at": (now or datetime.now(timezone.utc)).isoformat()} if current else {}
    _save(state)


def say_once(notice: Notice | None, kind: str, lead: str = "") -> bool:
    """For a command that met a condition while it ran (a search that fell
    back): the prose on stderr when its fingerprint is new, recorded either
    way. None records that the kind was looked at and is absent. Under
    --quiet it does nothing, so the first command that is not quiet says it.
    Returns whether it said it."""
    import sys

    from griot import common

    if common.quiet():
        return False
    if notice is None:
        record({kind}, [])
        return False
    new = stored_fingerprint(notice.id) != notice.fingerprint
    if new:
        print(prose(notice, lead), file=sys.stderr)
        SPOKEN.add(notice.id)
    record({kind}, [notice])
    return new


def said_in_result(notice: Notice | None, kind: str) -> None:
    """For a command whose result carried the condition (`griot search
    --json` puts it in its note on every run): recorded as say_once()
    records it, and counted as said, so the footer has nothing new to say
    about it. Under --quiet too: the result is not a notice, and it said it."""
    if notice is not None:
        SPOKEN.add(notice.id)
    record({kind}, [notice] if notice is not None else [])


# --- the MCP server's memory: once per fingerprint per process ---------------------------------------

class ProcessMemory:
    """The fingerprint each condition's prose was last given with, in this
    process. Tested and changed only under the lock: the SDK runs sync tools
    in worker threads, and two first searches at once must not both carry
    the note."""

    def __init__(self):
        self.given: dict[str, str] = {}
        self.lock = threading.Lock()

    def first_time(self, notice: Notice) -> bool:
        """True, and remembered, when `notice` has not been given with this
        fingerprint: the prose goes with this result."""
        with self.lock:
            if self.given.get(notice.id) == notice.fingerprint:
                return False
            self.given[notice.id] = notice.fingerprint
            return True

    def forget(self, notice_id: str) -> None:
        """The condition was looked at and is gone: said again if it returns."""
        with self.lock:
            self.given.pop(notice_id, None)

    def clear(self) -> None:
        with self.lock:
            self.given.clear()
