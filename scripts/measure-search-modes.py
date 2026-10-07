#!/usr/bin/env python3
"""Measures griot's three search modes (vector, keyword, hybrid) on a
throwaway index of the repositories given, per kind of query, and prints
MRR@10 and recall@10 for each.

This is the measurement that made hybrid the default search mode
(docs/indexing-model.md quotes its numbers). Run it again when the default
embedding profile or the fusion changes, before keeping the default:

    .venv/bin/python scripts/measure-search-modes.py \\
        --repo . --queries scripts/search-mode-queries/griot.json

Each query file names one repository (by its directory name, the `repo` of
every point griot writes) and holds three kinds of query:

    {"repo": "griot",
     "descriptive": [{"query": "a question in words", "relevant": ["path/in/repo.py", ...]}, ...],
     "identifier": ["function_or_class_name", ...],
     "commit_hash": {"count": 15, "prefix_lengths": [7, 8, 9, 10, 12, 40]}}

A descriptive query is judged against the files written next to it. An
identifier is relevant where it is DEFINED, not where it is used, in any
file griot indexes (definition_pattern says what counts: a def or class, a
TS/JS function, class or const, a shell function, a Terraform block label,
and so on). Commit-hash queries
are drawn from the repository's own history, evenly spaced, each cut to the
next prefix length, so the same history gives the same queries.

A result is relevant when its DOCUMENT is: a file whose repository and path
are in the query's relevant set, or the commit whose hash the query is a
prefix of. Ranks count documents, not chunks, and the ranking is the one a
reader gets (diverse=True, as the CLI and the MCP tool call it).

Everything is built in a fresh temporary directory, with GRIOT_CONFIG_DIR
and GRIOT_DATA_DIR pointing inside it, and deleted at the end, model
download included (its Hugging Face cache and logs are pointed there too):
the script never reads or writes the real griot directories, and refuses to
run if it cannot be sure of that. An indexing step whose process fails ends
the run, unless the process had recorded a complete run first (a crash at
exit), which the script says and goes on. On a paid
profile (--profile), indexing the repositories costs what indexing them
costs."""

import argparse
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# The order the table prints, and the JSON key each kind is read from.
KINDS = {"descriptive": "descriptive", "identifier": "identifier", "commit_hash": "commit hash"}
SOURCES_INDEXED = ("code", "commits")


# --- the metrics -------------------------------------------------------------

def doc_of(payload: dict) -> tuple:
    """The document a point belongs to: a commit by its hash, anything else
    by its repository and path. A point written before source_type existed
    is code."""
    source_type = payload.get("source_type", "code")
    if source_type == "commit":
        return ("commit", payload.get("repo"), payload.get("commit_hash"))
    return (source_type, payload.get("repo"), payload.get("file_path"))


def ranked_documents(payloads) -> list:
    """The documents of a result list in the order they first appear: the
    second chunk of a file is the same document and takes no rank."""
    docs = []
    for payload in payloads:
        doc = doc_of(payload or {})
        if doc not in docs:
            docs.append(doc)
    return docs


def reciprocal_rank(docs: list, relevant: set) -> float:
    for rank, doc in enumerate(docs, start=1):
        if doc in relevant:
            return 1 / rank
    return 0.0


def recall(docs: list, relevant: set) -> float:
    return len(relevant & set(docs)) / len(relevant)


def format_table(totals: dict, modes) -> str:
    """totals maps (kind, mode) to [sum of reciprocal ranks, sum of recalls,
    number of queries]; each cell is the mean."""
    header = f"{'kind':<14}{'n':>4}  " + "  ".join(f"{m + ' MRR':>14}{m + ' R@10':>14}" for m in modes)
    lines = [header]
    for kind in KINDS.values():
        if (kind, modes[0]) not in totals:
            continue
        cells = []
        for mode in modes:
            sum_rr, sum_recall, n = totals[(kind, mode)]
            cells.append(f"{sum_rr / n:>14.3f}{sum_recall / n:>14.3f}")
        lines.append(f"{kind:<14}{totals[(kind, modes[0])][2]:>4}  " + "  ".join(cells))
    return "\n".join(lines)


# --- the arguments -----------------------------------------------------------

def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measures MRR and recall of griot's search modes on a throwaway index of the repositories "
                    "given, per kind of query. Never touches the real griot directories.")
    parser.add_argument("--repo", dest="repos", action="append", type=Path, required=True,
                        help="A repository to index (repeat for several). Results from every repository compete.")
    parser.add_argument("--queries", action="append", type=Path, required=True,
                        help="A query file (JSON; see this script's docstring). Repeat for several.")
    parser.add_argument("--limit", type=_positive, default=10, help="Results per search (the @k); default 10.")
    parser.add_argument("--profile", help="Embedding profile to index and search with; default griot's own.")
    parser.add_argument("--per-query", type=Path,
                        help="Also write each query's rank per mode to this JSON file.")
    return parser.parse_args(argv)


def repository_roots(paths) -> dict:
    """Directory name -> root. Points name their repository by its directory
    name, so two roots with the same name would be one repository."""
    roots = {}
    for path in paths:
        root = Path(path).resolve()
        if not (root / ".git").exists():
            raise SystemExit(f"{root} is not the root of a git repository.")
        if root.name in roots:
            raise SystemExit(f"Two repositories are named '{root.name}' ({roots[root.name]} and {root}): "
                             f"griot tells repositories apart by directory name.")
        roots[root.name] = root
    return roots


# --- never the real directories ----------------------------------------------

def require_throwaway_dirs(environ) -> tuple:
    """The griot directories this run writes to, refused unless both are set
    and both lie inside the system's temporary directory. griot.common reads
    them once, when it is imported, so a griot already imported from
    elsewhere would write wherever IT points: refused too."""
    temp = Path(tempfile.gettempdir()).resolve()
    dirs = []
    for name in ("GRIOT_CONFIG_DIR", "GRIOT_DATA_DIR"):
        value = environ.get(name)
        if not value:
            raise SystemExit(f"Refusing to run: {name} is not set, so griot would use the real directories.")
        path = Path(value).resolve()
        if path == temp or not path.is_relative_to(temp):
            raise SystemExit(f"Refusing to run: {name} ({path}) is not inside the temporary directory ({temp}).")
        dirs.append(path)
    config, data = dirs
    common = sys.modules.get("griot.common")
    if common is not None and (Path(common.CONFIG_DIR).resolve() != config / "griot"
                               or Path(common.DATA_DIR).resolve() != data / "griot"):
        raise SystemExit(f"Refusing to run: griot was already loaded with other directories "
                         f"({common.CONFIG_DIR}, {common.DATA_DIR}).")
    return config, data


# Where the model download keeps its cache and writes its logs, relative to the
# throwaway directory. griot gives the embedding library its own cache_dir,
# but huggingface_hub and hf_xet still write beside it: hf_xet put log files
# in ~/.cache/huggingface/xet/logs during a measurement. hf_xet takes its
# directory from HF_XET_CACHE, else HF_HOME/xet, else
# XDG_CACHE_HOME/huggingface/xet; huggingface_hub keeps the rest under
# HF_HOME. All three are set, so none of them falls back to the home directory.
_CACHE_VARIABLES = {"HF_HOME": "huggingface", "HF_XET_CACHE": "huggingface/xet", "XDG_CACHE_HOME": "cache"}


@contextlib.contextmanager
def throwaway_dirs():
    """A fresh temporary directory holding the griot config and data
    directories and the model download's caches and logs, set in the
    environment while the block runs (indexing children inherit it);
    afterwards the variables are what they were and the directory, index and
    downloaded model included, is gone, whether the run ended well or not."""
    base = Path(tempfile.mkdtemp(prefix="griot-measure-")).resolve()
    saved = {name: os.environ.get(name) for name in ("GRIOT_CONFIG_DIR", "GRIOT_DATA_DIR", *_CACHE_VARIABLES)}
    try:
        os.environ["GRIOT_CONFIG_DIR"] = str(base / "config")
        os.environ["GRIOT_DATA_DIR"] = str(base / "data")
        for name, sub in _CACHE_VARIABLES.items():
            os.environ[name] = str(base / sub)
        yield base
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(base, ignore_errors=True)


# --- the queries -------------------------------------------------------------

def _git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def indexed_file_rules() -> tuple:
    """(extensions, ignored directories, ignored file names, size limit) of
    the files griot indexes, read from griot's own indexer so that the two
    cannot drift apart. Read from its source, not imported: importing any
    module that loads griot.common fixes griot's directories, and this runs
    before the throwaway ones are set. Finding the module imports only the
    griot package, which loads nothing."""
    import ast
    import importlib.util

    source = Path(importlib.util.find_spec("griot.index_code").origin).read_text()
    wanted = ("SUPPORTED_EXTENSIONS", "IGNORE_DIRS", "IGNORE_FILES", "MAX_FILE_BYTES")
    values = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and getattr(node.targets[0], "id", None) in wanted:
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return tuple(values[name] for name in wanted)


def indexed_files(root: Path) -> list:
    """The files of `root` that `griot index code` reads, by the indexer's
    own rules (index_code.discover_files): what git does not ignore, with an
    extension griot indexes, outside the directories and files it skips, a
    regular file within the size limit. A definition anywhere else can never
    be found, so it cannot count as relevant."""
    extensions, ignore_dirs, ignore_files, max_bytes = indexed_file_rules()
    found = set()
    for rel in _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
        if not rel:
            continue
        path = Path(rel)
        if (path.suffix not in extensions or path.name in ignore_files or ".min." in path.name.lower()
                or any(part in ignore_dirs for part in path.parts[:-1])):
            continue
        try:
            info = os.lstat(root / rel)
        except OSError:
            continue  # tracked but deleted from disk
        if stat.S_ISREG(info.st_mode) and info.st_size <= max_bytes:
            found.add(rel)
    return sorted(found)


# Words that may stand before a declaration without changing what it declares
# (export default async function f, pub(crate) fn f, private static f() {}).
_MODIFIERS = (r"(?:(?:export|default|declare|abstract|async|public|private|protected|internal|static|final"
              r"|sealed|override|readonly|unsafe|extern|pub(?:\([^)\n]*\))?)[ \t]+)*")
# Keywords whose next word is the name they declare, across the languages
# griot indexes (Python, TS/JS, Go, Rust, Ruby, Scala, PHP, C#, shell).
_DECLARATION_KEYWORDS = (r"(?:def|class|function|const|let|var|interface|type|enum|func|fn|struct|trait"
                         r"|module|namespace|object|record|mod)")
# Words that begin a statement, not a declaration: a call after one of them
# (return f(x), if f(x) {, match f(x) {, new f() {) is a use, so none of them
# may be read as the return type of a `<type> name(` declaration.
_STATEMENT_KEYWORDS = (r"(?:return|if|else|elif|unless|until|while|for|foreach|do|switch|match|case|select|loop"
                       r"|catch|try|finally|new|delete|throw|throws|await|yield|go|defer|goto|not|and|or|in|is"
                       r"|typeof|sizeof|echo|print|puts)")
# One word of a return type: Map<String, List<T>>, String[], string?, std::string.
# A generic method's type parameters (<T>) stand alone, before its type.
_TYPE_WORD = (rf"(?:(?!{_STATEMENT_KEYWORDS}(?![\w$]))[\w$.:]+(?:<[^;{{}}()=\n]*>)?(?:\[\])*\??"
              rf"|<[^;{{}}()=\n]*>)")


def definition_pattern(name: str) -> re.Pattern:
    """Where `name` is DEFINED rather than used. The rule: a name is defined
    where a line binds it, and code binds a name in one of a few shapes
    whatever the language, none of which a use takes:

    - after a declaration keyword and its modifiers: def f, export const f,
      function* f, func (r *T) f, def self.f, type f, pub fn f;
    - at column 0, followed by an assignment or an annotation: a module-level
      f = ..., f: int, a shell f=..., export f=..., a top-level YAML key;
    - as a signature followed by its body: a shell f() {, a JS/TS method
      f(a): T {, the brace on the same line or the next;
    - after a return type, as a signature followed by its body: a Java or C#
      method public void f(int a) {, a C++ function static int f(void) or
      void Server::f() const, the brace on the same line or the next (no
      statement keyword reads as the type: return f(x), if f(x) {, new f() {);
    - bound to an arrow function, at any depth: f = () =>, a class field;
    - as the last label of a Terraform/HCL block: resource "type" "f" {,
      variable "f" {;
    - as what an SQL CREATE creates: CREATE TABLE IF NOT EXISTS s.f."""
    n = re.escape(name) + r"(?![\w$])"
    forms = [
        rf"^[ \t]*{_MODIFIERS}{_DECLARATION_KEYWORDS}(?:[ \t]*\*[ \t]*|[ \t]+)(?:\([^)\n]*\)[ \t]*|\w+\.)?{n}",
        rf"^(?:(?:export|readonly|declare)[ \t]+)?{n}[ \t]*(?::|=(?!=))",
        rf"^[ \t]*{_MODIFIERS}{n}[ \t]*\([^)\n]*\)[ \t]*(?::[^{{;\n]*)?(?:\{{|\n[ \t]*\{{)",
        rf"^[ \t]*(?:@[\w.]+(?:\([^)\n]*\))?[ \t]+)*(?:{_TYPE_WORD}(?:[ \t]+|[ \t]*[*&]+[ \t]*))+"
        rf"(?:\w+::)*{n}[ \t]*\([^();{{}}]*\)[^;{{}}()=\n]*(?:\{{|\n[ \t]*\{{)",
        rf"^[ \t]*{_MODIFIERS}{n}[ \t]*(?::[^=\n]*)?=[ \t]*(?:async[ \t]*)?(?:\([^)\n]*\)|[\w$]+)[ \t]*"
        rf"(?::[^=\n]*)?=>",
        rf'^[ \t]*(?:resource|data|module|variable|output|provider)(?:[ \t]+"[^"\n]*")*[ \t]+"{n}"[ \t]*\{{',
        rf"^[ \t]*(?i:create)(?:[ \t]+(?i:or[ \t]+replace|temp|temporary|unique|materialized))*[ \t]+"
        rf"(?i:table|view|function|procedure|index|type|trigger|schema|sequence)"
        rf'(?:[ \t]+(?i:if[ \t]+not[ \t]+exists))?[ \t]+(?:"?[\w$]+"?\.)?"?{n}',
    ]
    return re.compile("|".join(f"(?:{form})" for form in forms), re.MULTILINE)


def defining_files(root: Path, name: str) -> set:
    """The files griot indexes (indexed_files) that define `name`
    (definition_pattern), in whatever language they are written."""
    pattern = definition_pattern(name)
    return {path for path in indexed_files(root) if pattern.search((root / path).read_text(errors="replace"))}


def commit_hash_queries(root: Path, count: int, prefix_lengths: list) -> list:
    """`count` commits spread evenly over the history (merges left out: their
    message says only what was merged), each cut to the next prefix length
    in turn. Deterministic for one history."""
    history = _git(root, "log", "--no-merges", "--format=%H", "HEAD").split()
    if count > len(history):
        raise SystemExit(f"{root.name}: {count} commit-hash queries asked, the history has {len(history)} commits.")
    chosen = [history[i * len(history) // count] for i in range(count)]
    return [(full[:prefix_lengths[i % len(prefix_lengths)]], full) for i, full in enumerate(chosen)]


def query_cases(spec: dict, roots: dict, commit_hashes: bool = True) -> list:
    """(kind, query, relevant documents) for every query of one query file.
    A query with no relevant document would score 0 in every mode and only
    lower the averages, so one is refused rather than measured."""
    repo = spec.get("repo")
    if repo not in roots:
        raise SystemExit(f"The query file is for repository {repo!r}, which is not among --repo "
                         f"({', '.join(sorted(roots))}).")
    unknown = set(spec) - {"repo", *KINDS}
    if unknown:
        raise SystemExit(f"Unknown keys in the query file for {repo}: {', '.join(sorted(unknown))} "
                         f"(the kinds are {', '.join(KINDS)}).")
    root = roots[repo]
    cases = []
    for item in spec.get("descriptive", []):
        query, files = item["query"], item.get("relevant") or []
        if not files:
            raise SystemExit(f"{repo}: the descriptive query {query!r} names no relevant file.")
        for f in files:
            if not (root / f).is_file():
                raise SystemExit(f"{repo}: {f}, relevant to {query!r}, does not exist.")
        cases.append(("descriptive", query, {("code", repo, f) for f in files}))
    for name in spec.get("identifier", []):
        files = defining_files(root, name)
        if not files:
            raise SystemExit(f"{repo}: no file griot indexes defines the identifier {name!r}.")
        cases.append(("identifier", name, {("code", repo, f) for f in files}))
    hashes = spec.get("commit_hash")
    if commit_hashes and hashes:
        for prefix, full in commit_hash_queries(root, hashes["count"], hashes["prefix_lengths"]):
            cases.append(("commit hash", prefix, {("commit", repo, full)}))
    return cases


# --- the run -----------------------------------------------------------------

def _index_command(source: str, root: Path) -> list:
    return [sys.executable, "-m", "griot.cli", "index", source, "--path", str(root)]


def completed_run(source: str, root: Path, since: datetime) -> dict | None:
    """The run the indexing child for `source` of `root` recorded, if it is
    the newest in the run log, was written after `since`, and says the run
    finished with every document indexed. Each indexer records its run as the
    last thing it does, and a run that died is recorded with no counts, so
    such a record is the child's own word that its work is done."""
    from griot import common, logdb  # the throwaway directories are set by now

    for run in logdb.read_recent(common.LOG_DIR, "runs", limit=1):
        if (run.get("script") == f"index_{source}.py" and run.get("repo") == str(root)
                and datetime.fromisoformat(run["timestamp"]) >= since
                and run.get("indexed") is not None and run.get("failed") == 0):
            return run
    return None


def _index(root: Path) -> None:
    """Indexes a repository through the CLI, as a person would, in a child
    process that inherits the throwaway directories.

    A child can die AFTER finishing: one aborted at interpreter exit
    (libc++abi: recursive_mutex lock failed) once it had printed that it
    indexed everything with 0 failed, and that exit code ended a 45-minute
    measurement. So a failing exit code ends the run only when the child did
    not record a complete run of its own; when it did, the index it built is
    whole, and the measurement goes on, saying so."""
    for source in SOURCES_INDEXED:
        started = datetime.now(timezone.utc)
        code = subprocess.run(_index_command(source, root), env=os.environ.copy(),
                              stdin=subprocess.DEVNULL).returncode
        if code == 0:
            continue
        run = completed_run(source, root, started)
        if run is None:
            raise SystemExit(f"Indexing {source} of {root.name} failed (exit code {code}).")
        print(f"Note: indexing {source} of {root.name} exited with code {code} after recording a complete run "
              f"({run['indexed']} indexed, {run.get('skipped') or 0} unchanged, 0 failed); "
              f"its index is whole, so the measurement goes on.", file=sys.stderr)


def measure(cases: list, limit: int) -> tuple:
    from griot import common  # imported only once the throwaway directories are set
    require_throwaway_dirs(os.environ)
    totals, per_query = {}, []
    for kind, query, relevant in cases:
        for mode in common.SEARCH_MODES:
            try:
                hits = common.search(query, limit=limit, diverse=True, mode=mode)
            except common.SearchFilterError:
                # A query keyword search cannot read (no word it matches):
                # it found nothing in that mode, which is what it scores.
                hits = []
            docs = ranked_documents(h.payload for h in hits)
            rr = reciprocal_rank(docs, relevant)
            acc = totals.setdefault((kind, mode), [0.0, 0.0, 0])
            acc[0] += rr
            acc[1] += recall(docs, relevant)
            acc[2] += 1
            per_query.append({"kind": kind, "query": query, "mode": mode,
                              "rank": round(1 / rr) if rr else None})
    return totals, per_query, common.SEARCH_MODES


def main(argv=None) -> int:
    args = parse_args(argv)
    roots = repository_roots(args.repos)
    # Before any temporary directory exists: a typo in a query file should
    # not cost an indexing run.
    cases = []
    for path in args.queries:
        cases += query_cases(json.loads(path.read_text()), roots)
    with throwaway_dirs():
        require_throwaway_dirs(os.environ)
        if args.profile:
            os.environ["GRIOT_EMBED_PROFILE"] = args.profile
        for root in roots.values():
            _index(root)
        totals, per_query, modes = measure(cases, args.limit)
    print(format_table(totals, modes))
    if args.per_query:
        args.per_query.write_text(json.dumps(per_query, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
