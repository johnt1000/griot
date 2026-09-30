import argparse
import hashlib
import subprocess
import time
from pathlib import Path

from griot import common

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


def _repo_key_for_path(repo_path: Path) -> str:
    """Only used when the repo comes from --path (outside repos.json curation):
    appends a short hash of the resolved absolute path to the basename, so two
    directories with the same final name in different locations don't collide
    in the natural id. Only affects the id — metadata['repo']
    remains the raw basename, and the --repo/repos.json path never calls this,
    preserving the usual id so as not to break idempotency of already-indexed
    data."""
    digest = hashlib.md5(str(repo_path.resolve()).encode()).hexdigest()[:8]
    return f"{repo_path.name}-{digest}"


def list_tags(repo_path: Path) -> list[dict]:
    fmt = FIELD_SEP.join([
        "%(refname:short)", "%(objectname)", "%(creatordate:iso-strict)",
        "%(subject)", "%(contents)",
    ]) + RECORD_SEP
    try:
        output = common.run_git(repo_path, ["for-each-ref", "refs/tags", f"--format={fmt}"], timeout=60).stdout
    except subprocess.CalledProcessError as e:
        print(f"Error reading tags from {repo_path.name}: {e}")
        return []

    tags = []
    for record in output.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        name, commit_hash, date, subject, contents = record.split(FIELD_SEP)
        tags.append({
            "name": name,
            "commit_hash": commit_hash,
            "date": date,
            "subject": subject,
            "contents": contents.strip(),
        })
    return tags


def build_documents(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    tags = list_tags(repo_path)
    documents = []
    for tag in tags:
        text = tag["subject"]
        if tag["contents"] and tag["contents"] != tag["subject"]:
            text = f"{tag['subject']}\n\n{tag['contents']}"
        if not text.strip():
            text = tag["name"]
        documents.append({
            "id": f"{key}:tag:{tag['name']}",
            "content": text,
            "metadata": {
                "source_type": "tag",
                "repo": repo_path.name,
                "tag_name": tag["name"],
                "commit_hash": tag["commit_hash"],
                "date": tag["date"],
            },
        })
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description="Indexes the tags of the repositories into the RAG vector store.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many tags would need to be (re)embedded, without spending anything.")
    parser.add_argument("--prune", action="store_true", help="Remove stale points (their source is no longer there) even when they are more than half of what is indexed for a repository.")
    args = parser.parse_args(argv)

    start_time = time.time()

    if args.path:
        # --path skips repos.json entirely: the rest of the flow already operates
        # on Path objects identical to the ones coming from repos.json, with no change at all.
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
        print(f"{repo_path.name}: {len(docs)} tags")
        all_documents.extend(docs)

    # What stale-point removal may act on (see common.prune_orphans for the fences).
    prune_scope = dict(source_type="tag", repo_paths=repo_paths_str, used_path=bool(args.path),
                       incomplete=(), force=args.prune)

    if not all_documents:
        print("\nNo tags to index.")
        return

    if args.dry_run:
        pending, up_to_date = common.count_pending(all_documents, desc="Checking tags")
        print(f"\n[dry-run] {pending} tags would need to be (re)embedded, {up_to_date} are already up to date.")
        common.prune_orphans(all_documents, dry_run=True, **prune_scope)
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing tags")
    pruned = common.prune_orphans(all_documents, failed=failed, **prune_scope)

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} tags indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_tags.py", repo=args.repo or args.path or "all",
        indexed=indexed, skipped=skipped, failed=failed, pruned=pruned,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    main()
