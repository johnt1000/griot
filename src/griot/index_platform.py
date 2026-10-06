"""`griot index platform` (formerly `griot index gitlab`, generalized on
2026-08-13 to cover GitHub/GitLab/Bitbucket/Azure DevOps/Gitea — see
platforms.py for details on each adapter and what each one supports)."""

import argparse
import hashlib
import re
import sys
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
    chunks = common.chunk_text(content, where=id_prefix) if content else [""]
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


def build_documents(repo_path: Path, repo_key: str | None = None, fetches: list | None = None) -> list[dict]:
    """The documents of one repository's merge/pull requests, releases and
    issues. Each fetch attempted is appended to `fetches`, when given, as
    `{"id", "ok", "reason"}`: a fetch the platform refused is a failure of
    the run, not only a warning on the screen."""
    key = repo_key or repo_path.name
    remote_url = _remote_url(repo_path)
    if not remote_url:
        print(f"WARNING: could not determine the 'origin' remote of {repo_path.name}.")
        return []

    detected = platforms.detect_platform(remote_url)
    if not detected:
        # Without the user part: a remote is often written with a token in it.
        shown = re.sub(r"://[^/@\s]+@", "://", remote_url)
        print(f"WARNING: {repo_path.name}'s remote isn't from any recognized platform ({common.printable(shown)}).")
        return []
    platform, project_id, host = detected

    documents = []
    hinted = False
    for label, builder in [
        ("merge/pull requests", build_mr_documents),
        ("releases", build_release_documents),
        ("issues", build_issue_documents),
    ]:
        try:
            docs = builder(repo_path.name, platform, project_id, host, id_prefix=key)
            print(f"  {repo_path.name} [{platform}:{project_id}]: {len(docs)} chunks from {label}")
            documents.extend(docs)
            if fetches is not None:
                fetches.append({"id": f"{key}:platform:{label}", "ok": True, "reason": None})
        except Exception as e:
            print(f"  WARNING: failed fetching {label} from {project_id} ({platform}): {e}")
            status = getattr(getattr(e, "response", None), "status_code", None)
            if fetches is not None:
                # The status or the kind of error, never its text: the text
                # can hold the request URL, and this goes into logs.db.
                reason = f"HTTP {status}" if status else type(e).__name__
                fetches.append({"id": f"{key}:platform:{label}", "ok": False, "reason": reason})
            env_var = platforms.TOKEN_ENV.get(platform)
            if status in (401, 403) and env_var and not hinted:
                # Which token the platform refused: the one exported in the
                # shell can hide the one griot stores, and nothing said so.
                print(f"    {common.credential_hint(env_var)}")
                hinted = True
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
    fetches: list[dict] = []
    for path_str in repo_paths_str:
        repo_path = Path(path_str)
        if not repo_path.is_dir():
            print(f"WARNING: '{repo_path}' is not a valid directory.")
            continue
        repo_key = _repo_key_for_path(repo_path) if args.path else None
        all_documents.extend(build_documents(repo_path, repo_key=repo_key, fetches=fetches))

    refused = [{"id": f["id"], "reason": f["reason"]} for f in fetches if not f["ok"]]
    if refused and not any(f["ok"] for f in fetches):
        # Nothing the platform was asked for came back (an expired token
        # answers 401 to all of it): the run could not do its job, and an
        # exit status of 0 would let `griot index all` call it complete.
        print(f"Error: the platform refused all {len(refused)} fetch(es); nothing was indexed. "
              f"Check the token (`griot auth list`) and the warnings above.", file=sys.stderr)
        # `error` is what makes the record a run that did not do its job: the
        # freshness report does not count it as indexing the source, and
        # `griot doctor` and `griot stats` report it.
        # A dry run writes no run (cli.py::_run_index_source): one would
        # shadow the last real run in griot_index_status.
        reasons = ", ".join(sorted({f["reason"] for f in refused}))
        if not args.dry_run:
            _log_run(args, repo_paths_str, start_time, indexed=0, skipped=0, failed=len(refused), failures=refused,
                     error=f"the platform refused every fetch ({reasons})")
        return 1

    if not all_documents:
        print("\nNo platform items to index.")
        if refused and not args.dry_run:
            _log_run(args, repo_paths_str, start_time, indexed=0, skipped=0, failed=len(refused), failures=refused)
        return

    if args.dry_run:
        common.dry_run(all_documents, source="platform", unit="chunks", desc="Checking platform")
        return

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing platform")
    redacted = common.report_redactions()

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} chunks indexed, {skipped} unchanged (skipped), {failed + len(refused)} failed"
          + (f" ({len(refused)} fetch(es) refused by the platform)." if refused else "."))
    _log_run(args, repo_paths_str, start_time, indexed=indexed, skipped=skipped, failed=failed + len(refused),
             redacted=redacted,
             # [user-requested] WHICH documents failed, not just how many —
             # the count alone forced a grep through griot.log to diagnose.
             failures=(refused + common.last_run_failures())[:common.MAX_RECORDED_FAILURES])


def _log_run(args, repo_paths_str, start_time, *, indexed, skipped, failed, failures, redacted=0, **extra) -> None:
    common.log_run_summary(
        script="index_platform.py", repo=args.repo or args.path or "all", repo_paths=repo_paths_str,
        indexed=indexed, skipped=skipped, failed=failed, redacted=redacted,
        duration_seconds=round(time.time() - start_time, 2),
        spend_today_usd=common.get_spend_today(),
        failures=failures,
        **extra,
    )


if __name__ == "__main__":
    raise SystemExit(main())
