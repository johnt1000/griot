"""`griot repos` — manages the list of repos indexed in bulk
(`common.REPOS_JSON_PATH`, used by `griot index <source>`/`index all` without
`--path`). Closes a configuration gap that previously could only be edited by
hand-editing the JSON.
"""

import argparse
import json
import sys
from pathlib import Path

from griot import common


def _load() -> list[str]:
    if not common.REPOS_JSON_PATH.exists():
        return []
    return json.loads(common.REPOS_JSON_PATH.read_text())


def _save(paths: list[str]) -> None:
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    # [M2] 0600: the repo list reveals local paths and project names.
    # [security review, risk 5.2] atomic (tmp + rename): a crash mid-write
    # leaving this file truncated would feed straight into
    # jobs.index_path_allowed()'s allowlist check on the next read — a
    # corrupted repos.json needs to fail closed there, not exist at all
    # here in the first place.
    common.secure_write_text_atomic(common.REPOS_JSON_PATH, json.dumps(paths, indent=2))


def is_git_repo(path: str) -> bool:
    """Has a real .git — checked via .exists(), NOT .is_dir(): a git
    worktree or submodule has a .git FILE, a pointer to the real gitdir
    elsewhere, not a directory. Factored out of repo_status() so add_repo()
    callers can warn at registration time
    without duplicating the check."""
    return (Path(path) / ".git").exists()


def repo_status() -> list[dict]:
    """One dict per registered repo: path, exists (still a directory on
    disk), is_git. Used by cmd_list() for the CLI's own formatting and by
    the griot_repos_list tool."""
    return [
        {"path": path, "exists": Path(path).is_dir(), "is_git": is_git_repo(path)}
        for path in _load()
    ]


def add_repo(path: str) -> str:
    """Write half of repo_status() for adding one repo.
    Returns the resolved path that was added. Raises ValueError (message
    with no "Error: " prefix — that's the CLI's job, see cmd_add()) on an
    invalid or already-registered path; never touches the file in that case."""
    repo_path = Path(path)
    if not repo_path.is_dir():
        raise ValueError(f"'{path}' doesn't exist or isn't a directory.")

    resolved = str(repo_path.resolve())
    paths = _load()
    if resolved in paths:
        raise ValueError(f"'{resolved}' is already in {common.REPOS_JSON_PATH}.")

    paths.append(resolved)
    _save(paths)
    return resolved


def remove_repo(path: str) -> str:
    """Write half of repo_status() for removing one repo.
    Returns the resolved path that was removed. Raises ValueError on a
    missing repos.json or an unregistered path."""
    resolved = str(Path(path).resolve())
    paths = _load()
    if not paths:
        raise ValueError(f"{common.REPOS_JSON_PATH} doesn't exist or is empty.")
    if resolved not in paths:
        raise ValueError(f"'{resolved}' not found in {common.REPOS_JSON_PATH}.")

    paths.remove(resolved)
    _save(paths)
    return resolved


def cmd_add(path: str) -> int:
    try:
        resolved = add_repo(path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Added: {resolved}")
    if not is_git_repo(resolved):
        # [security review finding] 'index all' walks every registered path
        # for the 'code' source regardless of git-ness (jobs.py deliberately
        # skips the git check when path=None, since the target already IS
        # the full allowed list) — warn here so a non-git directory doesn't
        # get silently mass-indexed with no signal at registration time.
        print(
            f"Warning: '{resolved}' has no .git — 'index all' will still index its files "
            f"for the 'code' source, but commit/tag/branch history needs a real git repo.",
            file=sys.stderr,
        )
    return 0


def cmd_list() -> int:
    statuses = repo_status()
    if not statuses:
        print(f"No repo configured in {common.REPOS_JSON_PATH} (empty).")
        return 0

    for status in statuses:
        # [real finding from the session] repos.json can accumulate paths from
        # a different machine/point in time (repo moved, deleted, or copied
        # from another host) — silently invalid until `griot index` actually
        # tries them. Warning here, on read, is cheaper than discovering it
        # in the middle of a batch indexing run.
        marker = "✓" if status["exists"] else "✗ doesn't exist"
        print(f"  {marker}  {status['path']}")
    return 0


def cmd_remove(path: str) -> int:
    try:
        resolved = remove_repo(path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Removed: {resolved}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot repos",
        description="Manages the list of repos indexed in bulk (repos.json).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_add = sub.add_parser("add", help="Adds a repo (absolute or relative path)")
    p_add.add_argument("path")

    sub.add_parser("list", help="Lists configured repos, flagging the ones that no longer exist on disk")

    p_remove = sub.add_parser("remove", help="Removes a repo from the list")
    p_remove.add_argument("path")

    args = parser.parse_args(argv)
    if args.action == "add":
        return cmd_add(args.path)
    if args.action == "list":
        return cmd_list()
    return cmd_remove(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
