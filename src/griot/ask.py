import argparse
import sys
import time

from griot import common
from griot.cli import PROFILE_FLAG, at_least_one, chat_profile_flag, show_flags_read_by_griot

DEFAULT_LIMIT = 5


# Defined in common.py, where search uses it too; kept under this name
# because every caller says ask.source_label.
source_label = common.source_label


def build_context(results: list) -> str:
    parts = []
    for r in results:
        payload = r.payload or {}
        parts.append(f"[{source_label(payload)}]\n{common.stored_text(payload)}")
    return "\n\n---\n\n".join(parts)


def ask(question: str, model: str | None, limit: int, mode: str = "vector", *,
        repos: list[str] | None = None, source_types: list[str] | None = None) -> tuple[str | None, list]:
    """`mode` is the one that runs: main() resolves the default first
    (common.search_mode_for), because it logs and shows the mode that ran.

    repos / source_types narrow the search as they narrow `griot search`:
    the search refuses a value that cannot match (common.search_filter)
    before anything is embedded or the chat model is called, so a wrong name
    costs nothing.

    The answer is None when the search found nothing: the chat model, which
    can be paid, is not called over an empty context. Values that each exist
    can still match nothing together (a repository with no commits indexed,
    asked about commits), and an empty index matches nothing at all."""
    results = common.search(question, limit, diverse=True, mode=mode, repos=repos, source_types=source_types)
    if not results:
        return None, results
    context = build_context(results)

    prompt = (
        f"Context (code, commits, branches, tags, merge requests, releases and issues from the repositories):\n\n"
        f"{context}\n\n---\n\nQuestion: {question}"
    )
    answer = common.chat_completion(prompt, model=model)
    return answer, results


def _narrowed_to(repos: list[str] | None, source_types: list[str] | None) -> str:
    """The filters of a search in words, or "" when nothing narrowed it.
    The names come from the person's flags: shown as search shows names."""
    parts = []
    if repos:
        parts.append(f"{'repository' if len(repos) == 1 else 'repositories'} "
                     f"{', '.join(common.shown(r) for r in repos)}")
    if source_types:
        parts.append(f"{'source type' if len(source_types) == 1 else 'source types'} "
                     f"{', '.join(common.shown(k) for k in source_types)}")
    return " and ".join(parts)


def nothing_found_reason(repos: list[str] | None, source_types: list[str] | None) -> str:
    """Why a search may have found nothing, in one sentence, or "" when
    no filter narrowed it. Shared by `griot ask`, `griot search` and the
    griot_search tool, so the three say it the same way.

    Each filter value matched something indexed (search_filter refuses one
    that cannot), so without this sentence "No results." reads as "nothing
    about this anywhere" when the filters together are what left it empty."""
    narrowed = _narrowed_to(repos, source_types)
    if not narrowed:
        return ""
    return (f"The search was narrowed to {narrowed}: nothing indexed there matches it. "
            f"Without these filters it covers every repository and kind of source.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="griot ask",
        description="RAG question over the indexed repos (list in <config_dir>/repos.json), answered by the "
                    "active chat provider (GRIOT_CHAT_PROFILE, or --chat-profile below).",
    )
    show_flags_read_by_griot(parser, chat_profile_flag(common.CHAT_PROFILES), PROFILE_FLAG)
    parser.add_argument("question", help="Natural language question")
    parser.add_argument("--model", default=None, help=f"Model to use within the active chat profile (profile default: {common.ACTIVE_CHAT_PROFILE['model']})")
    # The type `griot search` takes: 0 or a negative limit is refused the
    # same way, with the same message.
    parser.add_argument("--limit", type=at_least_one, default=DEFAULT_LIMIT, help="How many chunks to search for (default: %(default)s)")
    parser.add_argument("--show-sources", action="store_true", help="List the sources used as context")
    # The same flags as `griot search` (cli.py), so a question can be asked
    # of exactly the context a search showed.
    parser.add_argument("--repo", action="append", metavar="NAME",
                        help="Only this repository, by its directory name (repeat for several). "
                             "One with nothing indexed is an error, not an empty result.")
    parser.add_argument("--source-type", action="append", metavar="KIND",
                        help=f"Only this kind of source (repeat for several): {', '.join(common.SOURCE_TYPES)}")
    # None, not "hybrid": see the same flag of `griot search` (cli.py).
    parser.add_argument("--mode", choices=common.SEARCH_MODES, default=None,
                        help="How the context is searched: hybrid (by meaning and by the exact words, the default; "
                             "vector on an index built before keyword search), vector (by meaning) or keyword (by "
                             "the exact words: a commit hash, an error code, where a name is used)")
    args = parser.parse_args(argv)

    start_time = time.time()
    mode, note = common.search_mode_for(args.question, args.mode)
    try:
        answer, results = ask(args.question, model=args.model, limit=args.limit, mode=mode,
                              repos=args.repo, source_types=args.source_type)
    except common.SearchFilterError as e:
        # Raised before the chat model is called: nothing was paid for.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    elapsed = time.time() - start_time
    if answer is None:
        # What `griot search` prints for the same search, and its status (0):
        # nothing found is a result, not an error.
        print("No results.")
        reason = nothing_found_reason(args.repo, args.source_type)
        if reason:
            print(reason)
    else:
        print(answer)
    if note:
        # stderr: the answer on stdout stays the answer alone.
        print(note, file=sys.stderr)

    if args.show_sources:
        print("\n--- sources ---")
        print(f"Mode: {mode}")
        for r in results:
            payload = r.payload or {}
            print(f"- {common.shown(source_label(payload))} (score={r.score:.3f})")

    common.log_query(
        # [M1] GRIOT_LOG_QUESTIONS=false omits the question text from the
        # persistent log (metrics keep being recorded) — questions about
        # work repos are frequently sensitive.
        question=common.logged_question(args.question),
        # Counterpart to mcp_server._log_search()'s via="mcp": both surfaces
        # write to the same table, so each has to say which one it was or
        # the two become indistinguishable in analysis.
        via="cli",
        # The model that answered, or None when the search found nothing and
        # no chat call was made: a row naming a model reads as an answer it
        # gave (and as a call that may have been paid for).
        model=(args.model or common.ACTIVE_CHAT_PROFILE["model"]) if answer is not None else None,
        chat_profile=common.ACTIVE_CHAT_PROFILE_NAME if answer is not None else None,
        limit=args.limit,
        # What the search was narrowed to, or None, as griot_search logs it:
        # `griot golden-set review` must not make a case (checked over every
        # repository) from a question asked of some of them.
        repos=args.repo, source_types=args.source_type,
        # Scores and results of the modes are not comparable: griot stats
        # tells them apart by this: the mode that ran, not the one asked for.
        mode=mode,
        num_sources=len(results), duration_seconds=round(elapsed, 2),
        sources=[source_label(r.payload or {}) for r in results],
        # What `griot golden-set review` needs to turn this question into a
        # case: the labels above are for reading only.
        results=common.logged_results(results),
        # Where each result stood in the two rankings of a hybrid search
        # (see mcp_server._log_search).
        **common.logged_ranks(results),
        # [real finding] without this, griot stats never saw chat spend —
        # only indexing wrote spend_today_usd, but it's the SAME circuit
        # breaker for both (shared .spend_state.json).
        spend_today_usd=common.get_spend_today(),
    )


if __name__ == "__main__":
    main()
