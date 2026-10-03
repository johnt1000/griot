import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from griot import common

MAX_AHEAD_COMMITS = 20


def _repo_key_for_path(repo_path: Path) -> str:
    """Only used when the repo comes from --path (outside repos.json
    curation): appends a short hash of the resolved absolute path to the
    basename, so two directories with the same final name in different
    locations don't collide in the natural id. Only
    affects the id — metadata['repo'] stays the raw basename, and the
    --repo/repos.json path never calls this, preserving the usual id so as
    not to break idempotency of already-indexed data."""
    digest = hashlib.md5(str(repo_path.resolve()).encode()).hexdigest()[:8]
    return f"{repo_path.name}-{digest}"


def run_git(repo_path: Path, *args: str) -> str:
    return common.run_git(repo_path, list(args), timeout=60).stdout


def default_branch(repo_path: Path) -> str | None:
    try:
        ref = run_git(repo_path, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
        return ref.removeprefix("refs/remotes/")
    except subprocess.CalledProcessError:
        return None


def remote_branches(repo_path: Path) -> list[str]:
    try:
        output = run_git(repo_path, "branch", "-r")
    except subprocess.CalledProcessError:
        return []
    branches = []
    for line in output.splitlines():
        name = line.strip()
        if not name or "->" in name:  # skip "origin/HEAD -> origin/main"
            continue
        branches.append(name)
    return branches


def last_commit(repo_path: Path, branch: str) -> dict | None:
    fmt = common.git_format("%H", "%an", "%aI", "%s")
    try:
        # [L1] --end-of-options: refs come from `git branch -r` (today always
        # origin/*), but a ref must never be interpretable as an option
        # The trailing `--` says the name is a revision and nothing else:
        # without it, a path of the same name in the work tree (a directory
        # `origin/feat`) makes git stop with "ambiguous argument", and the
        # branch was skipped.
        output = run_git(repo_path, "log", "-1", f"--pretty=format:{fmt}", "--end-of-options", branch, "--")
    except subprocess.CalledProcessError:
        return None
    records = common.git_records(output, 4)
    if not records or not common.is_git_hash(records[0][0]):
        return None
    commit_hash, author, date, subject = records[0]
    return {"hash": commit_hash, "author": author, "date": date, "subject": subject}


# How many names or hashes go into one git command line.
_PER_CALL = 400


def _in_chunks(items: list[str]) -> list[list[str]]:
    return [items[start:start + _PER_CALL] for start in range(0, len(items), _PER_CALL)]


def branch_tips(repo_path: Path, branches: list[str]) -> dict[str, dict] | None:
    """The last commit of every branch in `branches`, {branch: {"hash",
    "author", "date", "subject"}}, in two git calls for all of them instead
    of one `git log -1` per branch. None when git cannot answer that way
    for every one of them: the caller then asks about each on its own.

    The answer is the same text as one branch at a time gives: each name is
    resolved the way `git log -1 <name> --` resolves it (a tag or a local
    branch of the same name wins, a tag object is peeled to its commit,
    something that is no commit fails), and the fields come from `git
    log`'s own formatting (which re-encodes a legacy message and trims a
    subject). Reading the refs with `git for-each-ref` was faster
    still and answered differently in each of those cases; the text of a
    document decides whether it is embedded again."""
    hashes: list[str] = []
    commits: dict[str, dict] = {}
    if any(branch.startswith("-") for branch in branches):
        # `git rev-parse` has no way to say "what follows is not an option"
        # (it prints --end-of-options back), and a remote can be named
        # `-x`. Such a repository is read one branch at a time, where the
        # name goes after --end-of-options.
        return None
    try:
        for names in _in_chunks(branches):
            resolved = run_git(repo_path, "rev-parse", *[f"{name}^{{commit}}" for name in names])
            found = resolved.split()
            if len(found) != len(names) or not all(common.is_git_hash(value) for value in found):
                return None
            hashes.extend(found)
        fmt = common.git_format("%H", "%an", "%aI", "%s")
        for some in _in_chunks(sorted(set(hashes))):
            output = run_git(repo_path, "log", "--no-walk=unsorted", f"--pretty=format:{fmt}", "--end-of-options", *some)
            for commit_hash, author, date, subject in common.git_records(output, 4):
                commits[commit_hash] = {"hash": commit_hash, "author": author, "date": date, "subject": subject}
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None  # a name that is no commit, a git that is too old, a repository too slow
    if not all(commit_hash in commits for commit_hash in hashes):
        return None
    return {branch: commits[commit_hash] for branch, commit_hash in zip(branches, hashes)}


def tips_ahead_of(repo_path: Path, base: str, hashes: set[str]) -> set[str] | None:
    """Which of these commits `base` does not contain, in one git call per
    few hundred of them. A branch has something ahead of `base` exactly when
    its last commit is one of them, so `git log base..branch` is only worth
    running for those: on most repositories nearly every remote branch was
    merged long ago. None when git could not say (a default branch that
    does not resolve): every branch is then asked, as before.

    `base` is given by the very name ahead_commits() gives `git log`, as an
    argument and never inside a format string."""
    ahead: set[str] = set()
    try:
        for some in _in_chunks(sorted(hashes)):
            output = run_git(repo_path, "rev-list", "--end-of-options", *some, f"^{base}")
            ahead.update(output.split())
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None
    return ahead & hashes


def ahead_commits(repo_path: Path, base: str, branch: str) -> list[str]:
    try:
        # options before --end-of-options, range after (same reason as above)
        output = run_git(repo_path, "log", "--oneline", f"-{MAX_AHEAD_COMMITS}", "--end-of-options", f"{base}..{branch}")
    except subprocess.CalledProcessError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def build_documents(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    base = default_branch(repo_path)
    # The default branch is already covered by index_code.py (code) and index_commits.py.
    branches = [branch for branch in remote_branches(repo_path) if branch != base]
    tips = branch_tips(repo_path, branches)
    ahead_tips = tips_ahead_of(repo_path, base, {tip["hash"] for tip in tips.values()}) if tips and base else None
    documents = []
    for branch in branches:
        # Without the answer for all of them at once: one branch at a time.
        commit = tips[branch] if tips is not None else last_commit(repo_path, branch)
        if commit is None:
            continue
        if not base or (ahead_tips is not None and commit["hash"] not in ahead_tips):
            ahead = []  # the default branch contains it: there is nothing to list
        else:
            ahead = ahead_commits(repo_path, base, branch)

        text = f"Branch: {branch}\nLast commit: {commit['subject']} ({commit['author']}, {commit['date']})"
        if ahead:
            text += "\n\nCommits ahead of " + (base or "default") + ":\n" + "\n".join(ahead)

        documents.append({
            "id": f"{key}:branch:{branch}",
            "content": text,
            "metadata": {
                "source_type": "branch",
                "repo": repo_path.name,
                "branch_name": branch,
                "last_commit_hash": commit["hash"],
                "last_commit_date": commit["date"],
            },
        })
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description="Indexes a summary of the repositories' remote branches (excluding the default) into the RAG vector store.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many branches would need to be (re)embedded, without spending anything.")
    parser.add_argument("--prune", action="store_true", help="Remove stale points (their source is no longer there) even when they are more than half of what is indexed for a repository.")
    args = parser.parse_args(argv)

    start_time = time.time()

    if args.path:
        # --path skips repos.json entirely: the rest of the flow already
        # operates on Path objects identical to the ones from repos.json, no change needed.
        # Resolved: `--path .` must still give the repository its directory
        # name (Path(".").name is empty), in its points and in its ids.
        repo_paths_str = [str(Path(args.path).resolve())]
    else:
        try:
            repo_paths_str = common.load_repos()
        except FileNotFoundError:
            print(f"Error: {common.REPOS_JSON_PATH} not found.", file=sys.stderr)
            return 1

        if args.repo:
            repo_paths_str = [p for p in repo_paths_str if Path(p).name == args.repo]
            if not repo_paths_str:
                print(f"Error: no repo named '{args.repo}' in repos.json.", file=sys.stderr)
                return 1

    # One missing path among several is a warning (below). None of them
    # existing is the same failure as a name that matches nothing: the run
    # could not start, and saying "nothing to index" with exit status 0 would
    # let a script believe the index is up to date.
    if not repo_paths_str:
        print("Error: no repository is registered. Register one with `griot repos add <path>`.", file=sys.stderr)
        return 1
    if not any(Path(p).is_dir() for p in repo_paths_str):
        print(f"Error: none of the {len(repo_paths_str)} path(s) to index is a directory: "
              f"{', '.join(repo_paths_str[:3])}{' ...' if len(repo_paths_str) > 3 else ''}", file=sys.stderr)
        return 1

    all_documents = []
    for path_str in repo_paths_str:
        repo_path = Path(path_str)
        if not repo_path.is_dir():
            print(f"WARNING: '{repo_path}' is not a valid directory.")
            continue
        repo_key = _repo_key_for_path(repo_path) if args.path else None
        docs = build_documents(repo_path, repo_key=repo_key)
        print(f"{repo_path.name}: {len(docs)} branches")
        all_documents.extend(docs)

    # What stale-point removal may act on (see common.prune_orphans for the fences).
    prune_scope = dict(source_type="branch", repo_paths=repo_paths_str, used_path=bool(args.path),
                       incomplete=(), force=args.prune)

    if not all_documents:
        print("\nNo branches to index.")
        return

    if args.dry_run:
        common.dry_run(all_documents, source="branches", unit="branches", desc="Checking branches", prune_scope=prune_scope)
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing branches")
    redacted = common.report_redactions()
    pruned = common.prune_orphans(all_documents, failed=failed, **prune_scope)

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} branches indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_branches.py", repo=args.repo or args.path or "all", repo_paths=repo_paths_str,
        indexed=indexed, skipped=skipped, failed=failed, redacted=redacted, pruned=pruned,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
