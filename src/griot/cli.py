"""griot's single CLI.

Consolidates the standalone scripts into a single binary with subcommands.
Dispatch is LAZY on purpose: importing griot.common (and transitively
qdrant_client/fastembed) costs seconds — `griot --help` can't afford that
cost, so each subcommand module is only imported when the subcommand is
actually invoked (via importlib inside the dispatch, never at the top of
this file).

Each subcommand's arguments are still defined/parsed by the corresponding
module's own argparse (main(argv)) — this file doesn't duplicate flags like
--repo/--dry-run; it just forwards the rest of argv along
(argparse.REMAINDER), which also makes `griot index code --help` show the
real module's help.
"""

import argparse
import contextlib
import importlib
import json
import os
import shlex
import signal
import sys
from pathlib import Path

import griot
from griot import ConfigurationError, UnknownEmbedProfile, UsageError, __version__

# Full pipeline order: code
# first (largest volume), then the git metadata sources, "platform" last
# (the only one that depends on external network/API — GitHub/GitLab/
# Bitbucket/Azure DevOps/Gitea, detected from each repo's remote, see
# platforms.py; renamed from "gitlab" on 2026-08-13 when it stopped being
# GitLab-exclusive).
INDEX_SOURCES = ["code", "commits", "tags", "branches", "platform"]

# subcommand -> module with main(argv). Hyphenated names in the CLI become
# underscores in the module (golden-set -> golden_set).
_MODULES = {
    "ask": "griot.ask",
    "quality-check": "griot.quality_check",
    "auth": "griot.auth",
    "stats": "griot.stats",
    "mcp": "griot.mcp_server",
    "repos": "griot.repos",
    "golden-set": "griot.golden_set",
    "assist": "griot.harnesses",
    "audit": "griot.redaction",
    "config": "griot.config",
    "update": "griot.update",
}


def _run_module(module_name: str, argv: list) -> int:
    """Imports the subcommand's module only now (lazy) and runs its main.
    Normalizes the return value into an exit code: current mains return None
    on success (and signal failure via SystemExit or an exception)."""
    module = importlib.import_module(module_name)
    rc = module.main(argv)
    return 0 if rc is None else rc


def _is_dry_run(rest: list) -> bool:
    """True when the forwarded argv makes the source module's parser set
    --dry-run. argparse accepts any unambiguous prefix (`--dry`, `--dr`), so
    this cannot be a literal match: a prefix run would slip through as a real
    one. None of the index_* parsers has another --d* option, so a prefix of
    --dry-run is unambiguous; where it would be, the module's own parser
    rejects the argv (SystemExit) and nothing is recorded anyway."""
    for token in rest:
        flag = token.split("=", 1)[0]
        if len(flag) >= 3 and flag.startswith("--d") and "--dry-run".startswith(flag):
            return True
    return False


def _is_collection_busy(exc: BaseException) -> bool:
    """common is imported lazily, and the exception can only exist once it
    has been, so look it up instead of importing it (and its heavy imports)
    on the --help path."""
    common = sys.modules.get("griot.common")
    return common is not None and isinstance(exc, common.CollectionBusyError)


def _busy_message(exc, argv: list[str]) -> str:
    """Who holds the collection and what to do, then the command that was
    typed, last, to paste once it is free (policy, section 2). `argv` is the
    command line as typed, griot's own flags (--profile, --quiet) included:
    run again, it runs the way it did."""
    from griot import common

    holders = common.find_collection_holders(exc.path)
    who = "; ".join(
        f"PID {h['pid']}" + (f" ({h['command']}" + (f", started {h['started']}" if h["started"] else "") + ")"
                             if h["command"] else "")
        for h in holders)
    head = f"Another griot process holds the collection '{exc.collection}'." + (f" Holder: {who}." if who else "")
    roles = {h.get("role") for h in holders}
    if "mcp" in roles:
        idle = f"{common.IDLE_RELEASE_SECONDS:g}"
        tail = (f"That is a griot MCP server. It lets go of the collection after {idle} seconds without a tool call "
                f"(or when its session ends, if it runs in single mode). Close that session, or, from inside a "
                f"Claude Code session, index through the griot_index_repo tool; or wait and run the command again:")
    elif "index" in roles:
        tail = ("That is an indexing run (griot stats shows the last run). Wait for it to finish, then run the "
                "command again:")
    else:
        tail = "Wait for it to finish or close it, then run the command again:"
    return f"{head}\n{tail}\n  {shlex.join(['griot', *argv])}"


def _run_index_source(source: str, rest: list) -> int:
    """Runs one index source, recording a FAILED run if it dies.

    [real incident] Every index_*.py logs its run summary
    as the last statement of a successful main(), so a run that died before
    that point left no trace at all: a whole `griot index code` was lost to
    Kind(WouldBlock) (lock collision with another process) and `griot
    stats` kept showing the previous SUCCESS as the most
    recent run, as if nothing had happened. A run that starts must leave a
    trace whether it finishes or dies.

    Catches BaseException, not Exception: KeyboardInterrupt (Ctrl-C is the
    most common way a real indexing run ends early) is not an Exception.
    SystemExit is deliberately excluded — it's a controlled exit the module
    chose (argparse rejecting arguments, for instance), not a run dying
    mid-flight, and the module itself is responsible for whatever it wants
    logged in that case. The exception is always re-raised: this only adds
    a record, it never swallows a failure."""
    from griot import common  # lazy, same as every other subcommand here
    import time

    start_time = time.time()
    try:
        return _run_module(f"griot.index_{source}", rest)
    except SystemExit:
        raise
    except BaseException as e:
        # A dry-run writes nothing when it succeeds, so a dead one must not
        # leave a record either: it would count as a failed indexing run in
        # `griot stats` and shadow the last real run in griot_index_status.
        if _is_dry_run(rest):
            raise
        common.log_run_summary(
            script=f"index_{source}.py",
            # Genuinely unknown — a died run never counted anything. None,
            # not 0: 0 would read as "ran fine, indexed nothing" and would
            # also silently skew griot stats' totals.
            indexed=None, skipped=None, failed=None,
            duration_seconds=round(time.time() - start_time, 2),
            spend_today_usd=common.get_spend_today(),
            error=f"{type(e).__name__}: {e}",
        )
        raise


def _cmd_index_keywords(rest: list) -> int:
    """`griot index keywords`: gives every point of the active collection
    its keyword vector (common.build_keyword_index), which a collection
    indexed before keyword search lacks.

    A command of its own rather than a step of every index run: on such a
    collection it rewrites every point and needs as much free disk as the
    collection while it runs, and an index run is expected to touch only
    what changed. Not recorded as an indexing run either: it embeds nothing,
    and `griot stats` counts what runs embedded."""
    parser = argparse.ArgumentParser(
        prog="griot index keywords",
        description="Adds keyword (BM25) vectors to the active profile's collection, so that `griot search "
                    "--mode keyword|hybrid` works on what was indexed before keyword search existed. Local: "
                    "embeds nothing and costs nothing. A collection made before keyword search is copied into a "
                    "new one, which needs about as much free disk as the collection while it runs. Safe to run "
                    "again; run again after an interruption, it resumes the copy where it stopped.")
    show_flags_read_by_griot(parser, PROFILE_FLAG)
    parser.parse_args(rest)
    from griot import common

    try:
        result = common.build_keyword_index()
    except common.CollectionBusyError:
        raise  # main() names the process that holds it
    except RuntimeError as e:
        # The reasons build_keyword_index() stops on purpose (an index run
        # holds the lock, a copy that came out short) are RuntimeErrors that
        # say what to do; a traceback around them would read as a crash.
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not result["points"]:
        print(f"Nothing is indexed in collection '{common.COLLECTION_NAME}' yet: an index run adds keyword "
              f"vectors as it writes.")
    elif not result["written"] and not result["rebuilt"]:
        # A rebuilt collection is never a no-op, even with nothing written:
        # a copy killed after its last page but before the swap is finished
        # here, and the collection had no keyword vectors until then.
        if result["points"] == 1:
            print(f"The 1 point of collection '{common.COLLECTION_NAME}' already has keyword vectors.")
        else:
            print(f"All {result['points']} points of collection '{common.COLLECTION_NAME}' already have "
                  f"keyword vectors.")
    else:
        written, kept = result["written"], result["kept"]
        written_text = f"{written} point{'s' if written != 1 else ''}"
        if kept:
            # The points written now are only what the interrupted copy
            # lacked: alone they would read as a copy of that much of the
            # collection.
            done = (f"Resumed an interrupted copy of collection '{common.COLLECTION_NAME}': {kept} "
                    f"point{' was' if kept == 1 else 's were'} already copied, {written_text} copied now")
        else:
            how = "copied into a collection with keyword vectors" if result["rebuilt"] else "given keyword vectors"
            done = f"{written_text} {how} in collection '{common.COLLECTION_NAME}'"
        print(f"{done} (embedded nothing). `griot search` now runs hybrid by default, and `--mode keyword` and "
              f"`--mode hybrid` work.")
    return 0


YES_HELP = ("On a paid profile, do not ask before a run estimated at or above confirm-spend (default $0.50). "
            "The cost is still estimated first (a dry run of what is pending) and printed.")
DRY_RUN_HELP = ("Says how many chunks would be (re)embedded and, on a paid profile, their estimated cost, "
                "without embedding or spending anything.")


def _index_all_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="griot index all",
        description=f"Runs every source in one process, in this order: {', '.join(INDEX_SOURCES)}; stops at the "
                    f"first one that fails. On a paid profile it asks once, for every source, before the first "
                    f"one embeds. Every other flag is forwarded to each source: see `griot index code --help`.")
    parser.add_argument("--yes", action="store_true", help=YES_HELP)
    show_flags_read_by_griot(parser, SOURCES_FLAG, PROFILE_FLAG)
    return parser


def _gate_flags(rest: list):
    """What the spend confirmation needs from the forwarded argv: --help
    (nothing to confirm), --yes, and the repository named. Read the way the
    source modules' parsers read it (prefixes included); a command line they
    would refuse is left to them, so the gate is skipped (None)."""
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--repo")
    parser.add_argument("--path")
    try:
        known, _ = parser.parse_known_args(rest)
    except (argparse.ArgumentError, SystemExit):
        return None
    return None if known.help else known


def _pending_by_source(sources: list, rest: list):
    """What each source would embed: its own --dry-run, run in this process
    with its report read back as data (the same records the MCP preview
    reads), and its printed lines kept out of sight. Returns (records, 0),
    or (None, exit code) when a dry run failed: its own error is shown and
    the real run, which would fail the same way, never starts."""
    import contextlib
    import io
    import tempfile

    from griot import common

    fd, report = tempfile.mkstemp(prefix="griot-estimate-", suffix=".jsonl")
    os.close(fd)
    previous = os.environ.get("GRIOT_DRY_RUN_REPORT")
    os.environ["GRIOT_DRY_RUN_REPORT"] = report
    try:
        for source in sources:
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_module(f"griot.index_{source}", [*rest, "--dry-run"])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception as e:
                if _is_collection_busy(e):
                    raise
                sys.stderr.write(err.getvalue())
                print(f"Error: could not estimate what 'griot index {source}' would embed: {e}. Nothing was "
                      f"changed.\n  griot doctor", file=sys.stderr)
                return None, 1
            if rc != 0:
                sys.stderr.write(err.getvalue())
                return None, rc
        with open(report, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()], 0
    finally:
        if previous is None:
            os.environ.pop("GRIOT_DRY_RUN_REPORT", None)
        else:
            os.environ["GRIOT_DRY_RUN_REPORT"] = previous
        try:
            os.unlink(report)
        except OSError:
            pass
        common.take_redactions()  # counted by the dry runs; the real run counts and reports its own


def _confirm_index_spend(sources: list, rest: list, *, run: str, sources_override) -> int:
    """The confirmation before an index run spends money (decided
    2026-11-19): on a paid profile, what is pending is estimated first and
    said; at or above confirm-spend it asks, once for every source of `run`
    ("all" or one source) before the first one embeds. Asking inside the
    loop would spend on `code` before `commits` is asked. A refusal returns
    its exit code with only common.confirm()'s line: no progress is begun
    and no run is recorded, because nothing ran. 0 lets the run start.

    Costs a second read of every source on a paid profile (the dry run),
    --yes included: --yes answers the question, it does not skip the
    estimate, because what runs without a question leaves a trace (a
    script's log, griot_index_repo's job log). A local profile, a dry run
    and --help never come here."""
    from griot import common

    flags = _gate_flags(rest)
    if flags is None or _is_dry_run(rest) or not common.profile_is_paid():
        return 0
    # First, before the estimate is announced or made: a run that cannot
    # start must not say it is estimating or spending, nor pay for a dry run.
    refused = common.refuse_missing_credential()
    if refused:
        return refused
    if flags.repo:
        target = flags.repo
    elif flags.path:
        target = Path(flags.path).resolve().name
    else:
        try:
            registered = common.load_repos()
        except (OSError, ValueError):
            # Nothing registered yet (or a file griot cannot read): the dry
            # run below fails on it with the source's own error, the one a
            # local profile prints, and the gate names no repository.
            registered = None
        target = (None if not isinstance(registered, list) else Path(registered[0]).name if len(registered) == 1
                  else f"{len(registered)} registered repositories")
    target = None if target is None else common.printable(target)[:80]
    origin = f" from {target}" if target else ""
    # The dry run reads every file (and asks the platform's API) with its
    # output kept out of sight: said first, so a long read does not look hung.
    common.progress_note(f"Estimating the cost of {', '.join(sources)}{origin} with paid profile "
                         f"'{common.ACTIVE_PROFILE_NAME}'...")
    records, failed = _pending_by_source(sources, rest)
    if records is None:
        return failed
    pending = [record for record in records if record["to_embed"]]
    count = sum(record["to_embed"] for record in pending)
    if not count:
        return 0  # nothing to embed costs nothing: the run says what it found
    chunks = f"{count:,} chunk{'s' if count != 1 else ''}"
    kinds = (f"of {run}" if run != "all" else f"({', '.join(record['source'] for record in pending)})")
    what = f"{chunks} {kinds}{origin}"
    estimate = common.estimated_embedding_cost_usd(sum(record["to_embed_chars"] for record in pending))
    # The command as it was typed, with what griot took out of argv first.
    argv = ["griot", "index", run, *rest]
    if sources_override is not None:
        argv += ["--sources", ",".join(sources_override)]
    argv += global_flags_typed()
    return common.confirm_spend(what, estimate, yes=flags.yes, command=shlex.join(argv))


def _cmd_index(args) -> int:
    rest = list(args.rest)
    if args.source == "keywords":
        return _cmd_index_keywords(rest)
    if args.source != "all":
        refused = _confirm_index_spend([args.source], rest, run=args.source, sources_override=None)
        if refused:
            return refused
        return _run_index_source(args.source, rest)
    # `all` has a help of its own: otherwise --help reaches each source's
    # parser in turn, five helps followed by "all sources completed".
    # parse_known_args takes every spelling of --help each source's parser
    # takes (a prefix, -h) and leaves every other flag alone; its result is
    # not used, so `rest` is forwarded exactly as given.
    _index_all_parser().parse_known_args(rest)

    # `griot index all`: runs the sources in sequence within a single process.
    # `--sources` (resolved) filters which of the INDEX_SOURCES
    # run — this is what lets `griot_index_repo` (MCP) hold the lock for the
    # whole call by invoking this once, instead of once per source.
    sources = args.sources_override if args.sources_override is not None else INDEX_SOURCES
    unknown = [s for s in sources if s not in INDEX_SOURCES]
    if unknown:
        print(f"Error: unknown source(s) in --sources: {', '.join(unknown)}. Options: {', '.join(INDEX_SOURCES)}", file=sys.stderr)
        return 2

    from griot import common  # lazy, same as every other subcommand here

    # Before the progress record and the loop: a decline is not a failed
    # source, and one question covers every source.
    refused = _confirm_index_spend(sources, rest, run="all", sources_override=args.sources_override)
    if refused:
        return refused

    # Where the run is, for the host that started it (griot_index_repo): a
    # no-op unless that host named a file for it. The sources are announced
    # up front so that what is still to come shows as pending.
    common.progress_begin(sources)

    with common.notes_said_once():
        return _run_index_all(sources, rest)


def _stopped_at(source: str, sources: list, rest: list, what: str) -> str:
    """What `griot index all` says when `source` stopped it: which sources
    did not run, and the command that runs the failed one and those again,
    last. Read by a person and, through jobs._last_lines(), by the agent of
    griot_index_preview: the last lines of stderr are its reason. A source
    already indexed is skipped by content hash, so running from the failed
    one costs only what is left."""
    later = list(sources[sources.index(source) + 1:])
    # The error itself may end with its own command on a line of its own
    # (one of griot's: `griot auth set openai`): kept as a line, the fix
    # before the resume, which stays last. Its prose is one sentence.
    said = [line for line in what.splitlines() if line.strip()]
    prose = " ".join(line.strip() for line in said if not line.startswith("  "))
    fixes = "".join(f"\n  {line.strip()}" for line in said if line.startswith("  "))
    again = shlex.join(["griot", "index", "all", *rest, "--sources", ",".join([source, *later]), *global_flags_typed()])
    above = " Its error is above." if prose.startswith("failed (exit") else ""
    # A sentence of its own, closed once: an error that leads into its
    # command ("A credential is set in a terminal:") keeps its colon.
    head = f"Error: 'griot index {source}' {prose}".rstrip()
    head = head if head.endswith((".", "!", "?", ":")) else head + "."
    # Worded for one source or several: "no other source did not run" said
    # the opposite of what it meant.
    resume = (f"{', '.join(later)} did not run.{above} Once it is fixed, run them again" if later
              else f"{above.strip()} Once it is fixed, run it again".strip())
    separator = "\n" if fixes else " "
    return f"{head}{fixes}{separator}{resume} (what is already indexed is skipped):\n  {again}"


def _run_index_all(sources, rest) -> int:
    from griot import common

    # Stops at the FIRST error, saying which source failed — continuing to
    # index with a broken source would mask the problem, and the next run
    # would just re-pay the cost of discovering the error.
    for source in sources:
        common.progress_note(f"=== griot index {source} ===")
        common.progress_source(source, "reading")
        try:
            rc = _run_index_source(source, rest)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except BaseException as e:
            # BaseException: Ctrl-C or a kill ends the run here too, and the
            # record must not go on saying "reading".
            common.progress_source(source, "failed")
            if not isinstance(e, Exception) or _is_collection_busy(e):
                raise  # main() explains a busy collection once, with who holds it
            print(_stopped_at(source, sources, rest, f"failed: {e}"), file=sys.stderr)
            return 1
        if rc != 0:
            common.progress_source(source, "failed")
            print(_stopped_at(source, sources, rest, f"failed (exit {rc})"), file=sys.stderr)
            return rc
        common.progress_source(source, "done")
    # Progress, like the headers above it: a person piping stdout must not see a footer without them.
    common.progress_note("=== griot index all: all sources completed ===")
    return 0


# The kinds `--source-type` takes, for the help text. A copy of
# common.SOURCE_TYPES, because building the parser must not import the index
# (see the module docstring); tests/test_cli_search_filters.py holds the two
# together, and the search itself is what validates a value.
SEARCH_SOURCE_TYPES = ("code", "commit", "tag", "branch", "merge_request", "release", "issue")
# A copy of common.SEARCH_MODES, for the same reason; tests/test_keyword_search.py
# holds the two together.
SEARCH_MODES = ("vector", "keyword", "hybrid")


def at_least_one(text: str) -> int:
    """An argparse type: a whole number from 1 up. Zero finds nothing and a
    negative one reached the vector store as an overflow. Public because
    `griot ask` (ask.py, its own parser) takes it for its --limit too, so the
    two refuse the same values with the same message."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 (got {value})")
    return value


def _cmd_search(args) -> int:
    """Raw search on the vector store: finds the query (by meaning, by its
    words or both: --mode) and prints the hits with source label + score.
    NEVER calls chat_completion — this is the
    free/local path (aside from embedding the query under the direct
    profile); synthesis with an LLM is `griot ask`."""
    from griot import ask, common  # lazy: only imports qdrant/fastembed here

    mode, note = common.search_mode_for(args.query, args.mode)
    try:
        results = common.search(args.query, limit=args.limit, group_by_document=args.group_by_document,
                                repos=args.repo, source_types=args.source_type, diverse=True, mode=mode)
    except common.SearchFilterError as e:
        # A filter that cannot match is refused by the search itself, for
        # the same reason as in the MCP tool: "No results." would read as
        # "nothing about this there". A usage error, hence the status.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        # The griot_search document, built by the same function: an agent
        # reading this through a shell reads what the tool would have given
        # it, with one difference on purpose. One process per command keeps
        # no memory, so every current condition is in every note (the server
        # says each once per process), and each ends with commands to paste.
        from griot import search_result
        common.print_json(search_result.output(
            results, mode=mode, repos=args.repo, source_types=args.source_type, keyword_notice=note,
            asked_mode=args.mode, behind_next=search_result.terminal_commands,
            keyword_next=search_result.terminal_command))
        # Said in the note already: not on stderr too, but recorded, so that
        # the footer does not count it as news afterwards.
        say_keyword_fallback(args.mode, note, said_in_result=True)
        return 0
    if not results:
        print("No results.")
        # The line `griot ask` prints for the same search: which filters
        # narrowed it, when any did.
        reason = ask.nothing_found_reason(args.repo, args.source_type)
        if reason:
            print(reason)
    for r in results:
        payload = r.payload or {}
        print(f"[{r.score:.3f}] {common.shown(ask.source_label(payload))}")
        content = common.stored_text(payload).strip()
        if content:
            # one-line preview — the full result is ask/MCP's job
            first_line = content.splitlines()[0]
            print(f"        {common.printable(first_line[:200])}")
        also_in = getattr(r, "also_in", None)
        if also_in:
            print(f"        same text in: {', '.join(common.shown(label) for label in also_in)}")
    # Which mode ran, always: the default is not always what runs (see
    # common.search_mode_for), and scores are on that mode's own scale. It
    # is part of the result; why it is not hybrid is a notice (stderr, once
    # per collection, counted in the footer afterwards).
    print(f"Mode: {mode}")
    say_keyword_fallback(args.mode, note)
    return 0


def say_keyword_fallback(asked_mode, notice, *, said_in_result: bool = False) -> None:
    """What `griot search` and `griot ask` say when a default search fell
    back to vector: the ledger's prose once per collection, on stderr. A
    default search that ran as it should read the collection's state and
    found nothing to say: that is recorded too, so the prose comes again if
    the condition returns. An explicit mode read nothing and says nothing.
    `said_in_result`: the result itself carried it (`--json`'s note), so it
    is only recorded."""
    from griot import notices

    if notice is not None or asked_mode is None:
        if said_in_result:
            notices.said_in_result(notice, notices.KEYWORD_VECTORS_MISSING)
        else:
            notices.say_once(notice, notices.KEYWORD_VECTORS_MISSING, lead=notices.FELL_BACK)


_TIER_MIN_GB = {"light": 8, "medium": 16, "heavy": 32}


def profiles_list_result(*, ram_gb: float | None = None, active: str | None = None) -> dict:
    """What `griot profiles list --json` prints and what the
    griot_profiles_list tool returns (its keys are a subset of each entry),
    built once so the two cannot disagree: {profiles, active, ram_gb}, the
    profiles sorted by name.

    Credentials are resolved once, through auth.provider_status() (the
    environment, griot's .env, then the keychain), and only for the
    embedding profiles: each one checked can be a keychain prompt on macOS.
    `fits` is for a local profile only (null for one that calls an API),
    and "fits" means the machine's RAM is at least the tier's nominal floor
    (light 8 GB, medium 16 GB, heavy 32 GB), not a share of RAM computed
    from rss_estimate_mb: the floor is what the profile guide documents.
    `active` defaults to the profile this process runs with; a long-lived
    MCP server passes the one in effect now."""
    from griot import auth, common

    if ram_gb is None:
        import psutil
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    active = active or common.ACTIVE_PROFILE_NAME
    # Keyed by env var, not by provider label: the label is derived from the
    # var by a rule 'gemini' does not follow, and matching on the derived
    # name would silently miss it.
    key_envs = {common.credential_env_for_profile(name, profile) for name, profile in common.EMBED_PROFILES.items()}
    statuses = {s["env_var"]: s for s in auth.provider_status(key_envs - {None})}
    profiles = []
    for name in sorted(common.EMBED_PROFILES):
        profile = common.EMBED_PROFILES[name]
        key_env = common.credential_env_for_profile(name, profile)
        status = statuses.get(key_env) or {}
        configured = bool(status.get("configured")) if key_env else True
        local = key_env is None
        tier = profile.get("ram_tier") if local else None
        profiles.append({
            "profile": name,
            "is_active": name == active,
            # The one definition behind every `paid` field and the
            # confirmation before spending (common.profile_is_paid), so this
            # listing and the spend gate can never disagree about a profile.
            "paid": common.profile_is_paid(profile),
            "credential_configured": configured,
            "collection": common.collection_name_for(name),
            "dim": profile["dim"],
            "ram_tier": tier,
            "rss_max_mb": profile["rss_estimate_mb"][1] if local and profile.get("rss_estimate_mb") else None,
            "fits": (ram_gb >= _TIER_MIN_GB[tier]) if tier in _TIER_MIN_GB else None,
            "price_per_1m_tokens": None if local else profile.get("price_per_1m_tokens"),
            "credential_env": key_env,
            "fix": None if configured else f"griot auth set {status.get('provider') or name}",
        })
    return {"profiles": profiles, "active": active, "ram_gb": round(ram_gb, 1)}


def _cmd_profiles_list(args) -> int:
    """`griot profiles list`: every embedding profile, the local ones by RAM
    tier against the machine's RAM, the paid ones with their price and
    whether their credential is set. Read from profiles_list_result(), as
    --json and the griot_profiles_list tool are."""
    from griot import common

    result = profiles_list_result()
    if getattr(args, "json", False):
        common.print_json(result)
        return 0
    total_gb = result["ram_gb"]
    print(f"\nYour machine: {total_gb:.1f} GB RAM\n")

    tier_labels = {"light": "light (8GB)", "medium": "medium (16GB)", "heavy": "heavy (32GB+)"}
    for tier in ["light", "medium", "heavy"]:
        entries = [e for e in result["profiles"] if e["ram_tier"] == tier]
        if not entries:
            continue
        print(f"  LOCAL — {tier_labels[tier]}")
        for entry in entries:
            # An estimate that needs no action: a note, not a warning glyph.
            label = "fits comfortably" if entry["fits"] else f"might be tight (you have {total_gb:.1f}GB)"
            active = "   [ACTIVE]" if entry["is_active"] else ""
            print(f"    {entry['profile']:<14}{entry['dim']}d   ~{entry['rss_max_mb']}MB   {label}{active}")
        print()

    print("  PAID (requires API key)")
    # {command: the profiles it unlocks}: one provider's credential serves
    # each of its profiles, so the command is given once and names them.
    fixes: dict[str, list[str]] = {}
    for entry in (e for e in result["profiles"] if e["paid"]):
        # Worded as `griot doctor` words the same fact.
        configured = ("configured" if entry["credential_configured"]
                      else f"its credential ({entry['credential_env']}) is not configured")
        if entry["fix"]:
            fixes.setdefault(entry["fix"], []).append(entry["profile"])
        active = "   [ACTIVE]" if entry["is_active"] else ""
        price = f"${entry['price_per_1m_tokens']:.2f}/1M"
        print(f"    {entry['profile']:<14}{entry['dim']}d   {price}   {configured}{active}")
    # The commands last, the more urgent one at the very end: what switches,
    # then what sets a missing credential, without which that profile
    # cannot index or search.
    print("\n  griot profiles use <name>   # makes it the active profile")
    for fix, profiles in fixes.items():
        print(f"  {fix}   # for {', '.join(profiles)}")
    return 0


def unknown_profile_sentence(name: str, reason: str, options: list[str]) -> str:
    """The one wording for a profile name that does not exist, said by
    `profiles use` (both before and after the configuration loads) and by
    `profiles delete`: the profiles there are, then the command that lists
    them with what each needs."""
    return f"unknown profile {name!r}.{reason} Options: {', '.join(options)}.\n  griot profiles list"


def _cmd_profiles_use(args) -> int:
    """`griot profiles use <name>`: makes `name` the active embedding
    profile by writing GRIOT_EMBED_PROFILE to <config>/.env, the file every
    griot process reads (the MCP server included, which is registered with
    no environment of its own).

    Switching writes nothing but that line. It asks first when the profile
    calls an API, because from then on what is indexed and searched is sent
    there and billed: recoverable (switch back), so --yes answers it."""
    from dotenv import dotenv_values

    from griot import auth, common

    name = args.profile
    if name not in common.EMBED_PROFILES:
        print(f"Error: {unknown_profile_sentence(name, common.unknown_profile_reason(name), list(common.EMBED_PROFILES))}",
              file=sys.stderr)
        return 2
    profile = common.EMBED_PROFILES[name]
    in_file = dotenv_values(common.ENV_PATH).get("GRIOT_EMBED_PROFILE") if common.ENV_PATH.exists() else None
    # What the REAL environment said before this command touched it (see
    # _main). The file is loaded without overriding the environment, so a
    # variable exported there is the one in force, whatever the file says.
    exported = getattr(args, "environment_profile", None)
    calls_an_api = profile["backend"] != "local"

    if in_file == name:
        print(f"'{name}' is already the active embedding profile in {common.ENV_PATH}.")
    else:
        if calls_an_api:
            refused = common.confirm(
                f"Make '{name}' the active embedding profile? It calls an API: from now on what griot indexes "
                f"and searches is sent to it, and embedding is billed.", yes=args.yes)
            if refused:
                return refused
        common.env_file_set("GRIOT_EMBED_PROFILE", name)
        was = f" (was '{in_file}')" if in_file else ""
        print(f"Active embedding profile: '{name}'{was}. Written to {common.ENV_PATH}.")

    credential = common.credential_env_for_profile(name, profile)
    if credential and not common.credential(credential):
        provider = auth.provider_label(credential) if credential.startswith("GRIOT_") else name
        print(f"  It needs a credential that is not set ({credential}): griot auth set {provider}")
    collection = common.collection_name_for(name)
    if common.collection_exists(collection):
        # "A collection exists", not "it has an index": asked without opening
        # it (another process may hold it), and one that exists can be empty.
        print(f"  A collection already exists for it ({collection}); `griot index all` brings it up to date.")
    else:
        print("  Nothing is indexed under it yet: each profile has its own index. Run `griot index all`.")
    if in_file and in_file != name and common.collection_exists(common.collection_name_for(in_file)):
        print(f"  The index of '{in_file}' stays on disk; `griot profiles delete {in_file}` removes it.")
    print("  An MCP server that is already running keeps the profile it started with: restart open sessions.")
    if exported and exported != name:
        shown = common.printable(exported)[:80]
        if exported in common.EMBED_PROFILES:
            print(f"  GRIOT_EMBED_PROFILE is also set in the environment (to '{shown}'), and the environment wins "
                  f"over the file: this shell, and what is started from it, keeps using that profile until the "
                  f"variable is unset.")
        else:
            # The file is right now and the next command still fails: say
            # why, or its hint to run this very command goes in circles.
            print(f"  GRIOT_EMBED_PROFILE is also set in the environment, to '{shown}', which is not a profile. "
                  f"The environment wins over the file, so every griot command started from this shell fails "
                  f"until the variable is unset.")
    return 0


def delete_profile(profile_name: str, *, active_profile_name: str | None = None) -> str:
    """[user-requested] "how do I stop paying for an index I no longer
    want to use" — there was no way to reclaim disk space from a profile's
    collection except manually deleting qdrant_data/<collection> by hand.
    Pure function shared by `griot profiles delete` and the web
    a non-terminal caller (same repos.py-style split as
    add_repo()/remove_repo(): raise a bare ValueError, no "Error: " prefix
    — that's the caller's job). Refuses the ACTIVE profile (switch to a
    different one first — deleting out from under what search/index
    currently target would be confusing, not just irreversible) and a
    profile that was never indexed (nothing to delete). Returns the
    collection name that was deleted.

    active_profile_name: [review finding] override for "what counts as
    active" — defaults to common.ACTIVE_PROFILE_NAME, correct for a fresh
    CLI process (never stale, since GRIOT_EMBED_PROFILE is only resolved
    once at that process's own import). An MCP server is long-lived,
    though: a profile switch made through apply_setting() (Config page or
    the Collections 'Activate' button) updates the .env FILE but never
    that already-imported constant — comparing against it directly would
    let a user delete the profile they just activated, in the same
    still-running session, without a restart. service.delete_profile()
    passes its own live-resolved value here instead of trusting the
    default."""
    collection = check_delete_profile(profile_name, active_profile_name=active_profile_name)
    from griot import common
    common.delete_collection(collection)  # raises ValueError when never indexed, or while indexing is running
    return collection


class ActiveProfileError(UsageError):
    """The profile asked to delete is the active one: the step that fixes it
    is a different `profiles use`, not a listing."""


def check_delete_profile(profile_name: str, *, active_profile_name: str | None = None) -> str:
    """Read-only half of delete_profile(): the collection it would delete.
    Raises the same ValueError it would for an unknown profile, the active
    one, one that was never indexed, or while an indexing run holds the
    lock. Lets the CLI refuse before asking anyone to confirm. The delete
    repeats the last check itself: a run can start between the question and
    the answer."""
    from griot import common

    # A retired profile is still deletable: a run on it opened its collection
    # before the first embedding failed, and nothing else removes that one.
    if profile_name not in common.EMBED_PROFILES and profile_name not in common.RETIRED_EMBED_PROFILES:
        raise UsageError(unknown_profile_sentence(
            profile_name, common.unknown_profile_reason(profile_name), list(common.EMBED_PROFILES)))
    active = active_profile_name if active_profile_name is not None else common.ACTIVE_PROFILE_NAME
    if profile_name == active:
        raise ActiveProfileError(f"'{profile_name}' is the active profile — switch to a different one first "
                         f"(`griot profiles use <name>`), then delete.")
    collection = common.collection_name_for(profile_name)
    if not common.collection_exists(collection):
        raise UsageError(f"collection '{collection}' does not exist, so there is nothing to delete.")
    if common.index_lock_status()["running"]:
        raise ValueError("an indexing run is currently in progress — wait for it to finish, then try again.")
    return collection


def _cmd_profiles_delete(args) -> int:
    from griot import common

    try:
        check_delete_profile(args.profile)
    except ValueError as e:
        # The command that helps goes last. The sentence is shared with the MCP
        # tool, which has its own next step, so it is added here, and only
        # where the sentence does not end on one already.
        tail = ("\n  griot profiles use <name>" if isinstance(e, ActiveProfileError)
                else "\n  griot profiles list" if isinstance(e, UsageError) and "\n  griot " not in str(e) else "")
        print(f"Error: {e}{tail}", file=sys.stderr)
        # A wrong request (unknown or active profile, nothing to delete) is
        # incorrect use; a run holding the lock is the operation not able to
        # run now.
        return 2 if isinstance(e, UsageError) else 1
    # No --yes on purpose: the vectors are gone for good and rebuilding them
    # costs money (see common.confirm and griot_profiles_delete).
    refused = common.confirm(f"Delete profile '{args.profile}' and its indexed vectors? This cannot be undone, "
                             f"and rebuilding them costs whatever that profile charges to embed.", yes=None)
    if refused:
        return refused
    try:
        collection = delete_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Deleted profile '{args.profile}' (collection: {collection}).")
    return 0


def _needs_a_value(long_flag: str) -> SystemExit:
    """A flag read before the parser, given without its value: incorrect use,
    so exit 2 and a message that says what to type (a string passed to
    SystemExit would exit 1 with no `Error:` and no example)."""
    print(f"Error: {long_flag} requires a value. Example:\n  griot {long_flag} NAME ...", file=sys.stderr)
    return SystemExit(2)


def _extract_switch(argv: list, *spellings: str) -> tuple[list, bool]:
    """Takes a flag that has no value (`--quiet`, `-q`) out of argv, wherever
    it is, for the same reason _extract_flag does it: the passthrough
    subcommands forward everything to their own parser, which must not see
    it. `--quiet=1` is not a spelling: the flag takes no value. No subcommand
    has an argument of its own called `-q` or `--quiet`, so nothing legitimate
    is taken."""
    # Past `--` everything is data (a search query or a path that is named
    # `-q`), as it is for any command line: left alone.
    stop = argv.index("--") if "--" in argv else len(argv)
    head = [arg for arg in argv[:stop] if arg not in spellings]
    return head + argv[stop:], len(head) != stop


def _extract_flag(argv: list, flag: str) -> tuple[list, str | None]:
    """Extracts `--{flag} value`/`--{flag}=value` from argv BEFORE the normal
    parse. These flags can't be declared as an argument of a normal
    subparser because the index/passthrough subcommands use
    argparse.REMAINDER right after the positional — REMAINDER swallows
    everything to its right, including flags the parser itself knows about,
    so they'd never arrive in isolation. Intercepting here, outside
    argparse, avoids that collision and guarantees the flag never leaks
    into the argv forwarded to the real module's main() (section 8.1). Used
    by --profile (single value) and --sources (raw list, splitting is left
    to the caller) — same mechanism, raw value returned uninterpreted."""
    long_flag = f"--{flag}"
    argv = list(argv)
    value = None
    out = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == long_flag:
            if i + 1 >= len(argv):
                raise _needs_a_value(long_flag)
            value = argv[i + 1]
            i += 2
            continue
        if arg.startswith(f"{long_flag}="):
            value = arg.split("=", 1)[1]
            if not value:
                raise _needs_a_value(long_flag)
            i += 1
            continue
        out.append(arg)
        i += 1
    return out, value


# The flags _extract_flag() takes out, as each --help shows them. They are
# not declared as options of any parser: a declared option would also take
# argparse's prefixes (`--pro`, `--sour`), which the extraction does not, so
# a prefix would be parsed and silently dropped instead of refused as now,
# and it would make prefixes of other options ambiguous (`--pr` for
# --prune). show_flags_read_by_griot() adds them to the help only. Public
# because the modules griot forwards to (ask.py, index_*.py, ...) show them
# in their own parsers' help. (flag, metavar, help)
PROFILE_FLAG = ("--profile", "NAME", "Use this embedding profile for this run only, instead of the active one "
                                     "(`griot profiles list` names them, `griot profiles use` changes the active one)")
SOURCES_FLAG = ("--sources", "LIST", f"Run only these sources, comma-separated, in the order given (default: all "
                                     f"of {','.join(INDEX_SOURCES)})")
# Where more than `griot index all` reads the help, it says whose flag it is.
_SOURCES_OF_INDEX_ALL = (*SOURCES_FLAG[:2], f"`griot index all` only. {SOURCES_FLAG[2]}")


# A switch, not a flag with a value: show_flags_read_by_griot() lists it
# without a metavar. Every parser's help shows it (it is appended there), so a
# person who sees a notice they do not want finds the way to silence it in
# the help of the command that printed it.
QUIET_FLAG = ("--quiet", None, "Silence notices, warnings and progress (never errors, prompts or the result). "
                               "Same as GRIOT_QUIET=1. Also -q")


def json_flag_help(guide: str) -> str:
    """The --json help of every listing (`repos list`, `profiles list`,
    `golden-set list`, `search`): the same promise in the same words, and
    the guide that documents the keys of that one."""
    return (f"One JSON document on stdout instead of the list; notices and errors stay on stderr "
            f"(keys: {guide})")


def chat_profile_flag(names) -> tuple[str, str, str]:
    """`griot ask`'s --chat-profile. The names are passed in: they live in
    common.CHAT_PROFILES, which this module must not import to build a help
    (see the module docstring)."""
    return ("--chat-profile", "NAME", f"Answer with this chat provider for this run only ({', '.join(names)}), "
                                      f"instead of GRIOT_CHAT_PROFILE")


def global_flags_typed() -> list[str]:
    """The flags griot takes out of argv before a subcommand sees them
    (--profile, --quiet), as they were typed: a command rebuilt for a person
    to paste (a refusal's `... --yes`) runs the way the one typed would."""
    flags = []
    if griot.PROFILE_FROM_COMMAND_LINE is not None:
        flags += ["--profile", griot.PROFILE_FROM_COMMAND_LINE]
    if griot.QUIET_FROM_COMMAND_LINE:
        flags.append("--quiet")
    return flags


def show_flags_read_by_griot(parser: argparse.ArgumentParser, *flags: tuple[str, str, str]) -> None:
    """Adds to `parser`'s --help a section naming `flags`, which griot reads
    before that parser sees the command line. The section is laid out by
    argparse itself, on a parser that is only ever formatted, so it reads
    like the options above it; `parser` parses exactly what it did.

    Every parser below `parser` (its subparsers, however deep, and the ones
    added to it later) gets the section too, with --quiet alone: the help of a
    leaf command is where a person who just saw a notice looks, and a list of
    names kept by hand goes stale. Calling it again on a parser replaces its
    section instead of adding a second one."""
    shown = argparse.ArgumentParser(prog=parser.prog, add_help=False)
    group = shown.add_argument_group("read by griot itself, anywhere on the command line")
    for flag, metavar, help_text in (*flags, QUIET_FLAG):
        if metavar is None:
            group.add_argument(flag, action="store_true", help=help_text)
        else:
            group.add_argument(flag, metavar=metavar, help=help_text)
    # Without its usage paragraph, which names only these flags.
    section = shown.format_help().split("\n\n", 1)[1]
    base = getattr(parser, "_griot_format_help", None) or parser.format_help
    parser._griot_format_help = base
    parser.format_help = lambda: f"{base().rstrip()}\n\n{section}"
    _cover_subparsers(parser)


def _cover_subparsers(parser: argparse.ArgumentParser) -> None:
    """Gives the parsers `parser` has, and will have, the --quiet section."""
    def cover(child):
        if not hasattr(child, "_griot_format_help"):
            show_flags_read_by_griot(child)

    def wrap(action):
        add_parser = action.add_parser

        def add_covered(*args, **kwargs):
            child = add_parser(*args, **kwargs)
            cover(child)
            return child

        action.add_parser = add_covered
        for child in action.choices.values():
            cover(child)

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and not getattr(action, "_griot_covered", False):
            action._griot_covered = True
            wrap(action)
    if not getattr(parser, "_griot_covers_new_subparsers", False):
        parser._griot_covers_new_subparsers = True
        add_subparsers = parser.add_subparsers

        def add_covered_subparsers(*args, **kwargs):
            action = add_subparsers(*args, **kwargs)
            action._griot_covered = True
            wrap(action)
            return action

        parser.add_subparsers = add_covered_subparsers


def _extract_profile_override(argv: list) -> tuple[list, str | None]:
    return _extract_flag(argv, "profile")


def _extract_chat_profile_override(argv: list) -> tuple[list, str | None]:
    """`griot ask --chat-profile <name>` (gemini/openai/deepseek/groq — see
    common.CHAT_PROFILES) — same mechanism as --profile: extracted from argv
    before the normal parse, sets GRIOT_CHAT_PROFILE in the process before
    any import of common.py."""
    return _extract_flag(argv, "chat-profile")


def _extract_sources_override(argv: list) -> tuple[list, list[str] | None]:
    """`griot index all --sources code,commits` (, resolved
    this way): a single subcommand that filters `all`'s sources instead of
    one process per source — keeps everything in a single process, so
    `griot_index_repo` (via MCP) can hold the lock for the whole call by
    invoking this once, instead of once per source."""
    argv, raw = _extract_flag(argv, "sources")
    if raw is None:
        return argv, None
    return argv, [s.strip() for s in raw.split(",") if s.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="griot",
        description="Griot — local RAG over code, git history and code platforms (GitHub/GitLab/Bitbucket/Azure DevOps/Gitea) of your repositories.",
        epilog="Exit status: 0 done, 1 the operation ran and failed, 2 incorrect use (an unknown name, a bad "
               "value, no terminal where one is needed). Results go to stdout; notices, warnings, progress and "
               "errors go to stderr.",
    )
    show_flags_read_by_griot(parser, PROFILE_FLAG,
                             ("--chat-profile", "NAME", "`griot ask`: the chat provider for this run only (see "
                                                        "`griot ask --help`)"),
                             _SOURCES_OF_INDEX_ALL)
    parser.add_argument("--version", action="version", version=f"griot {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p_index = subparsers.add_parser(
        "index",
        help="Indexes one source (code|commits|tags|branches|platform) or all (all); keywords adds keyword "
             "vectors to what is already indexed",
        description="Indexes repositories into the vector store. Every other flag is forwarded to the source's "
                    "indexer: see `griot index <source> --help`. On a paid profile the cost is estimated first, "
                    "and a run estimated at or above confirm-spend asks (--yes answers). "
                    "`griot index keywords` is not a source: it adds keyword (BM25) vectors to a collection "
                    "indexed before keyword search existed, embedding nothing.",
    )
    show_flags_read_by_griot(p_index, PROFILE_FLAG, _SOURCES_OF_INDEX_ALL)
    p_index.add_argument("source", choices=INDEX_SOURCES + ["all", "keywords"], metavar="source",
                         help=f"One of: {', '.join(INDEX_SOURCES + ['all', 'keywords'])}")
    p_index.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_index.set_defaults(func=_cmd_index)

    p_search = subparsers.add_parser(
        "search",
        help="Raw search, by meaning or by exact words (no chat/LLM synthesis)",
        description="Searches the vector store and prints sources + score (or one JSON document with --json). "
                    "Never calls any chat model.",
    )
    show_flags_read_by_griot(p_search, PROFILE_FLAG)
    p_search.add_argument("query", help="Natural-language query")
    p_search.add_argument("--limit", type=at_least_one, default=5, help="How many results (default: %(default)s)")
    p_search.add_argument("--repo", action="append", metavar="NAME",
                          help="Only this repository, by its directory name (repeat for several). "
                               "One with nothing indexed is an error, not an empty result.")
    p_search.add_argument("--source-type", action="append", metavar="KIND",
                          help=f"Only this kind of source (repeat for several): {', '.join(SEARCH_SOURCE_TYPES)}")
    p_search.add_argument("--group-by-document", action="store_true",
                          help="The best chunk of each document, so --limit counts documents "
                               "(default: up to three chunks of one document)")
    # default None, not "hybrid": an explicit hybrid is refused where it cannot
    # run, the default falls back to vector there (common.search_mode_for).
    p_search.add_argument("--mode", choices=SEARCH_MODES, default=None,
                          help="hybrid (default): by meaning and by the exact words, fused by rank; vector on an "
                               "index built before keyword search. vector: by meaning. keyword: by the exact "
                               "words alone, for a commit hash, an error code or where a name is used (the "
                               "default finds where it is defined); embeds nothing. "
                               "Scores are on each mode's own scale.")
    p_search.add_argument("--json", action="store_true", help=json_flag_help("docs/search.md"))
    p_search.set_defaults(func=_cmd_search)

    for name, help_text in [
        ("ask", "Question with RAG (search + LLM synthesis — PAID path; provider set by --chat-profile)"),
        ("quality-check", "Tests vector search quality (self-check + golden set)"),
        ("auth", "Manages external credentials — paid embedding providers and code platforms (set/list/remove/migrate)"),
        ("stats", "Usage, spend and savings report (index, indexing runs, queries)"),
        # Deliberately not a list of tool names: it went stale twice as tools
        # were added, and a --help line is the wrong place to keep an
        # inventory in sync. The server answers list_tools() authoritatively.
        ("mcp", "Starts the MCP server (stdio) — exposes search, status, quality and management tools to an MCP client"),
        ("repos", "Manages the list of repos indexed in bulk (add/list/remove)"),
        ("golden-set", "Manages the curated golden set for quality-check (suggest/review/add/list/remove)"),
        ("assist", "Installs griot's Claude Code/opencode skills and agents for onboarding, indexing and workflow help (install)"),
        ("audit", "Lists where the index holds credential-looking values (locations only, never the values)"),
        ("config", "Shows and changes griot's settings without editing the config .env (list/get/set/unset)"),
        ("doctor", "Checks the whole setup at once (settings, profile, index, repositories, MCP registration, a newer release); changes no setting, index or file of yours"),
        ("update", "Upgrades griot to the newest release on PyPI with the installer that installed it (pipx, uv tool, pip); shows the command and asks first, then says what the new version needs done"),
    ]:
        # Registered ONLY so `griot --help` lists these with their help
        # text, and so an unknown command still gets argparse's normal
        # "invalid choice" error. Actual dispatch never reaches
        # parse_args() for these — main()'s `argv[0] in _MODULES` shortcut
        # (see its comment for why) intercepts them first, so no
        # `rest`/`func` is needed here.
        subparsers.add_parser(name, help=help_text, add_help=False)

    p_profiles = subparsers.add_parser(
        "profiles",
        help="Manages embedding profiles (--profile in index/ask/search)",
    )
    show_flags_read_by_griot(p_profiles)
    profiles_sub = p_profiles.add_subparsers(dest="profiles_command", metavar="<action>", required=True)
    p_profiles_list = profiles_sub.add_parser("list", help="Lists available profiles with real RAM detection")
    p_profiles_list.add_argument("--json", action="store_true", help=json_flag_help("docs/configuration.md"))
    p_profiles_use = profiles_sub.add_parser(
        "use", help="Makes a profile the active one (writes GRIOT_EMBED_PROFILE to the config .env)"
    )
    show_flags_read_by_griot(p_profiles_use)
    p_profiles_use.add_argument("profile", help="Profile name (see `griot profiles list`)")
    p_profiles_use.add_argument("--yes", action="store_true",
                                help="Do not ask before switching to a profile that calls an API")
    p_profiles_use.set_defaults(func=_cmd_profiles_use)
    p_profiles_delete = profiles_sub.add_parser(
        "delete", help="Permanently deletes a profile's on-disk collection (frees disk space)",
        description="Permanently deletes a profile's on-disk collection (frees disk space). It cannot be undone, "
                    "so it asks first, at an interactive terminal, and there is no flag that answers: run from a "
                    "script or an agent's shell, with no terminal, it changes nothing.",
    )
    show_flags_read_by_griot(p_profiles_delete)
    p_profiles_delete.add_argument("profile", help="Profile name (see `griot profiles list`)")
    p_profiles_delete.set_defaults(func=_cmd_profiles_delete)
    p_profiles.set_defaults(func=_cmd_profiles_list)

    return parser


def main(argv=None) -> int:
    """The command, plus how an indexing run ended, recorded where its host
    reads its progress (common.progress_end(); a no-op for every other
    command). Recorded here, the last place the exit status is known: the
    run itself can stop on an exception that only this function turns into
    one. The status is the one the interpreter will exit with, in the form
    Popen.returncode gives it, so a host reading it learns what its own
    child would have told it."""
    status = None
    try:
        status = _main_with_errors_explained(argv)
        if status == 0:
            # After a failure the error's own command is the last line, and a
            # host that reads the last lines of stderr as the reason (jobs.py)
            # must find it there.
            _end_with_notices()
        return status
    except SystemExit as e:
        status = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        raise
    except KeyboardInterrupt:
        # An uncaught Ctrl-C makes the interpreter kill itself with SIGINT.
        status = -signal.SIGINT
        raise
    except BaseException:
        status = 1  # a traceback, then exit 1
        raise
    finally:
        if "griot.common" in sys.modules:  # imported lazily: no run began without it
            sys.modules["griot.common"].progress_end(status)


# The command this process ran (the first word left once griot's own flags
# are taken out), for the footer. None until _main() knows it.
_command: str | None = None

# Commands that end without the footer: doctor and stats list every
# condition themselves, and a server's stderr is a log nobody reads as the
# end of a command.
_NO_FOOTER = frozenset({"doctor", "stats", "mcp"})


def _end_with_notices() -> None:
    """The one notice line a command ends with (notices.footer()), on stderr,
    after recording what this command can see for free: the renamed
    variables and old files on every command, and after `griot index` what
    an index run changes (which repositories are behind, whether the
    collection has keyword vectors). A ledger that cannot be read or
    written must never fail a command that succeeded."""
    if _command is None or _command in _NO_FOOTER or "griot.common" not in sys.modules:
        return
    if _command == "update" and getattr(sys.modules.get("griot.update"), "CHECKED_AFTER_UPGRADE", False):
        # It just said what the new version needs done, or pointed at
        # `griot doctor`: a footer would say the same again under it.
        return
    from griot import common, notices

    try:
        kinds, found = {notices.LEGACY_ENV, notices.LEGACY_LAYOUT}, notices.legacy(common)
        if _command == "index":
            from griot import freshness, stats

            status = {"repositories": freshness.repository_freshness(),
                      "keyword_search": common.keyword_search_status(common.COLLECTION_NAME)}
            # A config that cannot be read leaves a stored keyword fact as it was.
            kinds |= stats.kinds_looked_at(status, None)
            found += stats.ledger(status, None)
        notices.record(kinds, found)
        line = notices.footer()
    except Exception as e:  # whatever the ledger could not foresee: logged, never raised
        common.log_and_print(f"Warning: the notices ledger could not be updated: {type(e).__name__}: {e}",
                             level="warning", echo=False)
        return
    if line:
        print(line, file=sys.stderr)


@contextlib.contextmanager
def _repairing(repairs: bool):
    """griot.REPAIRING_SETTINGS while the configuration loads, for a command
    that repairs it (see the flag), and off again after: it is read only as
    common.py loads, and a process that runs more than one command (the
    test suite, a host calling _main) must not carry it into the next."""
    griot.REPAIRING_SETTINGS = repairs
    try:
        yield
    finally:
        griot.REPAIRING_SETTINGS = False


def _main_with_errors_explained(argv=None) -> int:
    try:
        return _main(argv)
    except ConfigurationError as e:
        # One line instead of a traceback: what is wrong is a setting, and the
        # message names it.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        if _is_collection_busy(e):
            print(_busy_message(e, sys.argv[1:] if argv is None else list(argv)), file=sys.stderr)
            return 1
        if isinstance(e, griot.GriotError):
            # An error griot raised on purpose (a missing credential, the
            # spend ceiling, a filter that cannot match) carries its own text,
            # what to do last: that text, never a traceback (debt 102).
            print(f"Error: {e}", file=sys.stderr)
            return 1
        raise


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Before everything, and as an environment variable: the notices that
    # matter are printed while common.py is imported, and the processes griot
    # starts (an index run's children) inherit it. Only ever set here, never
    # cleared: GRIOT_QUIET=1 in the environment is the same switch.
    argv, quiet_asked = _extract_switch(argv, "--quiet", "-q")
    if quiet_asked:
        os.environ["GRIOT_QUIET"] = "1"
        griot.QUIET_FROM_COMMAND_LINE = True
    # What the REAL environment says, before --profile below writes the same
    # variable and before .env is loaded: `profiles use` needs to tell an
    # exported variable from either of those.
    exported_profile = os.environ.get("GRIOT_EMBED_PROFILE")
    argv, profile = _extract_profile_override(argv)
    if profile is not None:
        os.environ["GRIOT_EMBED_PROFILE"] = profile
        griot.PROFILE_FROM_COMMAND_LINE = profile
    argv, chat_profile = _extract_chat_profile_override(argv)
    if chat_profile is not None:
        os.environ["GRIOT_CHAT_PROFILE"] = chat_profile
    argv, sources = _extract_sources_override(argv)
    global _command
    _command = argv[0] if argv and not argv[0].startswith("-") else None

    # [real finding] argparse.REMAINDER combined with
    # subparsers has a long-standing CPython bug (reproduced here on
    # 3.14.6, and documented as affecting earlier versions too): when a
    # subparser's ONLY positional is a bare REMAINDER (no other positional
    # consumed first, exactly the shape every passthrough subparser below
    # has), the very FIRST forwarded token being option-like (`--json`,
    # `--port`, ...) gets rejected as "unrecognized arguments" by the
    # TOP-LEVEL parser instead of ever reaching the subparser's REMAINDER.
    # Concretely: `griot stats --json` — documented in this project's own
    # README — was silently broken before this fix, and so would be any
    # subcommand taking a flag as its first argument.
    # `index`/`search`/`profiles` are NOT affected: each
    # consumes a real positional (source/query/action) before any
    # REMAINDER, which sidesteps the bug (confirmed empirically both ways).
    #
    # Fix: bypass argparse's subparser dispatch ENTIRELY for passthrough
    # commands — cli.py's only job for one of these is picking the right
    # module and forwarding argv raw; real validation already happens in
    # that module's own argparse (every passthrough subparser below is
    # declared add_help=False for exactly this reason). This preserves
    # `griot <passthrough> --help` showing the REAL module's help (the
    # module's own parser sees --help directly, same as the module
    # docstring already promises) and keeps index/search/profiles on the
    # unchanged, strict build_parser()/parse_args() path.
    if argv and argv[0] == "doctor":
        # Before the configuration is read, like `config`: the check that
        # matters most is the one for a file griot cannot start with.
        from griot import doctor
        return doctor.main(argv[1:])
    if argv and argv[0] in _MODULES:
        if argv[0] == "config":
            # Before the configuration is read: see the function. It is what
            # lets `griot config set` repair a file griot cannot start with.
            from griot import config
            if len(argv) == 1 or {"-h", "--help"} & set(argv[1:]):
                # The help names the settings, and it is what the error for
                # a broken file points at: it must not need that file.
                return config.main(argv[1:])
            config.before_configuration_loads(argv[1:])
        if argv[0] == "mcp":
            # Before the configuration is read (see the flag): a server does
            # not take its environment at its word.
            griot.ENVIRONMENT_ONLY_NARROWS = True
        with _repairing(argv[0] == "config" and argv[1:2] in (["set"], ["unset"])):
            from griot import common
        common.ensure_env_template()
        return _run_module(_MODULES[argv[0]], argv[1:])

    args = build_parser().parse_args(argv)
    args.sources_override = sources  # only used by `index all` — ignored by the other commands
    # Lazy on purpose (see module docstring): --help/--version exit inside
    # parse_args() above and never reach here, so they stay cheap. Every real
    # subcommand imports common.py transitively a few lines later anyway —
    # this doesn't add new cost, just moves the config-dir template creation
    # ahead of it (closest thing to "on install" a pip package can hook into).
    if args.func is _cmd_profiles_use:
        # The profile being chosen is put in force for this process before
        # common is imported, so that a file naming a profile that no
        # longer exists cannot stop the one command that repairs it (the
        # profile is validated when common loads).
        if profile is not None:
            print("Error: --profile picks a profile for one run, and `profiles use` sets the one that stays: "
                  "give only the name to `profiles use`.", file=sys.stderr)
            return 2
        args.environment_profile = exported_profile
        os.environ["GRIOT_EMBED_PROFILE"] = args.profile
        try:
            with _repairing(True):
                from griot import common  # noqa: F401 — loaded here to catch the one error below
        except UnknownEmbedProfile as e:
            if e.name != args.profile:
                raise
            # The name came from the command line, not from a setting: say so.
            print(f"Error: {unknown_profile_sentence(args.profile, e.reason, e.options)}", file=sys.stderr)
            return 2
    from griot import common
    common.ensure_env_template()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
