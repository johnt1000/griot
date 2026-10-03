"""Which repository is behind its own HEAD.

`griot stats` says when the index was last written, for the whole
collection. The question an agent has before trusting a result is a
different one: is what griot holds about THIS repository behind what the
repository holds now? Every indexing run records what each repository it
covered looked like (common.log_run_summary: the HEAD under `heads`, and a
fingerprint of the tag and remote-branch refs under `refs`); here that is
compared with the repository of the moment, per repository and per source.

Each source is measured by what it indexes. Code and commits follow HEAD:
the count of commits made since, or "the indexed commit is not in this
history" when it is no longer an ancestor of HEAD (rewritten since, another
branch checked out, a clone that does not have it). Tags and branches follow
their refs: changed or not, no count. The platform source (pull requests,
issues) lives on the platform, and nothing local can say whether it is
behind.

Read-only and cheap: a few git processes per registered repository, and the
runs already in the log.
"""

import hashlib
import subprocess
from pathlib import Path

from griot import common, logdb

# How far back in the log to look for the last run of each source. A
# repository indexed longer ago than this many runs reads as never indexed.
RUNS_LOOKED_AT = 2000
# What `script` a run records, and what a person calls that source.
SOURCE_OF_SCRIPT = {"index_code.py": "code", "index_commits.py": "commits", "index_tags.py": "tags",
                    "index_branches.py": "branches", "index_platform.py": "platform"}
SOURCES = tuple(SOURCE_OF_SCRIPT.values())
# What each source is measured against: the commits since its run, the refs
# it reads, or nothing that is here.
FOLLOWS_HEAD = ("code", "commits")
FOLLOWS_REFS = {"tags": "tags", "branches": "branches"}
# The refs each of those sources reads (index_tags.py, index_branches.py).
# All of refs/remotes for the branches, the base branch included although
# it is not indexed itself: each branch is described against it ("commits
# ahead of base"), so a base that moved changes what a run would write.
REFS_OF_SOURCE = {"tags": "refs/tags", "branches": "refs/remotes"}


def _git(path: str, args: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    if not Path(path).is_dir():
        return None
    try:
        return common.run_git(path, args, timeout=timeout, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def head_of(path: str) -> str | None:
    """The commit a repository is at, or None where there is no repository."""
    done = _git(path, ["rev-parse", "--verify", "-q", "HEAD^{commit}"], 10)
    head = done.stdout.strip() if done is not None else ""
    return head if done is not None and done.returncode == 0 and common.is_git_hash(head) else None


def heads_of(paths: list[str]) -> dict[str, str | None]:
    """{directory name: HEAD} for the repositories a run covers. The
    directory name is what everything indexed is keyed by (repos.py)."""
    return {Path(path).name: head_of(path) for path in paths}


def snapshot_of(paths: list[str]) -> dict[str, dict]:
    """What a run records about each repository it covers, with one
    `rev-parse` per repository: {name: {"head", "refs"}}."""
    found = {}
    for path in paths:
        head = head_of(path)
        found[Path(path).name] = {"head": head, "refs": _refs(path) if head is not None else None}
    return found


def _fingerprint(path: str, refs: str) -> str | None:
    """One hash over the names and targets of the refs under `refs`: equal
    while nothing among them changed, whatever changed elsewhere."""
    done = _git(path, ["for-each-ref", "--format=%(refname) %(objectname)", refs], 30)
    if done is None or done.returncode != 0:
        return None
    return hashlib.sha256(done.stdout.encode("utf-8", "replace")).hexdigest()


def _refs(path: str) -> dict[str, str | None]:
    return {source: _fingerprint(path, refs) for source, refs in REFS_OF_SOURCE.items()}


def refs_of(paths: list[str]) -> dict[str, dict[str, str | None] | None]:
    """{directory name: {source: fingerprint}} for the sources that read
    refs, None where there is no repository."""
    return {name: snapshot["refs"] for name, snapshot in snapshot_of(paths).items()}


def _commits_between(path: str, old: str, new: str) -> int | None:
    """How many commits `new` is ahead of `old`, or None when `old` is not
    in the history of `new`: rewritten since the run (amend, reset, force
    push), another branch checked out, or a clone that never had it. (git
    still counts "commits in new that are not in old" in that case and gives
    1, or 0, for an index of a commit that is not in the history: so
    ancestry is asked first.)"""
    if old == new:
        return 0
    ancestry = _git(path, ["merge-base", "--is-ancestor", old, new], 30)
    if ancestry is None or ancestry.returncode != 0:
        return None
    done = _git(path, ["rev-list", "--count", f"{old}..{new}"], 30)
    if done is None or done.returncode != 0 or not done.stdout.strip().isdigit():
        return None
    return int(done.stdout.strip())


def assess(runs: list[dict], repositories: list[dict]) -> list[dict]:
    """Per repository, how its index stands against the repository now.

    `runs` newest first, as logdb reads them; `repositories` as
    {name, path, head, refs} with head None where there is no repository and
    refs as refs_of() gives them. A run counts for a repository when it
    recorded that repository's head and did not die (a run with some failed
    documents did run: its failures are reported by the run itself); the
    newest such run of each source is the one that counts. A run from
    before heads were recorded says nothing; one from before refs were
    recorded cannot say whether tags or branches changed.

    `behind` is None when nothing can be said (never indexed, or no
    repository to compare with), else whether any source is behind;
    `behind_sources` names them; `commits_behind` is the worst count among
    the sources that follow HEAD, None when one of them cannot be counted
    (history rewritten); `missing_sources` are those that never ran."""
    reports = []
    for repository in repositories:
        name, path, now = repository["name"], repository["path"], repository["head"]
        refs_now = repository.get("refs") or {}
        sources: dict[str, dict] = {}
        last_indexed_at = None
        for run in runs:
            heads = run.get("heads")
            if run.get("error") or not isinstance(heads, dict) or name not in heads:
                continue
            last_indexed_at = last_indexed_at or run.get("timestamp")
            source = SOURCE_OF_SCRIPT.get(run.get("script") or "", run.get("script") or "unknown")
            if source in sources or heads[name] is None:
                continue
            entry = {"at": run.get("timestamp")}
            if source in FOLLOWS_HEAD:
                entry["indexed_head"] = heads[name]
                entry["commits_behind"] = _commits_between(path, heads[name], now) if now is not None else None
            elif source in FOLLOWS_REFS:
                recorded = ((run.get("refs") or {}).get(name) or {}).get(source)
                entry["changed"] = None if recorded is None or refs_now.get(source) is None else recorded != refs_now[source]
            sources[source] = entry
        behind_sources = [source for source in SOURCES if source in sources and (
            (source in FOLLOWS_HEAD and (sources[source]["commits_behind"] is None or sources[source]["commits_behind"] > 0))
            or (source in FOLLOWS_REFS and sources[source]["changed"] is True))]
        counts = [entry["commits_behind"] for source, entry in sources.items() if source in FOLLOWS_HEAD]
        if now is None or not sources:
            behind, commits_behind = None, None
        else:
            behind = bool(behind_sources)
            commits_behind = None if any(count is None for count in counts) else (max(counts) if counts else 0)
        reports.append({"repo": name, "path": path, "head": now, "last_indexed_at": last_indexed_at,
                        "behind": behind, "commits_behind": commits_behind, "behind_sources": behind_sources,
                        # Nothing could have run for a path that is not a repository.
                        "missing_sources": [source for source in SOURCES if source not in sources] if now is not None else [],
                        "sources": sources})
    return reports


def _runs(collection: str | None) -> list[dict]:
    if not common.LOG_DIR.exists():
        return []
    return logdb.read_recent(common.LOG_DIR, "runs", collection=collection or common.COLLECTION_NAME,
                             limit=RUNS_LOOKED_AT)


def repository_freshness(collection: str | None = None, *, repos: list[str] | None = None,
                         runs: list[dict] | None = None) -> list[dict]:
    """assess() for the registered repositories (or those named in `repos`),
    against the runs of `collection` (the active one by default; `runs`
    when the caller has read them already)."""
    try:
        paths = common.load_repos()
    except (OSError, ValueError):
        paths = []
    chosen = [path for path in paths if repos is None or Path(path).name in repos]
    if not chosen:
        return []
    runs = _runs(collection) if runs is None else runs
    snapshots = snapshot_of(chosen)
    repositories = [{"name": Path(path).name, "path": path, **snapshots[Path(path).name]} for path in chosen]
    return assess(runs, repositories)


def behind_among(repo_names: list[str], collection: str | None = None) -> dict[str, int | None]:
    """{repository: commits behind} for the named repositories that ARE
    behind: what a search appends to its results. Asks git nothing while no
    run has recorded a head (before the first run of this version): there
    is nothing to compare with, and a search must not pay for the
    comparison."""
    names = list(dict.fromkeys(repo_names))
    if not names:
        return {}
    runs = _runs(collection)
    if not any(isinstance(run.get("heads"), dict) for run in runs):
        return {}
    return {report["repo"]: report["commits_behind"]
            for report in repository_freshness(collection, repos=names, runs=runs) if report["behind"]}
