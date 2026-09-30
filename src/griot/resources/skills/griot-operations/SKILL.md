---
name: griot-operations
description: Runbook for OPERATING griot from inside a Claude Code/opencode session — indexing or re-indexing a registered repo, following a run to completion, and recovering when a run fails with "failed to open WAL ... WouldBlock" or "A griot process is already running". Covers the one rule that decides whether indexing can work at all in a session that has griot's MCP server attached, and which of the two indexing paths (the griot_index_repo tool or the griot CLI via the shell) to use. Use whenever you are about to index or re-index, or an index run just failed. Not for choosing what or how to index (griot-indexing), for first-time setup (griot-onboarding), or for search/ask usage (griot-workflows).
---

# griot operations

## The rule that decides everything: one process per collection

The vector store is embedded — no server. Each embedding profile's
collection (`codebase__<profile>`) can be open in **one OS process at a
time**. Every indexing run, every `--dry-run`, and every search opens it.

A `griot mcp` server attached to this session opens the collection the first
time any griot tool that reads it is called (`griot_search`,
`griot_index_status`, `griot_quality_check`) and then **keeps it open**:

- `GRIOT_MCP_CONCURRENCY_MODE=single` (the default): for the server's whole life.
- `GRIOT_MCP_CONCURRENCY_MODE=multi`: until the server has gone
  `GRIOT_MCP_IDLE_RELEASE_SECONDS` (default 30) without a tool call. It then
  releases the collection on its own, and reopens it on the next call.

The mode is in the `env` of the project's `.mcp.json` `griot` entry. Absent
means `single`.

So in `single` mode, once you have used any griot MCP tool in this session,
**`griot index ...` run through the shell collides with your own MCP
server**. In `multi` mode it collides until the idle window has passed. The
error:

```
Service runtime error: failed to open WAL .../qdrant_data/codebase__<profile>/wal: Can't init WAL: Kind(WouldBlock)
```

`griot index ... --dry-run` collides the same way: it reads the collection
to compare content hashes. A server in *another* session — another project
whose `.mcp.json` uses the same `GRIOT_EMBED_PROFILE` — holds it the same way.

## Step 1 — pick the indexing path

Check which griot tools you have (they appear as `mcp__griot__*`):

| Situation | Path |
|---|---|
| `griot_index_repo` is available | **Path A** (MCP). It releases this server's handle before starting, so it works in every case this server controls. |
| griot MCP attached, `griot_index_repo` missing, `multi` mode | Path B, once no griot MCP tool has been called for the idle window (default 30s). |
| griot MCP attached, `griot_index_repo` missing, `single` mode, no griot tool used yet this session | Path B works. |
| griot MCP attached, `griot_index_repo` missing, `single` mode, a griot tool already used | Path B **will fail**. Tell the user and offer the fixes below. Do not retry. |
| No griot MCP server attached | **Path B** (CLI). |

Whenever you take Path B with a griot MCP server attached, call no griot MCP
tool until the run finishes. A tool call would reopen the collection and
break the run.

The fixes are the user's decision; offer them, don't make them. Both go in
the `env` of the project's `.mcp.json` `griot` entry, followed by a session
restart so the server picks up the new environment:

```json
"env": {
  "GRIOT_EMBED_PROFILE": "<profile>",
  "GRIOT_MCP_ENABLE_INDEX": "true",
  "GRIOT_MCP_CONCURRENCY_MODE": "multi"
}
```

- `GRIOT_MCP_ENABLE_INDEX=true` enables `griot_index_repo` (Path A). It is
  off by default because on a paid profile it spends money. It still only
  indexes paths registered with `griot repos add` (or under
  `GRIOT_MCP_INDEX_ROOTS`), and asks the user to confirm each run.
- `GRIOT_MCP_CONCURRENCY_MODE=multi` lets an idle server release the
  collection, so Path B and other sessions on the same profile work after
  the idle window. The cost is a short reopen on the next tool call.

## Step 2 — before spending anything

1. Confirm the repo is registered: `griot_repos_list` (usable means
   `exists` and `is_git` are both true) or `griot repos list` (its `✓` only
   says the directory exists, not that it is a git repo). `--repo` takes the
   repo's **directory name**, not the path.
2. Know the active profile: `griot profiles list` (or `griot_index_status`
   → `embed_profile`). `openai-small` and `gemini` are paid.
3. On a paid profile, get the size first:
   `griot index all --repo <name> --dry-run` (free, but it opens the
   collection too, so the Step 1 rule applies). Tell the user how many
   chunks will be embedded before the real run. Re-indexing an unchanged
   repo embeds almost nothing (content-hash reuse).

## Path A — `griot_index_repo`

```
griot_index_repo(path="/abs/path/to/repo")                       # code, commits, tags, branches
griot_index_repo(path="/abs/path/to/repo", sources=["commits"])  # a subset
```

- `platform` (PRs/issues/releases) is **not** in the default sources. Pass
  it explicitly, and only if its token is configured (`griot_auth_guidance`).
- The tool asks the user to confirm. Never pass `confirm=true` yourself
  unless the user explicitly told you to skip confirmation.
- It returns right away with `started`, `pid` or a `reason`. A `reason` is
  final: an unregistered path, an invalid repo, or a run already in progress.
  Relay it to the user; do not retry.
- Follow the run with `griot_index_status`: `running` / `pid` while it
  works; when `running` is false, read `last_indexed`. If
  `last_indexed.error` is set, the run died. `indexed`/`skipped`/`failed`
  are then null on purpose, not zero. The run's full output goes to
  `griot_index.log` in griot's log directory
  (default `~/.local/share/griot/logs/`).
- **Don't call `griot_search` while it runs.** The indexing process holds
  the collection, so a search fails until it finishes. `points_count` in
  `griot_index_status` reads null during the run. That means the collection
  is busy, not empty.

## Path B — the CLI through the shell

```bash
griot index all --repo <name>                       # code, commits, tags, branches, platform
griot index all --repo <name> --sources code,commits
```

- A first run, or any run on a large repo, outlives a foreground shell
  timeout. Run it in the background (in Claude Code, Bash with
  `run_in_background`) and read the result when it finishes. Never wrap it
  in `timeout`: a killed run records nothing.
- `griot index all` stops at the first source that fails and names it.
  `platform` runs last and is the only one that needs network and a token.
  If only `platform` failed, the other sources are already indexed.
- Confirm with `griot stats`. The `Indexing:` line shows embedded vs
  skipped, and a line `N runs died before counting anything — last: <error>`
  tells you a run failed and why.

## When a run fails

**`failed to open WAL ... WouldBlock`**: another process has the
collection open. Find it:

```bash
lsof +D ~/.local/share/griot/qdrant_data/codebase__<profile> | awk 'NR>1{print $2}' | sort -u
ps -o pid,lstart,command -p <pid>
```

- It is `griot mcp` from this session: use Path A; in `multi` mode, wait
  out the idle window without calling griot tools; otherwise offer the
  `.mcp.json` fixes above.
- It is `griot mcp` from another session or an old leftover (check the start
  time): tell the user which one it is. Closing that session releases it.
  **Do not kill it yourself.** It belongs to a session you don't control.
- It is another `griot index`: wait for it to finish.

**`A griot process is already running (PID N)`**: an indexing run holds
griot's lock. Wait, following it with `griot_index_status` or `ps -p N`.
Only if that PID is gone (the process died from `kill -9` or out-of-memory)
may the lock file named in the message be removed, and then only with the
user's agreement.

**`griot stats` says `point count unavailable (collection in use)`**: this
is not an error. Something holds the collection right now (see above).

**Spend circuit breaker refused a paid call, or a missing credential**: see
griot-troubleshooting. Credentials are always set by the user in their own
terminal (`griot auth set <provider>`), never through you.

## Never

- Kill a `griot mcp` or `griot index` process you did not start.
- Delete a lock file whose PID is still alive, or delete anything under
  `qdrant_data/`.
- Retry a run that failed with `WouldBlock` without first changing what
  holds the collection. The retry fails the same way and adds another dead
  run to `griot stats`.
- Pass `confirm=true` on the user's behalf, or run `griot profiles delete`
  unasked.
