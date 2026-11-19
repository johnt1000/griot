"""`griot repos` — manages the list of repos indexed in bulk
(`common.REPOS_JSON_PATH`, used by `griot index <source>`/`index all` without
`--path`). Closes a configuration gap that previously could only be edited by
hand-editing the JSON.
"""

import argparse
import json
import sys
from pathlib import Path

from griot import UsageError, common


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
    """One dict per registered repo: name (its directory name, which is the
    `repo` of everything indexed from it and what a search is filtered by),
    path, exists (still a directory on disk), is_git. Used by cmd_list() for
    the CLI's own formatting and by the griot_repos_list tool."""
    return [
        {"name": Path(path).name, "path": path, "exists": Path(path).is_dir(), "is_git": is_git_repo(path)}
        for path in _load()
    ]


def list_result() -> dict:
    """What `griot repos list --json`, the griot_repos_list tool and the
    griot://repos resource return, built here so the three cannot come to
    disagree: {repos: repo_status(), count}. Raises OSError or ValueError
    when repos.json cannot be read (unreadable_sentence() says it)."""
    entries = repo_status()
    return {"repos": entries, "count": len(entries)}


# What fixes a repos.json griot cannot read, once it was fixed or deleted.
_REGISTER = "griot repos add <path>"


def unreadable_sentence(cause) -> str:
    """The one wording for a repos.json that could not be read, on every
    surface that reads it (`griot repos list`, griot_repos_list, `griot
    doctor`): the file, why (common.unreadable_cause), and the command last.
    `cause` is the exception or its already classified text."""
    if isinstance(cause, BaseException):
        cause = common.unreadable_cause(cause)
    return (f"{common.REPOS_JSON_PATH} could not be read ({cause}). Fix or delete it, then register again:\n"
            f"  {_REGISTER}")


def unreadable_fix() -> str:
    """The fix `griot doctor` gives for the same file, in its fix column."""
    return f"{_REGISTER}   # after fixing or deleting {common.REPOS_JSON_PATH}"


# How a registered path that is no longer a directory is named on the CLI
# surfaces (`griot repos list`, `griot doctor`); `exists: false` in the JSON
# of `griot repos list --json` and griot_repos_list.
GONE = "registered but no longer on disk"


def check_add(path: str) -> str:
    """Read-only half of add_repo(): the resolved path it would register.
    Raises the same ValueError it would. Lets the CLI refuse a bad path
    before asking anyone to confirm it."""
    repo_path = Path(path)
    if not repo_path.is_dir():
        raise UsageError(f"'{path}' doesn't exist or isn't a directory.")

    resolved = str(repo_path.resolve())
    registered = _load()
    if resolved in registered:
        # A conflict with what the list holds, not a wrong argument: the
        # command was well formed and did not run (exit 1).
        raise ValueError(f"'{resolved}' is already in {common.REPOS_JSON_PATH}.")
    # The directory name is what a repository's points and ids are keyed on.
    # Two registered repositories with the same one overwrite each other's
    # points on every run, re-embed them each time, and both report success.
    name = Path(resolved).name
    same_name = [p for p in registered if Path(p).name == name]
    if same_name:
        raise ValueError(
            f"another registered repository is already named '{name}' ({same_name[0]}). griot tells "
            f"repositories apart by directory name: rename one of the directories, or index this one "
            f"on its own with `griot index all --path`.")
    return resolved


def add_repo(path: str) -> str:
    """Write half of repo_status() for adding one repo.
    Returns the resolved path that was added. Raises ValueError (message
    with no "Error: " prefix — that's the CLI's job, see cmd_add()) on an
    invalid or already-registered path; never touches the file in that case."""
    resolved = check_add(path)
    paths = _load()
    paths.append(resolved)
    _save(paths)
    return resolved


def check_remove(path: str) -> str:
    """Read-only half of remove_repo(): the resolved path it would remove.
    Raises the same ValueError it would."""
    resolved = str(Path(path).resolve())
    paths = _load()
    if not paths:
        raise UsageError(f"{common.REPOS_JSON_PATH} doesn't exist or is empty.")
    if resolved not in paths:
        raise UsageError(f"'{resolved}' not found in {common.REPOS_JSON_PATH}.")
    return resolved


def remove_repo(path: str) -> str:
    """Write half of repo_status() for removing one repo.
    Returns the resolved path that was removed. Raises ValueError on a
    missing repos.json or an unregistered path."""
    resolved = check_remove(path)
    paths = _load()
    paths.remove(resolved)
    _save(paths)
    return resolved


_LIST = "griot repos list"
_ADD = "griot repos add <directory>"


def _refuse(error: ValueError, next_command: str) -> int:
    """The one way a refused repos command ends: the sentence, the next
    command last, and the exit status by kind (2 for a wrong request, 1 for
    a conflict with the list or a list that could not be read). The last line
    matches the cause: a path that is not a directory is answered with how to
    add one that is, not with the list."""
    print(f"Error: {error}\n  {next_command}", file=sys.stderr)
    return 2 if isinstance(error, UsageError) else 1


def _next_after_refused_add(error: ValueError) -> str:
    return _ADD if isinstance(error, UsageError) else _LIST


def cmd_add(path: str) -> int:
    try:
        resolved = add_repo(path)
    except ValueError as e:
        return _refuse(e, _next_after_refused_add(e))
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


def cmd_list(as_json: bool = False) -> int:
    try:
        result = list_result()
    except (OSError, ValueError) as e:
        # A file that is there and cannot be read is not "nothing
        # registered": an error on stderr, and stdout stays empty so that a
        # script reading --json does not take it for an empty list.
        print(f"Error: {unreadable_sentence(e)}", file=sys.stderr)
        return 1
    if as_json:
        common.print_json(result)
        return 0
    statuses = result["repos"]
    if not statuses:
        print(f"No repository registered in {common.REPOS_JSON_PATH}.\n  {_REGISTER}")
        return 0

    for status in statuses:
        # [real finding from the session] repos.json can accumulate paths from
        # a different machine/point in time (repo moved, deleted, or copied
        # from another host) — silently invalid until `griot index` actually
        # tries them. Warning here, on read, is cheaper than discovering it
        # in the middle of a batch indexing run.
        marker = "✓" if status["exists"] else f"✗ {GONE}"
        print(f"  {marker}  {status['path']}")
    # The fix last, as `griot doctor` gives it, one command per path.
    for status in statuses:
        if not status["exists"]:
            print(f"  griot repos remove {status['path']}")
    return 0


def cmd_remove(path: str) -> int:
    try:
        resolved = remove_repo(path)
    except ValueError as e:
        return _refuse(e, _LIST)
    print(f"Removed: {resolved}")
    return 0


def main(argv=None) -> int:
    from griot.cli import json_flag_help, show_flags_read_by_griot  # here: cli is imported before these modules at run time
    parser = argparse.ArgumentParser(
        prog="griot repos",
        description="Manages the list of repos indexed in bulk (repos.json).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    show_flags_read_by_griot(parser)

    p_add = sub.add_parser(
        "add", help="Adds a repo (absolute or relative path)",
        description="Adds a repo (absolute or relative path) to the list indexed in bulk. Anything under it can then "
                    "be sent to the embedding API, so it asks first, at an interactive terminal, and there is no "
                    "flag that answers: run from a script or an agent's shell, with no terminal, it changes nothing.")
    p_add.add_argument("path")

    p_list = sub.add_parser("list", help="Lists configured repos, flagging the ones that no longer exist on disk")
    p_list.add_argument("--json", action="store_true", help=json_flag_help("docs/getting-started.md"))

    p_remove = sub.add_parser("remove", help="Removes a repo from the list")
    p_remove.add_argument("path")
    p_remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    args = parser.parse_args(argv)
    if args.action == "list":
        return cmd_list(as_json=args.json)

    # Confirmation lives here, in the command-line entry, and not in
    # cmd_add()/cmd_remove(): those stay the plain operation. What is cheap
    # to check is checked first, so nobody is asked about a path that was
    # going to be refused anyway.
    adding = args.action == "add"
    try:
        resolved = check_add(args.path) if adding else check_remove(args.path)
    except ValueError as e:
        return _refuse(e, _next_after_refused_add(e) if adding else _LIST)
    if adding:
        # No --yes on purpose: registering widens what may be sent to the
        # embedding API (see common.confirm and griot_repos_add).
        refused = common.confirm(f"Register {resolved} as an indexable repo? "
                                 f"Anything under it can then be sent to the embedding API.", yes=None)
        return refused or cmd_add(args.path)
    refused = common.confirm(f"Remove {resolved} from the indexed repos? Data already indexed is kept.",
                             yes=args.yes)
    return refused or cmd_remove(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
