import argparse
import hashlib
import subprocess
import time
from pathlib import Path

from griot import common

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


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


def list_commits(repo_path: Path) -> list[dict]:
    """git log --all: covers commits reachable from any branch (local or
    remote), without duplicating — a commit reachable from two branches appears once."""
    fmt = FIELD_SEP.join(["%H", "%an", "%aI", "%s", "%b"]) + RECORD_SEP
    try:
        output = common.run_git(repo_path, ["log", "--all", f"--pretty=format:{fmt}"], timeout=120).stdout
    except subprocess.CalledProcessError as e:
        print(f"Error reading commits from {repo_path.name}: {e}")
        return []

    commits = []
    for record in output.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        commit_hash, author, date, subject, body = record.split(FIELD_SEP)
        commits.append({
            "hash": commit_hash,
            "author": author,
            "date": date,
            "subject": subject,
            "body": body.strip(),
        })
    return commits


def build_documents(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    documents = []
    for commit in list_commits(repo_path):
        text = commit["subject"]
        if commit["body"]:
            text = f"{commit['subject']}\n\n{commit['body']}"
        # [real bug fix, 2026-08-21] A release/promote commit's body can
        # run well over 8k tokens — embedding it as ONE oversized chunk
        # gets rejected outright by OpenAI-compatible APIs (HTTP 400),
        # failing the whole commit on EVERY reindex forever (a one-off
        # backfill script fixed the on-disk DATA once; this code itself
        # never actually chunked). Same common.chunk_text() index_code.py
        # already uses. The id format for the single-chunk case (the
        # overwhelming majority of commits) is deliberately UNCHANGED —
        # no ':0' suffix — appending one unconditionally would silently
        # break stable_id()-based idempotency for every commit ever
        # indexed before this fix, which is exactly the class of bug this
        # investigation started from (see jobs.py's --repo/--path fix).
        chunks = common.chunk_text(text)
        for i, chunk in enumerate(chunks):
            doc_id = f"{key}:commit:{commit['hash']}" if len(chunks) == 1 else f"{key}:commit:{commit['hash']}:{i}"
            documents.append({
                "id": doc_id,
                "content": chunk,
                "metadata": {
                    "source_type": "commit",
                    "repo": repo_path.name,
                    "commit_hash": commit["hash"],
                    "author": commit["author"],
                    "date": commit["date"],
                    "chunk_index": i,
                },
            })
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description="Indexes the commit history (git log --all) of the repositories into the RAG vector store.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many commits would need to be (re)embedded, without spending anything.")
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
        print(f"{repo_path.name}: {len(docs)} commits")
        all_documents.extend(docs)

    if not all_documents:
        print("\nNo commits to index.")
        return

    if args.dry_run:
        pending, up_to_date = common.count_pending(all_documents, desc="Checking commits")
        print(f"\n[dry-run] {pending} commits would need to be (re)embedded, {up_to_date} are already up to date.")
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing commits")

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} commits indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_commits.py", repo=args.repo or args.path or "all",
        indexed=indexed, skipped=skipped, failed=failed,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    main()
