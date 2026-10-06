"""`griot golden-set` — manages `quality_golden_set.json` (tier 2, curated,
from `griot quality-check` — see quality_check.run_golden_set()) without
hand-editing the JSON.

`suggest` derives candidates FOR FREE from `git log` (reuses
`retrieval_eval.build_qrels_from_git()`, doesn't duplicate the qrels logic —
the same function that already feeds the Recall@k/MRR ruler), with
case-by-case human approval — no case enters the golden set without explicit
confirmation. `add` runs a REAL search against the current index and lets the
human pick which results form the `must_include` — covers questions that
don't come from any specific commit. `review` derives candidates from the
query log instead (questions asked more than once, or that scored low), and a
person at a terminal picks which logged result was right. `list`/`remove`
round out the CRUD, same pattern as `griot repos`."""

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

from griot import common, logdb, retrieval_eval

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


# Moved to common.case_entry() when the query log began recording the same
# entry for every result (see `review`); this name is what `add` and the
# tests have always used.
_must_include_entry = common.case_entry


def _load() -> list:
    if not common.GOLDEN_SET_PATH.exists():
        return []
    return json.loads(common.GOLDEN_SET_PATH.read_text())


def _save(cases: list) -> None:
    common.secure_mkdir(common.GOLDEN_SET_PATH.parent)
    # [M2] 0600: curated queries reveal real repo/file names
    # Through a scratch file and a rename: written in place, a write that
    # stopped half-way (a full disk, Ctrl-C) left a file nobody could read,
    # and with it every case curated so far.
    common.secure_write_text_atomic(common.GOLDEN_SET_PATH, json.dumps(cases, indent=2, ensure_ascii=False))


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


# How many results a case is searched with when it does not say (see
# quality_check.run_golden_set), and so how many files one may require.
CASE_LIMIT = 5


def suggest_candidates(repo_path: Path, *, max_commits: int | None = None, limit: int = 10) -> dict:
    """Data half of `golden-set suggest`: candidate cases from the git log
    of `repo_path`, none written. A commit's message is the question and
    the files it touched are what must come back.

    Only a case that CAN pass is a candidate. A touched file is required
    only if the index holds a chunk of it: `griot index code` reads it (an
    image, a lock file or a file the repository ignores is never there)
    and it has text (an empty `__init__.py` is listed and stores nothing).
    A commit that touched more such files than a search returns is left
    out: every file is required, so it could never pass. So is one without
    a message: there is no question. Approved, a case like those failed
    forever, and a failing golden set is what says an index is broken.

    `repo_path` may be a directory inside a work tree (that is how it is
    indexed, then): git names files from the top of the tree, so they are
    brought to the directory's own paths, and a file outside it is not its.

    {repo, candidates: [{query, limit, must_include, commit}], commits,
    not_indexable, too_many_files, no_message, problem}: the counts are the
    commits left out for each reason, over the whole log that was read;
    `problem` is what the file listing said when it found nothing. `limit`
    is how many candidates to return, counted after leaving those out.
    Raises ValueError when `repo_path` is not a git work tree."""
    import subprocess

    from griot import index_code  # lazy: only this command needs the file discovery

    repo_name = repo_path.name
    try:
        top = Path(common.run_git(repo_path, ["rev-parse", "--show-toplevel"], timeout=30).stdout.strip())
        prefix = common.run_git(repo_path, ["rev-parse", "--show-prefix"], timeout=30).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as e:
        # Said plainly, and as a failure: carrying on printed the git
        # command that failed and then "no candidates", with exit status 0.
        raise ValueError(f"'{repo_path}' is not a git work tree: candidates come from its git log.") from e
    qrels = retrieval_eval.build_qrels_from_git(top, repo_name, max_commits=max_commits)
    # What it reports about files it skips is for an index run. Kept only to
    # say why nothing was listed, when nothing was.
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        indexable = {path.relative_to(repo_path).as_posix(): path for path in index_code.discover_files(repo_path)}
    root = repo_path.resolve()
    has_text: dict[str, bool] = {}

    def in_the_index(name: str) -> bool:
        """Whether an index run stores at least one chunk for this file:
        the same two checks index_code.process_repository() makes."""
        if name not in indexable:
            return False
        if name not in has_text:
            content = index_code._read_source(indexable[name], root)
            has_text[name] = bool(content and content.strip())
        return has_text[name]

    candidates, not_indexable, too_many_files, no_message = [], 0, 0, 0
    for qrel in qrels:
        if not qrel["query"].strip():
            no_message += 1
            continue
        here = [f[len(prefix):] for f in qrel["relevant_file_paths"] if f.startswith(prefix)]
        files = [f for f in here if in_the_index(f)]
        if not files:
            not_indexable += 1
        elif len(files) > CASE_LIMIT:
            too_many_files += 1
        elif len(candidates) < limit:
            candidates.append({
                "query": qrel["query"], "limit": CASE_LIMIT,
                "must_include": [{"repo": repo_name, "source_type": "code", "file_path": f} for f in files],
                "commit": qrel["commit_hash"],
            })
    return {"repo": repo_name, "candidates": candidates, "commits": len(qrels),
            "not_indexable": not_indexable, "too_many_files": too_many_files, "no_message": no_message,
            "problem": " ".join(said.getvalue().split()) or None if not indexable else None}


def left_out_note(found: dict) -> str | None:
    """Why some commits are not among the candidates, or None when all are."""
    reasons = []
    if found["too_many_files"]:
        reasons.append(f"{found['too_many_files']} commit(s) touched more files than a case can require "
                       f"({CASE_LIMIT}: every file must be among the results of one search)")
    if found["not_indexable"]:
        reasons.append(f"{found['not_indexable']} commit(s) touched no file the index would hold (none that "
                       f"`griot index code` reads, or only empty ones)")
    if found["no_message"]:
        reasons.append(f"{found['no_message']} commit(s) have no message, so there is no question to ask")
    return ("Left out: " + "; ".join(reasons) + ".") if reasons else None


def cmd_suggest(repo_path_str: str, max_commits: int | None = None, limit: int = 10) -> int:
    repo_path = Path(repo_path_str).resolve()  # `suggest .` must still name the repository
    if not repo_path.is_dir():
        print(f"Error: '{repo_path_str}' does not exist or is not a directory.", file=sys.stderr)
        return 1
    if limit < 1:
        print(f"Error: --limit must be at least 1 (got {limit}).", file=sys.stderr)
        return 2

    try:
        found = suggest_candidates(repo_path, max_commits=max_commits, limit=limit)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    note = left_out_note(found)
    if not found["candidates"]:
        print("No candidates found (no commit touched files that the index reads and that still exist in the "
              "working tree).")
        if found["problem"]:
            print(common.printable(found["problem"]))
        if note:
            print(note)
        return 0

    cases = _load()
    added = 0
    for candidate in found["candidates"]:
        display_query = candidate["query"].splitlines()[0]
        files = [entry["file_path"] for entry in candidate["must_include"]]
        print(f"\nCandidate query: {display_query}")
        print(f"  must_include: {len(files)} file(s) — {', '.join(files)}")
        answer = input("Approve this case? [y/N/q(uit)] ").strip().lower()
        if answer == "q":
            break
        if answer != "y":
            continue
        cases.append({"query": candidate["query"], "limit": candidate["limit"],
                      "must_include": candidate["must_include"]})
        added += 1

    if added:
        _save(cases)
    print(f"\n{added} case(s) added to {common.GOLDEN_SET_PATH}.")
    if note:
        print(note)
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


# --- review: cases from the questions actually asked ---------------------
#
# The golden set grows from real use here, the way `suggest` grows it from
# git history: the query log proposes, a person approves each case. Nothing
# is written anywhere but the golden set and the private record of
# rejections beside it; the index stays a copy derived from the
# repositories alone, which is why this reads the log and never the index.

# Fewer vector searches than this in one collection and none of them is
# called "hard": the bottom quarter of a handful of scores moves with every
# new search, so it would measure the sample, not the question.
HARD_MIN_SEARCHES = 20


def query_key(text: str) -> str:
    """When two logged questions are the same question: the same set of
    words, ignoring case, punctuation, spacing and word order. Cheap and
    explainable (no embedding call, nothing paid): "How is the lock
    released?" and "released: how is the lock" are one question, "how is
    the lock acquired" is another. Anything looser (stemming, synonyms)
    would merge questions a person meant differently."""
    import unicodedata

    spaced = "".join(" " if unicodedata.category(ch).startswith("P") else ch for ch in text.casefold())
    return " ".join(sorted(set(spaced.split())))


def rejected_path() -> Path:
    """The private record of questions a person rejected in a review: beside
    the golden set, in the configuration directory, never in the index."""
    return common.GOLDEN_SET_PATH.with_name("golden_set_rejected.json")


def _digest(key: str) -> str:
    # A digest, not the text: remembering "never offer this again" should
    # not keep a copy of a question the person chose to throw away.
    import hashlib

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _load_rejected() -> set[str]:
    path = rejected_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        # An unreadable record offers rejected questions again: a person
        # sees them and can reject them again, which is the harmless way to fail.
        return set()
    return {item for item in data if isinstance(item, str)} if isinstance(data, list) else set()


def _reject(key: str) -> None:
    rejected = _load_rejected()
    rejected.add(_digest(key))
    path = rejected_path()
    common.secure_mkdir(path.parent)
    common.secure_write_text_atomic(path, json.dumps(sorted(rejected)))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mode(row: dict) -> str:
    return row.get("mode") or "vector"  # a row from before modes existed was a vector search


def _hard_thresholds(rows: list[dict]) -> dict:
    """Per collection, the top score at or under which a vector search counts
    as hard: the first quartile of that collection's vector top scores.

    Per collection because two embedding models do not score on one scale.
    Vector only: a keyword score grows with the query's words and their
    rarity, and a hybrid score is a rank (reciprocal rank fusion), so a low
    one says nothing about how well the best result matched. The other hard
    signal, the vector and keyword rankings disagreeing, is not used: a
    hybrid search logs only the fused results, not the two rankings."""
    import statistics

    scores: dict = {}
    for row in rows:
        if _mode(row) == "vector" and _is_number(row.get("top_score")):
            scores.setdefault(row.get("collection"), []).append(float(row["top_score"]))
    return {collection: statistics.quantiles(values, n=4, method="inclusive")[0]
            for collection, values in scores.items() if len(values) >= HARD_MIN_SEARCHES}


def _candidate(kind: str, key: str, group: list[dict], thresholds: dict) -> dict:
    # The newest asking whose results can be picked from; failing that, the newest.
    row = next((r for r in reversed(group) if any(isinstance(e, dict) for e in r.get("results") or [])), group[-1])
    sources = [s for s in row.get("sources") or [] if isinstance(s, str)]
    results = row.get("results")
    if not isinstance(results, list) or len(results) != len(sources):
        results = None  # logged before results were recorded: shown, not made a case
    limit = row.get("limit")
    return {
        "kind": kind, "key": key, "query": row["question"], "times": len(group),
        "projects": len({r.get("project") for r in group if r.get("project")}),
        "mode": _mode(row), "collection": row.get("collection"),
        "top_score": row.get("top_score") if _is_number(row.get("top_score")) else None,
        "threshold": thresholds.get(row.get("collection")),
        "limit": limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else CASE_LIMIT,
        "timestamp": row.get("timestamp"), "repos": row.get("repos"), "source_types": row.get("source_types"),
        "sources": sources, "results": results,
    }


def review_candidates(rows: list[dict] | None = None, *, limit: int = 10) -> dict:
    """Data half of `golden-set review`: questions from the query log worth
    making into cases, none written.

    Two kinds, repeated first: a question asked more than once (by
    query_key(), across sessions and projects), most asked first; then a
    vector search whose best result scored in the bottom quarter of its
    collection's (see _hard_thresholds()), lowest first. A question already
    in the golden set, or rejected in an earlier review, is not offered.
    A search logged without its question (GRIOT_LOG_QUESTIONS off) cannot
    be: it is counted in `omitted`.

    {candidates, searches, omitted}. `rows` defaults to every search the log
    still keeps."""
    if rows is None:
        rows = logdb.read_since(common.LOG_DIR, "queries", days=common.LOG_RETENTION_DAYS)
    covered = {query_key(case["query"]) for case in _load()
               if isinstance(case, dict) and isinstance(case.get("query"), str)}
    rejected = _load_rejected()
    omitted = 0
    groups: dict[str, list[dict]] = {}
    for row in rows:
        question = row.get("question")
        if question == common.OMITTED_QUESTION:
            omitted += 1
            continue
        if not isinstance(question, str):
            continue
        key = query_key(question)
        if key and key not in covered and _digest(key) not in rejected:
            groups.setdefault(key, []).append(row)

    # Over every search, not only the candidates: the threshold describes
    # how this collection usually scores.
    thresholds = _hard_thresholds(rows)
    repeated, hard = [], []
    for key, group in groups.items():
        if len(group) > 1:
            repeated.append(_candidate("repeated", key, group, thresholds))
            continue
        row = group[0]
        threshold = thresholds.get(row.get("collection"))
        if threshold is not None and _mode(row) == "vector" and _is_number(row.get("top_score")) \
                and row["top_score"] <= threshold:
            hard.append(_candidate("hard", key, group, thresholds))
    repeated.sort(key=lambda c: (c["times"], c["timestamp"] or ""), reverse=True)
    hard.sort(key=lambda c: c["top_score"])
    return {"candidates": (repeated + hard)[:limit], "searches": len(rows), "omitted": omitted}


def _not_logged_note(omitted: int) -> str | None:
    if not omitted and common.log_questions_enabled():
        return None
    said = f"{omitted} search(es) were logged without their question" if omitted else "No question was withheld yet"
    return (f"{said}: question logging is "
            f"{'on now' if common.log_questions_enabled() else 'off (GRIOT_LOG_QUESTIONS=false)'}, and a search "
            f"logged without its question cannot be reviewed. To log questions from now on: "
            f"griot config set log-questions true")


_CANNOT = ("cannot become a case: it was logged before griot recorded what a case matches on, "
           "or its name looked like a credential")


def _show(candidate: dict, number: int, total: int) -> None:
    print(f"\n[{number}/{total}] {common.shown(candidate['query'].splitlines()[0] if candidate['query'] else '')}")
    if candidate["kind"] == "repeated":
        where = f", from {candidate['projects']} projects" if candidate["projects"] > 1 else ""
        print(f"  Why: asked {candidate['times']} times{where}.")
    else:
        print(f"  Why: low score. Its best result scored {candidate['top_score']:.2f}, at or under "
              f"{candidate['threshold']:.2f}: the bottom quarter of the vector searches in this collection.")
    narrowed = [f"{name} {', '.join(map(str, candidate[name]))}" for name in ("repos", "source_types")
                if isinstance(candidate[name], list) and candidate[name]]
    print(f"  Results logged for it ({candidate['mode']} search, {str(candidate['timestamp'] or '?')[:10]}"
          f"{', narrowed to ' + '; '.join(common.shown(n) for n in narrowed) if narrowed else ''}):")
    results = candidate["results"]
    for j, label in enumerate(candidate["sources"], start=1):
        flag = "" if results is not None and isinstance(results[j - 1], dict) else "  (cannot become a case)"
        print(f"    [{j}] {common.shown(label)}{flag}")
    if results is None:
        print(f"  These results {_CANNOT}. Asked again, the question can be.")
    if candidate["mode"] != "vector" or narrowed:
        # quality_check.run_golden_set() runs every case as a plain vector search.
        print("  Note: a case is checked by a plain vector search, over every repository: that search may "
              "not return what this one did.")


def _ask(candidate: dict) -> str | list[dict]:
    """The person's answer for one candidate: "skip", "none", "reject",
    "quit", or the must_include entries of the results they picked."""
    sources, results = candidate["sources"], candidate["results"]
    pickable = results is not None and any(isinstance(e, dict) for e in results)
    prompt = (("Which result is the right one? number(s), comma-separated; " if pickable else "")
              + "n = none of them, s = skip, r = reject (never offer again), q = quit: ")
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            return "quit"
        if answer in ("", "s", "skip"):
            return "skip"
        if answer in ("n", "none"):
            return "none"
        if answer in ("r", "reject"):
            return "reject"
        if answer in ("q", "quit"):
            return "quit"
        try:
            picked = sorted({int(part) for part in answer.split(",") if part.strip()})
        except ValueError:
            print("  Answer with numbers (e.g. 1,3), n, s, r or q.")
            continue
        wrong = [j for j in picked if not 1 <= j <= len(sources)]
        if not picked or wrong:
            print(f"  {', '.join(map(str, wrong)) or 'Nothing'} out of range (1-{len(sources)}).")
            continue
        unusable = [j for j in picked if results is None or not isinstance(results[j - 1], dict)]
        if unusable:
            print(f"  Result {', '.join(map(str, unusable))} {_CANNOT}.")
            continue
        return [results[j - 1] for j in picked]


def cmd_review(limit: int = 10) -> int:
    if limit < 1:
        print(f"Error: --limit must be at least 1 (got {limit}).", file=sys.stderr)
        return 2
    # A person decides which result was right: that is what makes a case
    # worth trusting. Like the confirmations (see common.confirm), a guard
    # against the plain command run from an agent's shell, not a boundary.
    if not common.is_interactive():
        print("Error: the review asks a person which result was the right one, and there is no terminal to ask "
              "on. Nothing was changed. Run it in an interactive terminal.", file=sys.stderr)
        return 2
    try:
        found = review_candidates(limit=limit)
    except (OSError, ValueError) as e:
        print(f"Error: cannot read {common.GOLDEN_SET_PATH}: {e}", file=sys.stderr)
        return 1
    note = _not_logged_note(found["omitted"])
    candidates = found["candidates"]
    if not candidates:
        print(f"No candidates among {found['searches']} logged search(es): none asked more than once, or scoring "
              f"in the bottom quarter of its collection's vector searches (of at least {HARD_MIN_SEARCHES}), "
              f"that is not already a case or rejected.")
        if note:
            print(note)
        return 0
    if note:
        print(note)

    added = 0
    for number, candidate in enumerate(candidates, start=1):
        _show(candidate, number, len(candidates))
        try:
            answer = _ask(candidate)
        except KeyboardInterrupt:
            print(f"\nStopped. {added} case(s) added to {common.GOLDEN_SET_PATH}.")
            return 130
        if answer == "quit":
            break
        if answer == "none":
            print("  Nothing added: a case needs the result that should come back. It will be offered again.")
        elif answer == "reject":
            _reject(candidate["key"])
            print("  Rejected: it will not be offered again.")
        elif isinstance(answer, list):
            try:
                add_case(candidate["query"], answer, limit=candidate["limit"])
            except ValueError as e:
                print(f"  Not added: {e}")
                continue
            added += 1
            print("  Case added.")
    print(f"\n{added} case(s) added to {common.GOLDEN_SET_PATH}.")
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

    p_review = sub.add_parser("review", help="Offers questions from the query log (asked more than once, or scoring "
                                             "low) as cases; at a terminal, you pick the right result")
    p_review.add_argument("--limit", type=int, default=10, help="How many candidates to offer (default: %(default)s)")

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
    if args.action == "review":
        return cmd_review(limit=args.limit)
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
