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
import importlib
import os
import sys

from griot import __version__

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


def _busy_message(exc) -> str:
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
                f"(or when its session ends, if it runs in single mode). Wait and run the command again, close that "
                f"session, or, from inside a Claude Code session, index through the griot_index_repo tool.")
    elif "index" in roles:
        tail = "That is an indexing run. Wait for it to finish (`griot stats` shows the last run), then run the command again."
    else:
        tail = "Wait for it to finish or close it, then run the command again."
    return f"{head}\n{tail}"


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


def _cmd_index(args) -> int:
    rest = list(args.rest)
    if args.source != "all":
        return _run_index_source(args.source, rest)

    # `griot index all`: runs the sources in sequence within a single process.
    # `--sources` (resolved) filters which of the INDEX_SOURCES
    # run — this is what lets `griot_index_repo` (MCP) hold the lock for the
    # whole call by invoking this once, instead of once per source.
    sources = args.sources_override if args.sources_override is not None else INDEX_SOURCES
    unknown = [s for s in sources if s not in INDEX_SOURCES]
    if unknown:
        print(f"Error: unknown source(s) in --sources: {', '.join(unknown)}. Options: {', '.join(INDEX_SOURCES)}", file=sys.stderr)
        return 2

    # Stops at the FIRST error, saying which source failed — continuing to
    # index with a broken source would mask the problem, and the next run
    # would just re-pay the cost of discovering the error.
    for source in sources:
        print(f"=== griot index {source} ===")
        try:
            rc = _run_index_source(source, rest)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            if _is_collection_busy(e):
                raise  # main() explains it once, with who holds the collection
            print(f"Error: 'griot index {source}' failed: {e}", file=sys.stderr)
            return 1
        if rc != 0:
            print(f"Error: 'griot index {source}' failed (exit {rc}) — pipeline stopped.", file=sys.stderr)
            return rc
    print("=== griot index all: all sources completed ===")
    return 0


def _cmd_search(args) -> int:
    """Raw search on the vector store: embeds the query and prints the hits
    with source label + score. NEVER calls chat_completion — this is the
    free/local path (aside from embedding the query under the direct
    profile); synthesis with an LLM is `griot ask`."""
    from griot import ask, common  # lazy: only imports qdrant/fastembed here

    results = common.search(args.query, limit=args.limit)
    if not results:
        print("No results.")
        return 0
    for r in results:
        payload = r.payload or {}
        print(f"[{r.score:.3f}] {common.shown(ask.source_label(payload))}")
        content = common.stored_text(payload).strip()
        if content:
            # one-line preview — the full result is ask/MCP's job
            first_line = content.splitlines()[0]
            print(f"        {common.printable(first_line[:200])}")
    return 0


_TIER_MIN_GB = {"light": 8, "medium": 16, "heavy": 32}


def _cmd_profiles_list(args) -> int:
    """`griot profiles list`: detects the machine's real
    RAM via psutil and classifies each local profile by ram_tier.

    "Fits comfortably" criterion: detected RAM >= the tier's nominal floor
    (light=8GB/medium=16GB/heavy=32GB), not a percentage computed on top of
    rss_estimate_mb — an earlier version tried "a model alone shouldn't
    exceed ~25-30% of total RAM", which matched section 9.3's TEXT, but
    diverges from that same section's own mockup (16GB: bge-m3/bge-large-en,
    heavy tier, shown as "might be tight" even though it fits under that
    percentage). The nominal floor per tier reproduces the mockup exactly
    and is the source of truth when the plan's two criteria conflict with
    each other (review finding)."""
    import psutil

    from griot import common

    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    print(f"\nYour machine: {total_gb:.1f} GB total RAM (detected via psutil)\n")

    tier_labels = {"light": "light (8GB)", "medium": "medium (16GB)", "heavy": "heavy (32GB+)"}

    by_tier = {}
    paid = []
    for name, profile in common.EMBED_PROFILES.items():
        if profile["backend"] == "local":
            by_tier.setdefault(profile.get("ram_tier"), []).append((name, profile))
        elif profile["backend"] == "direct":
            paid.append((name, profile))

    for tier in ["light", "medium", "heavy"]:
        entries = by_tier.get(tier)
        if not entries:
            continue
        print(f"  LOCAL — {tier_labels[tier]}")
        fits = total_gb >= _TIER_MIN_GB[tier]
        label = "✓ fits comfortably" if fits else f"⚠ might be tight (you have {total_gb:.1f}GB)"
        for name, profile in entries:
            rss_max = profile["rss_estimate_mb"][1]
            active = "   [ACTIVE]" if name == common.ACTIVE_PROFILE_NAME else ""
            print(f"    {name:<14}{profile['dim']}d   ~{rss_max}MB   {label}{active}")
        print()

    print("  PAID (requires API key)")
    from griot import auth  # lazy: only used here, avoids pulling dotenv into the rest of the CLI

    for name, profile in paid:
        # gemini uses GEMINI_TOKEN (a historical variable, with no
        # api_key_env in the profile); the openai_compatible profiles
        # (openai-small and future Voyage/remote ones) declare their own
        # api_key_env — never reuse GEMINI_TOKEN to check another
        # provider's credential. [review finding] Resolved through
        # credential_env_for_profile() rather than read off the profile dict:
        # that special case has to live in exactly one place, or a new remote
        # profile added next to gemini gets reported as free by whichever
        # caller still guesses from api_key_env.
        api_key_env = common.credential_env_for_profile(name, profile)
        provider_label = auth._provider_label(api_key_env) if api_key_env.startswith("GRIOT_") else name
        configured = "configured" if os.getenv(api_key_env) else f"missing {api_key_env} — run: griot auth set {provider_label}"
        active = "   [ACTIVE]" if name == common.ACTIVE_PROFILE_NAME else ""
        price = f"${profile['price_per_1m_tokens']:.2f}/1M"
        print(f"    {name:<14}{profile['dim']}d   {price}   {configured}{active}")

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


def check_delete_profile(profile_name: str, *, active_profile_name: str | None = None) -> str:
    """Read-only half of delete_profile(): the collection it would delete.
    Raises the same ValueError it would for an unknown profile, the active
    one, one that was never indexed, or while an indexing run holds the
    lock. Lets the CLI refuse before asking anyone to confirm. The delete
    repeats the last check itself: a run can start between the question and
    the answer."""
    from griot import common

    if profile_name not in common.EMBED_PROFILES:
        raise ValueError(f"unknown profile '{profile_name}'.")
    active = active_profile_name if active_profile_name is not None else common.ACTIVE_PROFILE_NAME
    if profile_name == active:
        raise ValueError(f"'{profile_name}' is the active profile — switch to a different one first, then delete.")
    collection = common.collection_name_for(profile_name)
    if not common.collection_exists(collection):
        raise ValueError(f"collection '{collection}' does not exist.")
    if common.index_lock_status()["running"]:
        raise ValueError("an indexing run is currently in progress — wait for it to finish, then try again.")
    return collection


def _cmd_profiles_delete(args) -> int:
    from griot import common

    try:
        check_delete_profile(args.profile)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
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
                raise SystemExit(f"{long_flag} requires a value")
            value = argv[i + 1]
            i += 2
            continue
        if arg.startswith(f"{long_flag}="):
            value = arg.split("=", 1)[1]
            if not value:
                raise SystemExit(f"{long_flag} requires a value")
            i += 1
            continue
        out.append(arg)
        i += 1
    return out, value


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
    )
    parser.add_argument("--version", action="version", version=f"griot {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p_index = subparsers.add_parser(
        "index",
        help="Indexes one source (code|commits|tags|branches|platform) or all (all)",
        description="Indexes repositories into the vector store. Extra flags (--repo, --dry-run, ...) "
                    "are forwarded to the source's indexer — use `griot index <source> --help`.",
    )
    p_index.add_argument("source", choices=INDEX_SOURCES + ["all"], metavar="source",
                         help=f"One of: {', '.join(INDEX_SOURCES + ['all'])}")
    p_index.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_index.set_defaults(func=_cmd_index)

    p_search = subparsers.add_parser(
        "search",
        help="Raw vector search (no chat/LLM synthesis)",
        description="Searches the vector store and prints sources + score. Never calls any chat model.",
    )
    p_search.add_argument("query", help="Natural-language query")
    p_search.add_argument("--limit", type=int, default=5, help="How many results (default: %(default)s)")
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
        ("golden-set", "Manages the curated golden set for quality-check (suggest/add/list/remove)"),
        ("assist", "Installs griot's Claude Code/opencode skills and agents for onboarding, indexing and workflow help (install)"),
        ("audit", "Lists where the index holds credential-looking values (locations only, never the values)"),
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
    profiles_sub = p_profiles.add_subparsers(dest="profiles_command", metavar="<action>", required=True)
    profiles_sub.add_parser("list", help="Lists available profiles with real RAM detection")
    p_profiles_delete = profiles_sub.add_parser(
        "delete", help="Permanently deletes a profile's on-disk collection (frees disk space)"
    )
    p_profiles_delete.add_argument("profile", help="Profile name (see `griot profiles list`)")
    p_profiles_delete.set_defaults(func=_cmd_profiles_delete)
    p_profiles.set_defaults(func=_cmd_profiles_list)

    return parser


def main(argv=None) -> int:
    try:
        return _main(argv)
    except Exception as e:
        if not _is_collection_busy(e):
            raise
        print(_busy_message(e), file=sys.stderr)
        return 1


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    argv, profile = _extract_profile_override(argv)
    if profile is not None:
        os.environ["GRIOT_EMBED_PROFILE"] = profile
    argv, chat_profile = _extract_chat_profile_override(argv)
    if chat_profile is not None:
        os.environ["GRIOT_CHAT_PROFILE"] = chat_profile
    argv, sources = _extract_sources_override(argv)

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
    if argv and argv[0] in _MODULES:
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
    from griot import common
    common.ensure_env_template()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
