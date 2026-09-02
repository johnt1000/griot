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
import shutil
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


def cmd_install(scope: str, harness_choice: str) -> int:
    if harness_choice == "all":
        targets = detect_harnesses()
        if not targets:
            known = ", ".join(h.id for h in HARNESSES)
            print(f"No supported harness found ({known}) — install one of them, or pass --harness explicitly.")
            return 0
    else:
        targets = [h for h in HARNESSES if h.id == harness_choice]

    for harness in targets:
        _print_result(install(harness, scope))
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

    args = parser.parse_args(argv)
    return cmd_install(args.scope, args.harness)


if __name__ == "__main__":
    raise SystemExit(main())
