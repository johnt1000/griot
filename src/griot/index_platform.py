"""`griot index platform` (formerly `griot index gitlab`, generalized on
2026-08-13 to cover GitHub/GitLab/Bitbucket/Azure DevOps/Gitea — see
platforms.py for details on each adapter and what each one supports)."""

import argparse
import hashlib
import time
from pathlib import Path

from griot import common, platforms


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


def _remote_url(repo_path: Path) -> str | None:
    try:
        return common.run_git(repo_path, ["remote", "get-url", "origin"], timeout=10).stdout.strip()
    except Exception:
        return None


def build_chunks(content: str, id_prefix: str, base_metadata: dict) -> list[dict]:
    chunks = common.chunk_text(content) if content else [""]
    documents = []
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        documents.append({
            "id": f"{id_prefix}:{i}",
            "content": chunk,
            "metadata": {**base_metadata, "chunk_index": i},
        })
    return documents


def build_mr_documents(repo_name: str, platform: str, project_id: str, host: str | None, id_prefix: str | None = None) -> list[dict]:
    key = id_prefix or repo_name
    mrs = platforms.fetch_pull_requests(platform, project_id, host)
    documents = []
    for mr in mrs:
        text = mr.get("title", "")
        if mr.get("description"):
            text += "\n\n" + mr["description"]
        documents.extend(build_chunks(text, f"{key}:mr:{mr.get('iid')}", {
            "source_type": "merge_request",
            "repo": repo_name,
            "mr_iid": mr.get("iid"),
            "state": mr.get("state"),
            "author": mr.get("author"),
            "created_at": mr.get("created_at"),
            "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"),
        }))
    return documents


def build_release_documents(repo_name: str, platform: str, project_id: str, host: str | None, id_prefix: str | None = None) -> list[dict]:
    key = id_prefix or repo_name
    releases = platforms.fetch_releases(platform, project_id, host)
    documents = []
    for release in releases:
        text = release.get("name") or release.get("tag_name") or ""
        if release.get("description"):
            text += "\n\n" + release["description"]
        documents.extend(build_chunks(text, f"{key}:release:{release.get('tag_name')}", {
            "source_type": "release",
            "repo": repo_name,
            "tag_name": release.get("tag_name"),
            "released_at": release.get("released_at"),
        }))
    return documents


def build_issue_documents(repo_name: str, platform: str, project_id: str, host: str | None, id_prefix: str | None = None) -> list[dict]:
    key = id_prefix or repo_name
    issues = platforms.fetch_issues(platform, project_id, host)
    documents = []
    for issue in issues:
        text = issue.get("title", "")
        if issue.get("description"):
            text += "\n\n" + issue["description"]
        documents.extend(build_chunks(text, f"{key}:issue:{issue.get('iid')}", {
            "source_type": "issue",
            "repo": repo_name,
            "issue_iid": issue.get("iid"),
            "state": issue.get("state"),
            "author": issue.get("author"),
            "created_at": issue.get("created_at"),
        }))
    return documents


def build_documents(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    remote_url = _remote_url(repo_path)
    if not remote_url:
        print(f"WARNING: could not determine the 'origin' remote of {repo_path.name}.")
        return []

    detected = platforms.detect_platform(remote_url)
    if not detected:
        print(f"WARNING: {repo_path.name}'s remote isn't from any recognized platform ({remote_url}).")
        return []
    platform, project_id, host = detected

    documents = []
    for label, builder in [
        ("merge/pull requests", build_mr_documents),
        ("releases", build_release_documents),
        ("issues", build_issue_documents),
    ]:
        try:
            docs = builder(repo_path.name, platform, project_id, host, id_prefix=key)
            print(f"  {repo_path.name} [{platform}:{project_id}]: {len(docs)} chunks from {label}")
            documents.extend(docs)
        except Exception as e:
            print(f"  WARNING: failed fetching {label} from {project_id} ({platform}): {e}")
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description="Indexes merge/pull requests, releases and issues (GitHub/GitLab/Bitbucket/Azure DevOps/Gitea) from the repositories into the RAG vector store.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many chunks would need to be (re)embedded, without spending anything on embedding (still queries the platform's API to know what exists).")
    parser.add_argument("--prune", action="store_true", help="Accepted so `griot index all --prune` can forward it; points from the code platform are never removed automatically, because a failed or partial API listing would look the same as deleted items.")
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
        all_documents.extend(build_documents(repo_path, repo_key=repo_key))

    if not all_documents:
        print("\nNo platform items to index.")
        return

    if args.dry_run:
        pending, up_to_date = common.count_pending(all_documents, desc="Checking platform")
        print(f"\n[dry-run] {pending} chunks would need to be (re)embedded, {up_to_date} are already up to date.")
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing platform")

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} chunks indexed, {skipped} unchanged (skipped), {failed} failed.")
    common.log_run_summary(
        script="index_platform.py", repo=args.repo or args.path or "all",
        indexed=indexed, skipped=skipped, failed=failed,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    main()
