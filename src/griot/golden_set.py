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
from griot.cli import PROFILE_FLAG, show_flags_read_by_griot

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


def case_mode(case: dict) -> str:
    """The search mode a case is checked with. A case without one was made
    when every check searched by meaning, so absent is "vector": that keeps
    each older case's meaning, and the pass-rate trend comparable."""
    return case.get("mode", "vector")


def _mode_problem(mode) -> str | None:
    """Why `mode` cannot be a case's mode, or None. The search modes only,
    exactly as written: a case is spent later, by a check nobody watches,
    so a value the search would refuse is refused where it is written."""
    # `in` on a tuple compares with ==, so a null, a number or a list is
    # simply not found: no type check needed first.
    if mode not in common.SEARCH_MODES:
        return (f"`mode` must be one of {', '.join(common.SEARCH_MODES)} "
                f"(got {common.printable(repr(mode))[:40]}).")
    return None


def mode_refusal(query: str, mode) -> str | None:
    """Why a case of `query` cannot be checked in `mode`, or None: a mode
    that is not a search mode, or a keyword or hybrid case whose query has
    no word keyword search can match (the search refuses it on every check:
    a case that could never run). Local and free, so a caller that asks a
    person first (griot_golden_set_add) asks this before."""
    problem = _mode_problem(mode)
    if problem:
        return problem
    if mode != "vector" and not common.keyword_query_matches(query):
        return (f"a {mode} case needs a query with a word keyword search can match; this one has only common "
                f"English words or punctuation. Name the identifier itself, or use mode 'vector'.")
    return None


# Said when a case made without a mode is a vector case because the
# collection has no keyword vectors: the case is written that way for good,
# so the person (or agent) hears what it measures and how to get the default.
# Worded for one case or several: add says it after its one case, suggest
# once for every case of its run.
VECTOR_FALLBACK_NOTE = (
    "The case(s) just added are checked by meaning only (mode vector): this collection was indexed before keyword "
    "search, so the default, hybrid, cannot run on it yet. `griot index keywords` builds it once (local, embeds "
    "nothing); to check a question in hybrid after that, remove its case and add it again.")

# Said when a case made without a mode is a vector case because the
# collection's config cannot be read: whether it can run hybrid is unknown,
# and a hybrid case it cannot run would only ever be skipped.
UNREADABLE_CONFIG_NOTE = (
    "The case(s) just added are checked by meaning only (mode vector): this collection's config could not be read, "
    "so whether the default, hybrid, can run on it is unknown (griot's log says why). To check a question in hybrid "
    "once it reads, remove its case and add it again.")


def case_mode_for(query: str, mode: str | None) -> tuple[str, str | None]:
    """The mode a new case of `query` is made in, and a note to show or None.
    An explicit `mode` is kept as given (add_case refuses one that cannot
    run). None is the default, resolved the way a search's default is
    (common.search_mode_for): hybrid, which is what griot search, ask and
    griot_search give a reader, or vector where hybrid cannot run. A hybrid
    case on a collection without keyword vectors would only ever be skipped
    by the check, so there the case is made in vector, and the note says so.

    Two places differ from a search's default. With nothing indexed yet the
    case is hybrid, with no note: it is checked once something is indexed,
    and an index run makes every new collection with keyword vectors
    (common._collection_config), so hybrid is what a reader will get then.
    With a collection whose config cannot be read the case is vector and the
    note says why: a search falls back the same way without a word, but a
    case is written for good.

    Every writer of a new case resolves a missing mode here (add, suggest,
    griot_golden_set_add), so a case's mode does not depend on the command
    that made it. Cases already in the file are untouched: absent still
    reads as vector (case_mode)."""
    if mode is not None:
        return mode, None
    if not common.keyword_query_matches(query):
        # Hybrid refuses this query on every collection: vector, and there
        # is nothing anyone could build to change that.
        return "vector", None
    if not common.collection_exists(common.COLLECTION_NAME):
        return common.SEARCH_DEFAULT_MODE, None
    resolved, search_note = common.search_mode_for(query, None)
    if search_note:
        return resolved, VECTOR_FALLBACK_NOTE
    if resolved == "vector":
        # The collection exists, the query can be matched and there is no
        # note: what is left is a config search_mode_for could not read.
        return resolved, UNREADABLE_CONFIG_NOTE
    return resolved, None


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
        # Present means it was written on purpose: a null or a misspelling
        # is refused rather than read as "vector", which would run the case
        # in a mode nobody chose.
        if "mode" in case and (problem := _mode_problem(case["mode"])):
            raise ValueError(f"case {number}: {problem}")


def add_case(query: str, must_include: list[dict], limit: int = 5, mode: str = "vector") -> dict:
    """Data half of `golden-set add`: persists a case and returns it. The
    caller decides which search results are correct and passes the
    already-built must_include entries (see _must_include_entry() for how
    cmd_add() builds one from a raw search result) — this function does NOT
    call common.search() itself, so it works for cases that don't come from
    an interactive search session too. Raises ValueError (message with no
    "Error: " prefix — that's the CLI's job, see cmd_add()) on an empty
    query, an empty must_include, an entry that constrains nothing (see
    has_effective_constraint), a mode that is not a search mode, or a keyword
    or hybrid case whose query has no word keyword search can match; never
    touches the file in those cases.

    `mode` is the search the case is checked with (see case_mode()): the one
    its results came from. Written only when it is not "vector", so a vector
    case is stored exactly as every case was before modes existed."""
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

    problem = mode_refusal(query, mode)
    if problem:
        raise ValueError(problem)

    case = {"query": query, "limit": limit, "must_include": must_include}
    if mode != "vector":
        case["mode"] = mode
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
            content = index_code.read_source(indexable[name], root)
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

    added, refused, mode_notes = 0, 0, []
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
        # The mode every command gives a case made without one
        # (case_mode_for): the files come from the git log, not from a
        # search, so nothing ties this case to vector. Written through
        # add_case like every other new case, so each is saved as approved.
        mode, mode_note = case_mode_for(candidate["query"], None)
        try:
            add_case(candidate["query"], candidate["must_include"], limit=candidate["limit"], mode=mode)
        except json.JSONDecodeError as e:
            # The file every candidate is written to cannot be read: each
            # would fail the same way, so the run stops here instead of
            # asking about candidates that cannot be saved.
            print(f"Error: {common.GOLDEN_SET_PATH} is not valid JSON ({e}); fix or remove it, then run suggest "
                  f"again. {added} case(s) were added before this.", file=sys.stderr)
            return 1
        except ValueError as e:
            # A case add_case refuses: said with the reason, under the
            # candidate, and the run goes on (as review does), so one refused
            # candidate costs neither the cases already approved (each is
            # written when approved) nor the ones after it.
            print(f"  Not added: {e}")
            refused += 1
            continue
        added += 1
        if mode_note and mode_note not in mode_notes:
            mode_notes.append(mode_note)

    print(f"\n{added} case(s) added to {common.GOLDEN_SET_PATH}.")
    if refused:
        print(f"{refused} approved candidate(s) not added (the reason is above).")
    # Once for the run, not once per case: every case of it was resolved
    # against the same collection.
    for mode_note in mode_notes:
        print(mode_note)
    if note:
        print(note)
    return 0


def cmd_add(query: str, limit: int = 5, mode: str | None = None) -> int:
    from griot import ask  # lazy — same pattern as cli.py, avoids pulling in qdrant/fastembed before needed

    # None is the default: hybrid, or vector where hybrid cannot run
    # (case_mode_for). Resolved once, before the search, so the results shown
    # come from the mode the case is written in.
    mode, note = case_mode_for(query, mode)
    # The search the golden-set check will repeat for this case (in its
    # mode, shaped for a reader: see quality_check.run_golden_set), so the results a person
    # approves are the ones the case is later held to.
    from griot import quality_check  # lazy: quality_check imports this module
    try:
        results = common.search(query, limit=limit, mode=mode, diverse=quality_check.GOLDEN_SET_DIVERSE)
    except common.SearchFilterError as e:
        # A mode this collection cannot run yet, or a query it cannot match:
        # what to do instead, not a traceback.
        print(f"Error: {e}", file=sys.stderr)
        return 1
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
        add_case(query, must_include, limit=limit, mode=mode)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Case added to {common.GOLDEN_SET_PATH} ({mode} search).")
    if note:
        print(note)
    return 0


def cmd_list() -> int:
    cases = list_cases()
    if not cases:
        print(f"No cases in {common.GOLDEN_SET_PATH} (empty).")
        return 0
    for i, case in enumerate(cases, start=1):
        query = case.get("query", "")
        preview = query if len(query) <= 80 else query[:80] + "..."
        print(f"  [{i}] {preview} ({len(case.get('must_include', []))} must_include, "
              f"{common.printable(str(case_mode(case)))[:20]} search)")
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

# How far down the other ranking a hybrid search's first results must be for
# the two rankings to disagree (see _disagreement()). Ten is about what a
# reader takes in of a result list (an agent asks for 6 to 8): a result the
# other ranking placed within its first ten is one it still found relevant,
# and only past that is the other ranking's opinion really a different one.
# Measured on 2026-10-08 on a throwaway index of this repository's code
# (default profile, the 50 descriptive and identifier queries of
# scripts/search-mode-queries/griot.json, hybrid, as griot_search runs them at
# its default limit of 8): depth 5 offered 17 of the 50 searches, 10 offered
# 7, 20 offered 5. At 5 a third of all searches would be offered, most with
# each first still in the other ranking's top ten; 20 drops only two more,
# so 10 stays.
DISAGREE_DEPTH = 10

# How soon after a search the next one of the same session must come to be
# read as a rewording of it (see _reformulations()). Measured on 2026-10-09
# on constructed logs, no one's real use (scripts/measure-reformulation-
# window.py: 1,000 pairs each of rewordings by an agent or a person at a
# terminal, follow-ups on another facet of the same subject, and returns to
# the subject after other work, at the gaps assumed there). Of the
# rewordings, 60 s offered 74%, 120 s 88%, 300 s 98%, 600 s all; of the
# returns, none up to 120 s, 4% at 300 s, 18% at 600 s, 68% at 1800 s.
# Facets are offered at every window (the word rule cannot tell them apart).
# 300 s keeps nearly every rewording, a person reading a list included,
# before returns come in.
REFORMULATION_WINDOW_SECONDS = 300

# Words shorter than this are not taken as a sign two questions are about
# the same thing: "how", "is", "the", "of" are shared by unrelated questions
# in any language. Four keeps short subject words such as "lock" or "spend".
REFORMULATION_MIN_WORD = 4


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
    one says nothing about how well the best result matched. A hybrid
    search has its own signal instead, its two rankings disagreeing
    (_disagreement())."""
    import statistics

    scores: dict = {}
    for row in rows:
        if _mode(row) == "vector" and _is_number(row.get("top_score")):
            scores.setdefault(row.get("collection"), []).append(float(row["top_score"]))
    return {collection: statistics.quantiles(values, n=4, method="inclusive")[0]
            for collection, values in scores.items() if len(values) >= HARD_MIN_SEARCHES}


def _unlike_the_check(row: dict) -> list[str]:
    """How this logged search differs from the one that checks a case.

    quality_check.run_golden_set() checks every case in the case's own mode
    (the one recorded here: the mode that RAN), over every repository,
    searched as readers search (at most a few chunks per document, copies of
    the same text folded), never grouped by document. A search narrowed to
    some repositories or source types, or a grouped one (where the limit
    counts documents, not chunks), asserts what THAT search returned, so it
    can fail on every check without retrieval getting any worse: a
    permanently red case in the very ruler the golden set is. So can a mode
    the check does not know. Empty when the check would repeat this
    search."""
    unlike = []
    if _mode(row) not in common.SEARCH_MODES:
        unlike.append(f"a {common.printable(str(_mode(row)))[:20]} search")
    narrowed = [f"{name} {', '.join(map(str, row[name]))}" for name in ("repos", "source_types")
                if isinstance(row.get(name), list) and row[name]]
    if narrowed:
        unlike.append("narrowed to " + "; ".join(common.shown(n) for n in narrowed))
    if row.get("group_by_document"):
        unlike.append("grouped by document")
    return unlike


def _is_rank(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _sources(row: dict) -> list[str]:
    return [s for s in row.get("sources") or [] if isinstance(s, str)]


def _ranks(row: dict) -> list[dict] | None:
    """A hybrid search's rank of each result in the vector and the keyword
    ranking (common.logged_ranks()), in result order, or None: a row logged
    before ranks were, a search of another mode, or ranks that cannot be
    read as they were written (one per result, each a whole number from 1,
    or null). Only whole rows: a rank list half read would place results
    wrongly."""
    ranks = row.get("ranks")
    if not isinstance(ranks, list) or len(ranks) != len(_sources(row)):
        return None
    for entry in ranks:
        if not isinstance(entry, dict) or "vector" not in entry or "keyword" not in entry:
            return None
        if any(value is not None and not _is_rank(value) for value in (entry["vector"], entry["keyword"])):
            return None
    return ranks


def _disagreement(row: dict) -> tuple[int, int] | None:
    """Where, among the results of this hybrid search, the first result of
    the vector ranking and the first of the keyword ranking are (1-based),
    when the two rankings disagree; None when they do not.

    They disagree when each ranking's first result is one the other did not
    place in its first DISAGREE_DEPTH: two different results, each the best
    by one way of ranking and not even near the top by the other. That is
    what makes such a question a case worth having: the fused list puts
    them side by side and cannot say which ranking was right, and the
    person picking the right result can; the case then holds a result that
    one ranking alone would lose, so a change to either ranking, or to how
    they are fused, shows in the golden set. Each first must be among the
    results logged (what the reader got). A keyword ranking that matched
    nothing has no first, and no opinion to disagree with: not a
    disagreement.

    A null rank says only "not among the first `rank_window` points that
    ranking was asked for". With a window that reaches the depth, that is
    past the depth. With a narrower window w it is only past w, and the
    result could have been anywhere from w+1 to the depth in the other
    ranking, which is near by the depth's own argument: comparing against
    min(depth, w) instead would offer rankings that agree. So such a null
    is never counted as far. That costs no disagreement in practice: every
    search that logs ranks (`griot ask`, griot_search) is made for a reader
    (diverse), whose two rankings are each fetched several times wider than
    the list returned (common._GROUPING_OVERFETCH), so already at limit 2,
    the smallest limit with two results to disagree about, the window
    reaches the depth (tests/test_hybrid_rankings_logged.py holds every
    such surface to that). Only a limit-1 search logs a narrower window,
    and one result cannot disagree with itself."""
    if _mode(row) != "hybrid":
        return None
    ranks = _ranks(row)
    if ranks is None:
        return None
    window = row.get("rank_window") if _is_rank(row.get("rank_window")) else None

    def far(rank) -> bool:
        return rank > DISAGREE_DEPTH if rank is not None else window is not None and window >= DISAGREE_DEPTH

    firsts = []
    for name in ("vector", "keyword"):
        at = next((j for j, entry in enumerate(ranks, start=1) if entry[name] == 1), None)
        if at is None:
            return None
        firsts.append(at)
    by_meaning, by_words = firsts
    # Two different results by construction: a result first in both has a
    # rank of 1 in the other ranking, which is not far.
    if not (far(ranks[by_meaning - 1]["keyword"]) and far(ranks[by_words - 1]["vector"])):
        return None
    return by_meaning, by_words


def _logged_at(row: dict):
    """When a row was logged, as an aware datetime, or None: a time that
    cannot be read, or one without a zone (log_query() always writes UTC
    with its offset; a naive one cannot be compared with it)."""
    from datetime import datetime

    try:
        at = datetime.fromisoformat(row.get("timestamp"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo is not None else None


def _subject_words(question: str) -> set[str]:
    return {w for w in query_key(question).split() if len(w) >= REFORMULATION_MIN_WORD}


def _rewords(first: dict, then: dict) -> bool:
    """Whether `then`, the next search of the same session, looks like the
    person rewording `first` because its list did not serve. All of:

    - the same collection: the same index answered both;
    - within REFORMULATION_WINDOW_SECONDS after it: a rewording comes once
      the list is read; much later it is a return to the subject;
    - not the same question (query_key()): that is "repeated", and
      review_candidates() offers it as such first, since both askings
      fall in one group;
    - about the same thing: the subject words (REFORMULATION_MIN_WORD
      characters or more, normalised as query_key() does) they share are at
      least half of the shorter question's; one word in common between two
      long questions is a different question on a nearby subject;
    - a different list came back (as a set of results): a rewording that
      returned what the first did changed nothing the person saw, so it does
      not single out the first list.

    It cannot tell a rewording from a question about another facet of the
    same thing ("how is the lock acquired" after "how is the lock
    released"); the person reviewing can, which is why the follow-up is
    shown beside the question.

    Nor can it tell two conversations apart that log one session: subagents
    reaching griot through their parent's `griot mcp` process share the
    parent's session, so a parallel subagent's search on a nearby subject
    can be paired as "the next search" of another's. Accepted as a limit:
    the shared-words rule above keeps out searches on other subjects, two
    searches at the same instant are never paired, and the person sees the
    follow-up and its results and can answer `n` or `s`."""
    if not all(isinstance(r.get("question"), str) and r["question"] != common.OMITTED_QUESTION
               for r in (first, then)):
        return False
    if first.get("collection") != then.get("collection"):
        return False
    start, end = _logged_at(first), _logged_at(then)
    if start is None or end is None or not 0 < (end - start).total_seconds() <= REFORMULATION_WINDOW_SECONDS:
        return False
    words, other = _subject_words(first["question"]), _subject_words(then["question"])
    if not words or not other or 2 * len(words & other) < min(len(words), len(other)):
        return False
    before, after = first.get("sources"), then.get("sources")
    if not isinstance(before, list) or not isinstance(after, list):
        return False
    return set(_sources(first)) != set(_sources(then))


def _reformulations(rows: list[dict]) -> dict[int, dict]:
    """For each search reworded by the next search of its session (see
    _rewords()), that follow-up, by the search's id(). Only rows that say
    their session (common.log_session()) and a readable time are placed in
    one: a session is never guessed from timestamps. A search logged
    without its question still takes its place in the session (the person
    did search in between), it is only never paired."""
    sessions: dict[str, list[tuple]] = {}
    for row in rows:
        session, at = row.get("session"), _logged_at(row)
        if isinstance(session, str) and session and at is not None:
            sessions.setdefault(session, []).append((at, row))
    found = {}
    for searches in sessions.values():
        searches.sort(key=lambda pair: pair[0])
        for (_, first), (_, then) in zip(searches, searches[1:]):
            if _rewords(first, then):
                found[id(first)] = then
    return found


def _has_pickable_results(row: dict) -> bool:
    return any(isinstance(e, dict) for e in row.get("results") or [])


def _candidate(kind: str, key: str, group: list[dict], thresholds: dict, followup: dict | None = None) -> dict:
    # The newest asking a case can be made from (results recorded, and a
    # search the check repeats); failing that, the newest with results;
    # failing that, the newest.
    row = (next((r for r in reversed(group) if _has_pickable_results(r) and not _unlike_the_check(r)), None)
           or next((r for r in reversed(group) if _has_pickable_results(r)), None)
           or group[-1])
    sources, results = _sources(row), _results(row)
    limit = row.get("limit")
    return {
        "kind": kind, "key": key, "query": row["question"], "times": len(group),
        "projects": len({r.get("project") for r in group if r.get("project")}),
        "mode": _mode(row), "collection": row.get("collection"),
        "top_score": row.get("top_score") if _is_number(row.get("top_score")) else None,
        "threshold": thresholds.get(row.get("collection")),
        "limit": limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else CASE_LIMIT,
        "timestamp": row.get("timestamp"), "unlike": _unlike_the_check(row),
        "sources": sources, "results": results,
        # Shown beside each result when the search logged them; for a
        # "disagree" candidate, which results were each ranking's first.
        "ranks": _ranks(row), "firsts": _disagreement(row) if kind == "disagree" else None,
        # For a "reformulated" candidate, the search that reworded it and how
        # many seconds later: what tells the person why it is offered.
        "followup": followup["question"] if followup else None,
        "followup_after": round((_logged_at(followup) - _logged_at(row)).total_seconds()) if followup else None,
        # And what the follow-up returned: it is the search that served, so
        # the right document may be only in its list (see _choices()).
        "followup_mode": _mode(followup) if followup else None,
        "followup_sources": _sources(followup) if followup else None,
        "followup_results": _results(followup) if followup else None,
        "followup_unlike": _unlike_the_check(followup) if followup else None,
    }


def _results(row: dict) -> list | None:
    """A row's case entries, one per source, or None when they cannot be
    read as written: logged before results were recorded, shown and not
    made a case."""
    results = row.get("results")
    return results if isinstance(results, list) and len(results) == len(_sources(row)) else None


def _choices(candidate: dict) -> list[dict]:
    """What the person can pick from, in the order shown: {label, entry,
    origin, blocked, rank}. `entry` is the must_include entry a pick adds;
    `blocked` is None when it can be picked, "cannot" when the log holds no
    entry for it, or the note saying why its search cannot make a case;
    `rank` is its place in the first search's list (None when only the
    follow-up returned it).

    For most candidates, the results of the one search offered (origin
    None). For a "reformulated" one, the first search's results, then the
    follow-up's that the first did not return, each marked with the search
    that returned it ("first", "follow-up", or "both", shown once). A pick
    from either list makes a case of the FIRST question in the first
    search's mode: its list is the one that did not serve, and the person
    is saying which document answers it.

    Which results can be picked follows from the case that would be made,
    checked in the first search's mode over every repository and not
    grouped (see _unlike_the_check()): a result can be picked when the
    search that returned it drew from that same pool, neither narrowed nor
    grouped, and the first search's mode is one the check knows. The
    follow-up's own mode does not matter, since the case does not repeat
    it. A narrowed first search does not block the follow-up's results: the
    case asserts nothing about what the first search returned, only which
    document answers its question, and that judgement was made among
    results from the check's pool. A document in both lists can be picked
    when either search allows it."""
    first_results = candidate["results"]
    reformulated = candidate["followup_sources"] is not None
    first_note = (_unlike_note(candidate["unlike"], "The first search's" if reformulated else "These")
                  if candidate["unlike"] else None)

    def entry_of(results, j, note):
        if note:
            return None, note
        if results is None or not isinstance(results[j], dict):
            return None, "cannot"
        return results[j], None

    if not reformulated:
        return [{"label": label, "entry": entry, "origin": None, "blocked": blocked, "rank": j}
                for j, label in enumerate(candidate["sources"])
                for entry, blocked in [entry_of(first_results, j, first_note)]]

    follow_sources, follow_results = candidate["followup_sources"], candidate["followup_results"]
    if candidate["mode"] not in common.SEARCH_MODES:
        follow_note = first_note  # no case can be checked in the first search's mode at all
    elif candidate["followup_unlike"]:
        follow_note = _unlike_note(candidate["followup_unlike"], "The follow-up's")
    else:
        follow_note = None
    in_follow: dict[str, int] = {}
    for j, label in enumerate(follow_sources):
        in_follow.setdefault(label, j)

    choices = []
    for j, label in enumerate(candidate["sources"]):
        entry, blocked = entry_of(first_results, j, first_note)
        if label in in_follow and blocked:
            other, other_blocked = entry_of(follow_results, in_follow[label], follow_note)
            if not other_blocked:
                entry, blocked = other, None
        choices.append({"label": label, "entry": entry, "origin": "both" if label in in_follow else "first",
                        "blocked": blocked, "rank": j})
    shown = set(candidate["sources"])
    for j, label in enumerate(follow_sources):
        if label not in shown:
            shown.add(label)
            entry, blocked = entry_of(follow_results, j, follow_note)
            choices.append({"label": label, "entry": entry, "origin": "follow-up", "blocked": blocked,
                            "rank": None})
    return choices


def review_candidates(rows: list[dict] | None = None, *, limit: int = 10) -> dict:
    """Data half of `golden-set review`: questions from the query log worth
    making into cases, none written.

    Four kinds, taking turns one candidate each in this order until
    `limit` (see _take_turns()), each kind in its own order: a question
    asked more than once (by query_key(), across sessions and projects),
    most asked first; a
    vector search whose best result scored in the bottom quarter of its
    collection's (see _hard_thresholds()), lowest first; a hybrid search
    whose vector and keyword rankings disagreed about what comes first
    (see _disagreement()), newest first; a search the next search of its
    session reworded soon after (see _rewords()), newest first: the first
    of the two, whose list apparently did not serve. A question already
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
    reworded = _reformulations(rows)
    repeated, hard, disagree, reformulated = [], [], [], []
    for key, group in groups.items():
        if len(group) > 1:
            repeated.append(_candidate("repeated", key, group, thresholds))
            continue
        row = group[0]
        threshold = thresholds.get(row.get("collection"))
        if threshold is not None and _mode(row) == "vector" and _is_number(row.get("top_score")) \
                and row["top_score"] <= threshold:
            hard.append(_candidate("hard", key, group, thresholds))
        elif _disagreement(row):
            disagree.append(_candidate("disagree", key, group, thresholds))
        elif id(row) in reworded:
            reformulated.append(_candidate("reformulated", key, group, thresholds, followup=reworded[id(row)]))
    repeated.sort(key=lambda c: (c["times"], c["timestamp"] or ""), reverse=True)
    hard.sort(key=lambda c: c["top_score"])
    # Newest first: how far apart two rankings are has no scale to sort on
    # that means more than "past the depth".
    disagree.sort(key=lambda c: str(c["timestamp"] or ""), reverse=True)
    reformulated.sort(key=lambda c: str(c["timestamp"] or ""), reverse=True)
    return {"candidates": _take_turns([repeated, hard, disagree, reformulated], limit), "searches": len(rows),
            "omitted": omitted}


def _take_turns(kinds: list[list[dict]], limit: int) -> list[dict]:
    """One candidate of each kind in turn, each kind in its own order, until
    `limit`. Cutting the kinds one after another at `limit` let a kind with
    many candidates (a log full of repeated questions) crowd out every kind
    after it, so a reworded search was never shown at the default limit;
    taking turns shows each kind while it has candidates left."""
    taken: list[dict] = []
    for turn in range(max(map(len, kinds), default=0)):
        taken.extend(kind[turn] for kind in kinds if turn < len(kind))
    return taken[:limit]


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


def _unlike_note(unlike: list[str], whose: str = "These") -> str:
    it = "this" if whose == "These" else "it"
    return (f"{whose} results cannot become a case: {it} was {', '.join(unlike)}, and a case is checked in its own "
            f"mode (vector, keyword or hybrid) over every repository, searched as readers search and not grouped "
            f"by document, which may never return them. "
            f"Asked again that way, the question can be.")


# Which kind each candidate is, at the head of it: the kinds take turns in
# one list (see _take_turns()), so the order alone no longer says.
_KIND_LABELS = {"repeated": "asked again", "hard": "low score", "disagree": "rankings disagree",
                "reformulated": "reworded"}

_ORIGIN_MARKS = {None: "", "first": "  (first search only)", "both": "  (both searches)",
                 "follow-up": "  (follow-up only)"}


def _show(candidate: dict, number: int, total: int) -> None:
    print(f"\n[{number}/{total}] ({_KIND_LABELS[candidate['kind']]}) "
          f"{common.shown(candidate['query'].splitlines()[0] if candidate['query'] else '')}")
    if candidate["kind"] == "repeated":
        where = f", from {candidate['projects']} projects" if candidate["projects"] > 1 else ""
        print(f"  Why: asked {candidate['times']} times{where}.")
    elif candidate["kind"] == "disagree":
        by_meaning, by_words = candidate["firsts"]
        print(f"  Why: its two rankings disagree. The vector ranking's first result ([{by_meaning}]) is not in "
              f"the keyword ranking's first {DISAGREE_DEPTH}, and the keyword ranking's first ([{by_words}]) is "
              f"not in the vector ranking's first {DISAGREE_DEPTH}: which one was right is what a case from it "
              f"keeps.")
    elif candidate["kind"] == "reformulated":
        print(f"  Why: reworded {candidate['followup_after']} s later in the same session, as "
              f"\"{common.shown(candidate['followup'].splitlines()[0])}\", which brought back other results: "
              f"this list apparently did not serve.")
    else:
        print(f"  Why: low score. Its best result scored {candidate['top_score']:.2f}, at or under "
              f"{candidate['threshold']:.2f}: the bottom quarter of the vector searches in this collection.")
    unlike = candidate["unlike"]
    reformulated = candidate["followup_sources"] is not None
    also = f" and for its follow-up ({candidate['followup_mode']} search)" if reformulated else ""
    print(f"  Results logged for it ({candidate['mode']} search, {str(candidate['timestamp'] or '?')[:10]}){also}:")
    ranks = candidate["ranks"]
    for j, choice in enumerate(_choices(candidate), start=1):
        flag = "  (cannot become a case)" if choice["blocked"] else ""
        # Where it stood in each ranking of the first search; "-" for not
        # among the points that ranking was asked for.
        where = (f"  (vector {ranks[choice['rank']]['vector'] or '-'}, "
                 f"keyword {ranks[choice['rank']]['keyword'] or '-'})"
                 if ranks is not None and choice["rank"] is not None else "")
        print(f"    [{j}] {common.shown(choice['label'])}{where}{_ORIGIN_MARKS[choice['origin']]}{flag}")
    whose = "The first search's" if reformulated else "These"
    if unlike:
        print(f"  {_unlike_note(unlike, whose)}")
    elif candidate["results"] is None:
        print(f"  {whose} results {_CANNOT}. Asked again, the question can be.")
    if reformulated and candidate["followup_unlike"]:
        print("  " + _unlike_note(candidate["followup_unlike"], "The follow-up's"))
    elif reformulated and candidate["followup_results"] is None:
        print(f"  The follow-up's results {_CANNOT}.")


def _ask(candidate: dict) -> str | list[dict]:
    """The person's answer for one candidate: "skip", "none", "reject",
    "quit", or the must_include entries of the results they picked."""
    choices = _choices(candidate)
    pickable = any(not choice["blocked"] for choice in choices)
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
        wrong = [j for j in picked if not 1 <= j <= len(choices)]
        if not picked or wrong:
            print(f"  {', '.join(map(str, wrong)) or 'Nothing'} out of range (1-{len(choices)}).")
            continue
        blocked = [choices[j - 1]["blocked"] for j in picked if choices[j - 1]["blocked"]]
        if blocked:
            for note in dict.fromkeys(b for b in blocked if b != "cannot"):
                print(f"  {note}")
            unusable = [j for j in picked if choices[j - 1]["blocked"] == "cannot"]
            if unusable:
                print(f"  Result {', '.join(map(str, unusable))} {_CANNOT}.")
            continue
        return [choices[j - 1]["entry"] for j in picked]


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
        print(f"No candidates among {found['searches']} logged search(es): none asked more than once, scoring "
              f"in the bottom quarter of its collection's vector searches (of at least {HARD_MIN_SEARCHES}), "
              f"or a hybrid search whose vector and keyword rankings each put first a result the other did not "
              f"place in its first {DISAGREE_DEPTH}, or reworded by the next search of its session within "
              f"{REFORMULATION_WINDOW_SECONDS} s, that is not already a case or rejected.")
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
                add_case(candidate["query"], answer, limit=candidate["limit"], mode=candidate["mode"])
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
    show_flags_read_by_griot(parser, PROFILE_FLAG)
    # On each action's help too, but only where the profile changes what the
    # action does: suggest and add resolve a new case's mode against the
    # active collection (case_mode_for), and add searches it. list, remove
    # and review never open it: the golden set is one file for every
    # profile, and review reads the query log of every collection.
    sub = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    p_suggest = sub.add_parser(
        "suggest", help="Suggests candidates for free from the git log, with case-by-case human approval",
        description="Suggests candidates for free from the git log, with case-by-case human approval. An approved "
                    "case is checked in the same mode `golden-set add` gives one by default: hybrid, as griot "
                    "search (also with nothing indexed yet: an index run builds keyword vectors); vector on a "
                    "collection without keyword vectors, which `griot index keywords` builds, or whose config "
                    "cannot be read.")
    show_flags_read_by_griot(p_suggest, PROFILE_FLAG)
    p_suggest.add_argument("repo_path", help="Local git repository directory")
    p_suggest.add_argument("--max-commits", type=int, default=None, help="Limit of commits to consider (default: all)")
    p_suggest.add_argument("--limit", type=int, default=10, help="How many candidates to offer for approval (default: %(default)s)")

    p_add = sub.add_parser("add", help="Runs a real search and lets you approve which results are the must_include")
    show_flags_read_by_griot(p_add, PROFILE_FLAG)
    p_add.add_argument("query", help="Natural language question/term")
    p_add.add_argument("--limit", type=int, default=5, help="How many results to show to choose from (default: %(default)s)")
    p_add.add_argument("--mode", choices=common.SEARCH_MODES, default=None,
                       help="How to search, now and every time the case is checked: vector (by meaning), keyword "
                            "(the exact words) or hybrid (both). Keyword and hybrid need `griot index keywords` on "
                            "an older collection (default: hybrid, as griot search; vector on a collection without "
                            "keyword vectors, and the command says so)")

    p_review = sub.add_parser("review", help="Offers questions from the query log (asked more than once, scoring "
                                             "low, ranked very differently by meaning and by the words, or "
                                             "reworded soon after) as cases; at a terminal, you pick the right "
                                             "result")
    p_review.add_argument("--limit", type=int, default=10, help="How many candidates to offer (default: %(default)s)")

    sub.add_parser("list", help="Lists the already-curated cases")

    p_remove = sub.add_parser("remove", help="Removes a case by number (see `list`)")
    p_remove.add_argument("index", type=int)
    p_remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")

    args = parser.parse_args(argv)
    if args.action == "suggest":
        return cmd_suggest(args.repo_path, max_commits=args.max_commits, limit=args.limit)
    if args.action == "add":
        return cmd_add(args.query, limit=args.limit, mode=args.mode)
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
