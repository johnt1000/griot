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
    parser.add_argument("--mode", choices=common.SEARCH_MODES, default="vector",
                        help="How the context is searched: vector (by meaning, the default), keyword (by the exact "
                             "words: an identifier, an error code, a hash) or hybrid (both)")
    args = parser.parse_args(argv)

    start_time = time.time()
    try:
        answer, results = ask(args.question, model=args.model, limit=args.limit, mode=args.mode)
    except common.SearchFilterError as e:
        # Raised before the chat model is called: nothing was paid for.
        print(f"Error: {e}", file=sys.stderr)
        return 2
    elapsed = time.time() - start_time
    print(answer)

    if args.show_sources:
        print("\n--- sources ---")
        for r in results:
            payload = r.payload or {}
            print(f"- {common.shown(source_label(payload))} (score={r.score:.3f})")

    common.log_query(
        # [M1] GRIOT_LOG_QUESTIONS=false omits the question text from the
        # persistent log (metrics keep being recorded) — questions about
        # work repos are frequently sensitive.
        question=args.question if common.log_questions_enabled() else "<omitted: GRIOT_LOG_QUESTIONS=false>",
        # Counterpart to mcp_server._log_search()'s via="mcp": both surfaces
        # write to the same table, so each has to say which one it was or
        # the two become indistinguishable in analysis.
        via="cli",
        model=args.model or common.ACTIVE_CHAT_PROFILE["model"],
        chat_profile=common.ACTIVE_CHAT_PROFILE_NAME, limit=args.limit,
        # Scores and results of the modes are not comparable: griot stats
        # tells them apart by this.
        mode=args.mode,
        num_sources=len(results), duration_seconds=round(elapsed, 2),
        sources=[source_label(r.payload or {}) for r in results],
        # [real finding] without this, griot stats never saw chat spend —
        # only indexing wrote spend_today_usd, but it's the SAME circuit
        # breaker for both (shared .spend_state.json).
        spend_today_usd=common.get_spend_today(),
    )


if __name__ == "__main__":
    main()
