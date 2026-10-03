import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from griot import common



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


def _commit_of(repo_path: Path, name: str) -> str | None:
    """The commit a tag leads to, however many tags are on the way. Asked of
    git only for a tag of a tag: for-each-ref peels one level."""
    try:
        done = common.run_git(repo_path, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                                          f"refs/tags/{name}^{{commit}}"], timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def list_tags(repo_path: Path) -> list[dict]:
    # `objectname` of an annotated tag is the hash of the TAG OBJECT, which
    # names no commit; `*objectname` is what it points at (empty for a
    # lightweight tag, which is the commit itself). The message is asked for
    # as subject and body: `contents` repeats the subject and, for a signed
    # tag, ends with the signature block.
    fmt = common.git_format(
        "%(refname:short)", "%(objectname)", "%(objecttype)", "%(*objectname)", "%(*objecttype)",
        "%(creatordate:iso-strict)", "%(subject)", "%(contents:body)", nul="%00")
    try:
        output = common.run_git(repo_path, ["for-each-ref", "refs/tags", f"--format={fmt}"], timeout=60).stdout
    except subprocess.CalledProcessError as e:
        print(f"Error reading tags from {repo_path.name}: {e}")
        return []

    tags = []
    for name, object_hash, object_type, peeled_hash, peeled_type, date, subject, body in common.git_records(output, 8):
        if not name or not common.is_git_hash(object_hash):
            continue  # not where a record begins: see git_records()
        if object_type != "tag":
            commit_hash = object_hash
        elif peeled_type == "commit":
            commit_hash = peeled_hash
        else:
            # A tag of a tag (or of something that is no commit at all: then
            # the hash of what it points at is the best there is).
            commit_hash = _commit_of(repo_path, name) or peeled_hash or object_hash
        tags.append({
            "name": name,
            "commit_hash": commit_hash,
            "date": date,
            "subject": subject,
            "contents": body.strip(),
        })
    return tags


def build_documents(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    tags = list_tags(repo_path)
    documents = []
    for tag in tags:
        text = tag["subject"]
        if tag["contents"]:
            text = f"{tag['subject']}\n\n{tag['contents']}"
        if not text.strip():
            text = tag["name"]
        # A release tag can hold the whole release note. As ONE document an
        # embedding model reads only its start, and an API that refuses an
        # oversized input fails that tag on every run: cut like a commit
        # message is. The id of a tag that fits in one chunk (nearly all of
        # them) is deliberately unchanged, with no ':0' suffix, so that
        # what is already indexed is recognised.
        chunks = common.chunk_text(text, where=f"{repo_path.name} tag {tag['name']}")
        for i, chunk in enumerate(chunks):
            doc_id = f"{key}:tag:{tag['name']}" if len(chunks) == 1 else f"{key}:tag:{tag['name']}:{i}"
            documents.append({
                "id": doc_id,
                "content": chunk,
                "metadata": {
                    "source_type": "tag",
                    "repo": repo_path.name,
                    "tag_name": tag["name"],
                    "commit_hash": tag["commit_hash"],
                    "date": tag["date"],
                    "chunk_index": i,
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
        print(f"{repo_path.name}: {len(docs)} tags")
        all_documents.extend(docs)

    # What stale-point removal may act on (see common.prune_orphans for the fences).
    prune_scope = dict(source_type="tag", repo_paths=repo_paths_str, used_path=bool(args.path),
                       incomplete=(), force=args.prune)

    if not all_documents:
        print("\nNo tags to index.")
        return

    if args.dry_run:
        common.dry_run(all_documents, source="tags", unit="tags", desc="Checking tags", prune_scope=prune_scope)
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing tags")
    redacted = common.report_redactions()
    pruned = common.prune_orphans(all_documents, failed=failed, **prune_scope)

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} tags indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_tags.py", repo=args.repo or args.path or "all", repo_paths=repo_paths_str,
        indexed=indexed, skipped=skipped, failed=failed, redacted=redacted, pruned=pruned,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
