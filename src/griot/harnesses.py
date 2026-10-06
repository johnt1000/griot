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
    # The one that says what is registered under the name griot (read-only).
    mcp_inspect: list[str] | None = None
    # Where the harness keeps the tools that may run without asking: the
    # user's file (given the home) and a project's PERSONAL one (given the
    # project), and how it names one of griot's tools in a rule. None where
    # griot does not know the format: then nothing is offered.
    global_settings_file: Callable[[Path], Path] | None = None
    local_settings_file: Callable[[Path], Path] | None = None
    tool_rule: Callable[[str], str] | None = None
    # The environment variable that moves the harness's user directory, when
    # it has one, and why its current value cannot be used (or None).
    user_dir_variable: str | None = None
    user_dir_problem: Callable[[], str | None] | None = None


def _claude_user_dir(home: Path) -> Path:
    """Where Claude Code keeps its user files: the directory CLAUDE_CONFIG_DIR
    names when it is set (a leading `~` is the home directory), `~/.claude`
    otherwise. With the variable set the harness reads everything from
    there, settings, memory file, skills and agents alike, and nothing from
    `~/.claude`: files installed in the default place would be files nobody
    loads, reported as installed."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not configured or _claude_user_dir_problem():
        # With a value that cannot be used nothing global is written at all
        # (install_refusal says why); this path only keeps detection working.
        return home / ".claude"
    return Path(configured).expanduser()


def _claude_user_dir_problem() -> str | None:
    """Why the value of CLAUDE_CONFIG_DIR does not name one place, or None.
    The variable comes from whoever started the process: for the MCP server
    that can be a project's own file, so its value is input. A relative one
    would be read against the directory the process happens to be in."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not configured:
        return None
    shown = repr(configured)[:120]
    try:
        expanded = Path(configured).expanduser()
    except RuntimeError:
        return (f"CLAUDE_CONFIG_DIR is {shown}, which names the home directory of a user that cannot be found. "
                f"Set it to an absolute path.")
    if not expanded.is_absolute():
        return (f"CLAUDE_CONFIG_DIR is {shown}, which is not an absolute path: griot does not guess what it is "
                f"relative to. Set it to an absolute path.")
    return None


HARNESSES = [
    Harness(
        id="claude-code",
        display_name="Claude Code",
        local_skills_dir=lambda cwd: cwd / ".claude" / "skills",
        local_agents_dir=lambda cwd: cwd / ".claude" / "agents",
        global_skills_dir=lambda home: _claude_user_dir(home) / "skills",
        global_agents_dir=lambda home: _claude_user_dir(home) / "agents",
        agent_content_subdir="claude-code",
        detect=lambda: shutil.which("claude") is not None or _claude_user_dir(Path.home()).is_dir(),
        global_instructions_file=lambda home: _claude_user_dir(home) / "CLAUDE.md",
        instructions_resource="claude-code.md",
        mcp_register=lambda scope, griot: ["claude", "mcp", "add", "--scope", scope, "griot", "--", griot, "mcp"],
        # With the scope: once a server is registered in two scopes, a remove without one is refused.
        mcp_unregister=lambda scope: ["claude", "mcp", "remove", "--scope", scope, "griot"],
        mcp_inspect=["claude", "mcp", "get", "griot"],
        global_settings_file=lambda home: _claude_user_dir(home) / "settings.json",
        # settings.local.json, not settings.json: the latter is the one a team commits.
        local_settings_file=lambda cwd: cwd / ".claude" / "settings.local.json",
        # mcp__<server>__<tool>, for a server registered under the name "griot".
        tool_rule=lambda tool: f"mcp__griot__{tool}",
        user_dir_variable="CLAUDE_CONFIG_DIR",
        user_dir_problem=_claude_user_dir_problem,
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
    """Creates `path` and whatever is missing above it, 0700. A directory
    that is already there keeps its mode: it is the user's (their `agents/`,
    a project's `.claude/`), and only what griot creates is griot's to set."""
    missing = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in missing:
        os.chmod(created, 0o700)


def user_dir_set_by(harness: Harness, scope: str) -> str | None:
    """The name of the environment variable that decided where a GLOBAL
    install goes, or None when the default place is used."""
    variable = harness.user_dir_variable
    return variable if scope == "global" and variable and os.environ.get(variable, "").strip() else None


def _user_dir_problem(harness: Harness, scope: str) -> str | None:
    return harness.user_dir_problem() if scope == "global" and harness.user_dir_problem else None


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
    above = dest.parent
    while not above.exists() and above != above.parent:
        above = above.parent
    if not above.is_dir():
        return f"{above} is not a directory, so nothing can be installed under it."
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


def destinations(harness: Harness, scope: str, *, home: Path | None = None,
                 cwd: Path | None = None) -> tuple[Path, Path]:
    """(skills directory, agents directory) an install would write into. For
    whoever has to SHOW the place before anything is written."""
    _, skills_target, agents_target, _ = _plan(harness, scope, home, cwd)
    return skills_target, agents_target


def install_refusal(harness_list: list[Harness], scope: str, *, home: Path | None = None,
                    cwd: Path | None = None) -> str | None:
    """Why this install must not happen, or None. Looks at EVERY destination
    of every harness and writes nothing, so that a caller can refuse before
    the first file (and the MCP tool before asking a person)."""
    for harness in harness_list:
        problem = _user_dir_problem(harness, scope)
        if problem:
            return problem
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
    Returns n/a | skipped | unsafe | current | malformed | not-interactive | declined |
    created | added | updated. Nothing is written without a typed "y" or "yes"."""
    if scope != "global" or harness.global_instructions_file is None:
        return "n/a"  # a project's own CLAUDE.md is usually committed and shared: not ours to edit
    block = instructions_block(harness)
    if block is None:
        return "n/a"
    if not ask:
        return "skipped"
    problem = _user_dir_problem(harness, scope)
    if problem:
        print(f"  instructions: NOT touched. {problem}")
        return "unsafe"
    path = harness.global_instructions_file(home or Path.home())
    state = instructions_state(path, block)
    if state == "current":
        print(f"  instructions: already up to date in {path}")
        return "current"
    if state == "malformed":
        print(f"  instructions: NOT touched. The griot markers in {path} are damaged, or it is not UTF-8 text; fix or remove them by hand.")
        return "malformed"
    if not _is_interactive():
        print(f"  instructions: {path} has no up-to-date griot block. Run `griot assist install` "
              f"in a terminal to be asked; there is no flag that answers, and nothing is written without your answer.")
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


def mcp_registration(harness: Harness) -> dict | None:
    """What the harness itself says about a server named griot:
    {"scope": "user" | "local" | "project" | None, "command": <the program>,
    "args": <its arguments, as one string>}, {} when none is registered, and
    None when the harness could not be asked or its answer could not be read
    (then nothing is assumed). Where several scopes have one, the harness
    answers with the one that takes precedence: the others are not seen.

    The harness checks the server it describes, so this can start a `griot
    mcp` for a moment; a registration that hangs is cut off by the timeout
    of _run_harness_command() and counts as "could not be asked".

    Asked before anything is offered: registering used to be how griot found
    out that a server was already there, and it never saw what that server
    ran, so a registration pointing at a griot that had since moved was
    reported as "already registered"."""
    if harness.mcp_inspect is None or _which(harness.mcp_inspect[0]) is None:
        return None
    try:
        done = _run_harness_command(list(harness.mcp_inspect))
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or "") + (done.stderr or "")
    if "No MCP server named" in text:
        return {}
    command = scope = None
    args = ""
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "Command":
            command = value.strip() or None
        elif key == "Args":
            args = value.strip()
        elif key == "Scope":
            label = value.strip().lower()
            scope = next((name for name in ("user", "local", "project") if label.startswith(name)), None)
    return {"scope": scope, "command": command, "args": args} if command else None


def _command_is_gone(command: str) -> bool:
    """Whether what a registration runs is certainly not there any more. A
    bare name is found by the harness through the PATH, and a relative path
    from wherever the harness starts the server: only an absolute path that
    does not exist, or a bare name that is nowhere on the PATH, is "gone".
    Anything griot cannot tell is left alone: calling a working registration
    dead would have it removed."""
    if Path(command).is_absolute():
        return not Path(command).exists()
    # Decided on the text as registered: Path("./griot") forgets the "./".
    if "/" not in command and "\\" not in command:
        return _which(command) is None
    return False


# Outcomes of offer_mcp_server() after which a server named griot is there.
SERVER_IS_THERE = ("registered", "replaced", "shadowed", "current", "exists")


def offer_mcp_server(harness: Harness, scope: str, *, mode: str = "ask") -> str:
    """Offers to register griot's MCP server with the harness, which is what
    makes its tools exist in a session at all. Returns n/a | skipped | no-cli |
    no-path | not-interactive | declined | registered | replaced | shadowed |
    current | exists | stale | failed.

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

    # What is there already, before anything is asked. A registration for
    # every project covers an install into one; one for a single project
    # does not cover "every project".
    found = mcp_registration(harness)
    replacing = shadow = None
    if found and found["scope"]:
        where = {"user": "every project", "local": "this project", "project": "this project (its .mcp.json)"}[found["scope"]]
        running = " ".join(part for part in (found["command"], found["args"]) if part)
        covers = found["scope"] == "user" or not everywhere
        # This very griot is running: whatever else, its command is there.
        if found["command"] == griot or not _command_is_gone(found["command"]):
            if covers and found["command"] == griot and found["args"] == "mcp":
                print(f"  MCP server: already registered for {where}, running this griot.")
                return "current"
            if covers:
                print(f"  MCP server: already registered for {where}. It runs `{running}`, which is not this "
                      f"griot's server (`{griot} mcp`); left as it is.")
                return "exists"
        elif found["scope"] == harness_scope:
            replacing = found
        elif everywhere:
            # Not ours to remove from an install for every project, and it
            # still wins in this one.
            shadow = (f"  Note: {where} has its own registration, which runs `{running}`. That is not there any "
                      f"more, and it takes precedence here: in this project the server will not start until that "
                      f"registration is removed.")
        else:
            # Asked for this project only: what is registered for every
            # project is left alone, and this project's own comes before it.
            shadow = (f"  Note: the registration for {where} runs `{running}`, which is not there any more. It is "
                      f"left alone: this project's own registration comes before it.")
            if found["scope"] == "user":
                shadow += " `griot assist install` (for every project) offers to replace it."

    in_environment = Path(griot).parent == Path(sys.prefix) / "bin"
    fragile = (f"  Note: this griot lives inside a virtual environment ({Path(griot).parent}). The registration stops "
               f"working if that environment is removed; a pipx or `uv tool` install gives a path that stays."
               if in_environment else None)
    remove = harness.mcp_unregister(replacing["scope"]) if replacing and harness.mcp_unregister else None
    if mode != "yes":
        if not _is_interactive():
            what = (f"registered, but it runs {replacing['command']}, which is not there any more"
                    if replacing else "not registered")
            steps = "".join(f"\n    {step}" for step in ([shlex.join(remove)] if remove else []) + [command])
            print(f"  MCP server: {what} (no terminal to ask on). To make griot's tools available in "
                  f"{reach}, run:{steps}")
            if shadow:
                print(shadow)
            return "stale" if replacing else "not-interactive"
        if replacing:
            print(f"\n  griot's MCP server is registered with {harness.display_name} for {reach}, but it runs "
                  f"{replacing['command']}, which is not there any more: the server cannot start.")
            print(f"  Replacing it runs:\n    {shlex.join(remove) if remove else '(nothing to remove with)'}"
                  f"\n    {command}")
            question = f"  Replace it with {griot}? [y/N] "
        else:
            print(f"\n  griot can register its MCP server with {harness.display_name} for {reach}.")
            print(f"  It runs:\n    {command}")
            question = "  Register it? [y/N] "
        if fragile:
            print(fragile)
        if shadow:
            print(shadow)
        if everywhere and not replacing:
            print(f"  Each open session then starts its own griot server. {_model_note()}")
            from griot import common
            if common.CONCURRENCY_MODE == "single":
                print("  In your griot configuration GRIOT_MCP_CONCURRENCY_MODE is 'single': one server keeps the "
                      "index for as long as it runs, so sessions in different projects would block each other. "
                      "Switch to 'multi' first.")
        try:
            answer = input(question)
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            if replacing:
                print("  MCP server: left as it is. It will not start until the registration is replaced.")
                return "stale"
            print("  MCP server: skipped, nothing registered")
            return "declined"
    else:
        for note in (fragile, shadow):
            if note:
                print(note)

    how_to_undo = f" To undo: {undo}" if undo else ""
    variable = user_dir_set_by(harness, scope)
    if how_to_undo and variable:
        # The harness's own CLI follows the variable: without it, the undo
        # would look in another configuration and find nothing.
        how_to_undo += f" (with {variable} set as it is now)"
    # Said with every failure after the old registration is gone: by then
    # "nothing registered" is news, not the state the person started from.
    gone = ""
    try:
        if remove:
            removed = _run_harness_command(remove)
            if removed.returncode != 0:
                print(f"  MCP server: the old registration could not be removed "
                      f"({(removed.stdout + removed.stderr).strip() or 'no message'}); nothing was changed.")
                return "failed"
            gone = " The old registration was removed, so griot is not registered now."
        done = _run_harness_command(argv)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  MCP server: could not run `{argv[0]}` ({e}).{gone} To register it yourself:\n    {command}")
        return "failed"
    output = (done.stdout + done.stderr).strip()
    if done.returncode == 0:
        verb = "replaced; registered" if replacing else "registered"
        print(f"  MCP server: {verb} for {reach}. Restart open sessions to see griot's tools.{how_to_undo}")
        if replacing:
            return "replaced"
        # "shadowed": registered for every project while this project's own
        # broken registration still comes first (the note above says so).
        return "shadowed" if shadow and everywhere else "registered"
    if "already exists" in output:
        # Only reached when the harness could not be asked beforehand: it
        # says a server of that name is there, and nothing about what it runs.
        replace = f" To replace it: {undo}, then run this again." if undo else ""
        print(f"  MCP server: a server named griot is already registered for {reach}; the command it runs was not "
              f"checked.{replace}")
        return "exists"
    print(f"  MCP server: `{argv[0]}` refused ({output or 'no message'}).{gone} To register it yourself:"
          f"\n    {command}")
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
        problem = _user_dir_problem(harness, scope)
        if problem:
            print(f"  tool approval: NOT touched. {problem}")
            return "unsafe"
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
              f"`griot assist install{'' if everywhere else ' --scope local'}` in a terminal to be asked "
              f"whether the read-only ones may run without that; there is no flag that answers, and nothing is "
              f"written without your answer.")
        return "not-interactive"

    print(f"\n  griot can let {harness.display_name} call griot's read-only tools without asking each time, "
          f"in {reach}.")
    print(f"  It adds these rules to `permissions.allow` in {path}:\n")
    print("    " + "\n    ".join(to_add))
    print("\n  They do not change your index or your configuration: they search what you indexed, list "
          "repositories, profiles and settings, and report status and usage (each call is logged).\n"
          "  griot_search embeds the query, which on a paid embedding profile costs a fraction of a "
          "cent per search.\n"
          "  griot adds no rule for the tools that change something, nor for the read-only ones that cost or read "
          "far more than a search does (the quality check is one).\n"
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


def _summary(done: list[dict], scope: str) -> None:
    """What the install did, what it left out, and what comes next. The
    command used to end on the answer to its last question."""
    reach = "every project" if scope == "global" else "this project"
    server_words = {
        "registered": f"registered for {reach}", "replaced": f"registered for {reach} (replaced a dead one)",
        "shadowed": f"registered for {reach}; in this project a broken registration of its own still comes first",
        "current": "already registered, running this griot", "exists": "already registered (see above for what it runs)",
        "skipped": "not looked at (--no-mcp)", "declined": "not registered (declined)",
        "stale": "registered, but its command is gone and it was not replaced",
        "not-interactive": "not registered (no terminal to ask on)", "no-cli": "not registered (harness CLI not found)",
        "no-path": "not registered (griot has no absolute path)", "failed": "not registered (the harness refused)",
        "n/a": "to be added by hand (see above)",
    }
    tools_words = {
        "created": "allowed without asking", "added": "allowed without asking",
        "current": "already settled in the settings file", "declined": "not allowed (declined)",
        "skipped": "not asked (--no-allow-tools)", "not-interactive": "not changed (no terminal to ask on)",
        "unsafe": "settings file not touched (see above)", "malformed": "settings file not touched (see above)",
        "failed": "the settings file could not be written (see above)",
        "not offered": "not offered (no MCP server)",
    }
    instructions_words = {
        "created": "added", "added": "added", "updated": "updated", "current": "already there",
        "declined": "not added (declined)", "skipped": "not asked (--no-instructions)",
        "not-interactive": "not added (no terminal to ask on)",
        "unsafe": "instructions file not touched (see above)", "malformed": "instructions file not touched (see above)",
        "not offered": "not offered (no MCP server)",
    }
    print("\nSummary")
    for entry in done:
        result = entry["files"]
        changed = len(result["created"]) + len(result["updated"])
        print(f"  {result['harness']}: skills and agent in {Path(result['skills_target']).parent} "
              f"({changed} written, {len(result['unchanged'])} unchanged)")
        if "server" not in entry:
            continue  # --skills-only: nothing else was looked at
        print(f"    MCP server: {server_words.get(entry['server'], entry['server'])}")
        if entry["tools"] != "n/a":
            print(f"    read-only tools: {tools_words.get(entry['tools'], entry['tools'])}")
        if entry["instructions"] != "n/a":
            print(f"    instructions: {instructions_words.get(entry['instructions'], entry['instructions'])}")

    there = [entry for entry in done if entry.get("server") in SERVER_IS_THERE]
    if not there:
        return
    print("\nNext")
    changed = any(entry["files"]["created"] or entry["files"]["updated"]
                  or entry.get("server") in ("registered", "replaced", "shadowed")
                  or entry.get("tools") in ("created", "added")
                  or entry.get("instructions") in ("created", "added", "updated") for entry in done)
    if changed:
        print("  Restart the sessions that are already open: they do not have what was just written.")
    try:
        from griot import common  # only now: what is registered to index, and under which profile
    except Exception as e:  # noqa: BLE001 - the install is done; a summary line is not worth failing it for
        print(f"  griot's own configuration could not be read ({e}). `griot config list` shows the settings; "
              f"nothing more can be said about what to index until that is fixed.")
        return
    try:
        registered = common.load_repos()
    except (OSError, ValueError):
        registered = []
    if registered:
        print(f"  {len(registered)} repositor{'y is' if len(registered) == 1 else 'ies are'} registered: "
              f"`griot index all` brings the index up to date (`--dry-run` first says what it would do).")
    else:
        print("  Nothing is registered to index yet: `griot repos add <path>` for each repository, then "
              "`griot index all`.")
    print(f"  Active embedding profile: {common.ACTIVE_PROFILE_NAME} (`griot profiles list` shows the others, "
          f"`griot profiles use <name>` changes it).")


def cmd_install(scope: str, harness_choice: str, *, ask_instructions: bool = True, mcp: str = "ask",
                ask_tools: bool = True, skills_only: bool = False, home: Path | None = None) -> int:
    if harness_choice == "all":
        targets = detect_harnesses()
        if not targets:
            known = ", ".join(h.id for h in HARNESSES)
            # A failure: a script must not carry on as if something had been installed.
            print(f"Error: nothing was installed. No supported harness found ({known}) on this machine: install "
                  f"one of them, or pass --harness explicitly.", file=sys.stderr)
            return 1
    else:
        targets = [h for h in HARNESSES if h.id == harness_choice]

    # Checked for every harness before the first file of any of them.
    refusal = install_refusal(targets, scope, home=home)
    if refusal:
        print(f"Error: nothing was installed. {refusal}", file=sys.stderr)
        return 1

    done = []
    for harness in targets:
        variable = user_dir_set_by(harness, scope)
        if variable:
            print(f"{harness.id}: {variable} is set, so its user files go where that points, not to the default place.")
        try:
            result = install(harness, scope, home=home)
        except UnsafeDestination as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        _print_result(result)
        entry = {"files": result}
        done.append(entry)
        if skills_only:
            continue

        # The server first: without it there are no tools to allow and
        # nothing for the instructions to point at.
        entry["server"] = offer_mcp_server(harness, scope, mode=mcp)
        # --no-mcp ("skipped") is someone who looks after the server
        # themselves; a harness griot cannot register with ("n/a") has
        # neither of the two steps below anyway; and without the harness's
        # command line ("no-cli") nothing was looked at, so "not registered"
        # would be a guess.
        if entry["server"] in SERVER_IS_THERE + ("skipped", "n/a", "no-cli"):
            entry["tools"] = offer_tool_approval(harness, scope, ask=ask_tools, home=home)
            entry["instructions"] = offer_instructions(harness, scope, ask=ask_instructions, home=home)
        else:
            entry["tools"] = entry["instructions"] = "not offered"
            print("  tool approval and instructions: not offered, because griot's MCP server is not registered. "
                  "Run this again once it is.")
    _summary(done, scope)
    # Asked for with --mcp, a registration that did not happen is a failure of
    # the command: a script must not carry on as if the tools were there.
    # Asked interactively it is a courtesy on top of an install that worked.
    if mcp == "yes" and any(entry.get("server") in ("failed", "no-cli", "no-path") for entry in done):
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot assist",
        description="Installs griot's bundled Claude Code/opencode skills and agents for onboarding, indexing and workflow help.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_install = sub.add_parser(
        "install", help="Copies griot's bundled skills/agents into each detected harness's config dir",
        description="Copies griot's bundled skills/agents into each detected harness's config dir, then asks, at an "
                    "interactive terminal, whether to register the MCP server, to let its read-only tools run "
                    "without a prompt and (--scope global) to add griot's block to the global instructions file. "
                    "There is no flag that answers the last two: with no terminal they are left as they are.")
    p_install.add_argument(
        "--scope",
        choices=["local", "global"],
        default="global",
        help="global (default): for every project, in the harness's user directory; "
             "local: for this project only, in <cwd>/.<harness>",
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

    p_install.add_argument(
        "--skills-only",
        action="store_true",
        help="Copy the skills and the agent and ask nothing: no MCP registration, no tool rules, no instructions block",
    )

    args = parser.parse_args(argv)
    if args.skills_only and args.mcp == "yes":
        parser.error("--skills-only copies files and nothing else: it does not go with --mcp")
    return cmd_install(args.scope, args.harness, ask_instructions=not args.no_instructions, mcp=args.mcp,
                       ask_tools=not args.no_allow_tools, skills_only=args.skills_only)


if __name__ == "__main__":
    raise SystemExit(main())
