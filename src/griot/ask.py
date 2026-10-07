import argparse
import sys
import time

from griot import common

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


def ask(question: str, model: str | None, limit: int, mode: str = "vector") -> tuple[str, list]:
    """`mode` is the one that runs: main() resolves the default first
    (common.search_mode_for), because it logs and shows the mode that ran."""
    results = common.search(question, limit, diverse=True, mode=mode)
    context = build_context(results)

    prompt = (
        f"Context (code, commits, branches, tags, merge requests, releases and issues from the repositories):\n\n"
        f"{context}\n\n---\n\nQuestion: {question}"
    )
    answer = common.chat_completion(prompt, model=model)
    return answer, results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="RAG question over the indexed repos (list in <config_dir>/repos.json). "
                    "Chat provider switchable via --chat-profile <gemini|openai|deepseek|groq> "
                    "(or GRIOT_CHAT_PROFILE) — extracted by the CLI before this parser, same "
                    "mechanism as --profile for embedding."
    )
    parser.add_argument("question", help="Natural language question")
    parser.add_argument("--model", default=None, help=f"Model to use within the active chat profile (profile default: {common.ACTIVE_CHAT_PROFILE['model']})")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="How many chunks to search for (default: %(default)s)")
    parser.add_argument("--show-sources", action="store_true", help="List the sources used as context")
    # None, not "hybrid": see the same flag of `griot search` (cli.py).
    parser.add_argument("--mode", choices=common.SEARCH_MODES, default=None,
                        help="How the context is searched: hybrid (by meaning and by the exact words, the default; "
                             "vector on an index built before keyword search), vector (by meaning) or keyword (by "
                             "the exact words: a commit hash, an error code, where a name is used)")
    args = parser.parse_args(argv)

    start_time = time.time()
    mode, note = common.search_mode_for(args.question, args.mode)
    try:
        answer, results = ask(args.question, model=args.model, limit=args.limit, mode=mode)
    except common.SearchFilterError as e:
        # Raised before the chat model is called: nothing was paid for.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    elapsed = time.time() - start_time
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
        model=args.model or common.ACTIVE_CHAT_PROFILE["model"],
        chat_profile=common.ACTIVE_CHAT_PROFILE_NAME, limit=args.limit,
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
