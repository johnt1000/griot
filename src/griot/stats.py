"""`griot stats` — usage, spend, and savings report.
Zero new data collection: aggregates logs/logs.db (SQLite, see logdb.py)
and common.get_index_status() (Qdrant), which the rest of the code already
writes/exposes.
"""

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone

from griot import common, logdb

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


def _filter_by_days(records: list[dict], days: int, now_iso: str | None = None) -> list[dict]:
    """Records without a parseable 'timestamp' are left OUT (there's no way
    to know if they're in the window) — safer than including by default in a
    "last N days" report."""
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
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


def compute_stats(runs: list[dict], queries: list[dict], index_status: dict,
                  quality_checks: list[dict] | None = None,
                  tool_calls: list[dict] | None = None) -> dict:
    """Pure aggregation logic — no I/O, testable on its own. `runs`/`queries`
    should already come filtered by the desired day window (see main()).

    `runs` MUST be in ascending chronological order: recent_reuse_rate takes
    a trailing slice, so an out-of-order list yields a silently wrong number
    rather than an error. load_window() guarantees the order (logdb's
    `ORDER BY timestamp`, which _filter_by_days() only filters, never
    reorders) — a caller assembling `runs` from anywhere else must sort.

    quality_checks is optional (defaults to none) so existing callers keep
    working unchanged: each entry is {timestamp, collection, pass_rate} as
    stored since an earlier decision."""
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
    for r in runs + queries:
        spend = r.get("spend_today_usd")
        ts = r.get("timestamp")
        if spend is None or not ts:
            continue
        # ts[:10] is textual slicing of the ISO string, not timezone parsing —
        # it's only safe because common.log_run_summary() always writes with
        # datetime.now(timezone.utc) (same timezone for everyone). If some day
        # a source writes a local timestamp without normalizing to UTC first,
        # two records from the same instant could fall into different
        # "calendar days" here.
        date = ts[:10]
        spend_by_date[date] = max(spend_by_date.get(date, 0.0), spend)
    total_spend_usd = round(sum(spend_by_date.values()), 4)

    num_queries = len(queries)
    avg_query_latency_seconds = (
        round(sum(q.get("duration_seconds", 0) for q in queries) / num_queries, 2) if num_queries else None
    )

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
    top_scores = [q["top_score"] for q in queries if q.get("top_score") is not None]
    median_top_score = round(statistics.median(top_scores), 4) if top_scores else None
    empty_searches = sum(1 for q in queries if q.get("top_score") is None and q.get("num_sources") == 0)

    source_breakdown: dict[str, int] = {}
    for q in queries:
        for label in q.get("sources", []):
            kind = _classify_source_label(label)
            source_breakdown[kind] = source_breakdown.get(kind, 0) + 1

    return {
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
        # different problems, and a single percentage blurs them.
        # [user-requested] WHY documents failed, grouped by reason. A
        # systemic failure (bad credential, oversized input) repeats one
        # reason across every document, so the grouping is the diagnosis —
        # the raw ids matter only once you know which reason to chase.
        # Runs recorded before this field existed simply contribute nothing.
        "failure_reasons": _count_by(
            [f for r in runs for f in (r.get("failures") or [])], "reason"),
        "tool_calls": _count_by(tool_calls or [], "tool"),
        "failed_tool_calls": _count_by([c for c in (tool_calls or []) if not c.get("ok")], "tool"),
    }


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


def format_stats(s: dict, days: int) -> str:
    lines = [
        f"griot — report (last {days} days)",
        "═" * 38,
    ]

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

    reuse = f"{s['reuse_rate'] * 100:.1f}% reused" if s["reuse_rate"] is not None else "no reuse data"
    lines.append("")
    lines.append(f"Indexing:    {s['num_runs']} runs · {s['total_indexed']} embedded · {s['total_skipped']} skipped ({reuse})")
    if s["total_failed"]:
        lines.append(f"             {s['total_failed']} failures in the period")
        # Most-common reason first: a systemic failure dominates the list,
        # and that is exactly the one worth chasing.
        for reason, count in sorted((s.get("failure_reasons") or {}).items(), key=lambda kv: -kv[1]):
            lines.append(f"               {count}× {reason}")

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
    lines.append(f"API spend:   ${s['total_spend_usd']:.4f} in the period (ceiling: ${ceiling:.2f}/day)")
    # The bar answers "how close am I to the ceiling?" at a glance — the
    # one thing the two numbers side by side don't. Compared against the
    # DAILY ceiling, matching how the breaker actually trips.
    if ceiling > 0:
        used = s["total_spend_usd"] / ceiling
        lines.append(f"             {_bar(used)} {used * 100:.0f}% of one day's ceiling")

    # [real gap, left by the front end's removal] spend_by_date has always been computed
    # here; the only thing that ever rendered it was the removed front end's
    # chart, so since its removal the series was computed on every call and
    # discarded. One day is not a trend, hence the >1 guard.
    trend = s.get("spend_by_date") or []
    if len(trend) > 1:
        lines.append(f"             {_sparkline([p['amount'] for p in trend])} spend trend, {len(trend)} days")

    lines.append("")
    if s["num_queries"]:
        lines.append(f"Queries:     {s['num_queries']} · avg latency {s['avg_query_latency_seconds']}s")
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
        if s.get("median_top_score") is not None:
            found = f"median top score {s['median_top_score']:.2f}"
            if s.get("empty_searches"):
                found += f" · {s['empty_searches']} found nothing"
            lines.append(f"             {found}")
        elif s.get("empty_searches"):
            lines.append(f"             {s['empty_searches']} found nothing")
        if s["source_breakdown"]:
            total = sum(s["source_breakdown"].values())
            breakdown = " · ".join(
                f"{kind} {count / total * 100:.0f}%"
                for kind, count in sorted(s["source_breakdown"].items(), key=lambda kv: -kv[1])
            )
            lines.append(f"             most used sources: {breakdown}")
    else:
        lines.append("Queries:     none in the period")

    tools = s.get("tool_calls") or {}
    if tools:
        failed = s.get("failed_tool_calls") or {}
        lines.append("")
        lines.append(f"MCP tools:   {sum(tools.values())} calls across {len(tools)} tool"
                     f"{'s' if len(tools) > 1 else ''}")
        # Ordered by volume: during the MCP validation the top of this list
        # is the answer to "which tools earn their place".
        for tool, count in sorted(tools.items(), key=lambda kv: -kv[1]):
            suffix = f" · {failed[tool]} failed" if tool in failed else ""
            lines.append(f"             {tool} {count}{suffix}")

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
            lines.append(f"             {_sparkline(rates)} trend {direction} "
                         f"(median of earlier checks: {baseline * 100:.0f}%)")

    return "\n".join(lines)


def load_window(days: int) -> tuple[list[dict], list[dict]]:
    """(runs, queries) already filtered to the last `days` days — the I/O
    half of `griot stats`'s main(), extracted so a front end (the web
    caller) doesn't have to reach into this module's private
    _filter_by_days() to get the same data. logdb.read_since() does an
    indexed SQL narrowing by timestamp first (the actual performance win
    over the old runs.jsonl/queries.jsonl, which had to be read in full
    every time); _filter_by_days() still runs afterward as the
    authoritative, already-tested validation/filtering pass."""
    runs = _filter_by_days(logdb.read_since(common.LOG_DIR, "runs", days), days)
    queries = _filter_by_days(logdb.read_since(common.LOG_DIR, "queries", days), days)
    return runs, queries


def load_tool_calls(days: int) -> list[dict]:
    """MCP tool invocations in the window. Thin passthrough — the counting
    happens in compute_stats() so it stays testable without I/O."""
    return logdb.read_tool_calls(common.LOG_DIR, days)


def load_quality_window(days: int) -> list[dict]:
    """Quality-check history in the window, oldest first, reduced to what a
    trend needs: when, which collection, and the pass rate.

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
    for record in _filter_by_days(logdb.read_since(common.LOG_DIR, "quality_checks", days), days):
        self_check = record.get("self_check") or {}
        sampled = self_check.get("sampled") or 0
        passed = self_check.get("passed") or 0
        entries.append({
            "timestamp": record.get("timestamp"),
            "collection": record.get("collection"),
            "pass_rate": round(passed / sampled, 4) if sampled else None,
        })
    return entries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot stats",
        description="Usage, spend, and savings report — aggregates already-recorded logs, no new collection.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Window in days (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="Raw JSON output instead of formatted text")
    args = parser.parse_args(argv)

    runs, queries = load_window(args.days)
    index_status = common.get_index_status()

    result = compute_stats(runs, queries, index_status,
                           quality_checks=load_quality_window(args.days),
                           tool_calls=load_tool_calls(args.days))

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(format_stats(result, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
