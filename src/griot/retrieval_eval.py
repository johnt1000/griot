"""Qrels generator (query → relevant documents pairs) from the git log.

Free ground truth for measuring retrieval quality (Recall@k, MRR) without
spending on an LLM: a commit's message becomes the query; the files the
commit touched become the documents expected in the response. See plan,
section 10.3, item 1 ("Ruler first").

`griot quality-check --from-history` runs it per registered repository
(history_pairs() and evaluate_history() below; quality_check.py reports and
records it). evaluate_qrels() and the __main__ block are the older manual
ruler, still invoked as `python -m griot.retrieval_eval`.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

import qdrant_edge as qe

from griot import common


def _commits_with_files(repo_path: Path, max_commits: int | None = None) -> list[dict]:
    """git log --all --name-only in a single call (same performance rationale
    as index_commits.py — no subprocess per commit to get the touched files).

    Each record BEGINS with NUL and each of its three fields ends with one
    (NUL is the one character a message cannot hold: see
    common.git_records()). --name-only writes the file list AFTER the
    fields, so what follows the last NUL of a record, up to the NUL that
    begins the next, is that commit's files. Preserves git log's default
    order (most recent first) — holdout_recent depends on it."""
    fmt = "%x00" + common.git_format("%H", "%s", "%b")
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
        # stderr: `quality-check --from-history --json` owns stdout, and a
        # repository with no commit yet ends here.
        print(f"Error reading commits from {repo_path.name}: {(e.stderr or str(e)).strip()}", file=sys.stderr)
        return []

    # ["", hash, subject, body, files, hash, subject, body, files, ...]
    parts = output.split("\x00")[1:]
    commits = []
    for start in range(0, len(parts) - 3, 4):
        commit_hash, subject, body, rest = parts[start:start + 4]
        if not common.is_git_hash(commit_hash):
            continue  # not where a record begins
        commits.append({
            "hash": commit_hash,
            "subject": subject,
            "body": body.strip(),
            "files": [f for f in rest.split("\n") if f.strip()],
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
        # Vector, explicitly: this evaluates retrieval by meaning.
        results = common.search(item["query"], limit=max_k, mode="vector")
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


# --- `griot quality-check --from-history` ------------------------------------
#
# Ranks count DOCUMENTS (a file of a repository), not chunks: the second
# chunk of a file is the same answer and takes no rank, as in
# scripts/measure-search-modes.py, so MRR@10 and recall@10 mean the same
# thing in both. The search itself stays raw (every point, in the store's
# order: see test_what_measures_retrieval_is_not), so a file that fills
# several of the ten places leaves fewer documents in them.

HISTORY_K = 10

# How a commit message is written changes what it can find: a subject alone is
# a few words, a body adds the why and often the names involved. Reported
# apart so that a change in the mix of a history does not read as a change of
# quality.
QUERY_KINDS = ("subject", "subject_and_body")


def query_kind(qrel: dict) -> str:
    """"subject_and_body" when the message has a body (build_qrels_from_git
    joins the two with a blank line), "subject" otherwise."""
    return "subject_and_body" if "\n\n" in qrel["query"] else "subject"


def spread(items: list, n: int | None) -> list:
    """At most `n` of `items`, evenly spaced from the first, in their order:
    the same history always gives the same choice (no randomness, so a run
    can be repeated and two runs compared), and the choice covers the whole
    history rather than its most recent end. None takes everything."""
    if n is None or n >= len(items):
        return list(items)
    return [items[i * len(items) // n] for i in range(n)]


def indexed_code_files(client, repo: str) -> set[str]:
    """The file paths indexed as code for `repo` in the collection `client`
    holds. A commit's touched file that is not among them can never be found
    (not indexed: an image, a lockfile, an ignored directory), so it is no
    answer to ask for: counted, it would only lower recall for a reason that
    is not search."""
    scope = qe.Filter(must=[
        qe.FieldCondition(key="repo", match=qe.MatchValue(value=repo)),
        qe.FieldCondition(key="source_type", match=qe.MatchValue(value="code")),
    ])
    files, offset = set(), None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(limit=1000, offset=offset, filter=scope,
                                                        with_payload=["file_path"], with_vector=False))
        files.update(p.payload["file_path"] for p in points if (p.payload or {}).get("file_path"))
        if offset is None:
            return files


def history_pairs(repo_path: Path, repo_name: str, indexed_files: set[str], limit: int | None = None) -> list[dict]:
    """build_qrels_from_git()'s pairs, each with only the touched files that
    are indexed (a pair left with none is dropped) and its `kind`, then at
    most `limit` of them spread over the history (spread())."""
    pairs = []
    for qrel in build_qrels_from_git(repo_path, repo_name):
        relevant = [f for f in qrel["relevant_file_paths"] if f in indexed_files]
        if relevant:
            pairs.append({**qrel, "relevant_file_paths": relevant, "kind": query_kind(qrel)})
    return spread(pairs, limit)


def questions_fingerprint(pairs: list[dict]) -> str:
    """Which commits were asked, in a few characters: two runs with the same
    fingerprint asked the same questions, so their numbers compare; a
    different one means the history (or the index) changed in between."""
    return hashlib.sha256("\n".join(p["commit_hash"] for p in pairs).encode()).hexdigest()[:12]


def ranked_documents(payloads) -> list[tuple]:
    """(repo, file_path) of each result, in the order first met: a document's
    later chunks take no rank."""
    docs = []
    for payload in payloads:
        doc = (payload.get("repo"), payload.get("file_path"))
        if doc not in docs:
            docs.append(doc)
    return docs


def reciprocal_rank(docs: list, relevant: set) -> float:
    """1 / the rank of the first relevant document, 0.0 when none is there."""
    for rank, doc in enumerate(docs, start=1):
        if doc in relevant:
            return 1.0 / rank
    return 0.0


def recall(docs: list, relevant: set) -> float:
    """The share of the relevant documents that are among `docs`."""
    return len(relevant & set(docs)) / len(relevant)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(scored: list[dict]) -> dict:
    """Means of `rr` and `recall` over every scored pair and per kind. None,
    not 0.0, when nothing was scored: zero would read as "measured, and found
    nothing". A kind with no pair is absent rather than a row of Nones."""
    by_kind = {}
    for kind in QUERY_KINDS:
        mine = [s for s in scored if s["kind"] == kind]
        if mine:
            by_kind[kind] = {"pairs": len(mine), "mrr": _mean([s["rr"] for s in mine]),
                             "recall": _mean([s["recall"] for s in mine])}
    return {"pairs": len(scored), "mrr": _mean([s["rr"] for s in scored]),
            "recall": _mean([s["recall"] for s in scored]), "by_kind": by_kind}


def evaluate_history(pairs: list[dict], mode: str, k: int = HISTORY_K) -> dict:
    """summarize() of one search per pair, in `mode`, of code only and within
    the pair's own repository.

    Code only: the commit itself is indexed too (source_type commit), holds
    the query word for word, and would take first place in every search.
    Within the repository: what another repository holds (and how much of
    it) would otherwise move this repository's number, and the point is a
    number that changes when THIS repository's retrieval does.

    A question the search refuses (in keyword or hybrid mode, a message of
    only words keyword search leaves out) found nothing: it scores 0 and is
    counted in `refused`, so a run where many were refused says so."""
    scored, refused = [], 0
    for pair in pairs:
        relevant = {(pair["repo"], f) for f in pair["relevant_file_paths"]}
        try:
            results = common.search(pair["query"], limit=k, mode=mode,
                                    repos=[pair["repo"]], source_types=["code"])
        except common.SearchModeUnavailable:
            raise  # the whole run cannot be done in this mode: not one question's score
        except common.SearchFilterError:
            refused += 1
            scored.append({"kind": pair["kind"], "rr": 0.0, "recall": 0.0})
            continue
        docs = ranked_documents(point.payload or {} for point in results)
        scored.append({"kind": pair["kind"], "rr": reciprocal_rank(docs, relevant),
                       "recall": recall(docs, relevant)})
    return {**summarize(scored), "refused": refused}


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
