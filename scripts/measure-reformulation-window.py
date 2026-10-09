#!/usr/bin/env python3
"""Measures how the window of `griot golden-set review`'s "reformulated"
candidates (golden_set.REFORMULATION_WINDOW_SECONDS) changes what is
offered, on CONSTRUCTED query logs: no real log is read, nothing is written.

    env GRIOT_CONFIG_DIR=$(mktemp -d) GRIOT_DATA_DIR=$(mktemp -d) \\
        .venv/bin/python scripts/measure-reformulation-window.py

(griot.common creates its directories on import, hence the throwaway ones.)

Each constructed pair is a search followed by the next search of its
session, of one of three kinds, at gaps drawn from a log-normal around the
median given (seeded, so the same numbers come out every run):

- reword: the person or agent asks the same thing in other words after
  reading the list. An agent rewords within seconds (median 20 s); a person
  at a terminal reads an answer first (median 90 s). 70% agent, as griot is
  mostly searched by agents.
- facet: the next question is another facet of the same subject (median
  10 s, an agent's burst). The word rule cannot tell it from a rewording at
  any window, so it is counted, not optimised for.
- return: the next search of the session comes back to the subject after
  other work (median 20 min).

Every pair passes the rule's other conditions (same collection, a
different question sharing its subject words, other results), so the
window alone decides. A good window offers nearly every rewording and few
returns.

The gaps are assumptions, not measurements of anyone's use: rerun this
with other medians to see how sensitive the choice is."""

import argparse
import random
from datetime import datetime, timedelta, timezone

from griot import golden_set

KINDS = {  # kind -> list of (share, median seconds)
    "reword": [(0.7, 20), (0.3, 90)],
    "facet": [(1.0, 10)],
    "return": [(1.0, 20 * 60)],
}
SIGMA = 0.8
WINDOWS = (60, 120, 300, 600, 1800)


def gap(rng: random.Random, kind: str) -> float:
    roll, acc = rng.random(), 0.0
    for share, median in KINDS[kind]:
        acc += share
        if roll <= acc:
            break
    return max(1.0, rng.lognormvariate(0, SIGMA) * median)


def constructed_rows(rng: random.Random, pairs: int) -> list[tuple[str, list[dict]]]:
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    out = []
    for i in range(pairs):
        for kind in KINDS:
            at = start + timedelta(hours=i)
            first = {"timestamp": at.isoformat(), "session": f"{kind}-{i}", "collection": "c",
                     "question": f"how is the lock{i} released", "sources": [f"a{i}"]}
            then = {"timestamp": (at + timedelta(seconds=gap(rng, kind))).isoformat(), "session": f"{kind}-{i}",
                    "collection": "c", "question": f"lock{i} released when the process exits",
                    "sources": [f"b{i}"]}
            out.append((kind, [first, then]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pairs", type=int, default=1000, help="Pairs of each kind (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    data = constructed_rows(random.Random(args.seed), args.pairs)
    print(f"{'window':>8}  " + "  ".join(f"{kind:>7}" for kind in KINDS))
    for window in WINDOWS:
        golden_set.REFORMULATION_WINDOW_SECONDS = window
        caught = {kind: 0 for kind in KINDS}
        for kind, rows in data:
            caught[kind] += bool(golden_set._reformulations(rows))
        print(f"{window:>7}s  " + "  ".join(f"{100 * caught[k] / args.pairs:>6.0f}%" for k in KINDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
