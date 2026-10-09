"""`griot index platform` (formerly `griot index gitlab`, generalized on
2026-08-13 to cover GitHub/GitLab/Bitbucket/Azure DevOps/Gitea — see
platforms.py for details on each adapter and what each one supports)."""

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

from griot import common, freshness, platforms


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
            "metadata": common.result_metadata({**base_metadata, "chunk_index": i}),
        })
    return documents


def _url_field(item: dict) -> dict:
    """The item's web page, for an adapter that gives one (GitLab). Left out
    rather than stored as null otherwise, so the points of the platforms that
    give none keep the payload they always had."""
    return {"url": item["url"]} if item.get("url") else {}


def _author_field(item: dict) -> dict:
    """The release's author, for an adapter whose platform gives one. Left
    out rather than stored as null otherwise: Bitbucket and Azure DevOps
    have no release griot reads, and a release indexed before releases had
    authors keeps the payload it was written with until a run gives it one."""
    return {"author": item["author"]} if item.get("author") else {}


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
            **_url_field(mr),
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
            **_author_field(release),
            **_url_field(release),
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
            **_url_field(issue),
        }))
    return documents


def build_documents(repo_path: Path, repo_key: str | None = None, fetches: list | None = None) -> list[dict]:
    """The documents of one repository's merge/pull requests, releases and
    issues. Each fetch attempted is appended to `fetches`, when given, as
    `{"id", "ok", "reason"}`: a fetch the platform refused is a failure of
    the run, not only a warning on the screen."""
    key = repo_key or repo_path.name
    # As printed: a directory name, or a project path read from the remote,
    # can hold an escape sequence that would drive the terminal.
    shown_name = common.printable(repo_path.name)
    remote_url = _remote_url(repo_path)
    if not remote_url:
        print(f"WARNING: could not determine the 'origin' remote of {shown_name}.")
        return []

    # Without the user part: a remote is often written with a token in it.
    shown_remote = common.printable(re.sub(r"://[^/@\s]+@", "://", remote_url))
    detected = platforms.detect_platform(remote_url)
    if not detected:
        print(f"WARNING: {shown_name}'s remote isn't from any recognized platform ({shown_remote}).")
        return []
    platform, project_id, host = detected
    note = platforms.anonymous_read_note(platform)
    if note:
        print(f"  {shown_name}: {note}")

    documents = []
    hinted = False
    for label, builder in [
        ("merge/pull requests", build_mr_documents),
        ("releases", build_release_documents),
        ("issues", build_issue_documents),
    ]:
        try:
            docs = builder(repo_path.name, platform, project_id, host, id_prefix=key)
            print(f"  {shown_name} [{platform}:{common.printable(project_id)}]: {len(docs)} chunks from {label}")
            documents.extend(docs)
            if fetches is not None:
                fetches.append({"id": f"{key}:platform:{label}", "ok": True, "reason": None})
        except Exception as e:
            print(f"  WARNING: failed fetching {label} from {common.printable(project_id)} ({platform}): {e}")
            status = getattr(getattr(e, "response", None), "status_code", None)
            env_var = platforms.TOKEN_ENV.get(platform)
            if fetches is not None:
                # The status or the kind of error, never its text: the text
                # can hold the request URL, and this goes into logs.db.
                reason = f"HTTP {status}" if status else type(e).__name__
                # label, cause and where serve the closing message only:
                # main() records id and reason alone.
                fetches.append({"id": f"{key}:platform:{label}", "ok": False, "reason": reason,
                                "label": label, "cause": _refusal_cause(e, status, env_var),
                                "where": f"{common.printable(project_id)} on {platform} (remote {shown_remote})"})
            # A TokenNeeded already says which variable to set and how.
            if status in (401, 403) and env_var and not hinted and not isinstance(e, platforms.TokenNeeded):
                # Which token the platform refused: the one exported in the
                # shell can hide the one griot stores, and nothing said so.
                print(f"    {common.credential_hint(env_var)}")
                hinted = True
    return documents


# What a refused fetch is blamed on, which decides what the run tells the
# user to fix ([debt 67], seen for real on 2026-10-08: a GitLab project the
# token could not see answered 404 to everything, and the run said "Check
# the token"). The platforms answer 404, not 403, both for a project that
# does not exist and for one the token's account cannot see, so a 404 under
# a token points at the project path and the token's reach, not at the token.
# Recorded per refused repository (refusal_causes), so the readers in
# freshness.py own the names.
NOT_FOUND, TOKEN, OTHER = freshness.REFUSAL_CAUSES
NOT_FOUND_WHY = "HTTP 404: not found, or not visible to the token"


def _refusal_cause(error: Exception, status: int | None, env_var: str | None) -> str:
    token_set = bool(env_var and os.getenv(env_var))
    # TokenNeeded is GitLab's 404 (or 401) to a caller without a token,
    # which a token may well change.
    if isinstance(error, platforms.TokenNeeded) or status in (401, 403):
        return TOKEN
    if status == 404:
        # With no token set, a 404 says nothing about the project.
        return NOT_FOUND if token_set else TOKEN
    # No answer at all: a missing token raises before any request is made,
    # so it is the token's fault only when there is none.
    if status is None and not token_set:
        return TOKEN
    return OTHER


def _refusal_lines(refused_fetches: list[dict]) -> list[str]:
    """One line per project and cause: which fetches got which answer, and
    what to check for it. A mix of causes says which fetch got which."""
    lines = []
    by_where: dict[str, list[dict]] = {}
    for f in refused_fetches:
        by_where.setdefault(f["where"], []).append(f)
    for where, fetches in by_where.items():
        for cause in (NOT_FOUND, TOKEN, OTHER):
            group = [f for f in fetches if f["cause"] == cause]
            if not group:
                continue
            labels = ", ".join(f["label"] for f in group)
            reasons = ", ".join(sorted({f["reason"] for f in group}))
            if cause == NOT_FOUND:
                lines.append(f"  {where}: not found, or not visible to the token in use ({labels}: {reasons}). "
                             f"Check the project path in that remote, and that the token's account can see the "
                             f"project (`griot auth list` shows which token is in use).")
            elif cause == TOKEN:
                lines.append(f"  {where}: {labels} refused ({reasons}). "
                             f"Check the token (`griot auth list`) and the warnings above.")
            else:
                # No answer, a timeout, a 5xx: the platform's side or the
                # network, which a new token would not fix ([debt 72]).
                lines.append(f"  {where}: the platform did not answer or failed ({labels}: {reasons}). "
                             f"This is not the token: check the network or the platform's status, and try "
                             f"again later; see the warnings above.")
    return lines


def _refused_or_failed(refused_fetches: list[dict]) -> str:
    """The verb of the closing message's first line: "refused" blames the
    platform's answer to the request, which is wrong when every fetch
    failed for another cause (no answer, a server error)."""
    return "failed" if all(f["cause"] == OTHER for f in refused_fetches) else "refused"


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
                print(f"Error: no repo named '{common.printable(args.repo)}' in repos.json.", file=sys.stderr)
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
              f"{', '.join(common.printable(p) for p in repo_paths_str[:3])}{' ...' if len(repo_paths_str) > 3 else ''}", file=sys.stderr)
        return 1

    all_documents = []
    fetches: list[dict] = []
    # The repositories whose platform refused every fetch asked of it: the
    # run could not index them, even when the others answered.
    refused_repos: list[str] = []
    # Their refused fetches, for the closing message.
    refused_fetches: list[dict] = []
    # Those of them whose every fetch answered 404 under a token: recorded,
    # so `griot stats` and `griot doctor` point at the project, not the token.
    not_found_repos: list[str] = []
    # Per refused repository, which fetches were refused for which cause
    # (the causes of _refusal_lines()): one whose fetches got a 404 and a
    # 401 is not in not_found_repos, and without this the readers could only
    # give it the token advice ([debt 70]).
    refusal_causes: dict[str, dict[str, list[str]]] = {}
    # Per refused repository, the statuses or kinds of error of its fetches
    # that failed for another cause (no answer, a server error): the
    # readers name them instead of blaming the token ([debt 72]). Only the
    # status or the type, as in `failures`: an error's text can hold a URL.
    other_reasons: dict[str, list[str]] = {}
    for path_str in repo_paths_str:
        repo_path = Path(path_str)
        if not repo_path.is_dir():
            print(f"WARNING: '{common.printable(path_str)}' is not a valid directory.")
            continue
        repo_key = _repo_key_for_path(repo_path) if args.path else None
        repo_fetches: list[dict] = []
        all_documents.extend(build_documents(repo_path, repo_key=repo_key, fetches=repo_fetches))
        fetches.extend(repo_fetches)
        if repo_fetches and not any(f["ok"] for f in repo_fetches):
            refused_repos.append(path_str)
            refused_fetches.extend(repo_fetches)
            if all(f["cause"] == NOT_FOUND for f in repo_fetches):
                not_found_repos.append(path_str)
            causes: dict[str, list[str]] = {}
            for f in repo_fetches:
                causes.setdefault(f["cause"], []).append(f["label"])
            refusal_causes[repo_path.name] = causes
            failed_why = sorted({f["reason"] for f in repo_fetches if f["cause"] == OTHER})
            if failed_why:
                other_reasons[repo_path.name] = failed_why

    refused =[{"id": f["id"], "reason": f["reason"]} for f in fetches if not f["ok"]]
    # Named in the record, so `griot doctor` and `griot stats` can say which
    # repository was refused once this stderr is gone (freshness.py).
    # Explicitly, not derived from the failure ids: those are capped at
    # MAX_RECORDED_FAILURES, and under --path they carry the repository's
    # key instead of its name, which is what the freshness report matches.
    refused_names = [Path(p).name for p in refused_repos]
    marked = {"refused_repos": refused_names} if refused_names else {}
    if not_found_repos:
        marked["not_found_repos"] = [Path(p).name for p in not_found_repos]
    if refusal_causes:
        marked["refusal_causes"] = refusal_causes
    if other_reasons:
        marked["other_reasons"] = other_reasons
    verb = _refused_or_failed(refused_fetches)
    if refused and not any(f["ok"] for f in fetches):
        # Nothing the platform was asked for came back (an expired token
        # answers 401 to all of it): the run could not do its job, and an
        # exit status of 0 would let `griot index all` call it complete.
        if verb == "failed":
            print(f"Error: all {len(refused)} fetch(es) to the platform failed; nothing was indexed:", file=sys.stderr)
        else:
            print(f"Error: the platform refused all {len(refused)} fetch(es); nothing was indexed:", file=sys.stderr)
        for line in _refusal_lines(refused_fetches):
            print(line, file=sys.stderr)
        # `error` is what makes the record a run that did not do its job: the
        # freshness report does not count it as indexing the source, and
        # `griot doctor` and `griot stats` report it.
        # A dry run writes no run (cli.py::_run_index_source): one would
        # shadow the last real run in griot_index_status.
        # A 404 under a token says what it means here too: stats and doctor
        # show this error when no line names the refused repositories.
        reasons = ", ".join(sorted({NOT_FOUND_WHY if f["cause"] == NOT_FOUND else f["reason"] for f in refused_fetches}))
        if not args.dry_run:
            # refused_repos too: the error is reported only while this is the
            # newest run of all, and a later code or commits run would hide
            # it; the freshness report finds the refusal in the newest
            # PLATFORM run instead. The error still keeps the run from
            # counting as indexing.
            _log_run(args, repo_paths_str, start_time, indexed=0, skipped=0, failed=len(refused), failures=refused,
                     error=(f"every fetch to the platform failed ({reasons})" if verb == "failed"
                            else f"the platform refused every fetch ({reasons})"), **marked)
        return 1

    # Some repository was refused entirely while others answered: theirs are
    # indexed below, and the run still fails, so `griot index all` stops
    # instead of reporting the refused one as indexed. Not with `error`: the
    # freshness report skips such a run for EVERY repository, and those that
    # answered were indexed. The run's repositories are those that answered
    # (its recorded heads), so the freshness report does not count it for
    # the refused ones; its failures name them (`<repo>:platform:<label>`).
    covered = [p for p in repo_paths_str if p not in refused_repos]
    rc = None
    if refused_repos:
        names = ", ".join(common.printable(n) for n in refused_names)
        print((f"Error: every fetch for {names} failed; " if verb == "failed"
               else f"Error: the platform refused every fetch for {names}; ")
              + f"nothing of {'it' if len(refused_repos) == 1 else 'them'} was indexed:", file=sys.stderr)
        for line in _refusal_lines(refused_fetches):
            print(line, file=sys.stderr)
        rc = 1

    if not all_documents:
        print("\nNo platform items to index.")
        if refused and not args.dry_run:
            _log_run(args, covered, start_time, indexed=0, skipped=0, failed=len(refused), failures=refused, **marked)
        return rc

    if args.dry_run:
        common.dry_run(all_documents, source="platform", unit="chunks", desc="Checking platform")
        return rc

    indexed, skipped, failed = common.index_documents(all_documents, desc="Indexing platform")
    redacted = common.report_redactions()

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} chunks indexed, {skipped} unchanged (skipped), {failed + len(refused)} failed"
          + (f" ({len(refused)} fetch(es) refused by the platform)." if refused else "."))
    _log_run(args, covered, start_time, indexed=indexed, skipped=skipped, failed=failed + len(refused),
             redacted=redacted,
             # [user-requested] WHICH documents failed, not just how many —
             # the count alone forced a grep through griot.log to diagnose.
             failures=(refused + common.last_run_failures())[:common.MAX_RECORDED_FAILURES], **marked)
    return rc


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
