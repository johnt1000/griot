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

Deliberately does NOT import griot.common: that module pulls in
qdrant_edge/fastembed at import time (seconds of cost, see cli.py's docstring
on lazy dispatch) for a command that only copies a handful of text files.
"""

import argparse
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


def _write_file(dest: Path, content: bytes) -> str:
    """Writes `content` to `dest` (mode 0600), returns which of
    created/updated/unchanged it was. Leaves an unchanged file untouched
    (no rewrite, no chmod) — see SECURITY.md's 0600/0700 convention."""
    if dest.exists() and dest.read_bytes() == content:
        return "unchanged"
    status = "updated" if dest.exists() else "created"
    _secure_mkdir(dest.parent)
    fd = os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # O_CREAT only applies the mode on creation — repairs a pre-existing file
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(content)
    finally:
        if fd is not None:
            os.close(fd)
    return status


def _copy_tree(src_root: Path, dest_root: Path, buckets: dict) -> None:
    if not src_root.is_dir():
        return
    for src_file in sorted(p for p in src_root.rglob("*") if p.is_file()):
        rel = src_file.relative_to(src_root)
        buckets[_write_file(dest_root / rel, src_file.read_bytes())].append(str(rel))


def install(harness: Harness, scope: str, *, home: Path | None = None, cwd: Path | None = None) -> dict:
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
    created, updated, unchanged = [], [], []
    buckets = {"created": created, "updated": updated, "unchanged": unchanged}

    _copy_tree(root / "skills", skills_target, buckets)
    _copy_tree(root / "agents" / harness.agent_content_subdir, agents_target, buckets)

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
                home: Path | None = None) -> int:
    if harness_choice == "all":
        targets = detect_harnesses()
        if not targets:
            known = ", ".join(h.id for h in HARNESSES)
            print(f"No supported harness found ({known}) — install one of them, or pass --harness explicitly.")
            return 0
    else:
        targets = [h for h in HARNESSES if h.id == harness_choice]

    outcomes = []
    for harness in targets:
        _print_result(install(harness, scope, home=home))
        offer_instructions(harness, scope, ask=ask_instructions, home=home)
        outcomes.append(offer_mcp_server(harness, scope, mode=mcp))
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

    args = parser.parse_args(argv)
    return cmd_install(args.scope, args.harness, ask_instructions=not args.no_instructions, mcp=args.mcp)


if __name__ == "__main__":
    raise SystemExit(main())
