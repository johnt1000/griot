import argparse
import json
import statistics
import sys

import qdrant_edge as qe

# golden_set imports only common/retrieval_eval, so this is not a cycle;
# aliased because `golden_set` is also this module's parameter name.
from griot import common, logdb
from griot import golden_set as golden_set_mod

# Cap on how many individual failures a stored record keeps. A self-check
# over a large corpus with a high failure rate would otherwise persist a
# multi-MB blob for what is meant to be a summary — same cap the (now
# removed) UI applied for the same reason.
MAX_STORED_FAILURES = 20


def _record_for_trend(collection: str, self_check: dict, golden_check: dict | None) -> None:
    """Appends this run to logs.db's quality_checks table, which is what
    `griot stats` reads to show the pass-rate trend.

    [regression] A service layer removed with the old front end used to
    be the ONLY caller of logdb.write_quality_check() — deleting the
    that layer deleted the writer with it, leaving the trend reading a table
    nothing filled. The CLI is the surviving surface, so the CLI records it.

    Never raises: recording is observability, not the job. A storage failure
    must not fail the check the user actually asked for."""
    from datetime import datetime, timezone

    stored = dict(self_check)
    failures = stored.get("failures") or []
    if len(failures) > MAX_STORED_FAILURES:
        stored["failures"] = failures[:MAX_STORED_FAILURES]
        stored["failures_truncated"] = True
    try:
        common.secure_mkdir(common.LOG_DIR)
        logdb.write_quality_check(common.LOG_DIR, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "collection": collection,
            "self_check": stored,
            # Stored whole: it is already bounded by the curated golden set's
            # own size, and knowing WHICH curated questions regressed is the
            # point of curating them.
            "golden_check": golden_check,
        })
    except Exception as e:  # noqa: BLE001 — see docstring
        common.log_and_print(f"Warning: could not record this quality check for the trend: {e}",
                             level="warning", echo=False)

SELF_CHECK_SAMPLE_SIZE = 30
SELF_CHECK_MIN_SCORE = 0.90


def sample_points(client: "qe.EdgeShard", n: int) -> list:
    """Deterministic sampling: fetches up to 4x n points via scroll (stable
    order from Qdrant Edge for a shard that isn't being written to at the
    moment — confirmed by introspection: repeated shard.scroll() calls with
    no writes in between return exactly the same order) and picks n evenly
    spaced across the pool — covers varied repos/sources without relying on
    real randomness, so a failure is always reproducible by running it again.

    Unlike the old qdrant-client's client.scroll(collection_name=...,
    limit=...) (a multiplexed client serving several collections),
    EdgeShard.scroll() takes a single qe.ScrollRequest object (same pattern
    as qe.QueryRequest/qe.UpdateOperation already used in common.py) and
    doesn't take 'collection_name' — the client itself is already opened on
    ONE specific shard/collection, so the choice of which collection happens
    when the shard is opened (see quality_check.main()), not on every call.
    Returns (points, next_offset), same contract as before."""
    pool, _ = client.scroll(qe.ScrollRequest(limit=n * 4, with_payload=True))
    if not pool:
        return []
    step = max(1, len(pool) // n)
    return pool[::step][:n]


def _open_shard_for_sampling(collection: str) -> "qe.EdgeShard | None":
    """Opens the shard sample_points() will read from. If it's the ACTIVE
    collection (the real use case — mcp_server.griot_quality_check() and the
    CLI without --collection always pass common.COLLECTION_NAME), reuses the
    memoized handle from common.get_client() (doesn't close it — it's a
    shared owner). Otherwise, opens a separate read-only shard (same pattern
    as get_index_status() for a non-active collection) that the CALLER needs
    to close. None if the shard doesn't even exist on disk (collection never
    indexed) — sample_points handles an empty pool, so the caller only needs
    to treat 'no shard' as 'no sample'."""
    if collection == common.COLLECTION_NAME:
        return common.get_client()
    path = common._collection_path(collection)
    if not (path / common._EDGE_CONFIG_MARKER).exists():
        return None
    return qe.EdgeShard.load(str(path))


def run_self_check(collection: str, sample_size: int = SELF_CHECK_SAMPLE_SIZE, min_score: float = SELF_CHECK_MIN_SCORE) -> dict:
    """Level 1 (auto-check, zero curation): takes real already-indexed
    points, searches for their EXACT content and confirms the point itself
    shows up in the top-5 with a high score (close to 1.0 — same text, same
    model, should self-retrieve almost perfectly). Doesn't require knowing
    anything about the repos' domain — works for any new collection. Catches
    pipeline breakage (wrong dimension, model swapped without reindexing,
    empty/corrupted collection, etc), not fine-grained semantic quality
    (that's the golden set, level 2).

    common.search() (used below for the re-match) always searches the ACTIVE
    collection (common.COLLECTION_NAME) — it doesn't take 'collection' as a
    parameter. This was already the case before the migration to Edge
    (multiplexed client, but the search call always went with a fixed
    collection_name=COLLECTION_NAME); this module's 'collection' parameter
    only produces a semantically correct result when it matches the active
    collection — that's the real use case (see
    mcp_server.griot_quality_check), --collection on the CLI is just for
    convenience/debugging and kept for compatibility."""
    client = _open_shard_for_sampling(collection)
    try:
        samples = sample_points(client, sample_size) if client is not None else []
        failures = []
        scores = []

        for point in samples:
            content = common.stored_text(point.payload)
            if not content.strip():
                continue
            results = common.search(content, limit=5)
            match = next((r for r in results if str(r.id) == str(point.id)), None)
            if match is None:
                failures.append({"id": str(point.id), "repo": point.payload.get("repo"), "reason": "did not appear even in its own top-5 search results"})
                continue
            scores.append(match.score)
            if match.score < min_score:
                failures.append({"id": str(point.id), "repo": point.payload.get("repo"), "reason": f"score too low for a self-match ({match.score:.3f} < {min_score})"})
    finally:
        if client is not None and collection != common.COLLECTION_NAME:
            client.close()

    return {
        "sampled": len(samples),
        "passed": len(samples) - len(failures),
        "failed": len(failures),
        "failures": failures,
        "avg_score": round(statistics.mean(scores), 4) if scores else None,
    }


def _matches(payload: dict, expected: dict) -> bool:
    return all(payload.get(k) == v for k, v in expected.items())


def run_golden_set(golden_set: list) -> dict:
    """Level 2 (curated): real semantic questions with what SHOULD appear in
    the top-K — tests actual search quality (not just "the pipeline didn't
    break"). Always against the ACTIVE collection: common.search() has no
    other, whatever --collection the self-check was given. Cases in quality_golden_set.json, manually validated against the
    full corpus before becoming a golden case (see griot's README.md)."""
    results = []
    indexed: dict[str, bool] = {}

    def is_indexed(repo: str) -> bool:
        if repo not in indexed:
            indexed[repo] = common.repository_is_indexed(common.get_client(), repo)
        return indexed[repo]

    for case in golden_set:
        # [review finding] add_case() guards the two write paths that go
        # through it, but cmd_suggest() writes via _save() directly, the file
        # is edited by hand as a documented workflow, and files curated before
        # that guard existed are still on disk. Read time is the only point
        # that covers all of them.
        #
        # FAILED rather than skipped, deliberately: what a vacuous case
        # damages is this command's exit code (`ok` requires failed == 0), so
        # skipping would leave the gate open on a case that measured nothing.
        # Failing closes it and names the case to fix.
        vacuous = [exp for exp in case["must_include"]
                   if not golden_set_mod.has_effective_constraint(exp)]
        if vacuous:
            results.append({
                "query": case["query"],
                "passed": False,
                "missing": vacuous,
                "reason": ("must_include entries that constrain nothing — every value is null, "
                           "so this case matches any result and can never fail. Re-curate it with "
                           "`griot golden-set remove` then `griot golden-set add`."),
                "top_results": [],
            })
            continue
        # "Search did not find it" and "there is nothing to find" are
        # different findings. Curated cases that expect a repository nobody
        # indexed (renamed, removed, never indexed with this profile) read as
        # "search quality is 0 of N" when the search was never at fault.
        # Still FAILED, for the same reason as above: the gate stays closed;
        # what changes is that the case says what to do. Asked before the
        # search, so a case that cannot pass does not pay for an embedding.
        nowhere = [exp for exp in case["must_include"]
                   if isinstance(exp.get("repo"), str) and exp["repo"] and not is_indexed(exp["repo"])]
        if nowhere:
            names = ", ".join(sorted({common.shown(exp["repo"]) for exp in nowhere}))
            results.append({
                "query": case["query"],
                "passed": False,
                "missing": nowhere,
                "reason": (f"nothing is indexed for {names} with profile '{common.ACTIVE_PROFILE_NAME}': "
                           f"this case cannot pass until that repository is indexed. If it is gone for "
                           f"good, remove the case with `griot golden-set remove`."),
                "top_results": [],
            })
            continue
        hits = common.search(case["query"], limit=case.get("limit", 5))
        payloads = [h.payload for h in hits]
        missing = [exp for exp in case["must_include"] if not any(_matches(p, exp) for p in payloads)]
        results.append({
            "query": case["query"],
            "passed": not missing,
            "missing": missing,
            "top_results": [{"score": round(h.score, 3), "source_type": h.payload.get("source_type"), "repo": h.payload.get("repo")} for h in hits],
        })
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "cases": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Tests the VECTOR SEARCH quality of a collection (self-check + curated golden set). "
                    "Does not call any chat LLM (only embeds the query) — tests retrieval, not ask.py's "
                    "final answer, which isn't deterministic."
    )
    parser.add_argument("--collection", help="Collection name (default: the active GRIOT_EMBED_PROFILE's)")
    parser.add_argument("--sample-size", type=int, default=SELF_CHECK_SAMPLE_SIZE, help="How many points to sample in the self-check (default: %(default)s)")
    parser.add_argument("--min-score", type=float, default=SELF_CHECK_MIN_SCORE, help="Minimum acceptable self-match score (default: %(default)s)")
    parser.add_argument("--skip-golden-set", action="store_true", help="Runs only the self-check, skips the curated golden set")
    parser.add_argument("--json", action="store_true", help="[--json stdout contract] Output ONLY a single JSON payload on stdout (for scripting/subprocess consumers, e.g. jobs.run_quality_check_job()) — every human-readable print() is suppressed in this mode. A genuine execution error (bad collection) still goes to stderr, never stdout, so a caller can tell 'valid JSON on stdout' apart from 'something broke before we even got a result' just from which stream has content.")
    args = parser.parse_args(argv)

    collection = args.collection or common.COLLECTION_NAME
    # Existence check WITHOUT opening/creating anything (the old
    # qdrant-client's client.collection_exists() has no equivalent in
    # EdgeShard — each collection is a shard in its own directory, so
    # existence is the presence of the marker written by EdgeShard.create(),
    # same pattern common.get_index_status() already uses for a non-active
    # collection). Important to check BEFORE opening any shard: if
    # 'collection' is the active one and doesn't exist yet,
    # common.get_client() would CREATE it from scratch — wrong here,
    # quality_check should only inspect what already exists.
    if not (common._collection_path(collection) / common._EDGE_CONFIG_MARKER).exists():
        # [--json stdout contract] --json's stdout contract is "JSON or nothing" — an
        # error this early (no result to report) belongs on stderr in that
        # mode, never stdout, or a caller parsing stdout as JSON would choke.
        print(f"Error: collection '{collection}' does not exist.", file=sys.stderr if args.json else sys.stdout)
        raise SystemExit(1)

    if not args.json:
        print(f"=== Self-check: {collection} ===")
    self_check = run_self_check(collection, args.sample_size, args.min_score)
    if not args.json:
        print(f"{self_check['passed']}/{self_check['sampled']} samples self-recovered correctly (avg score: {self_check['avg_score']})")
        for f in self_check["failures"]:
            print(f"  FAILURE: {f['id']} ({f['repo']}) — {f['reason']}")

    golden_check = None
    if not args.skip_golden_set:
        # resolved via common (CONFIG_DIR/quality_golden_set.json) — hand-curated
        # by the user, not part of the installed code
        if not common.GOLDEN_SET_PATH.exists():
            if not args.json:
                print(f"\nWarning: {common.GOLDEN_SET_PATH} does not exist, skipping golden set.")
        else:
            golden_set = golden_set_mod.list_cases()
            if not args.json:
                print(f"\n=== Golden set: {len(golden_set)} curated questions ===")
            golden_check = run_golden_set(golden_set)
            if not args.json:
                for case in golden_check["cases"]:
                    status = "OK" if case["passed"] else "FAILED"
                    print(f"  [{status}] {case['query']!r}")
                    if not case["passed"] and case.get("reason"):
                        print(f"        {case['reason']}")
                    elif not case["passed"]:
                        print(f"        expected and not found: {case['missing']}")
                        print(f"        top results: {case['top_results']}")
                print(f"\n{golden_check['passed']}/{golden_check['total']} golden set questions passed")

    _record_for_trend(collection, self_check, golden_check)

    common.log_and_print(
        f"quality_check: collection={collection} "
        f"self_check={self_check['passed']}/{self_check['sampled']} "
        f"golden_set={golden_check['passed'] if golden_check else '-'}/{golden_check['total'] if golden_check else '-'}",
        echo=not args.json,  # [--json stdout contract] audit trail (griot.log) still gets the entry — only stdout is suppressed
    )

    # A check that sampled nothing measured nothing. It used to pass ("0/0
    # samples self-recovered"), which is how an empty index got a green gate.
    measured = self_check["sampled"] > 0
    ok = measured and self_check["failed"] == 0 and (golden_check is None or golden_check["failed"] == 0)
    if args.json:
        print(json.dumps({"collection": collection, "self_check": self_check, "golden_check": golden_check, "ok": ok}))
    elif not measured:
        print(f"\nQuality NOT measured: collection '{collection}' has nothing to sample. Index a repository first.")
    else:
        print("\nQuality OK." if ok else "\nQuality FAILED — see failures above.")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
