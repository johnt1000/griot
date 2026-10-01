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

- `GRIOT_MCP_CONCURRENCY_MODE=multi` (the default): until the server has
  gone `GRIOT_MCP_IDLE_RELEASE_SECONDS` (default 30) without a tool call. It
  then releases the collection on its own and reopens it, in roughly 90 ms,
  on the next call.
- `GRIOT_MCP_CONCURRENCY_MODE=single`: for the server's whole life.

The mode comes from griot's own configuration (`<config>/.env`), unless the
server was registered with an `env` of its own, which wins. Absent means
`multi`. An older `.env` (generated before `multi` became the default) can
carry an active `GRIOT_MCP_CONCURRENCY_MODE=single` line that pins the old
behavior. Check only that setting, and never print the whole `.env`, which
holds credentials:

```bash
griot config get mcp-concurrency   # what griot's own configuration says
claude mcp get griot               # how the server is registered, and any env of its own
```

A server registered by `griot assist install` has no `env`; a project that
defines its own `griot` entry in `.mcp.json` may have one.

So a `griot index ...` run through the shell **collides with your own MCP
server**: until the idle window has passed in `multi` mode, until the
session ends in `single` mode. The CLI waits about 12 seconds in `multi` mode,
then says so and names the holder:

```
Another griot process holds the collection 'codebase__<profile>'. Holder: PID 4242 (Python -m griot.mcp_server, started 2026-09-24 01:00).
```

That message is the whole diagnosis: it says who holds the collection and what
to do. Read it before doing anything else. The engine's raw error
(`failed to open WAL ... Kind(WouldBlock)`) only shows up in older run records
and in `griot.log`.

`griot index ... --dry-run` collides the same way: it reads the collection
to compare content hashes. A server in *another* session, in any project, on the same embedding
profile holds it the same way.

## Step 1 — pick the indexing path

Check which griot tools you have (they appear as `mcp__griot__*`):

| Situation | Path |
|---|---|
| `griot_index_repo` is available | **Path A** (MCP). It releases this server's handle before starting, so it works in every case this server controls. |
| griot MCP attached, `griot_index_repo` missing, `multi` mode (the default) | Path B, once no griot MCP tool has been called for the idle window (default 30s). |
| griot MCP attached, `griot_index_repo` missing, `single` mode | Path B works only if no griot tool has been used yet this session. Otherwise it **will fail**: tell the user and offer the fixes below, and do not retry. |
| No griot MCP server attached | **Path B** (CLI). |

Whenever you take Path B with a griot MCP server attached, call no griot MCP
tool until the run finishes. A tool call would reopen the collection and
break the run.

The fixes are the user's decision; offer them, don't make them. Each is a
command the user runs in a terminal, followed by a session restart so the
server starts with the new setting:

- `griot config set mcp-index true` enables `griot_index_repo` (Path A). It
  asks for confirmation at an interactive terminal and has no flag that
  answers, so it is the user's step. Indexing through MCP is off by default
  because on a paid profile it spends money; enabled, it still only indexes
  paths registered with `griot repos add` (or under `mcp-index-roots`), and
  asks the user to confirm each run.
- If the mode is `single`: `griot config unset mcp-concurrency` (the
  default is `multi`). `multi` lets an idle server release the collection,
  so Path B and other sessions on the same profile work after the idle
  window. The cost is a short reopen on the first tool call after an idle
  stretch.

Only when the project defines its own `griot` server with an `env` block do
those settings have to change there instead: a variable in the server's own
`env` wins over griot's configuration. Do not put `GRIOT_EMBED_PROFILE`
there: it would silently override `griot profiles use`.

## Step 2 — before spending anything

1. Confirm the repo is registered: `griot_repos_list` (usable means
   `exists` and `is_git` are both true) or `griot repos list` (its `✓` only
   says the directory exists, not that it is a git repo). `--repo` takes the
   repo's **directory name**, not the path.
2. Know the active profile: `griot profiles list` (or `griot_index_status`
   → `embed_profile`). `openai-small` and `gemini` are paid.
3. On a paid profile, get the size first. With the MCP server attached,
   `griot_index_preview(path="/abs/path/to/repo")` returns, per source, how
   many chunks would be embedded, how many are up to date and how many stale
   points would be removed (`held_back` when a plain run would leave them:
   read `notes`), plus an estimated cost; it embeds and removes nothing, and
   releases the server's handle itself. From a shell,
   `griot index all --repo <name> --dry-run` does the same (free, but it
   opens the collection too, so the Step 1 rule applies). Tell the user the
   numbers before the real run. Re-indexing an unchanged repo embeds almost
   nothing (content-hash reuse).

## Path A — `griot_index_repo`

```
griot_index_repo(path="/abs/path/to/repo")                       # code, commits, tags, branches
griot_index_repo(path="/abs/path/to/repo", sources=["commits"])  # a subset
```

- `platform` (PRs/issues/releases) is **not** in the default sources. Pass
  it explicitly, and only if its token is configured (`griot_auth_guidance`).
- The tool asks the user to confirm. Where the client can show that dialog,
  the user's answer is the only thing that counts: `confirm=true` is ignored,
  and a "no" is final. `confirm=true` only works in a client that cannot ask;
  never pass it unless the user explicitly told you to.
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

**`Another griot process holds the collection ...`** (or, in a run record
or a log, `failed to open WAL ... WouldBlock`, or `Could not open collection
... another griot process still has it open` from an MCP tool): another
process has the collection open. The CLI message already names the holder
(`Holder: PID ...`). If it names none, `lsof` was unavailable or found
nothing, so look yourself:

```bash
lsof +D ~/.local/share/griot/qdrant_data/codebase__<profile> | awk 'NR>1{print $2}' | sort -u
ps -o pid,lstart,command -p <pid>
```

- It is `griot mcp` from this session: use Path A; in `multi` mode (the
  default), wait out the idle window without calling griot tools; in
  `single` mode, offer the fixes above.
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
- Retry a run that failed because the collection is held without first
  changing what holds it. The retry fails the same way and adds another dead
  run to `griot stats`. (A `--dry-run` leaves no record either way.)
- Pass `confirm=true` on the user's behalf, or call a tool again after the
  user declined it.
- Try to get around a command that wants a terminal (`griot repos add`,
  `griot profiles delete`): hand the command to the user instead.
