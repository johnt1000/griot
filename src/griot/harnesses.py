"""`griot assist install` — copies griot's bundled Skill/Agent files (shipped
inside the package under resources/{skills,agents/<harness-id>}/) into
whichever supported AI coding harness(es) are present on the machine —
Claude Code and/or opencode today, more may be added later by appending to
HARNESSES — local (<cwd>/.<harness>) or global (per-harness home location),
so users get griot-specific onboarding/usage help wherever they use griot.

Skills are a single shared file per skill, reused across harnesses (both
Claude Code and opencode read a `skills/<name>/SKILL.md` layout). Agents use
a DIFFERENT frontmatter schema per harness (Claude Code: name/description/
tools/model; opencode: description/mode/permission), so agent content lives
in a separate resources/agents/<harness.id>/ subdirectory per harness —
agent_content_subdir picks the right one.

Deliberately does NOT import griot.common when it is loaded: that module
pulls in qdrant_edge and reads the configuration (see cli.py's docstring on
lazy dispatch), for a command that mostly copies a handful of text files.
The two steps that need it import it where they run: the note about the
concurrency mode when registering the MCP server, and the list of read-only
tools when offering to pre-approve them.
"""

import argparse
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Callable


def _resources_root() -> Path:
    """Resolved via importlib.resources so this works both for an editable
    install (`pip install -e .`) and a real installed wheel — Path(__file__)
    would break once resources/ moves outside the source tree layout."""
    return Path(str(importlib_resources.files("griot") / "resources"))


@dataclass(frozen=True)
class Harness:
    id: str
    display_name: str
    local_skills_dir: Callable[[Path], Path]
    local_agents_dir: Callable[[Path], Path]
    global_skills_dir: Callable[[Path], Path]
    global_agents_dir: Callable[[Path], Path]
    agent_content_subdir: str
    detect: Callable[[], bool]
    # The harness's GLOBAL instructions file and the bundled text griot offers to
    # put in it (resources/instructions/<name>). None where the file is not known.
    global_instructions_file: Callable[[Path], Path] | None = None
    instructions_resource: str | None = None
    # The harness's OWN command that registers an MCP server, given the scope
    # ("user" or "local") and the command that starts griot. None where griot
    # does not know one: then the person is told what to add, and nothing runs.
    mcp_register: Callable[[str, str], list[str]] | None = None
    # And the one that undoes exactly that, given the same scope.
    mcp_unregister: Callable[[str], list[str]] | None = None
    # Where the harness keeps the tools that may run without asking: the
    # user's file (given the home) and a project's PERSONAL one (given the
    # project), and how it names one of griot's tools in a rule. None where
    # griot does not know the format: then nothing is offered.
    global_settings_file: Callable[[Path], Path] | None = None
    local_settings_file: Callable[[Path], Path] | None = None
    tool_rule: Callable[[str], str] | None = None


HARNESSES = [
    Harness(
        id="claude-code",
        display_name="Claude Code",
        local_skills_dir=lambda cwd: cwd / ".claude" / "skills",
        local_agents_dir=lambda cwd: cwd / ".claude" / "agents",
        global_skills_dir=lambda home: home / ".claude" / "skills",
        global_agents_dir=lambda home: home / ".claude" / "agents",
        agent_content_subdir="claude-code",
        detect=lambda: shutil.which("claude") is not None or (Path.home() / ".claude").is_dir(),
        global_instructions_file=lambda home: home / ".claude" / "CLAUDE.md",
        instructions_resource="claude-code.md",
        mcp_register=lambda scope, griot: ["claude", "mcp", "add", "--scope", scope, "griot", "--", griot, "mcp"],
        # With the scope: once a server is registered in two scopes, a remove without one is refused.
        mcp_unregister=lambda scope: ["claude", "mcp", "remove", "--scope", scope, "griot"],
        global_settings_file=lambda home: home / ".claude" / "settings.json",
        # settings.local.json, not settings.json: the latter is the one a team commits.
        local_settings_file=lambda cwd: cwd / ".claude" / "settings.local.json",
        # mcp__<server>__<tool>, for a server registered under the name "griot".
        tool_rule=lambda tool: f"mcp__griot__{tool}",
    ),
    Harness(
        id="opencode",
        display_name="opencode",
        local_skills_dir=lambda cwd: cwd / ".opencode" / "skills",
        local_agents_dir=lambda cwd: cwd / ".opencode" / "agents",
        global_skills_dir=lambda home: home / ".config" / "opencode" / "skills",
        global_agents_dir=lambda home: home / ".config" / "opencode" / "agents",
        agent_content_subdir="opencode",
        detect=lambda: shutil.which("opencode") is not None or (Path.home() / ".config" / "opencode").is_dir(),
    ),
]


def detect_harnesses(candidates: list[Harness] | None = None) -> list[Harness]:
    return [h for h in (HARNESSES if candidates is None else candidates) if h.detect()]


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


class UnsafeDestination(OSError):
    """An install destination that would make griot write somewhere other
    than the place it names."""


def _destination_problem(dest: Path, base: Path, contained: bool) -> str | None:
    """Why `dest` must not be written, or None. The destination is inside a
    directory griot does not own: in local scope it is a project, and a
    repository can ship `.claude/skills/<name>/SKILL.md` as a link to any
    file the user can write, or `.claude` as a link to any directory.

    The file itself is never a link, in either scope. A directory on the way
    may be one as long as the write still lands inside `base`; that is only
    required in local scope (`contained`), because where `~/.claude` points
    is the user's own layout (a dotfiles checkout, another volume)."""
    if dest.is_symlink():
        return (f"{dest} is a symbolic link, and griot does not write through one. "
                f"Remove it, then run the install again.")
    if dest.exists() and not dest.is_file():
        return f"{dest} exists and is not a regular file."
    if contained:
        real_base, real_parent = os.path.realpath(base), os.path.realpath(dest.parent)
        if os.path.commonpath([real_base, real_parent]) != real_base:
            return (f"{dest.parent} leads outside {base} (to {real_parent}), through a symbolic link. "
                    f"Remove the link, then run the install again.")
    return None


def _write_file(dest: Path, content: bytes) -> str:
    """Writes `content` to `dest` (mode 0600), returns which of
    created/updated/unchanged it was. Leaves an unchanged file untouched
    (no rewrite, no chmod) — see SECURITY.md's 0600/0700 convention."""
    if dest.is_symlink():
        raise UnsafeDestination(f"{dest} is a symbolic link, and griot does not write through one.")
    if dest.exists() and dest.read_bytes() == content:
        return "unchanged"
    status = "updated" if dest.exists() else "created"
    _secure_mkdir(dest.parent)
    # Written beside the destination and moved over it, never INTO what is
    # there: an existing file may share its inode with another name (a hard
    # link, which no check can tell from an ordinary file), and the rename
    # replaces the name instead of the content behind it. mkstemp creates the
    # file itself (0600, exclusive), so nothing planted is opened.
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.griot-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return status


def _tree(src_root: Path, dest_root: Path) -> list[tuple[Path, Path, str]]:
    """(source file, destination, relative name) for every bundled file."""
    if not src_root.is_dir():
        return []
    return [(src, dest_root / src.relative_to(src_root), str(src.relative_to(src_root)))
            for src in sorted(p for p in src_root.rglob("*") if p.is_file())]


def _plan(harness: Harness, scope: str, home: Path | None, cwd: Path | None):
    if scope not in ("local", "global"):
        raise ValueError(f"scope must be 'local' or 'global', got {scope!r}")
    if scope == "local":
        base = cwd or Path.cwd()
        skills_target = harness.local_skills_dir(base)
        agents_target = harness.local_agents_dir(base)
    else:
        base = home or Path.home()
        skills_target = harness.global_skills_dir(base)
        agents_target = harness.global_agents_dir(base)
    root = _resources_root()
    files = (_tree(root / "skills", skills_target)
             + _tree(root / "agents" / harness.agent_content_subdir, agents_target))
    return base, skills_target, agents_target, files


def install_refusal(harness_list: list[Harness], scope: str, *, home: Path | None = None,
                    cwd: Path | None = None) -> str | None:
    """Why this install must not happen, or None. Looks at EVERY destination
    of every harness and writes nothing, so that a caller can refuse before
    the first file (and the MCP tool before asking a person)."""
    for harness in harness_list:
        base, _, _, files = _plan(harness, scope, home, cwd)
        for _, dest, _ in files:
            problem = _destination_problem(dest, base, contained=scope == "local")
            if problem:
                return problem
    return None


def install(harness: Harness, scope: str, *, home: Path | None = None, cwd: Path | None = None) -> dict:
    base, skills_target, agents_target, files = _plan(harness, scope, home, cwd)
    refusal = install_refusal([harness], scope, home=home, cwd=cwd)
    if refusal:
        raise UnsafeDestination(refusal)

    created, updated, unchanged = [], [], []
    buckets = {"created": created, "updated": updated, "unchanged": unchanged}
    for src, dest, rel in files:
        buckets[_write_file(dest, src.read_bytes())].append(rel)

    return {
        "harness": harness.id,
        "scope": scope,
        "skills_target": str(skills_target),
        "agents_target": str(agents_target),
        "created": sorted(created),
        "updated": sorted(updated),
        "unchanged": sorted(unchanged),
    }


def install_many(harness_list: list[Harness], scope: str, *, home: Path | None = None, cwd: Path | None = None) -> list[dict]:
    # All of them are checked before the first file of any: a refusal for the
    # second harness must not leave the first one installed.
    refusal = install_refusal(harness_list, scope, home=home, cwd=cwd)
    if refusal:
        raise UnsafeDestination(refusal)
    return [install(h, scope, home=home, cwd=cwd) for h in harness_list]


# --- the global instructions block ---------------------------------------------------
# A block in the harness's GLOBAL instructions file tells every agent in every project
# when to use griot. It is text a future session loads and follows everywhere, so it
# is written only after a person answers a question in a terminal. install() and
# install_many(), which the MCP tool calls, never touch it, and there is deliberately
# no flag that answers the question.

BLOCK_BEGIN = "<!-- griot:begin (managed by `griot assist install`; delete this block to remove it) -->"
BLOCK_END = "<!-- griot:end -->"
_BEGIN_PREFIX = "<!-- griot:begin"


def instructions_block(harness: Harness) -> str | None:
    """The bundled text wrapped in markers, or None when the harness has none."""
    if harness.instructions_resource is None:
        return None
    text = (_resources_root() / "instructions" / harness.instructions_resource).read_text(encoding="utf-8").strip()
    return f"{BLOCK_BEGIN}\n{text}\n{BLOCK_END}"


def _read_text(real: Path) -> str | None:
    """The file as UTF-8 text, or None when it cannot be read as such. newline=""
    keeps every line ending exactly as it is: read_text() would turn CRLF into LF
    and the write would then change every line of the user's file."""
    try:
        with open(real, encoding="utf-8", newline="") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None


def _locate(lines: list[str]) -> tuple[int, int] | None | str:
    """(begin, end) line indexes of THE block, None when there is none, or
    "malformed" for anything that is not exactly one begin followed by one end.
    Markers are recognised wherever they start a line, so a file that QUOTES them
    at column 0 inside a code fence is read as holding the block: with one quoted
    marker that is "malformed" and refused, with a quoted pair it would be replaced."""
    begins = [i for i, line in enumerate(lines) if line.startswith(_BEGIN_PREFIX)]
    ends = [i for i, line in enumerate(lines) if line.strip() == BLOCK_END]
    if not begins and not ends:
        return None
    if len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        return begins[0], ends[0]
    return "malformed"


def instructions_state(path: Path, block: str) -> str:
    """missing_file | absent | current | outdated | malformed."""
    real = Path(os.path.realpath(path))
    if not real.exists():
        return "missing_file"
    text = _read_text(real)
    if text is None:
        return "malformed"
    lines = text.split("\n")
    where = _locate(lines)
    if where is None:
        return "absent"
    if where == "malformed":
        return "malformed"
    begin, end = where
    region = "\n".join(line.rstrip("\r") for line in lines[begin:end + 1])  # a CRLF block is the same block
    return "current" if region == block else "outdated"


def _atomic_write(real: Path, content: str) -> None:
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.griot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if real.exists():
            shutil.copymode(real, tmp)  # it is the user's file: keep its mode, do not force 0600
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def apply_instructions(path: Path, block: str) -> str:
    """Puts `block` in the file: created | added | updated | unchanged. Text
    outside the markers is never touched, a symlink is written through (it is
    often a link into a dotfiles repository), and a file whose markers are
    damaged raises ValueError before anything is written. Line endings are the
    file's own: a CRLF file stays CRLF. Not safe against another editor saving the
    same file between the check and the write; this is a per-user interactive
    command, so that window is accepted rather than locked."""
    state = instructions_state(path, block)
    if state == "malformed":
        raise ValueError(f"{path}: the griot markers are damaged or the file is not UTF-8 text; refusing to edit it.")
    if state == "current":
        return "unchanged"
    real = Path(os.path.realpath(path))
    text = _read_text(real) if real.exists() else ""
    eol = "\r\n" if "\r\n" in text else "\n"
    if state == "outdated":
        lines = text.split("\n")  # a CRLF file keeps its "\r" at the end of each line
        begin, end = _locate(lines)
        cr = "\r" if lines[end].endswith("\r") else ""
        new = "\n".join(lines[:begin] + [line + cr for line in block.split("\n")] + lines[end + 1:])
        outcome = "updated"
    else:
        if not text or text.endswith(("\n\n", "\r\n\r\n")):
            sep = ""
        elif text.endswith("\n"):
            sep = eol
        else:
            sep = eol * 2
        new = text + sep + block.replace("\n", eol) + eol
        outcome = "created" if state == "missing_file" else "added"
    _atomic_write(real, new)
    return outcome


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def offer_instructions(harness: Harness, scope: str, *, ask: bool = True, home: Path | None = None) -> str:
    """Asks whether to put griot's block in the harness's global instructions file.
    Returns n/a | skipped | current | malformed | not-interactive | declined |
    created | added | updated. Nothing is written without a typed "y" or "yes"."""
    if scope != "global" or harness.global_instructions_file is None:
        return "n/a"  # a project's own CLAUDE.md is usually committed and shared: not ours to edit
    block = instructions_block(harness)
    if block is None:
        return "n/a"
    if not ask:
        return "skipped"
    path = harness.global_instructions_file(home or Path.home())
    state = instructions_state(path, block)
    if state == "current":
        print(f"  instructions: already up to date in {path}")
        return "current"
    if state == "malformed":
        print(f"  instructions: NOT touched. The griot markers in {path} are damaged, or it is not UTF-8 text; fix or remove them by hand.")
        return "malformed"
    if not _is_interactive():
        print(f"  instructions: {path} has no up-to-date griot block. Run `griot assist install --scope global` "
              f"in a terminal to be asked; nothing is written without your answer.")
        return "not-interactive"

    verb = "update the griot block in" if state == "outdated" else "add this to"
    print(f"\n  griot can {verb} {path}")
    print(f"  That file is loaded in EVERY project you open with {harness.display_name}, so it is your call:\n")
    for line in block.split("\n"):
        print(f"    {line}")
    print()
    try:
        answer = input(f"  Write it to {path}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("  instructions: skipped, nothing written")
        return "declined"
    outcome = apply_instructions(path, block)
    print(f"  instructions: {outcome} {path}")
    return outcome


def _which(name: str) -> str | None:
    return shutil.which(name)


def _griot_command() -> str:
    """How to start griot from outside a shell: the absolute path of the
    griot being run. A harness started from a desktop launcher does not have
    the terminal's PATH, so a bare `griot` could fail there. Not resolved: a
    pipx shim is a symlink into a versioned environment, and the shim is the
    path that survives an upgrade."""
    running = Path(sys.argv[0])
    if running.name == "griot" and running.is_file():
        return str(running.absolute())
    return _which("griot") or "griot"


def _run_harness_command(argv: list[str]) -> "subprocess.CompletedProcess":
    """The one place this module runs another program. Its own function so
    that the test suite can forbid it outright: a test that answers "y" to a
    simulated prompt must never change the real configuration of whoever
    runs the tests."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _model_note() -> str:
    """What one more griot server costs in memory, for the profile that is
    actually active. The figures are the profile's own ESTIMATE (see
    EMBED_PROFILES), so they are called one."""
    from griot import common  # only here: everything else in this module works without it
    estimate = common.ACTIVE_PROFILE.get("rss_estimate_mb")
    if not estimate:
        return f"The active embedding profile ({common.ACTIVE_PROFILE_NAME}) calls an API: a server loads no model."
    return (f"With the active embedding profile ({common.ACTIVE_PROFILE_NAME}) each one loads the model on its first "
            f"search: {estimate[0]} to {estimate[1]} MB of memory, by the profile's own estimate.")


def offer_mcp_server(harness: Harness, scope: str, *, mode: str = "ask") -> str:
    """Offers to register griot's MCP server with the harness, which is what
    makes its tools exist in a session at all. Returns n/a | skipped | no-cli |
    no-path | not-interactive | declined | registered | exists | failed.

    `--scope global` registers for every project (the harness's user scope),
    anything else for this project only, privately: the shared, committed
    project file is no place for a path on one machine.

    Registration used to be documented per project only, because a server
    held the index for as long as it ran. Since the index is released when
    idle, several sessions share it, and a project without the server simply
    never uses griot.

    It is done through the harness's own command line, never by editing its
    config file, and only after a typed yes or `mode="yes"` (the --mcp
    flag); afterwards it says how to undo exactly what it did. Without the
    harness's command or without a terminal, the command is printed.

    What is registered is a path, so the path is looked at first: a griot
    that can only be named by the bare word is not registered at all (a
    harness started from a desktop launcher would not find it), and one that
    lives inside a virtual environment is pointed out, because the
    registration dies with the environment."""
    if mode == "no":
        return "skipped"
    griot = _griot_command()
    if harness.mcp_register is None:
        print(f"  MCP server: griot cannot register itself with {harness.display_name}. To use its tools there, add "
              f"a local MCP server that runs: {shlex.join([griot, 'mcp'])}")
        return "n/a"
    everywhere = scope == "global"
    harness_scope = "user" if everywhere else "local"
    argv = harness.mcp_register(harness_scope, griot)
    command = shlex.join(argv)
    undo = shlex.join(harness.mcp_unregister(harness_scope)) if harness.mcp_unregister else None
    reach = "every project on this machine" if everywhere else "this project only"

    if not Path(griot).is_absolute():
        print(f"  MCP server: nothing was registered. griot was not started from an installed `griot` command and "
              f"none is on the PATH, so there is no absolute path to register; a harness would not find a bare "
              f"`{griot}`. Install griot as a tool (pipx or `uv tool`) and run this again.")
        return "no-path"
    if _which(argv[0]) is None:
        print(f"  MCP server: `{argv[0]}` is not on the PATH, so nothing was registered. To make griot's tools "
              f"available in {reach}, run:\n    {command}")
        return "no-cli"
    in_environment = Path(griot).parent == Path(sys.prefix) / "bin"
    fragile = (f"  Note: this griot lives inside a virtual environment ({Path(griot).parent}). The registration stops "
               f"working if that environment is removed; a pipx or `uv tool` install gives a path that stays."
               if in_environment else None)
    if mode != "yes":
        if not _is_interactive():
            print(f"  MCP server: not registered (no terminal to ask on). To make griot's tools available in "
                  f"{reach}, run:\n    {command}")
            return "not-interactive"
        print(f"\n  griot can register its MCP server with {harness.display_name} for {reach}.")
        print(f"  It runs:\n    {command}")
        if fragile:
            print(fragile)
        if everywhere:
            print(f"  Each open session then starts its own griot server. {_model_note()}")
            from griot import common
            if common.CONCURRENCY_MODE == "single":
                print("  In your griot configuration GRIOT_MCP_CONCURRENCY_MODE is 'single': one server keeps the "
                      "index for as long as it runs, so sessions in different projects would block each other. "
                      "Switch to 'multi' first.")
        try:
            answer = input("  Register it? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("  MCP server: skipped, nothing registered")
            return "declined"
    elif fragile:
        print(fragile)

    how_to_undo = f" To undo: {undo}" if undo else ""
    try:
        done = _run_harness_command(argv)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  MCP server: could not run `{argv[0]}` ({e}). To register it yourself:\n    {command}")
        return "failed"
    output = (done.stdout + done.stderr).strip()
    if done.returncode == 0:
        print(f"  MCP server: registered for {reach}. Restart open sessions to see griot's tools.{how_to_undo}")
        return "registered"
    if "already exists" in output:
        # The harness only says a server of that name is there. It may point
        # at a griot that has since moved: say so rather than vouch for it.
        replace = f" To replace it: {undo}, then run this again." if undo else ""
        print(f"  MCP server: a server named griot is already registered for {reach}; the command it runs was not "
              f"checked.{replace}")
        return "exists"
    print(f"  MCP server: `{argv[0]}` refused ({output or 'no message'}). To register it yourself:\n    {command}")
    return "failed"


# --- tools that may run without asking -----------------------------------------------
# A harness asks a person before each tool call unless a rule in its settings
# allows the tool. An agent that has to ask before every search mostly does not
# search. griot can add rules for its READ-ONLY tools. That widens what an agent
# may do without a person, so it follows the rule of the instructions block: a
# typed "y" at a terminal with the exact rules on screen, no flag that answers
# yes, and never from the MCP tool.


def tool_rules(harness: Harness) -> list[str]:
    """The rules griot offers, in the harness's own syntax. Which tools comes
    from the server itself (mcp_server.tools_safe_to_preapprove)."""
    from griot import mcp_server  # heavy, and only needed when this is offered

    return [harness.tool_rule(name) for name in mcp_server.tools_safe_to_preapprove()]


def settings_file(harness: Harness, scope: str, *, home: Path | None = None, cwd: Path | None = None) -> Path | None:
    if scope == "global":
        return harness.global_settings_file(home or Path.home()) if harness.global_settings_file else None
    return harness.local_settings_file(cwd or Path.cwd()) if harness.local_settings_file else None


_RULE_LISTS = ("allow", "deny", "ask")


class _KeyGivenTwice(ValueError):
    pass


def _no_key_twice(pairs: list) -> dict:
    """json.loads keeps the last of two equal keys and says nothing. Written
    back, the other value would be gone from the user's file."""
    keys = [key for key, _ in pairs]
    twice = next((key for key in keys if keys.count(key) > 1), None)
    if twice is not None:
        raise _KeyGivenTwice(twice)
    return dict(pairs)


def _read_settings(real: Path) -> tuple[dict | None, str | None, str]:
    """(settings, why they cannot be edited, the text as read). A file that is
    missing is empty settings. Anything griot does not recognise as settings
    is refused rather than guessed at: the file is the user's, and a wrong
    rewrite of it breaks their harness."""
    if not real.exists():
        return {}, None, ""
    if not os.access(real, os.R_OK):
        return None, "it cannot be read", ""
    if not os.access(real, os.W_OK):
        # The write is a rename, which replaces a read-only file without
        # complaint. Whoever made it read-only meant it.
        return None, "it is read-only", ""
    text = _read_text(real)
    if text is None:
        return None, "it is not UTF-8 text", ""
    if not text.strip():
        return {}, None, text
    try:
        settings = json.loads(text, object_pairs_hook=_no_key_twice)
    except _KeyGivenTwice as e:
        return None, f"it gives the key {str(e)!r} twice", text
    except ValueError as e:
        return None, f"it is not valid JSON ({e})", text
    if not isinstance(settings, dict):
        return None, "it does not hold a JSON object", text
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        return None, "its `permissions` is not an object", text
    for name in _RULE_LISTS:
        rules = permissions.get(name, [])
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            return None, f"its `permissions.{name}` is not a list of rules", text
    return settings, None, text


def _covers(pattern: str, rule: str, *, allowing: bool) -> bool:
    """Whether a rule of the user's applies to one of ours: the same rule,
    the whole server (`mcp__griot`), or a wildcard that matches it. An ALLOW
    rule only counts when its server part is literal: the harness ignores an
    allow rule such as `mcp__*`, so it allows nothing. Under deny and ask any
    wildcard counts."""
    server = rule.rsplit("__", 1)[0]
    if pattern in (rule, server):
        return True
    if "*" not in pattern:
        return False
    if allowing and not pattern.startswith(f"{server}__"):
        return False
    return fnmatch.fnmatchcase(rule, pattern)


def _approval_plan(settings: dict, rules: list[str]) -> tuple[list[str], dict[str, str]]:
    """(rules to add, rules left out and under which list the user already
    decided otherwise). A rule that is already allowed is neither."""
    permissions = settings.get("permissions", {})
    to_add, left_out = [], {}
    for rule in rules:
        decided = next((name for name in ("deny", "ask")
                        if any(_covers(theirs, rule, allowing=False) for theirs in permissions.get(name, []))), None)
        if decided:
            left_out[rule] = decided  # theirs is a decision; it also wins in the harness
        elif not any(_covers(theirs, rule, allowing=True) for theirs in permissions.get("allow", [])):
            to_add.append(rule)
    return to_add, left_out


def _indent_of(text: str) -> int | str:
    """The indentation the file already uses, so that adding rules changes as
    little of its layout as a rewrite can."""
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            lead = line[: len(line) - len(stripped)]
            return "\t" if lead[0] == "\t" else len(lead)
    return 2


def _write_settings(path: Path, text: str) -> None:
    """The one place a settings file is written. Its own function so that
    the test suite can keep every such write inside its own directory. A
    file griot creates is private (0600); an existing one keeps its mode."""
    created = not path.exists()
    _atomic_write(path, text)
    if created:
        os.chmod(path, 0o600)


def _tracked_by_git(path: Path) -> bool:
    """Whether git tracks this file. Best effort: anything that goes wrong
    reads as "not tracked", and the hardened runner is used because the
    repository asked about is not necessarily the user's own."""
    try:
        from griot import common

        done = common.run_git(path.parent, ["ls-files", "--error-unmatch", "--", path.name], timeout=10, check=False)
        return done.returncode == 0
    except Exception:  # noqa: BLE001 — a warning that could not be worked out is no warning
        return False


def _existing_project_file_note(real: Path, settings: dict) -> list[str]:
    """What to say about a project settings file that is already there. A
    repository can ship one with permissions and hooks of its own; adding to
    it must not read as vouching for it."""
    if not real.exists():
        return []
    held = []
    others = len(settings.get("permissions", {}).get("allow", []))
    if others:
        held.append(f"{others} other allow rule{'s' if others != 1 else ''}")
    held.extend(key for key in ("hooks", "env") if settings.get(key))
    lines = [f"  That file already exists{' (' + ', '.join(held) + ')' if held else ''}; griot keeps what is in it. "
             f"If this project is not yours, read it first: it decides what an agent may do here."]
    if _tracked_by_git(real):
        lines.append("  It is tracked by git in this repository: what you add is shared with whoever clones it.")
    return lines


def offer_tool_approval(harness: Harness, scope: str, *, ask: bool = True, home: Path | None = None,
                        cwd: Path | None = None) -> str:
    """Asks whether griot's read-only tools may run without the harness
    asking each time. Returns n/a | skipped | unsafe | malformed | current |
    not-interactive | declined | failed | created | added. Nothing is written
    without a typed "y" or "yes", and nothing it does raises: the install it
    follows has already happened."""
    path = settings_file(harness, scope, home=home, cwd=cwd)
    if path is None or harness.tool_rule is None:
        return "n/a"
    if not ask:
        return "skipped"
    everywhere = scope == "global"
    reach = "every project" if everywhere else "this project"

    if everywhere:
        real = Path(os.path.realpath(path))  # the user's own file: often a link into a dotfiles checkout
    else:
        # A project is not the user's: a repository can ship this file, or
        # `.claude`, as a link to a file of theirs.
        problem = _destination_problem(path, cwd or Path.cwd(), contained=True)
        if problem:
            print(f"  tool approval: NOT touched. {problem}")
            return "unsafe"
        real = path

    rules = tool_rules(harness)  # loads the server: only now that something may be offered
    by_hand = "    " + "\n    ".join(rules)
    settings, why_not, text = _read_settings(real)
    if settings is None:
        print(f"  tool approval: NOT touched. {path} is not something griot can edit safely: {why_not}. "
              f"To let {harness.display_name} call griot's read-only tools without asking, add these to "
              f"`permissions.allow` there by hand:\n{by_hand}")
        return "malformed"

    to_add, left_out = _approval_plan(settings, rules)
    kept_out = ""
    if left_out:
        kept_out = ("  Left out, because that file already has a rule for them under "
                    f"{' or '.join(f'`{name}`' for name in sorted(set(left_out.values())))}, and that is your "
                    f"decision: {', '.join(sorted(left_out))}")
    if not to_add:
        print(f"  tool approval: nothing to add in {path}.")
        if kept_out:
            print(kept_out)
        return "current"
    if not _is_interactive():
        print(f"  tool approval: {harness.display_name} asks before each griot tool call. Run "
              f"`griot assist install{' --scope global' if everywhere else ''}` in a terminal to be asked "
              f"whether the read-only ones may run without that; nothing is written without your answer.")
        return "not-interactive"

    print(f"\n  griot can let {harness.display_name} call griot's read-only tools without asking each time, "
          f"in {reach}.")
    print(f"  It adds these rules to `permissions.allow` in {path}:\n")
    print("    " + "\n    ".join(to_add))
    print("\n  They do not change your index or your configuration: they search what you indexed, list "
          "repositories and profiles, and report status and usage (each call is logged).\n"
          "  griot_search embeds the query, which on a paid embedding profile costs a fraction of a "
          "cent per search.\n"
          "  griot adds no rule for the tools that change something, nor for the quality check.\n"
          "  A rule matches any MCP server named `griot`, whoever defines it: in a project that ships its "
          "own server under that name, these rules apply to that one.")
    if kept_out:
        print(kept_out)
    if not everywhere:
        for line in _existing_project_file_note(real, settings) or [
                "  That file is your personal settings for this project: keep it out of version control."]:
            print(line)
    try:
        answer = input(f"  Add them to {path}? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("  tool approval: skipped, nothing written")
        return "declined"

    permissions = settings.setdefault("permissions", {})
    permissions.setdefault("allow", []).extend(to_add)
    eol = "\r\n" if "\r\n" in text else "\n"
    outcome = "added" if real.exists() else "created"
    try:
        new = json.dumps(settings, indent=_indent_of(text), ensure_ascii=False).replace("\n", eol) + eol
        _write_settings(real, new)
    except (OSError, ValueError) as e:
        # The skills are installed and the answer was yes: say what happened
        # and how to finish by hand, instead of a traceback after the fact.
        print(f"  tool approval: NOT written, {path} could not be saved ({e}). To add the rules yourself, put "
              f"these in `permissions.allow` there:\n" + "    " + "\n    ".join(to_add))
        return "failed"
    print(f"  tool approval: {outcome} {len(to_add)} rule(s) in {path}. To undo, remove them from "
          f"`permissions.allow` in that file.")
    return outcome


def _print_result(result: dict) -> None:
    created, updated, unchanged = result["created"], result["updated"], result["unchanged"]
    print(f"{result['harness']}: skills -> {result['skills_target']}, agents -> {result['agents_target']}")
    if not created and not updated and not unchanged:
        print("  nothing to install")
        return
    if created:
        print(f"  created ({len(created)}):")
        for rel in created:
            print(f"    {rel}")
    if updated:
        print(f"  updated ({len(updated)}):")
        for rel in updated:
            print(f"    {rel}")
    if unchanged:
        print(f"  unchanged: {len(unchanged)}")


def cmd_install(scope: str, harness_choice: str, *, ask_instructions: bool = True, mcp: str = "ask",
                ask_tools: bool = True, home: Path | None = None) -> int:
    if harness_choice == "all":
        targets = detect_harnesses()
        if not targets:
            known = ", ".join(h.id for h in HARNESSES)
            print(f"No supported harness found ({known}) — install one of them, or pass --harness explicitly.")
            return 0
    else:
        targets = [h for h in HARNESSES if h.id == harness_choice]

    # Checked for every harness before the first file of any of them.
    refusal = install_refusal(targets, scope, home=home)
    if refusal:
        print(f"Error: nothing was installed. {refusal}", file=sys.stderr)
        return 1

    outcomes = []
    for harness in targets:
        try:
            result = install(harness, scope, home=home)
        except UnsafeDestination as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        _print_result(result)
        offer_instructions(harness, scope, ask=ask_instructions, home=home)
        outcomes.append(offer_mcp_server(harness, scope, mode=mcp))
        # A courtesy, like the instructions block: whatever its outcome, the
        # install itself worked.
        offer_tool_approval(harness, scope, ask=ask_tools, home=home)
    # Asked for with --mcp, a registration that did not happen is a failure of
    # the command: a script must not carry on as if the tools were there.
    # Asked interactively it is a courtesy on top of an install that worked.
    if mcp == "yes" and any(outcome in ("failed", "no-cli", "no-path") for outcome in outcomes):
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot assist",
        description="Installs griot's bundled Claude Code/opencode skills and agents for onboarding, indexing and workflow help.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_install = sub.add_parser("install", help="Copies griot's bundled skills/agents into each detected harness's config dir")
    p_install.add_argument(
        "--scope",
        choices=["local", "global"],
        default="local",
        help="local: <cwd>/.<harness> (default); global: per-harness home location",
    )
    p_install.add_argument(
        "--harness",
        choices=[*(h.id for h in HARNESSES), "all"],
        default="all",
        help="Which harness to install into ('all' auto-detects what's present on this machine)",
    )

    p_install.add_argument(
        "--no-instructions",
        action="store_true",
        help="At global scope, do not offer to add griot's block to the harness's global instructions file. "
             "There is deliberately no flag that answers that question for you.",
    )

    mcp = p_install.add_mutually_exclusive_group()
    mcp.add_argument("--mcp", dest="mcp", action="store_const", const="yes", default="ask",
                     help="Register griot's MCP server with the harness without asking "
                          "(--scope global: every project; otherwise this project only)")
    mcp.add_argument("--no-mcp", dest="mcp", action="store_const", const="no",
                     help="Do not offer to register griot's MCP server")

    p_install.add_argument(
        "--no-allow-tools",
        action="store_true",
        help="Do not offer to let the harness call griot's read-only tools without asking each time. "
             "There is deliberately no flag that answers that question for you.",
    )

    args = parser.parse_args(argv)
    return cmd_install(args.scope, args.harness, ask_instructions=not args.no_instructions, mcp=args.mcp,
                       ask_tools=not args.no_allow_tools)


if __name__ == "__main__":
    raise SystemExit(main())
