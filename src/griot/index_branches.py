import argparse
import hashlib
import subprocess
import time
from pathlib import Path

from griot import common

FIELD_SEP = "\x1f"
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
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout


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
    fmt = FIELD_SEP.join(["%H", "%an", "%aI", "%s"])
    try:
        # [L1] --end-of-options: refs come from `git branch -r` (today always
        # origin/*), but a ref must never be interpretable as an option
        line = run_git(repo_path, "log", "-1", f"--pretty=format:{fmt}", "--end-of-options", branch).strip()
    except subprocess.CalledProcessError:
        return None
    if not line:
        return None
    commit_hash, author, date, subject = line.split(FIELD_SEP)
    return {"hash": commit_hash, "author": author, "date": date, "subject": subject}


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
    documents = []
    for branch in remote_branches(repo_path):
        if branch == base:
            continue  # already covered by index_code.py (code) and index_commits.py
        commit = last_commit(repo_path, branch)
        if commit is None:
            continue
        ahead = ahead_commits(repo_path, base, branch) if base else []

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
    args = parser.parse_args(argv)

    start_time = time.time()

    if args.path:
        # --path skips repos.json entirely: the rest of the flow already
        # operates on Path objects identical to the ones from repos.json, no change needed.
        repo_paths_str = [args.path]
    else:
        try:
            repo_paths_str = common.load_repos()
        except FileNotFoundError:
            print(f"Error: {common.REPOS_JSON_PATH} not found.")
            return

        if args.repo:
            repo_paths_str = [p for p in repo_paths_str if Path(p).name == args.repo]
            if not repo_paths_str:
                print(f"Error: no repo named '{args.repo}' in repos.json.")
                return

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

    if not all_documents:
        print("\nNo branches to index.")
        return

    if args.dry_run:
        pending, up_to_date = common.count_pending(all_documents, desc="Checking branches")
        print(f"\n[dry-run] {pending} branches would need to be (re)embedded, {up_to_date} are already up to date.")
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing branches")

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} branches indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_branches.py", repo=args.repo or args.path or "all",
        indexed=indexed, skipped=skipped, failed=failed,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    main()
