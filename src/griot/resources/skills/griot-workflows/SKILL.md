---
name: griot-workflows
description: Day-to-day usage of an EXISTING griot index — griot search vs griot ask (search only vs paid synthesis), search by meaning vs by exact words (mode), group_by_document tradeoffs, reading source_type on results, and (when working via Claude Code with griot's MCP server attached) the existing MCP prompts for common investigations. Use once an index already exists; for first-time setup see griot-onboarding, for indexing decisions see griot-indexing.
---

# griot workflows

## `griot search` vs `griot ask`

`griot search` is **search only** — free (aside from embedding the
query itself, which costs nothing on the default local profile), no LLM
call:

```bash
griot search "where is the retry logic for the payment API?" --limit 5
```

`griot ask` is search **plus LLM synthesis** into a written answer, and it
is a **paid path** — it calls a chat provider (default `gemini`, needing
`GEMINI_TOKEN`; switchable with `--chat-profile`/`GRIOT_CHAT_PROFILE`):

```bash
griot ask "how does authentication work in this codebase?" --show-sources
```

Reach for `search` when you (or the agent you're working through) can read
and reason over raw excerpts yourselves — this is the common case if you're
an agent with your own LLM already in the loop, since paying for a second
synthesis layer adds nothing. Reach for `ask` when a human wants a written,
cited answer directly, or when the context is too large/scattered to reason
over by hand. `griot ask --show-sources` prints every source it used, which
is worth turning on whenever you want to verify the answer against the
underlying excerpts.

If you're an agent talking to griot over MCP: **there is no `griot_ask`
MCP tool, deliberately** — you already have your own LLM, so use
`griot_search` and reason over the results yourself.

## `group_by_document`

`griot_search` (MCP tool) and the underlying search function take an
optional `group_by_document` parameter — off by default. It collapses each
document's matching chunks down to its single best-scoring chunk.

- **Off (default)** favors **depth**: repeated hits from the same file,
  commit, or PR are usually *not* redundant — they're different chunks of
  the same document each carrying different, real information. One document
  fills at most three results, so a long file cannot take every slot; for
  all of it, open the file.
- **On** favors **breadth**: the same `limit` reaches more distinct files,
  commits, and PRs, at the cost of only seeing each one's best chunk. Use
  this when you're not sure which document has the answer and want to
  survey more of them before drilling in.

On the CLI it is `griot search --group-by-document`; without it the same
three-per-document ceiling applies.

## By meaning or by the exact words: `mode`

The default, `mode` `hybrid`, ranks by meaning and by the exact words and
fuses the two rankings. Measured on two repositories, it ranked "how is X
done" questions at least as well as a search by meaning (`vector`) and
commit hashes far better; on a function or class name it ranked about like
`vector` (the right file slightly less often first, slightly more often in
the top ten). `keyword` alone put the file that defines a name first less
often than either, but it returns only chunks that mention the name, which
is what you want when you look for where it is used. `keyword` matches
the words alone (BM25), including the file path and the commit hash
stored with each chunk, whole or abbreviated, and returns only chunks that hold one of them; it embeds
nothing, so it is free on every profile. `vector` ranks by meaning alone.
Every result says which mode ran (`mode` in `griot_search`'s output, a
`Mode:` line from `griot search`). Scores are on each mode's own scale:
compare them within one mode only.

```bash
griot search "acquire_lock" --mode keyword
griot search "f96634a" --mode keyword --source-type commit
```

To find where a name is used in a repository you have open, grep is still
the better tool; `keyword` is for the ones you cannot grep (another
project, history).
On an index built before keyword search the default runs `vector` and
says so on the first default search of the session, naming
`griot index keywords` (later results only say `mode: vector`), which adds the keyword vectors
once (local, embeds nothing); an explicit `keyword` or `hybrid` is refused
there with the same command.

## Narrowing a search: `repos` and `source_types`

`griot_search` searches every registered repository and every kind of
source unless told otherwise:

- `repos` — a list of repository names: a repository is named after its
  directory, which is the `repo` field of a search result and the `name`
  that `griot_repos_list` gives. Use it when the question is about one
  project, or to compare two.
- `source_types` — a list of kinds (see the next section). `["commit",
  "merge_request"]` when the question is *why* something changed; `["code"]`
  when it is how something works today.

A repository with nothing indexed, or a kind that does not exist, is an
**error**, not an empty result: an empty list would read as "nothing was
found". Two values that both exist and match nothing together (a repository
with no pull requests indexed, say) do return an empty list. On the CLI the
same filters are `--repo NAME` and `--source-type KIND`, each repeatable,
on `griot search` and on `griot ask` alike; `ask` refuses a filter that
cannot match before it calls the chat model, so that costs nothing.

## Reading `source_type`

Every search result carries `source_type`: `code`, `commit`, `tag`,
`branch`, `merge_request`, `release`, or `issue`. This is the field worth
branching on, because the same question is answered differently depending
on which kind of source responds:

- **code** — what the implementation does *now*.
- **commit** — *when* it changed, and the commit message's own account of
  why.
- **merge_request** — what was argued *before* the change was accepted.
- **issue** — what problem *started* the change in the first place.

To get only some kinds, pass `source_types` (previous section). Each result
also carries `metadata`, what was stored with the source: `file_path` and
`chunk_index` for code; the whole `commit_hash`, `author` and `date` for a
commit; the tag, branch, pull request or issue identifier for the rest,
with a date (for a branch, the date of its last commit). That is what to
act on: open that file, show that commit, say when. A file or a commit that
is indexed in more than one place (a copied file, a fork) comes back once,
with the other places found among the best matches in `also_in`; two
different commits with the same message stay two results. One document
fills at most three results, and the search looks further down the ranking
to fill the rest, so `limit` results come back unless the index runs out
or a few very long files are all the query matches.

(`griot search` prints a labeled excerpt per hit, e.g. `commit a1b2c3d4 — my-service` or
`MR !245 (merged) — my-api`, so the source kind is visible at a glance even
from the CLI.)

## If you're working through Claude Code with griot's MCP server attached

griot ships four MCP prompts, surfaced as slash commands, that already
encode multi-step investigation strategies — don't re-derive the same
reasoning by hand when one of these already does it:

| Command | Use it for |
|---|---|
| `/mcp__griot__stats` | A read usage/spend snapshot — same report as `griot stats`. |
| `/mcp__griot__history` | Investigating a "why is this like this" question across every source type (code, commits, PRs, issues) as a cited history, oldest cause first — the multi-source strategy described above, already automated. |
| `/mcp__griot__health` | Whether the index is currently worth trusting, and which kind of failure it is if not. |
| `/mcp__griot__overview` | What's indexed here, and which registered repos are no longer usable. |

A prompt only injects text — the actual work still happens as a tool call
(`griot_search`, `griot_stats`, etc.), so invoking one never spends money or
runs anything in the background on its own.

## `griot stats` — usage and spend snapshot

```bash
griot stats               # last 30 days by default
griot stats --days 7
griot stats --all-profiles  # count every profile's collection, not only the active one's
griot stats --json        # raw JSON instead of the formatted report
```

One clock: `--days N` is today since local midnight plus the N-1 local
days before it, the same days the spend circuit breaker counts (a day the
clock changes counts whole). One scope: the counts (runs, chunks,
failures, queries, sources, quality trend) are those of the active
profile's collection, the same collection the state lines are about, and
the `Scope:` line names it; `--all-profiles` (`all_profiles` on
`griot_stats`) counts every profile, including old records that name no
collection. Spend and MCP tool calls are always every profile's: money is
spent per account, and a tool call records no collection.

It opens with what does not depend on the window: an `Attention:` block
when something needs someone (the last indexing run died, today's spend
reached the ceiling, the collection cannot be read), and how long ago the
index was last written and last searched. Then, for the window: indexing
runs (embedded/skipped/failed, reuse rate, stale points removed,
credential-looking values replaced), API spend against the daily ceiling,
query volume and latency (p50 and p90), which source types answered most
queries, and the quality-check trend. Quality says so when it was never
checked, or was checked before the index last changed; the golden set shows
its size, the result and age of its last run, and cases that expect a
repository that is not in `repos.json` (unless it was indexed with `--path`
they can only fail: `griot quality-check`, and the `griot_quality_check`
tool, check the index and say so case by case). If you're an agent, `griot_stats` (or the `stats` prompt above)
gives you the same data without shelling out.
