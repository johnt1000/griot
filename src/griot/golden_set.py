"""`griot golden-set` — manages `quality_golden_set.json` (tier 2, curated,
from `griot quality-check` — see quality_check.run_golden_set()) without
hand-editing the JSON.

`suggest` derives candidates FOR FREE from `git log` (reuses
`retrieval_eval.build_qrels_from_git()`, doesn't duplicate the qrels logic —
the same function that already feeds the Recall@k/MRR ruler), with
case-by-case human approval — no case enters the golden set without explicit
confirmation. `add` runs a REAL search against the current index and lets the
human pick which results form the `must_include` — covers questions that
don't come from any specific commit. `list`/`remove` round out the CRUD, same
pattern as `griot repos`."""

import argparse
import json
import sys
from pathlib import Path

from griot import common, retrieval_eval

# Fields that identify an item of each source_type specifically but without
# overfitting (e.g. it does NOT include chunk_index for code — a question
# should be able to match any chunk of that file, not just one) — same
# spirit as the examples already hand-curated in quality_golden_set.json
# before this task (e.g. {"repo": "my-project", "source_type": "commit"}).
# Moved to common.py when search() grew document grouping, which needs the
# identical mapping: one table, two readers.
_IDENTIFYING_FIELDS = common.IDENTIFYING_FIELDS


def has_effective_constraint(entry: dict) -> bool:
    """Whether a must_include entry can actually rule a result out.

    False when every value is None (including the empty dict), because
    quality_check._matches() compares with payload.get(k), which returns None
    for an absent key — so such an entry matches everything and the case can
    never fail. Lives here rather than in quality_check.py so the write guard
    and the read guard apply the identical rule; quality_check imports it."""
    return any(value is not None for value in entry.values())


def _must_include_entry(payload: dict) -> dict:
    source_type = payload.get("source_type", "code")
    entry = {"source_type": source_type}
    # [review finding] `repo` is written only when the payload HAS one. It
    # used to be set unconditionally to payload.get("repo"), so a payload
    # without a repo produced {"repo": None} — an entry that matches only
    # results which themselves have no repo, i.e. never. Same None-versus-
    # absent confusion as the guard in add_case(), pointing the other way:
    # a case that can never pass instead of one that can never fail.
    if payload.get("repo") is not None:
        entry["repo"] = payload["repo"]
    for field in _IDENTIFYING_FIELDS.get(source_type, []):
        if field in payload:
            entry[field] = payload[field]
    return entry


def _load() -> list:
    if not common.GOLDEN_SET_PATH.exists():
        return []
    return json.loads(common.GOLDEN_SET_PATH.read_text())


def _save(cases: list) -> None:
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    # [M2] 0600: curated queries reveal real repo/file names
    common.secure_write_text(common.GOLDEN_SET_PATH, json.dumps(cases, indent=2, ensure_ascii=False))


def list_cases() -> list[dict]:
    """Pure read of the curated cases — cmd_list()'s data half, so a
    non-terminal caller gets the data without print(). The caller today is
    the griot_golden_set_list MCP tool."""
    return _load()


def check_cases(cases) -> None:
    """Raises ValueError when what the file holds cannot be run as cases.

    add_case() guards what it writes, but the file is also edited by hand
    and written by cmd_suggest(): the point where the cases are RUN is the
    only one every writer passes. A case without its fields used to surface
    there as the name of the missing key. Cases are numbered as `golden-set
    list` and `golden-set remove` number them, from 1.

    An entry that constrains nothing is not refused here: run_golden_set()
    fails that case by name, which closes the gate and still runs the rest."""
    if not isinstance(cases, list):
        raise ValueError("it must hold a list of cases")
    for number, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(f"case {number} is not an object with `query` and `must_include`")
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError(f"case {number} has no `query` text")
        entries = case.get("must_include")
        if not isinstance(entries, list) or not entries or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError(f"case {number} needs `must_include`: a list of at least one object")
        limit = case.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"case {number} has a `limit` that is not a whole number of at least 1")


def add_case(query: str, must_include: list[dict], limit: int = 5) -> dict:
    """Data half of `golden-set add`: persists a case and returns it. The
    caller decides which search results are correct and passes the
    already-built must_include entries (see _must_include_entry() for how
    cmd_add() builds one from a raw search result) — this function does NOT
    call common.search() itself, so it works for cases that don't come from
    an interactive search session too. Raises ValueError (message with no
    "Error: " prefix — that's the CLI's job, see cmd_add()) on an empty
    query, an empty must_include, or an entry that constrains nothing (see
    has_effective_constraint); never touches the file in those cases."""
    if not query.strip():
        raise ValueError("query must not be empty.")
    if not must_include:
        raise ValueError("must_include must not be empty.")
    # An entry that constrains nothing can never fail, and the golden set is
    # what tells the user their index still works — a case that always passes
    # makes it report success it never measured, and makes `griot
    # quality-check` exit 0 on a gate it did not actually check.
    #
    # [review finding] The empty dict is the degenerate case, not the class.
    # quality_check._matches() is `payload.get(k) == v`, and .get() returns
    # None for a key that is absent — so {"bogus": None} matches every result
    # just as {} does, while being a perfectly non-empty dict. The rule is
    # about VALUES, not about the number of keys.
    #
    # A key carrying a real value that no payload has is the opposite and is
    # allowed: it never matches, so it fails loudly. Unsatisfiable is a
    # curation mistake the user sees; unfalsifiable is one nobody sees.
    if any(not isinstance(entry, dict) or not has_effective_constraint(entry)
           for entry in must_include):
        raise ValueError("each must_include entry must constrain at least one field "
                         "to a non-null value (e.g. repo, source_type) — an entry of "
                         "only null values matches every result and can never fail.")

    case = {"query": query, "limit": limit, "must_include": must_include}
    cases = _load()
    cases.append(case)
    _save(cases)
    return case


def remove_case(index: int) -> dict:
    """Data half of `golden-set remove`: removes by the same 1-to-N index
    list_cases() displays and returns the removed case. Raises ValueError on
    an out-of-range index — an empty set is just the (1-0) special case of
    that, no separate check needed."""
    cases = _load()
    get_case(index, cases)
    removed = cases.pop(index - 1)
    _save(cases)
    return removed


def get_case(index: int, cases: list[dict] | None = None) -> dict:
    """The case `golden-set remove <index>` would remove, by the same 1-to-N
    index. Raises the same ValueError remove_case() does, without changing
    anything."""
    cases = _load() if cases is None else cases
    if not (1 <= index <= len(cases)):
        raise ValueError(f"index {index} out of range (1-{len(cases)}).")
    return cases[index - 1]


def cmd_suggest(repo_path_str: str, max_commits: int | None = None, limit: int = 10) -> int:
    repo_path = Path(repo_path_str).resolve()  # `suggest .` must still name the repository
    if not repo_path.is_dir():
        print(f"Error: '{repo_path_str}' does not exist or is not a directory.", file=sys.stderr)
        return 1

    repo_name = repo_path.name
    qrels = retrieval_eval.build_qrels_from_git(repo_path, repo_name, max_commits=max_commits)
    if not qrels:
        print("No candidates found (no commits with touched files that still exist in the working tree).")
        return 0

    cases = _load()
    added = 0
    for qrel in qrels[:limit]:
        display_query = qrel["query"].splitlines()[0]
        files = qrel["relevant_file_paths"]
        preview = ", ".join(files[:5]) + ("..." if len(files) > 5 else "")
        print(f"\nCandidate query: {display_query}")
        print(f"  must_include: {len(files)} file(s) — {preview}")
        answer = input("Approve this case? [y/N/q(uit)] ").strip().lower()
        if answer == "q":
            break
        if answer != "y":
            continue
        cases.append({
            "query": qrel["query"],
            "must_include": [{"repo": repo_name, "source_type": "code", "file_path": f} for f in files],
        })
        added += 1

    if added:
        _save(cases)
    print(f"\n{added} case(s) added to {common.GOLDEN_SET_PATH}.")
    return 0


def cmd_add(query: str, limit: int = 5) -> int:
    from griot import ask  # lazy — same pattern as cli.py, avoids pulling in qdrant/fastembed before needed

    results = common.search(query, limit=limit)
    if not results:
        print("Error: search returned no results — nothing to approve.", file=sys.stderr)
        return 1

    print(f'Results for "{query}":')
    for i, r in enumerate(results, start=1):
        payload = r.payload or {}
        print(f"  [{i}] ({r.score:.3f}) {common.shown(ask.source_label(payload))}")

    choice = input("Which are the right result(s) (must_include)? comma-separated numbers, or empty to cancel: ").strip()
    if not choice:
        print("Cancelled — nothing added.")
        return 0

    try:
        indices = [int(x.strip()) for x in choice.split(",") if x.strip()]
    except ValueError:
        print("Error: invalid input — use comma-separated numbers (e.g. 1,3).", file=sys.stderr)
        return 1

    must_include = []
    for i in indices:
        if not (1 <= i <= len(results)):
            print(f"Error: {i} out of range (1-{len(results)}).", file=sys.stderr)
            return 1
        must_include.append(_must_include_entry(results[i - 1].payload or {}))

    try:
        add_case(query, must_include, limit=limit)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Case added to {common.GOLDEN_SET_PATH}.")
    return 0


def cmd_list() -> int:
    cases = list_cases()
    if not cases:
        print(f"No cases in {common.GOLDEN_SET_PATH} (empty).")
        return 0
    for i, case in enumerate(cases, start=1):
        query = case.get("query", "")
        preview = query if len(query) <= 80 else query[:80] + "..."
        print(f"  [{i}] {preview} ({len(case.get('must_include', []))} must_include)")
    return 0


def cmd_remove(index: int) -> int:
    try:
        removed = remove_case(index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    query = removed.get("query", "")
    print(f"Removed: {query[:80]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="griot golden-set",
        description="Manages quality_golden_set.json (curated level 2, from `griot quality-check`).",
    )
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_suggest = sub.add_parser("suggest", help="Suggests candidates for free from the git log, with case-by-case human approval")
    p_suggest.add_argument("repo_path", help="Local git repository directory")
    p_suggest.add_argument("--max-commits", type=int, default=None, help="Limit of commits to consider (default: all)")
    p_suggest.add_argument("--limit", type=int, default=10, help="How many candidates to offer for approval (default: %(default)s)")

    p_add = sub.add_parser("add", help="Runs a real search and lets you approve which results are the must_include")
    p_add.add_argument("query", help="Natural language question/term")
    p_add.add_argument("--limit", type=int, default=5, help="How many results to show to choose from (default: %(default)s)")

    sub.add_parser("list", help="Lists the already-curated cases")

    p_remove = sub.add_parser("remove", help="Removes a case by number (see `list`)")
    p_remove.add_argument("index", type=int)
    p_remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    args = parser.parse_args(argv)
    if args.action == "suggest":
        return cmd_suggest(args.repo_path, max_commits=args.max_commits, limit=args.limit)
    if args.action == "add":
        return cmd_add(args.query, limit=args.limit)
    if args.action == "list":
        return cmd_list()
    # Checked before asking, and the question shows WHICH case: a number
    # alone is easy to get wrong after an earlier removal shifted the list.
    try:
        case = get_case(args.index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    refused = common.confirm(f"Remove golden-set case #{args.index} ({str(case.get('query', ''))[:80]!r})? "
                             f"The indexed data is not touched.", yes=args.yes)
    return refused or cmd_remove(args.index)


if __name__ == "__main__":
    raise SystemExit(main())
