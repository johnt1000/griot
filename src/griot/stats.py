"""`griot stats` — usage, spend, and savings report.
Zero new data collection: aggregates logs/logs.db (SQLite, see logdb.py)
and common.get_index_status() (Qdrant), which the rest of the code already
writes/exposes.
"""

import argparse
import json
import os
import shutil
import statistics
import sys
from datetime import datetime, time, timedelta, timezone

from griot import common, logdb
from griot.cli import PROFILE_FLAG, show_flags_read_by_griot

DEFAULT_DAYS = 30

# How many trailing runs count as "recent" for the reuse comparison. Five is
# a bit more than one full `griot index all` (code, commits, tags, branches,
# platform), so it covers the last complete pass plus a little, without
# reaching back far enough to average in a pre-fix era.
_RECENT_RUNS = 5

# source_label() (ask.py) prefixes everything that is NOT "code" with a fixed
# word — code is the only one without a prefix ("{repo}/{file_path}"), so the
# classification here is by literal prefix, not generic parsing (format
# controlled by griot itself, see ask.py:source_label()).
_SOURCE_LABEL_PREFIXES = [
    ("commit ", "commit"),
    ("tag ", "tag"),
    ("branch ", "branch"),
    ("MR ", "merge_request"),
    ("release ", "release"),
    ("issue ", "issue"),
]


def _classify_source_label(label: str) -> str:
    for prefix, kind in _SOURCE_LABEL_PREFIXES:
        if label.startswith(prefix):
            return kind
    return "code"


def window_start(days: int, now: datetime | None = None) -> datetime:
    """The local midnight that opens a window of `days` local days: today and
    the `days - 1` days before it.

    Local, because spend is grouped by the day the circuit breaker counts,
    which resets at local midnight (common._today()). The window used to be
    `now - days` in UTC, so its first local day came in partial and the
    spend of that day was a fraction of what the breaker had counted.

    Built from the local DATE and then placed in time, not by subtracting
    24-hour days: a day that changes the clock lasts 23 or 25 hours, and
    astimezone() on a naive local midnight asks the system zone (mktime)
    which offset was in force at that moment."""
    now = now or datetime.now(timezone.utc)
    first_day = now.astimezone().date() - timedelta(days=days - 1)
    start = datetime.combine(first_day, time()).astimezone()
    # A midnight the clock jumps over (a zone whose summer time starts at
    # 00:00) does not exist, and which side of the gap mktime puts it on
    # depends on the Python: 3.10 gives 23:00 of the day before. The day
    # opens at its first instant, whatever the platform answered.
    while start.astimezone().date() < first_day:
        start += timedelta(minutes=15)
    return start.astimezone()


def _filter_by_days(records: list[dict], days: int, now_iso: str | None = None) -> list[dict]:
    """Records from the window of `days` local days (see window_start()).
    Records without a parseable 'timestamp' are left OUT (there's no way
    to know if they're in the window) — safer than including by default in a
    "last N days" report."""
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    cutoff = window_start(days, now)
    kept = []
    for r in records:
        ts = r.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # [review finding] comparing naive with aware raises TypeError, not
        # ValueError — a timestamp without a timezone (manual edit, or a
        # future source that doesn't write with datetime.now(timezone.utc)
        # the way common.py always does today) shouldn't be able to bring
        # down the whole report.
        try:
            in_window = when >= cutoff
        except TypeError:
            continue
        if in_window:
            kept.append(r)
    return kept


def _when(timestamp) -> datetime | None:
    """The moment a record was written, or None for anything that cannot be
    placed in time: not a string, not a date, or a date without a timezone
    (it cannot be compared with one that has it)."""
    if not timestamp or not isinstance(timestamp, str):
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _ago(timestamp, now: datetime | None = None) -> str | None:
    """How long ago, in the unit a person would use. None when unknown."""
    when = _when(timestamp)
    if when is None:
        return None
    seconds = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < 60:
        return "just now"
    for unit, size, limit in (("minute", 60, 3600), ("hour", 3600, 86400), ("day", 86400, None)):
        if limit is None or seconds < limit:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear interpolation between the two nearest ranks; with one value,
    that value."""
    position = (len(sorted_values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


# How far back load_state() looks for "the last run that changed the index"
# and "the last check that ran the golden set". Far more than the runs of a
# few `griot index all` (five each) or a few checks; bounded so that a long
# history is not read whole for one line of a report.
_STATE_LOOKBACK = 200


def _golden_cases_run(golden_check: dict) -> int | None:
    """How many curated cases a recorded run actually searched: the pass
    rate's denominator.

    passed + failed, not the stored `total`: records written after case
    modes and before the total excluded skipped cases stored every case in
    it, and no field tells those records apart. passed + failed is right for
    every record griot wrote (before modes nothing was skipped, so the two
    agree). A record without `failed`, which griot never wrote but logs.db
    is a file, keeps its stored total rather than reading as zero run."""
    passed, failed = golden_check.get("passed"), golden_check.get("failed")
    if isinstance(passed, int) and isinstance(failed, int):
        return passed + failed
    return golden_check.get("total")


def _golden_set_state() -> tuple[dict | None, str | None]:
    """(what the curated file holds, why it could not be read). A file that
    is there and cannot be used is NOT the same as no file: the first is
    something to fix, and saying nothing about it would read as "no golden
    set", which is how a broken one went unnoticed."""
    from pathlib import Path

    from griot import golden_set

    if not common.GOLDEN_SET_PATH.exists():
        return None, None
    try:
        cases = golden_set.list_cases()
        if not isinstance(cases, list):
            raise ValueError("it is not a list of cases")
        expected = set()
        for case in cases:
            for exp in case["must_include"]:
                if isinstance(exp.get("repo"), str) and exp["repo"]:
                    expected.add(exp["repo"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        return None, f"{type(e).__name__}: {e}"[:200]
    # Compared with repos.json, not with the index: asking the index costs a
    # scan per name and needs the collection, which another process may
    # hold. `griot quality-check` asks the index itself. When repos.json
    # cannot be read there is nothing to compare with, and nothing is claimed.
    try:
        registered = {Path(p).name for p in common.load_repos()}
    except (OSError, ValueError):
        return {"cases": len(cases), "unregistered_repos": []}, None
    return {"cases": len(cases), "unregistered_repos": sorted(expected - registered)}, None


def load_state() -> dict:
    """What is true NOW, whatever window the report covers: when the index
    was last searched, when it last changed, the last quality check and the
    last run of the golden set, and the curated golden set itself. A window
    of activity cannot say any of it: two reports a week apart printed the
    same lines, and a golden set nobody had run in a month did not appear.

    Everything about a collection is about the ACTIVE one: with two
    profiles, a check of the other collection says nothing about this one.
    Reads only, and never opens the collection."""
    collection = common.COLLECTION_NAME
    # The last search OF THIS COLLECTION: a search of another profile's
    # index says nothing about whether this one is in use.
    last_query = logdb.read_latest(common.LOG_DIR, "queries", collection=collection)

    last_quality = last_golden = None
    for record in logdb.read_recent(common.LOG_DIR, "quality_checks", collection=collection, limit=_STATE_LOOKBACK):
        self_check = record.get("self_check") or {}
        golden_check = record.get("golden_check") or {}
        if last_quality is None:
            sampled = self_check.get("sampled") or 0
            last_quality = {
                "timestamp": record.get("timestamp"),
                "pass_rate": round((self_check.get("passed") or 0) / sampled, 4) if sampled else None,
            }
        # The background check and `--skip-golden-set` record a check with
        # no golden part: the golden set's last result is in an older record.
        if last_golden is None and golden_check.get("total") is not None:
            # `skipped` is 0 for a record from before cases had modes: none
            # could be skipped then.
            last_golden = {"timestamp": record.get("timestamp"), "passed": golden_check.get("passed"),
                           "total": _golden_cases_run(golden_check), "skipped": golden_check.get("skipped") or 0}
        if last_quality and last_golden:
            break

    # The last run that wrote or removed something. Not simply the last run:
    # the last source of `griot index all` usually changes nothing.
    last_change = next((r.get("timestamp") for r in logdb.read_recent(
        common.LOG_DIR, "runs", collection=collection, limit=_STATE_LOOKBACK)
        if r.get("indexed") or r.get("pruned")), None)

    curated, unreadable = _golden_set_state()
    return {"last_query_at": (last_query or {}).get("timestamp"), "last_index_change_at": last_change,
            "last_quality_check": last_quality, "last_golden_check": last_golden,
            "golden_set": curated, "golden_set_error": unreadable}


def compute_stats(runs: list[dict], queries: list[dict], index_status: dict,
                  quality_checks: list[dict] | None = None,
                  tool_calls: list[dict] | None = None, *,
                  state: dict | None = None, now: datetime | None = None,
                  collection: str | None = None, since: datetime | None = None) -> dict:
    """Pure aggregation logic — no I/O, testable on its own. `runs`/`queries`
    should already come filtered by the desired day window (see main()).

    `runs` MUST be in ascending chronological order: recent_reuse_rate takes
    a trailing slice, so an out-of-order list yields a silently wrong number
    rather than an error. load_window() guarantees the order (logdb's
    `ORDER BY timestamp`, which _filter_by_days() only filters, never
    reorders) — a caller assembling `runs` from anywhere else must sort.

    quality_checks is optional (defaults to none) so existing callers keep
    working unchanged: each entry is {timestamp, collection, pass_rate} as
    stored since an earlier decision.

    state is what load_state() returns: facts that do not depend on the
    window. Without it the result simply lacks those keys (last_query_at,
    last_quality_check_at, last_quality_pass_rate,
    quality_is_older_than_index, golden_set): absent means "not looked at",
    which is not the same as None, "looked and there is none".

    collection is the scope of the counts: given, only the runs, queries and
    quality checks of that collection count, the same collection the state
    lines are about; None counts every profile. Spend is never scoped (money
    is spent per account, whichever collection a call was for), and MCP tool
    calls and resource reads cannot be (they record no collection). since is
    when the window opened (window_start()), echoed so the report can name it."""
    # Spend is read from everything in the window, before the scope narrows
    # it: the breaker's daily total is one number for the whole account.
    spend_records = runs + queries
    without_collection = 0
    if collection is not None:
        # A record that names no collection was written before the field
        # existed (a JSONL import, a legacy quality file). It cannot be placed
        # in any one collection, and guessing from its profile would claim a
        # mapping that may have changed since; it is left out of the scope and
        # COUNTED, so the report can say that --all-profiles shows more.
        without_collection = sum(1 for r in [*runs, *queries, *(quality_checks or [])] if not r.get("collection"))
        runs = [r for r in runs if r.get("collection") == collection]
        queries = [q for q in queries if q.get("collection") == collection]
        quality_checks = [q for q in (quality_checks or []) if q.get("collection") == collection]
    # `or 0`, not just the get() default: that decisionrecords a run that died
    # before finishing with counts set to None ON PURPOSE (0 would read as "it
    # ran and did nothing", a different fact that would distort these totals).
    # get(key, 0) returns the default only when the key is ABSENT — a key
    # present and null gives back None, and the sum raised TypeError, taking
    # the whole report down over a run that contributes nothing to it.
    # [real crash reported from production, 2026-08-22]
    total_indexed = sum(r.get("indexed") or 0 for r in runs)
    total_skipped = sum(r.get("skipped") or 0 for r in runs)
    total_failed = sum(r.get("failed") or 0 for r in runs)
    reads = [c for c in (tool_calls or []) if _is_resource_read(c.get("tool"))]
    tools = [c for c in (tool_calls or []) if not _is_resource_read(c.get("tool"))]

    # [review finding] A dead run (an earlier decision: died before counting
    # anything, counts written as None on purpose) is a run that HAPPENED —
    # it stays in num_runs, because hiding it would let the report claim a
    # clean history the user did not have. But it is not a FAILURE: a
    # failure counted failures, a dead run counted nothing, and merging them
    # would put a number in total_failed that no failure produced.
    #
    # It also must not stand in for a run wherever "run" means "run that
    # produced counts" — the two places below where it silently did:
    # the first-index gate, and the trailing slice.
    dead = [r for r in runs if r.get("indexed") is None and r.get("error")]
    counted_runs = [r for r in runs if r not in dead]
    last_error = next((r.get("error") for r in reversed(dead) if r.get("error")), None)
    reuse_denominator = total_indexed + total_skipped
    reuse_rate = (total_skipped / reuse_denominator) if reuse_denominator else None

    # [real case, 2026-08-22] The window average describes the PERIOD, which
    # stops describing the SYSTEM the moment a fix lands mid-window: after
    # the decision-65 fixes were validated in production the window held 14
    # broken runs (~0% reuse) next to 8 healthy ones (~100%), averaging to a
    # 47.7% that was true of neither era — and kept printing a "suspicious"
    # warning at the user who had just proved incremental indexing works.
    # A warning that cries wolf gets ignored when it finally matters, so the
    # recent slice is what the warning keys off. Same reasoning as the
    # quality trend's median: an aggregate over the whole window hides
    # exactly the recent behavior the reader is asking about.
    # Sliced over counted_runs, not runs: the slice is sized to "a bit more
    # than one full griot index all", and dead runs occupying slots would
    # shrink it to whatever survived — leaving recent_reuse_rate computed
    # from one run while being presented, here and by the stats prompt, as
    # the figure to trust.
    recent = counted_runs[-_RECENT_RUNS:] if len(counted_runs) > _RECENT_RUNS else []
    recent_indexed = sum(r.get("indexed", 0) or 0 for r in recent)
    recent_skipped = sum(r.get("skipped", 0) or 0 for r in recent)
    recent_denominator = recent_indexed + recent_skipped
    recent_reuse_rate = (recent_skipped / recent_denominator) if recent_denominator else None

    # spend_today_usd is a cumulative SNAPSHOT for the day (section 9, spend
    # circuit breaker) — several runs on the same day repeat the same growing
    # value; summing them all would count the same spend multiple times. The
    # correct rule is the maximum observed per calendar date, summed only
    # across distinct dates.
    #
    # [real finding, 2026-08-14] runs AND queries share the SAME circuit
    # breaker (.spend_state.json) — griot ask (chat) has written
    # spend_today_usd on the query ever since chat's multi-provider
    # generalization, so ignoring queries here used to underestimate real
    # spend whenever a question was the last spend event of the day (common:
    # few indexing runs, many questions).
    spend_by_date: dict[str, float] = {}
    for r in spend_records:
        spend = r.get("spend_today_usd")
        ts = r.get("timestamp")
        if spend is None or not ts:
            continue
        # The LOCAL date, because that is the day the breaker counts:
        # spend_today_usd resets at local midnight (common._today()). This
        # used to take the UTC date from the text of the timestamp, so one
        # local evening that crossed midnight UTC put the same growing total
        # under two dates and the sum counted it twice.
        when = _when(ts)
        if when is None:
            continue
        date = when.astimezone().date().isoformat()
        spend_by_date[date] = max(spend_by_date.get(date, 0.0), spend)
    total_spend_usd = round(sum(spend_by_date.values()), 4)

    num_queries = len(queries)
    # Only durations that are numbers: a record with none (null, or text
    # from a hand edit) used to take the whole report down in the average.
    durations = sorted(q["duration_seconds"] for q in queries
                       if isinstance(q.get("duration_seconds"), (int, float))
                       and not isinstance(q.get("duration_seconds"), bool))
    avg_query_latency_seconds = round(sum(durations) / len(durations), 2) if durations else None
    # What is typical and what is slow. The average answers neither: one
    # search that waited twenty seconds for a busy collection moves it more
    # than forty ordinary ones.
    latency_p50 = round(_percentile(durations, 0.5), 2) if durations else None
    latency_p90 = round(_percentile(durations, 0.9), 2) if durations else None

    # --- what is true now, whatever the window -----------------------------
    state_known, state = state is not None, state or {}
    last_indexed = index_status.get("last_indexed") or {}
    last_indexed_at = last_indexed.get("timestamp")
    last_quality = state.get("last_quality_check") or None
    last_quality_at = (last_quality or {}).get("timestamp")

    # "Checked before the index last changed": the last run that wrote or
    # removed something (load_state() finds it, whatever the window) is
    # newer than the last check.
    checked, changed = _when(last_quality_at), _when(state.get("last_index_change_at"))
    changed_after_check = checked is not None and changed is not None and changed > checked

    # Which repositories are behind their own repository (freshness.py), the
    # most behind first, those whose history was rewritten last; and which
    # have no code indexed at all. Absent from an older status dict, or when
    # the caller did not look.
    repositories = index_status.get("repositories") or []
    repositories_behind = sorted(
        ({"repo": r["repo"], "commits_behind": r.get("commits_behind"), "behind_sources": r.get("behind_sources") or []}
         for r in repositories if r.get("behind")),
        key=lambda r: (r["commits_behind"] is None, -(r["commits_behind"] or 0)))
    repositories_without_code = [r["repo"] for r in repositories if "code" in (r.get("missing_sources") or [])]
    attention = []
    if repositories_behind:
        attention.append("the index is behind the repository in: " + ", ".join(_behind_phrase(r) for r in repositories_behind)
                         + " — run `griot index all` (or the repository alone with --repo)")
    if repositories_without_code:
        attention.append("code never indexed in: " + ", ".join(repositories_without_code)
                         + " — a search finds nothing in their files until `griot index code` runs")
    refused, not_found = platform_refused_names(repositories), platform_not_found_names(repositories)
    mixed = platform_mixed_refusals(repositories)
    # A 404 under a token is fixed in the remote or the token's reach, and
    # pointing at the token sent the user to the wrong place (debt 67); a
    # repository refused for more than one reason gets the fix of each of
    # its causes, and no other (debt 70).
    token_refused = [name for name in refused if name not in not_found and name not in dict(mixed)]
    if token_refused:
        attention.append(platform_refused_phrase(token_refused) + " — " + _TOKEN_CHECK + ", then `griot index platform`")
    if not_found:
        attention.append(platform_not_found_phrase(not_found) + " — " + _PROJECT_CHECK + ", then `griot index platform`")
    for name, causes in mixed:
        checks = ([_TOKEN_CHECK + " for the fetches it refused"] if "token" in causes else []) + (
            [_PROJECT_CHECK] if "not_found" in causes else [])
        attention.append(platform_mixed_phrase(name, causes) + " — " + "; and ".join(checks)
                         + ", then `griot index platform`")
    if last_indexed.get("error") and not refusal_names_last_run(last_indexed, repositories):
        # A run with no counts died; one with counts finished and could not
        # do its job (the platform refused every fetch, say). A refusal the
        # line above already names is not said a second time.
        what = "did not finish" if last_indexed.get("indexed") is None else "failed"
        attention.append(f"the last indexing run {what}: {last_indexed['error']}")
    if index_status.get("spend_ceiling_exceeded"):
        attention.append("today's spend reached the daily ceiling: paid calls are refused until "
                         "tomorrow, or until the ceiling is raised")
    if (index_status.get("points_error") or "").startswith("unreadable"):
        attention.append(f"the collection could not be read: {index_status['points_error'][len('unreadable: '):][:160]}")

    if state.get("golden_set_error"):
        attention.append(f"the golden set file could not be read ({state['golden_set_error']}): fix or delete "
                         f"{common.GOLDEN_SET_PATH.name}, then curate again with `griot golden-set add`")

    curated = state.get("golden_set") or None
    golden = None
    if curated:
        last_golden = state.get("last_golden_check") or {}
        golden = {"cases": curated.get("cases"), "unregistered_repos": list(curated.get("unregistered_repos") or []),
                  "last_passed": last_golden.get("passed"), "last_total": last_golden.get("total"),
                  "last_skipped": last_golden.get("skipped"),
                  "last_run_at": last_golden.get("timestamp")}

    # [MCP validation] Which surface asked — the question "is the MCP path
    # actually being used, and does it work?" needs the split, not a single
    # total. Records written before griot_search was instrumented carry no
    # `via`: counted as "unknown" rather than dropped (they'd vanish) or
    # attributed to a surface (they'd lie).
    queries_by_surface: dict[str, int] = {}
    for q in queries:
        surface = q.get("via") or "unknown"
        queries_by_surface[surface] = queries_by_surface.get(surface, 0) + 1

    # Which project asked. Records from before the field existed carry none, and a null
    # one means it could not be worked out: both are "unknown", counted and not dropped.
    queries_by_project: dict[str, int] = {}
    for q in queries:
        project = q.get("project") or "unknown"
        queries_by_project[project] = queries_by_project.get(project, 0) + 1

    # Whether searches FIND anything, which a count of searches cannot say.
    # Median rather than mean: one lucky 0.98 shouldn't paper over a run of
    # mediocre retrievals. Searches that returned nothing are counted apart
    # instead of folded in — a zero-result search is the loudest signal the
    # index isn't answering, and averaging would bury it.
    # Over vector searches only: a keyword search scores with BM25 (unbounded)
    # and a hybrid one with a fused rank (around 0.03), and either folded in
    # would move the median without retrieval getting better or worse. A
    # record from before modes existed was a vector search.
    queries_by_mode: dict[str, int] = {}
    for q in queries:
        mode = q.get("mode") or "vector"
        queries_by_mode[mode] = queries_by_mode.get(mode, 0) + 1
    top_scores = [q["top_score"] for q in queries
                  if q.get("top_score") is not None and (q.get("mode") or "vector") == "vector"]
    median_top_score = round(statistics.median(top_scores), 4) if top_scores else None
    empty_searches = sum(1 for q in queries if q.get("top_score") is None and q.get("num_sources") == 0)

    source_breakdown: dict[str, int] = {}
    for q in queries:
        for label in q.get("sources", []):
            kind = _classify_source_label(label)
            source_breakdown[kind] = source_breakdown.get(kind, 0) + 1

    known = {
        "last_query_at": state.get("last_query_at"),
        "last_quality_check_at": last_quality_at,
        "last_quality_pass_rate": (last_quality or {}).get("pass_rate"),
        "quality_is_older_than_index": changed_after_check,
        "golden_set": golden,
    } if state_known else {}
    return {
        # First, and not tied to the window: what someone has to act on.
        "attention": attention,
        "last_indexed_at": last_indexed_at,
        "repositories_behind": repositories_behind,
        "repositories_without_code": repositories_without_code,
        # The last run is the last ATTEMPT: one that died wrote nothing.
        "last_indexed_error": last_indexed.get("error") or None,
        **known,
        # Which records the counts below are about: the active collection
        # ("active_profile") or every profile's. Spend and tool calls are
        # always every profile's, whatever this says.
        "scope": "active_profile" if collection is not None else "all_profiles",
        "scope_collection": collection,
        "records_without_collection": without_collection,
        "window_start": since.isoformat() if since is not None else None,
        # Rendering needs "now" to say how long ago; kept with the facts so
        # that the text and the numbers cannot be about two different moments.
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "total_pruned": sum(r.get("pruned") or 0 for r in runs),
        "total_redacted": sum(r.get("redacted") or 0 for r in runs),
        "query_latency_p50_seconds": latency_p50,
        "query_latency_p90_seconds": latency_p90,
        "points_count": index_status.get("points_count"),
        "points_error": index_status.get("points_error"),
        "embed_profile": index_status.get("embed_profile"),
        "num_runs": len(runs),
        "total_indexed": total_indexed,
        "total_skipped": total_skipped,
        "total_failed": total_failed,
        "reuse_rate": reuse_rate,
        # Both None when the window is too short for a trailing slice to
        # mean anything different from the aggregate (see _RECENT_RUNS).
        # recent_skipped is exposed alongside the rate so a renderer that
        # decides based on the recent slice can also COUNT from it —
        # praising "saved re-embedding N chunks" with the window's N while
        # describing the recent runs mixes two scales in one sentence
        # (review finding).
        "recent_reuse_rate": recent_reuse_rate,
        "recent_skipped": recent_skipped if recent else None,
        "total_spend_usd": total_spend_usd,
        # [user-requested] Same per-day breakdown already
        # computed above to derive total_spend_usd correctly — exposed
        # here (sorted oldest-first) for the spend-over-time
        # chart. Previously computed and discarded.
        "spend_by_date": [{"date": d, "amount": a} for d, a in sorted(spend_by_date.items())],
        "num_queries": num_queries,
        "avg_query_latency_seconds": avg_query_latency_seconds,
        "queries_by_surface": queries_by_surface,
        "queries_by_project": queries_by_project,
        "queries_by_mode": queries_by_mode,
        "dead_runs": len(dead),
        "last_error": last_error,
        "median_top_score": median_top_score,
        "empty_searches": empty_searches,
        "source_breakdown": source_breakdown,
        # [orphaned reader] Quality-check history has been recorded in
        # logs.db since an earlier decision, but the only reader was the web
        # Quality page — removing it left the data
        # accumulating with nothing surfacing it. Points with no pass rate
        # (a check that sampled nothing, e.g. an empty collection) are
        # dropped rather than plotted as zero, which would read as "failed
        # everything" instead of "nothing measured".
        "quality_trend": [q for q in (quality_checks or []) if q.get("pass_rate") is not None],
        # [orphan sweep] Which MCP tools an agent actually calls, and which
        # of those failed. Counted separately rather than as a success rate:
        # "called 40 times, 40 failed" and "called 40 times, 2 failed" are
        # different problems, and a single percentage blurs them. A read of
        # an MCP resource is logged in the same table, under its URI
        # (griot://repos), but counted apart in resource_reads: summed with
        # the tools, three reads of griot://stats read as three tool calls,
        # and the question the resources raise (does anyone read them, or
        # only the tools they duplicate?) needs the two side by side.
        # [user-requested] WHY documents failed, grouped by reason. A
        # systemic failure (bad credential, oversized input) repeats one
        # reason across every document, so the grouping is the diagnosis —
        # the raw ids matter only once you know which reason to chase.
        # Runs recorded before this field existed simply contribute nothing.
        "failure_reasons": _count_by(
            [f for r in runs for f in (r.get("failures") or [])], "reason"),
        "tool_calls": _count_by(tools, "tool"),
        "failed_tool_calls": _count_by([c for c in tools if not c.get("ok")], "tool"),
        "resource_reads": _count_by(reads, "tool"),
        "failed_resource_reads": _count_by([c for c in reads if not c.get("ok")], "tool"),
    }


def _is_resource_read(name: str | None) -> bool:
    """Whether a logged call is a resource read: resources are recorded under
    their URI. An MCP tool name is letters, digits, '_', '-' and '.', so a
    scheme separator can only belong to a URI, whatever its scheme."""
    return "://" in (name or "")


def _count_by(records: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        value = r.get(key) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return counts


# [user-requested, 2026-08-21] Density primitives. Plain unicode, no rich/
# textual/blessed: the value of a screen like Claude Code's /usage is
# density and interpretation, not navigation — and none of those libraries
# is a dependency here, nor should become one for drawing two shapes. The
# the previous front end was removed in 2026-08-21 precisely to stop paying for a
# second rendering stack; this is the CLI absorbing what was worth keeping.
_BAR_FULL = "█"
_BAR_EMPTY = "░"
_SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def _bar(fraction: float, width: int = 24) -> str:
    """A `width`-wide proportional bar. Clamped at both ends: spend can
    genuinely exceed its ceiling (the breaker stops the NEXT call, it can't
    un-spend the last one), and an overflowing bar would break the layout
    while telling the reader nothing extra."""
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)


def _sparkline(values: list[float]) -> str:
    """One block character per value, scaled to the series' own range.

    A flat series (every value equal) has no range to scale against —
    rendering it at the lowest level rather than dividing by zero, which is
    also the honest reading: nothing varied."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0:
        return _SPARK_LEVELS[0] * len(values)
    top = len(_SPARK_LEVELS) - 1
    return "".join(_SPARK_LEVELS[round((v - lo) / span * top)] for v in values)


# [debt 6, user decision 2026-10-06] Terminal charts for the three trends,
# drawn from the same block characters as above: still no dependency. They
# only replace the one-line forms when the output is a terminal that can show
# them; everywhere else (a pipe, a file, NO_COLOR, a narrow window) the
# report stays the plain text that scripts and pasted bug reports read.
_MIN_CHART_WIDTH = 60
# Past this a bar or a column chart gets longer without getting easier to
# read, and a maximised window would stretch the report into a sparse mess.
_MAX_CHART_WIDTH = 100
_CHART_HEIGHT = 4
# Every continuation line of the report starts under the section labels
# ("API spend:   " is 13 columns); the charts keep that column.
_INDENT = " " * 13
# Index = eighths filled, 0..8. Index 0 is a space: zero draws nothing.
_EIGHTHS = " ▁▂▃▄▅▆▇█"
# Below this a bar cannot show a proportion worth reading: the breakdown stays
# a sentence instead of truncating labels to make room.
_MIN_BAR_WIDTH = 10


def _chart_width(stream=None, environ=None) -> int | None:
    """The width to draw charts at, or None for the plain text.

    None when the output is not a terminal (the report is being piped or
    saved), when NO_COLOR is set (no-color.org: present and not empty — the
    convention for "plain output, please", which the charts are not), or
    when the terminal is too narrow for a chart to stay legible."""
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError, OSError):  # a replaced or closed stream
        tty = False
    if not tty or environ.get("NO_COLOR"):
        return None
    columns = shutil.get_terminal_size().columns
    if columns < _MIN_CHART_WIDTH:
        return None
    return min(columns, _MAX_CHART_WIDTH)


def _fill_days(series: list[dict]) -> list[tuple[str, float]] | None:
    """spend_by_date as one (date, amount) per CALENDAR day from the first to
    the last. spend_by_date only lists days with recorded spend, and a time
    axis that skipped the rest would draw two days a week apart as
    neighbours. A day it does not list had no spend recorded: zero.

    None when a date is not an ISO calendar date (what compute_stats()
    writes today): the chart is a nicety, and the caller falls back to the
    sparkline rather than crash a report the plain text could still show."""
    if not series:
        return []
    try:
        days = [datetime.strptime(p["date"], "%Y-%m-%d").date() for p in series]
    except (KeyError, TypeError, ValueError):
        return None
    amounts = {}
    for day, point in zip(days, series):
        amounts[day] = point.get("amount") or 0.0
    filled, day = [], min(days)
    while day <= max(days):
        filled.append((day.isoformat(), amounts.get(day, 0.0)))
        day += timedelta(days=1)
    return filled


def _columns(values: list[float], top: float, height: int = _CHART_HEIGHT) -> list[str]:
    """Rows, top first, of a column chart where `top` fills the height.

    Resolution is an eighth of a row. A positive value too small to round to
    one eighth still gets one: a day that spent something must not look like
    a day that spent nothing."""
    cells = []
    for v in values:
        units = round(max(0.0, v) / top * height * 8) if top > 0 else 0
        if v > 0 and units == 0:
            units = 1
        cells.append(units)
    rows = []
    for level in range(height - 1, -1, -1):
        # Clamped per row: a value above `top` just fills every row.
        rows.append("".join(_EIGHTHS[max(0, min(8, units - level * 8))] for units in cells))
    return rows


def _column_chart(title: str, unit: str, values: list[float], labels: list[str], top: float,
                  top_label: str, bottom_label: str, width: int) -> list[str]:
    """A small column chart: a title, the columns against a labelled scale,
    and the first and last label under them. One column per value; when
    there are more values than room, the most recent ones are kept and the
    title says how many of how many."""
    gutter = max(len(top_label), len(bottom_label))
    room = width - len(_INDENT) - gutter - 2  # the space and the axis
    total = len(values)
    if total > room:
        values, labels = values[-room:], labels[-room:]
    count = f"{total} {unit}" if total == len(values) else f"last {len(values)} of {total} {unit}"
    lines = [f"{_INDENT}{title}, {count}"]
    for i, row in enumerate(_columns(values, top)):
        scale, axis = (top_label, "┤") if i == 0 else ("", "│")
        lines.append(f"{_INDENT}{scale:>{gutter}} {axis}{row}".rstrip())
    lines.append(f"{_INDENT}{bottom_label:>{gutter}} └{'─' * len(values)}")
    lines.append(f"{_INDENT}{'':>{gutter}}  {labels[0]} → {labels[-1]}")
    return lines


def _local_day(timestamp) -> str:
    """The local calendar date of a timestamp, the same day spend is counted
    under; the raw value when it cannot be placed in time."""
    when = _when(timestamp)
    return when.astimezone().date().isoformat() if when else str(timestamp)


def _source_bars(breakdown: dict[str, int], width: int) -> list[str] | None:
    """The source breakdown as horizontal bars, largest first, the largest
    filling the bar. None when the longest label leaves no room for a bar
    worth reading: labels are never cut short to make room."""
    items = sorted(breakdown.items(), key=lambda kv: -kv[1])
    total = sum(breakdown.values())
    biggest = items[0][1]
    suffixes = [f" {count / total * 100:>3.0f}% ({count})" for _, count in items]
    label_width = max(len(kind) for kind, _ in items)
    indent = _INDENT + "  "
    bar_width = width - len(indent) - label_width - 1 - max(len(s) for s in suffixes)
    if bar_width < _MIN_BAR_WIDTH:
        return None
    lines = [f"{_INDENT}most used sources:"]
    for (kind, count), suffix in zip(items, suffixes):
        lines.append(f"{indent}{kind:<{label_width}} {_bar(count / biggest, bar_width)}{suffix}")
    return lines


def _behind_phrase(report: dict) -> str:
    """`one (3 commits; tags changed)`: the commits since for the sources
    that follow HEAD, and the refs sources that changed."""
    count = report.get("commits_behind")
    parts = []
    if count is None:
        parts.append("the indexed commit is not in this history")
    elif count:
        parts.append(f"{count} commit{'s' if count != 1 else ''}")
    changed = [source for source in report.get("behind_sources") or [] if source in ("tags", "branches")]
    if changed:
        parts.append(f"{' and '.join(changed)} changed")
    return f"{report['repo']} ({'; '.join(parts) or 'behind'})"


def platform_refused_names(reports: list[dict]) -> list[str]:
    """The repositories whose platform refused every fetch in the newest
    platform run that concerned them (freshness.py), as they may be printed:
    a directory name can hold an escape sequence."""
    return [common.printable(r["repo"]) for r in reports if r.get("platform_refused")]


def platform_not_found_names(reports: list[dict]) -> list[str]:
    """Those of platform_refused_names() whose refusal was a 404 to every
    fetch under a token (freshness.py), as they may be printed."""
    return [common.printable(r["repo"]) for r in reports if r.get("platform_refused") and r.get("platform_not_found")]


def platform_mixed_refusals(reports: list[dict]) -> list[tuple[str, dict[str, list[str]]]]:
    """The repositories of platform_refused_names() whose fetches were
    refused for more than one cause (freshness.py), with those causes, as
    they may be printed. Never one of platform_not_found_names(): that field
    says every fetch was a 404, and it keeps its word when a record edited
    by hand says both. A report without causes (a run from before they were
    recorded) is not one either: it reads as it always did."""
    return [(common.printable(r["repo"]), r["platform_refusal_causes"]) for r in reports
            if r.get("platform_refused") and not r.get("platform_not_found")
            and isinstance(r.get("platform_refusal_causes"), dict) and len(r["platform_refusal_causes"]) > 1]


# What each recorded cause of a refused fetch says, in the order the run's
# closing message gives them (index_platform.py::_refusal_lines).
_CAUSE_WORDS = {"not_found": "not found, or not visible to the token", "token": "refused for the token",
                "other": "failed for another reason"}
_TOKEN_CHECK = "check the platform's token (`griot auth list`)"
_PROJECT_CHECK = ("check the project path in its remote and that the token's account can see it "
                  "(`griot auth list` shows which token)")


def platform_mixed_phrase(name: str, causes: dict[str, list[str]]) -> str:
    """Shared by `griot stats` and `griot doctor`, as platform_refused_phrase():
    which fetches got which answer, so the token is blamed for its own alone."""
    groups = [", ".join(common.printable(label) for label in causes[cause]) + ": " + words
              for cause, words in _CAUSE_WORDS.items() if causes.get(cause)]
    return (f"the platform refused every fetch for {name} in the last platform run, for mixed reasons ("
            + "; ".join(groups) + "): nothing of it was indexed")


def refusal_names_last_run(last_indexed: dict, reports: list[dict]) -> bool:
    """Whether the last run failed only because its platform refused
    repositories that the platform_refused_phrase() line already names.

    Then `griot stats` and `griot doctor` leave out their generic "the last
    indexing run failed" line: the same refusal said twice, and the line
    naming the repositories says more. Any other failure keeps it, and so
    does a refusal of a repository no longer registered (no line names it)
    or a run recorded before `refused_repos` existed (it names nothing)."""
    refused = last_indexed.get("refused_repos")
    if not last_indexed.get("error") or not isinstance(refused, list) or not refused:
        return False
    named = set(platform_refused_names(reports))
    return all(isinstance(name, str) and common.printable(name) in named for name in refused)


def platform_refused_phrase(names: list[str]) -> str:
    """Shared by `griot stats` and `griot doctor`, so both say it the same way."""
    return ("the platform refused every fetch for " + ", ".join(names)
            + " in the last platform run: nothing of " + ("it" if len(names) == 1 else "them") + " was indexed")


def platform_not_found_phrase(names: list[str]) -> str:
    """Shared by `griot stats` and `griot doctor`, as platform_refused_phrase()."""
    return ("the platform answered 404 to every fetch for " + ", ".join(names)
            + " in the last platform run: the project in " + ("its" if len(names) == 1 else "their")
            + " remote was not found, or is not visible to the token; nothing of "
            + ("it" if len(names) == 1 else "them") + " was indexed")


def _mcp_section(lines: list[str], heading: str, event: str, kind: str,
                 counts: dict[str, int], failed: dict[str, int]) -> None:
    """Appends one MCP usage section (tools or resources), or nothing when
    there is nothing to count. Every profile, whatever the scope: neither a
    tool call nor a resource read records a collection."""
    if not counts:
        return
    total = sum(counts.values())
    lines.append("")
    lines.append(f"{heading:<{len(_INDENT)}}{total} {event}{'s' if total != 1 else ''} across {len(counts)} {kind}"
                 f"{'s' if len(counts) > 1 else ''}, every profile")
    # Ordered by volume: during the MCP validation the top of this list
    # is the answer to "which tools earn their place".
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        suffix = f" · {failed[name]} failed" if name in failed else ""
        lines.append(f"{_INDENT}{name} {count}{suffix}")


def format_stats(s: dict, days: int, width: int | None = None) -> str:
    """The report as text. `width` None is the plain form, the same for
    every reader; a width (see _chart_width()) draws the trends as charts
    that fit it."""
    opened = _when(s.get("window_start"))
    since = f", since {opened.date().isoformat()}, local time" if opened else ""
    lines = [
        f"griot — report (last {days} day{'s' if days != 1 else ''}{since})",
        "═" * 38,
    ]
    now = _when(s.get("generated_at"))

    # Before everything else, and whatever the window: what needs someone.
    for i, item in enumerate(s.get("attention") or []):
        lines.append(f"{'Attention:' if i == 0 else '':<13}{item}")
    if s.get("attention"):
        lines.append("")

    # Which records the counts are about, before the counts: the state lines
    # below are always the active collection's, so a total over every
    # profile next to them has to say so.
    if s.get("scope") == "active_profile":
        lines.append(f"Scope:       collection {s['scope_collection']} (active profile) · "
                     f"--all-profiles counts every profile")
        n = s.get("records_without_collection") or 0
        if n:
            lines.append(f"             {n} record{'s' if n != 1 else ''} in the period "
                         f"name{'s' if n == 1 else ''} no collection (written before the field existed): "
                         f"counted only with --all-profiles")
    elif s.get("scope") == "all_profiles":
        lines.append("Scope:       every profile's collection")

    # [real output, 2026-08-22] None is neither zero nor an error here: it
    # means the count could not be read because another process holds the
    # collection open — routine on a machine running `griot mcp`, which holds
    # the handle while it is in use (and for its whole life in single mode).
    # Printing "None points" reads as "the index is empty", the same illusion
    # that decisionremoved from the UI, reappearing in the surface that
    # replaced it.
    points = s["points_count"]
    profile = s["embed_profile"]
    if points is not None:
        count = f"{points} points"
    elif (s.get("points_error") or "").startswith("unreadable"):
        # Not the routine "busy": the collection could not be opened at all.
        count = f"point count unavailable, the collection could not be read ({s['points_error'][len('unreadable: '):][:120]})"
    else:
        count = "point count unavailable (collection in use)"
    lines.append(f"Index:       {count} · profile {profile}")
    # State, not activity: the same whatever window was asked for. A report
    # that only counted what happened in the window looked the same a week
    # later, and said nothing of an index nobody had refreshed in a month.
    if "last_indexed_at" in s:
        indexed_ago = _ago(s["last_indexed_at"], now)
        # "Indexed" only for a run that finished: one that died is an attempt.
        wrote = "last indexing attempt" if s.get("last_indexed_error") else "last indexed"
        recency = [f"{wrote} {indexed_ago}" if indexed_ago else "never indexed"]
        if "last_query_at" in s:  # absent when the caller did not look (see compute_stats)
            searched_ago = _ago(s["last_query_at"], now)
            recency.append(f"last search {searched_ago}" if searched_ago else "never searched")
        lines.append(f"             {' · '.join(recency)}")
    if s.get("repositories_behind"):
        lines.append(f"             index behind in {', '.join(_behind_phrase(r) for r in s['repositories_behind'])}")

    reuse = f"{s['reuse_rate'] * 100:.1f}% reused" if s["reuse_rate"] is not None else "no reuse data"
    lines.append("")
    lines.append(f"Indexing:    {s['num_runs']} runs · {s['total_indexed']} embedded · {s['total_skipped']} skipped ({reuse})")
    if s["total_failed"]:
        lines.append(f"             {s['total_failed']} failures in the period")
        # Most-common reason first: a systemic failure dominates the list,
        # and that is exactly the one worth chasing.
        for reason, count in sorted((s.get("failure_reasons") or {}).items(), key=lambda kv: -kv[1]):
            lines.append(f"               {count}× {reason}")

    cleaned = []
    if s.get("total_pruned"):
        cleaned.append(f"{s['total_pruned']} stale points removed")
    if s.get("total_redacted"):
        cleaned.append(f"{s['total_redacted']} credential-looking values replaced")
    if cleaned:
        lines.append(f"             {' · '.join(cleaned)}")

    # Reuse is the number that justifies incremental indexing existing at
    # all, so it gets a sentence rather than a bare percentage. A LOW rate
    # is the signature of the duplicate-point-id bug class (an earlier decision,
    # where reuse collapsed to 0.29% across 14 real runs) — worth naming
    # instead of leaving the reader to notice.
    #
    # [review finding] The warning is gated on more than one run in the
    # window: a FIRST indexing reuses nothing by definition (there was
    # nothing to reuse), so warning there would alarm every new user in the
    # most normal situation there is. With a single run we cannot tell
    # "first ever" from anything else, so we say nothing; with several, a
    # rerun over largely unchanged content genuinely should reuse.
    #
    # [real case, 2026-08-22] Both the sentence and the warning key off the
    # RECENT runs when there are enough of them, not the window average: a
    # fix landing mid-window leaves two eras averaged into a number true of
    # neither, and warning off that average fires at someone whose indexing
    # is demonstrably working. The window figure stays visible on the line
    # above — the point is to expose the disagreement, not replace one bias
    # with another.
    recent = s.get("recent_reuse_rate")
    effective = recent if recent is not None else s["reuse_rate"]
    if recent is not None and abs(recent - (s["reuse_rate"] or 0)) >= 0.1:
        lines.append(f"             recent runs: {recent * 100:.0f}% reused "
                     f"(period average {s['reuse_rate'] * 100:.0f}% spans older runs)")
    if effective is not None:
        if effective >= 0.5:
            # Counted from the same runs the rate came from — see
            # compute_stats()'s note on recent_skipped.
            saved = s["recent_skipped"] if recent is not None else s["total_skipped"]
            scope = "in the last runs" if recent is not None else "in the period"
            lines.append(f"             saved re-embedding {saved} unchanged chunks {scope}")
        elif s["num_runs"] - s.get("dead_runs", 0) > 1:
            lines.append("             low reuse across repeated runs — expected if the content really "
                         "changed, suspicious otherwise")

    # Independent of total_failed: a dead run contributes nothing to it, so
    # gating this on failures is how three dead runs printed as a clean
    # "3 runs · 0 embedded · 0 skipped".
    if s.get("dead_runs"):
        n = s["dead_runs"]
        detail = f" — last: {s['last_error']}" if s.get("last_error") else ""
        lines.append(f"             {n} run{'s' if n > 1 else ''} died before counting anything{detail}")

    ceiling = common.SPEND_CEILING_USD
    lines.append("")
    # Every profile, whatever the scope: the breaker counts the account's money.
    lines.append(f"API spend:   ${s['total_spend_usd']:.4f} in the period, every profile (ceiling: ${ceiling:.2f}/day)")
    # The bar answers "how close am I to the ceiling?" at a glance — the
    # one thing the two numbers side by side don't. Compared against the
    # DAILY ceiling, matching how the breaker actually trips.
    if ceiling > 0:
        used = s["total_spend_usd"] / ceiling
        share = f"{used * 100:.0f}% of one day's ceiling"
        # On a terminal the bar shrinks to fit rather than wrap; the plain
        # form keeps its fixed width.
        bar_width = min(24, width - len(_INDENT) - len(share) - 1) if width else 24
        lines.append(f"{_INDENT}{_bar(used, bar_width)} {share}")

    # [real gap, left by the front end's removal] spend_by_date has always been computed
    # here; the only thing that ever rendered it was the removed front end's
    # chart, so since its removal the series was computed on every call and
    # discarded. One day is not a trend, hence the >1 guard.
    trend = s.get("spend_by_date") or []
    if len(trend) > 1:
        days_filled = _fill_days(trend) if width else None
        if days_filled:
            amounts = [amount for _, amount in days_filled]
            lines.extend(_column_chart("spend per day", "days", amounts, [d for d, _ in days_filled],
                                       max(amounts), f"${max(amounts):.4f}", "$0", width))
        else:
            lines.append(f"             {_sparkline([p['amount'] for p in trend])} spend trend, {len(trend)} days")

    lines.append("")
    if s["num_queries"]:
        if s.get("query_latency_p50_seconds") is not None:
            latency = f"latency p50 {s['query_latency_p50_seconds']}s · p90 {s['query_latency_p90_seconds']}s"
        else:
            latency = f"avg latency {s['avg_query_latency_seconds']}s"  # a result computed before the percentiles
        lines.append(f"Queries:     {s['num_queries']} · {latency}")
        by_surface = s.get("queries_by_surface") or {}
        if by_surface:
            # Ordered by volume: the dominant surface is the one worth
            # reading first, and during the MCP validation that ordering
            # answers the question by itself.
            split = " · ".join(f"{k} {v}" for k, v in sorted(by_surface.items(), key=lambda kv: -kv[1]))
            lines.append(f"             by surface: {split}")
        by_project = s.get("queries_by_project") or {}
        if any(name != "unknown" for name in by_project):  # all-unknown is noise, not information
            split = " · ".join(f"{k} {v}" for k, v in sorted(by_project.items(), key=lambda kv: -kv[1]))
            lines.append(f"             by project: {split}")
        by_mode = s.get("queries_by_mode") or {}
        if set(by_mode) - {"vector"}:  # only vector searches: the line would say nothing new
            split = " · ".join(f"{k} {v}" for k, v in sorted(by_mode.items(), key=lambda kv: -kv[1]))
            lines.append(f"             by mode: {split}")
        if s.get("median_top_score") is not None:
            found = f"median top score {s['median_top_score']:.2f}" + (" (vector searches)" if set(by_mode) - {"vector"} else "")
            if s.get("empty_searches"):
                found += f" · {s['empty_searches']} found nothing"
            lines.append(f"             {found}")
        elif s.get("empty_searches"):
            lines.append(f"             {s['empty_searches']} found nothing")
        bars = _source_bars(s["source_breakdown"], width) if width and s["source_breakdown"] else None
        if bars:
            lines.extend(bars)
        elif s["source_breakdown"]:
            total = sum(s["source_breakdown"].values())
            breakdown = " · ".join(
                f"{kind} {count / total * 100:.0f}%"
                for kind, count in sorted(s["source_breakdown"].items(), key=lambda kv: -kv[1])
            )
            lines.append(f"             most used sources: {breakdown}")
    else:
        lines.append("Queries:     none in the period")
    # A window longer than the retention counts searches and tool calls over
    # the retention only (prune_logs_if_due): say so, or a 400-day report
    # reads as a year in which nobody searched before last spring. Resource
    # reads live in the tool-call table, so the same pruning applies to them.
    if days > common.LOG_RETENTION_DAYS:
        lines.append(f"             searches, tool calls and resource reads are kept for "
                     f"{common.LOG_RETENTION_DAYS} days "
                     f"(log-retention-days): older ones are not counted")

    # Two sections, not one heading over both: a resource read is logged like
    # a tool call, and summed under "MCP tools" it inflated the tool count
    # with reads that were never a tool call. Each heading names what it counts.
    # [debt 36] "MCP reads:", not "MCP resources:": every heading fits the
    # 13 columns of _INDENT, and the longer one pushed this value two
    # columns right of the item lines under it and of every other heading.
    _mcp_section(lines, "MCP tools:", "call", "tool",
                 s.get("tool_calls") or {}, s.get("failed_tool_calls") or {})
    _mcp_section(lines, "MCP reads:", "read", "resource",
                 s.get("resource_reads") or {}, s.get("failed_resource_reads") or {})

    quality = s.get("quality_trend") or []
    if quality:
        latest = quality[-1]["pass_rate"]
        lines.append("")
        lines.append(f"Quality:     {latest * 100:.0f}% of sampled points self-retrieved "
                     f"({len(quality)} check{'s' if len(quality) > 1 else ''})")
        if len(quality) > 1:
            rates = [q["pass_rate"] for q in quality]
            # Latest check compared against the MEDIAN of the ones before
            # it, not against the first. The median is robust in both
            # directions that matter here: a single noisy dip between
            # healthy checks doesn't read as a regression, AND an old
            # problem sitting at the window's edge can't make a fresh drop
            # look like an improvement (comparing against the first check
            # called 0.50 -> 0.95 -> 0.95 -> 0.60 an "up" trend, because
            # 0.60 > 0.50 — review finding, with the real slide hidden).
            baseline = statistics.median(rates[:-1])
            direction = "down" if rates[-1] < baseline else "up" if rates[-1] > baseline else "flat"
            median = f"(median of earlier checks: {baseline * 100:.0f}%)"
            if width:
                # A fixed 0-100% scale, unlike the sparkline's own range:
                # 95% next to 96% must not draw as a cliff.
                lines.extend(_column_chart("pass rate per check", "checks", rates,
                                           [_local_day(q.get("timestamp")) for q in quality],
                                           1.0, "100%", "0%", width))
                lines.append(f"             trend {direction} {median}")
            else:
                lines.append(f"             {_sparkline(rates)} trend {direction} {median}")
    elif "last_quality_check_at" in s:
        # Nothing in the window is not the same as nothing ever, and both
        # are worth a line: the section used to vanish, which read as "fine".
        lines.append("")
        checked_ago = _ago(s.get("last_quality_check_at"), now)
        if checked_ago:
            rate = s.get("last_quality_pass_rate")
            measured = f" ({rate * 100:.0f}% of sampled points self-retrieved)" if rate is not None else ""
            lines.append(f"Quality:     last checked {checked_ago}{measured}")
        else:
            lines.append("Quality:     never checked (`griot quality-check`)")
    if s.get("quality_is_older_than_index"):
        lines.append("             checked before the index last changed: it describes an older index")

    golden = s.get("golden_set")
    if golden:
        n = golden.get("cases") or 0
        if golden.get("last_total") is not None:
            ran = f"last run: {golden['last_passed']} of {golden['last_total']} passed"
            if golden.get("last_skipped"):
                # Not failed, not passed: cases whose mode needs keyword
                # search the collection did not have when they were run.
                ran += f", {golden['last_skipped']} skipped"
            ran_ago = _ago(golden.get("last_run_at"), now)
            ran += f" ({ran_ago})" if ran_ago else ""
        else:
            ran = "never run (`griot quality-check`)"
        lines.append("")
        lines.append(f"Golden set:  {n} case{'s' if n != 1 else ''} · {ran}")
        if golden.get("unregistered_repos"):
            names = ", ".join(common.shown(name) for name in golden["unregistered_repos"])
            # repos.json, not the index: a repository indexed with --path is
            # not in it and its cases can pass.
            lines.append(f"             cases expect a repository that is not in repos.json ({names}): "
                         f"unless it was indexed with --path, they can only fail")

    return "\n".join(lines)


def load_window(days: int, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """(runs, queries) already filtered to the window of `days` local days
    (window_start()) — the I/O half of report(), extracted so a front end
    doesn't have to reach into this module's private _filter_by_days() to
    get the same data. logdb.read_since() does an indexed SQL narrowing by
    timestamp first (the actual performance win over the old
    runs.jsonl/queries.jsonl, which had to be read in full every time);
    _filter_by_days() still runs afterward as the authoritative pass.

    The SQL narrowing asks for one day more than the window: the window
    opens at local midnight, which can be up to 25 hours a day before `now`
    (a day the clock falls back), and a stored timestamp written with
    another offset compares there as text, not as a moment. The extra day
    covers both; the Python pass cuts at the exact moment.

    `runs` holds only the runs logdb.prune_runs_beyond() kept (the last
    RUN_RETENTION of each repository and source): a window over more runs
    than that of one source counts the newest only. The order and the null
    counts of dead runs are what they were before a prune, so every figure
    is computed the same way over fewer runs, and the facts read from older
    runs (the last change, the last run of each source) are runs the prune
    never takes."""
    now_iso = now.isoformat() if now else None
    runs = _filter_by_days(logdb.read_since(common.LOG_DIR, "runs", days + 1, now), days, now_iso)
    queries = _filter_by_days(logdb.read_since(common.LOG_DIR, "queries", days + 1, now), days, now_iso)
    return runs, queries


def load_tool_calls(days: int, now: datetime | None = None) -> list[dict]:
    """MCP tool invocations in the window, cut at the same local midnight as
    load_window() (and narrowed in SQL with the same extra day). The counting
    happens in compute_stats() so it stays testable without I/O."""
    return _filter_by_days(logdb.read_tool_calls(common.LOG_DIR, days + 1, now), days,
                           now.isoformat() if now else None)


def load_quality_window(days: int, now: datetime | None = None) -> list[dict]:
    """Quality-check history in the window (the same local days as
    load_window()), oldest first, reduced to what a trend needs: when, which
    collection, and the pass rate.

    Same shape the removed front end's helper produced before its deletion
    removed it — that page was the only reader of this table, so without
    this the records kept accumulating with nothing surfacing them.
    pass_rate is None (never a ZeroDivisionError) for a check that sampled
    nothing, e.g. a collection that is still empty."""
    # [orphan sweep] One-time import of an existing install's
    # last_quality_check.json. That service layer used to trigger
    # this and took it with it when deleted, so the file silently stopped
    # being picked up — the reader is the natural place for it, matching the
    # "reader triggers the one-time import" pattern the runs/queries
    # migration already follows. logdb tracks the marker, so it is
    # idempotent and never re-imports.
    logdb.migrate_legacy_json_file(common.LOG_DIR, common.DATA_DIR / "last_quality_check.json",
                                   "quality_checks")
    entries = []
    for record in _filter_by_days(logdb.read_since(common.LOG_DIR, "quality_checks", days + 1, now), days,
                                  now.isoformat() if now else None):
        self_check = record.get("self_check") or {}
        sampled = self_check.get("sampled") or 0
        passed = self_check.get("passed") or 0
        entries.append({
            "timestamp": record.get("timestamp"),
            "collection": record.get("collection"),
            "pass_rate": round(passed / sampled, 4) if sampled else None,
        })
    return entries


def report(days: int, *, all_profiles: bool = False) -> dict:
    """The whole report, for `griot stats` and the griot_stats MCP tool
    alike, so the two cannot disagree on the window or the scope. One `now`
    for every read: the window, the spend days and "how long ago" are about
    the same moment.

    By default the counts are the active collection's, the collection every
    state line is about; all_profiles counts every profile's."""
    now = datetime.now(timezone.utc)
    runs, queries = load_window(days, now)
    return compute_stats(runs, queries, common.get_index_status(),
                         quality_checks=load_quality_window(days, now),
                         tool_calls=load_tool_calls(days, now),
                         state=load_state(), now=now,
                         collection=None if all_profiles else common.COLLECTION_NAME,
                         since=window_start(days, now))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot stats",
        description="Usage, spend, and savings report — aggregates already-recorded logs, no new collection.",
    )
    show_flags_read_by_griot(parser, PROFILE_FLAG)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="Window in local days: today since local midnight plus the N-1 days before it, "
                             "the days spend is counted by (default: %(default)s)")
    parser.add_argument("--all-profiles", action="store_true",
                        help="Count the runs, searches and quality checks of every profile's collection, not only "
                             "the active one's. The state lines are always the active collection's; spend and "
                             "MCP tool calls are always every profile's")
    parser.add_argument("--json", action="store_true", help="Raw JSON output instead of formatted text")
    args = parser.parse_args(argv)
    if args.days < 1:
        # A window of no days is always empty, which reads as "no activity".
        parser.error(f"--days must be at least 1 (got {args.days})")

    result = report(args.days, all_profiles=args.all_profiles)
    # Both front ends, so `--json` and griot_stats say over how long the
    # search and tool-call counts can reach.
    result["log_retention_days"] = common.LOG_RETENTION_DAYS
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        # The terminal decides between charts and plain text; --json above
        # never asks, so scripts get the same data wherever they run.
        print(format_stats(result, args.days, width=_chart_width()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
