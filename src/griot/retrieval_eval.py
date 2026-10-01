"""Qrels generator (query → relevant documents pairs) from the git log.

Free ground truth for measuring retrieval quality (Recall@k, MRR) without
spending on an LLM: a commit's message becomes the query; the files the
commit touched become the documents expected in the response. See plan,
section 10.3, item 1 ("Ruler first").

Has no CLI subcommand — this is a research/roadmap tool, outside v1's frozen
scope. The module is only importable/testable, invoked
manually via `python -m griot.retrieval_eval`.
"""

import subprocess
from pathlib import Path

from griot import common
from griot.index_commits import FIELD_SEP, RECORD_SEP


def _commits_with_files(repo_path: Path, max_commits: int | None = None) -> list[dict]:
    """git log --all --name-only in a single call (same performance rationale
    as index_commits.py — no subprocess per commit to get the touched files).
    RECORD_SEP as a PREFIX of each record (not a suffix, unlike list_commits)
    because here --name-only inserts the file list AFTER each commit's field
    block; a suffix separator would fall between a commit's fields and the
    SAME commit's file list, mixing it with the next record on split.
    Preserves git log's default order (most recent first) — holdout_recent
    depends on it."""
    fmt = RECORD_SEP + FIELD_SEP.join(["%H", "%s", "%b"]) + FIELD_SEP
    # core.quotepath off: with it, a name outside ASCII comes out as
    # `caf\303\251.py`, a path that exists nowhere, and the file was dropped
    # as "gone from the working tree".
    args = ["-c", "core.quotepath=false", "log", "--all"]
    if max_commits is not None:
        args.append(f"--max-count={max_commits}")
    args += ["--name-only", f"--pretty=format:{fmt}"]

    try:
        output = common.run_git(repo_path, args, timeout=120).stdout
    except subprocess.CalledProcessError as e:
        print(f"Error reading commits from {repo_path.name}: {e}")
        return []

    commits = []
    for chunk in output.split(RECORD_SEP):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        # maxsplit=3: %H, %s, and %b are separated by FIELD_SEP, plus the final
        # FIELD_SEP after %b (before the file block) — 4 parts total.
        commit_hash, subject, body, rest = chunk.split(FIELD_SEP, 3)
        files = [f for f in rest.split("\n") if f.strip()]
        commits.append({
            "hash": commit_hash,
            "subject": subject,
            "body": body.strip(),
            "files": files,
        })
    return commits


def build_qrels_from_git(
    repo_path: Path,
    repo_name: str,
    max_commits: int | None = None,
    holdout_recent: int = 0,
) -> list[dict]:
    """Builds (query, relevant files) pairs from a local git repo's history.
    Each commit with a non-empty file body becomes an item:
    {"query": <subject + body>, "repo": repo_name, "commit_hash": ...,
    "relevant_file_paths": [...]}. Commits with no touched files (merge
    commits with no diff, empty commits) are skipped — with no relevant
    document, the query doesn't measure anything. Touched files that no
    longer exist in the current working tree (deleted/renamed after the
    commit) are also dropped from relevant_file_paths — index_code.py only
    indexes the current snapshot, so those files could never produce a real
    match; if this empties the list, the whole qrel is
    discarded.

    Interface decision for the holdout: holdout_recent is NOT returned as a
    second value — the N most recent commits (git log --all's order,
    most-recent-first, is preserved by _commits_with_files) are simply
    excluded from the return. Rationale: v1 doesn't yet have any evaluation
    script consuming qrels (that's future roadmap work/10.5) —
    inventing a second return (tuple, or separate list) with no real
    consumer today would just add a speculative interface. When the
    evaluation script exists and needs to compare before/after a pipeline
    change using the holdout, the pattern is: call the function again with
    holdout_recent=0 (gets everything) and differentiate by commit_hash, or
    call it twice with different holdout_recent. Easy to do without changing
    this signature.
    """
    commits = _commits_with_files(repo_path, max_commits=max_commits)
    if holdout_recent > 0:
        commits = commits[holdout_recent:]

    qrels = []
    for commit in commits:
        # git log --all covers the ENTIRE history, but
        # index_code.py only indexes the CURRENT snapshot of the working
        # tree — a file touched in an old commit and later deleted/renamed
        # can never match in the index. Without filtering for this, the
        # query would enter the qrel guaranteed to underestimate
        # Recall/MRR (no possible result is "correct"). Filters against
        # what actually exists now, relative to repo_path; discards the
        # whole qrel if no touched file survives the filter.
        existing_files = [f for f in commit["files"] if (repo_path / f).exists()]
        if not existing_files:
            continue
        query = commit["subject"]
        if commit["body"]:
            query = f"{commit['subject']}\n\n{commit['body']}"
        qrels.append({
            "query": query,
            "repo": repo_name,
            "commit_hash": commit["hash"],
            "relevant_file_paths": existing_files,
        })
    return qrels


def evaluate_qrels(qrels: list[dict], k_values: list[int] | None = None) -> dict:
    """Measures the current pipeline's retrieval quality against qrels
    (format from build_qrels_from_git, but any source with the same fields
    works). For each item, calls common.search() once with
    limit=max(k_values) — the smaller top-k's are slices of the same result,
    they don't need their own search — and considers a "hit" a result whose
    payload is source_type=="code", repo==item["repo"], and file_path in
    item["relevant_file_paths"] (a commit may have touched several files;
    any one of them counts). No LLM call: only measures raw retrieval
    (embedding + vector search), the ruler from the design notes item 1.

    Recall@k = fraction of queries with at least one hit among the top-k.
    MRR = average of 1/position of the FIRST hit per query (0.0 if the query
    had no hit among the returned results, even if a match exists in theory
    beyond the requested limit — we don't know that without searching
    deeper, so we treat it as "not found" for this ruler)."""
    if k_values is None:
        k_values = [5, 20]
    max_k = max(k_values)

    hits_at_k = {k: 0 for k in k_values}
    reciprocal_ranks = []
    queries_without_match = []

    for item in qrels:
        results = common.search(item["query"], limit=max_k)
        relevant_paths = set(item["relevant_file_paths"])

        first_hit_rank = None
        for rank, point in enumerate(results, start=1):
            payload = point.payload or {}
            if (
                payload.get("source_type") == "code"
                and payload.get("repo") == item["repo"]
                and payload.get("file_path") in relevant_paths
            ):
                first_hit_rank = rank
                break

        if first_hit_rank is None:
            reciprocal_ranks.append(0.0)
            queries_without_match.append(item["query"])
            continue

        reciprocal_ranks.append(1.0 / first_hit_rank)
        for k in k_values:
            if first_hit_rank <= k:
                hits_at_k[k] += 1

    n_queries = len(qrels)
    recall_at_k = {k: (hits_at_k[k] / n_queries if n_queries else 0.0) for k in k_values}
    mrr = sum(reciprocal_ranks) / n_queries if n_queries else 0.0

    return {
        "n_queries": n_queries,
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "queries_without_match": queries_without_match,
    }


def print_report(report: dict) -> None:
    """Human-readable summary of evaluate_qrels() for the terminal — manual
    use (the __main__ block below), not a format meant to be parsed."""
    print(f"\n{report['n_queries']} queries evaluated")
    for k in sorted(report["recall_at_k"]):
        print(f"Recall@{k}: {report['recall_at_k'][k]:.3f}")
    print(f"MRR: {report['mrr']:.3f}")

    sem_match = report["queries_without_match"]
    if sem_match:
        print(f"\n{len(sem_match)} queries with no hit at all (first 5):")
        for query in sem_match[:5]:
            # only the first line (commit subject) — the body can be long.
            print(f"  - {query.splitlines()[0]}")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Generates qrels (query, relevant files) from a local repo's git log, to measure Recall@k/MRR of retrieval without an LLM."
    )
    parser.add_argument("repo_path", type=Path, help="Local git repository path.")
    parser.add_argument("--repo-name", help="Repo name in the generated qrels (default: directory name).")
    parser.add_argument("--max-commits", type=int, default=None, help="Limits how many (most recent) commits to consider.")
    parser.add_argument("--holdout-recent", type=int, default=0, help="Excludes the N most recent commits from the result (reserved for holdout).")
    parser.add_argument("--dump-qrels", action="store_true", help="Prints the generated qrels as JSON before evaluating (verbose).")
    parser.add_argument("--k", type=int, nargs="+", default=[5, 20], help="k values for Recall@k (default: 5 20).")
    args = parser.parse_args()

    name = args.repo_name or args.repo_path.name
    qrels = build_qrels_from_git(
        args.repo_path, name,
        max_commits=args.max_commits,
        holdout_recent=args.holdout_recent,
    )
    if args.dump_qrels:
        print(json.dumps(qrels, indent=2, ensure_ascii=False))
    print(f"\n{len(qrels)} qrel pairs generated from {args.repo_path}.")

    # Evaluation against the REAL index (real common.search, no mock) — requires
    # the repo to already be indexed in the active collection (common.COLLECTION_NAME).
    # Future manual use (the design notes/10.5); has no test coverage beyond what
    # evaluate_qrels/print_report already have with synthetic data.
    if qrels:
        report = evaluate_qrels(qrels, k_values=args.k)
        print_report(report)
    else:
        print("No qrels generated (repo has no commits with touched files) — nothing to evaluate.")
