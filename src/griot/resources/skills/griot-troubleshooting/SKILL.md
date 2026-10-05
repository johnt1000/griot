---
name: griot-troubleshooting
description: Symptom-to-fix reference for griot failures - a locked/in-use collection, the spend circuit breaker refusing a paid call, a missing credential error, a weak/empty search result, and the "point count unavailable" message from griot stats. Use when a griot command errors out or behaves unexpectedly, not for first-time setup (griot-onboarding) or indexing strategy (griot-indexing).
---

# griot troubleshooting

Each entry below is verified against griot's actual source — the error
messages quoted are the real ones the code raises, not paraphrases.

Run `griot doctor` first: it performs every check below in one go (and a
few more: the MCP registration, which read-only tools still ask, the
variables a server would ignore), says ok / warn / FAIL per check with what
to do, and changes no setting, index or file of yours. The entries here
explain the failures it names.

## Collection locked / "another griot process still has it open"

**Symptom**: an indexing or search command says `Another griot process holds
the collection '<name>'`, usually followed by `Holder: PID ... (...)` naming
the process and what to do about it. The CLI first waits about 12 seconds
for the collection to free up (the default `multi` concurrency mode). An MCP
tool that hits the same condition reports `Could not open collection '<name>'`
followed by `after N attempts — another griot process still has it open` (it
waited) or `another griot process has it open` (it did not). Older run
records show the engine's raw `failed to open WAL ... Kind(WouldBlock)`.

**Cause**: the vector store is embedded, not a server — only one OS process
can hold a given collection's directory open at a time. If `griot mcp` is
running and has touched a collection (via `griot_search`,
`griot_index_status`, etc.), it holds that handle while it is in use and,
in the default `multi` mode, until it has been idle for
`GRIOT_MCP_IDLE_RELEASE_SECONDS` (default 30). In `single` mode it holds it
for the rest of its life. A second, separate process touching the *same*
embedding profile's collection collides with it in the meantime. Subagents/workflow-spawned agents
sharing one parent MCP connection never hit this — only a genuinely
separate session (another window, another project) does.

**Fix**, any of:

- Close the session whose `griot mcp` holds the collection.
- To index from a session that has the server attached, use
  `griot_index_repo` (the user enables it with `griot config set mcp-index
  true`, which asks at a terminal). It
  lets go of the server's handle before starting the run, once no other
  griot tool call is using the index.
- Wait out the idle window (30 seconds by default) with no griot tool
  calls, then run the command again. Nothing to configure: this is what the
  default `multi` mode does.
- If the server is in `single` mode (set in `.mcp.json`, or pinned by an
  older `.env`: `griot config get mcp-concurrency` shows it), switch it back
  with `griot config unset mcp-concurrency`. Otherwise it holds the collection
  until its session ends.

The `griot-operations` skill has the full procedure, including how to find
which process holds the collection. Sessions on *different* embedding
profiles never collide — each profile is a separate collection/directory.

## Spend circuit breaker refuses a paid call

**Symptom**: a paid embedding or chat call fails with either:

> `Local circuit breaker: today's estimated spend ($X) has already reached
> the $Y ceiling (GRIOT_SPEND_CEILING_USD). Stop and check the reason
> before continuing — if this is expected, raise the ceiling explicitly.`

or:

> `Local circuit breaker: spend in the last 5min ($X) is way above what's
> expected for sequential use (ceiling: $Y, GRIOT_SPEND_VELOCITY_CEILING_USD).
> This usually indicates two processes running at once or a retry loop out
> of control (a burst of simultaneous calls). Stop and check before
> continuing.`

**Cause**: griot enforces two independent, atomic, on-disk spend ceilings
before any call to a paid provider — a **daily** ceiling
(`GRIOT_SPEND_CEILING_USD`, default $3) and a **5-minute velocity** ceiling
(`GRIOT_SPEND_VELOCITY_CEILING_USD`, default $1) that catches a runaway
burst (a retry loop, two processes indexing the same paid profile at once)
long before the daily ceiling would. Both ceilings are shared across
indexing and `griot ask` — they draw from the same spend state.

**Fix**: figure out *why* spend jumped before doing anything else — this
breaker exists specifically to make you stop and look, not to be raised
reflexively. If the spend really is expected (a deliberate large paid
reindex, for instance), the user raises the relevant ceiling with `griot
config set spend-ceiling <USD>` (or `spend-velocity-ceiling`). Raising one
asks for confirmation at an interactive terminal and has no flag that
answers, so it is the user's step, not an agent's. Note also: the `openai`
and `deepseek` **chat** profiles refuse to run at all until their price is
set (`griot config set openai-chat-price <USD per 1M tokens>`, or
`deepseek-chat-price`) — griot never assumes an
unverified price, so an unset price var looks like a different error but
has the same root cause: the breaker refusing to track spend it can't
compute.

## Missing credential

**Symptom**: a command using a paid profile or a platform adapter fails
with something like `GRIOT_OPENAI_API_KEY not found in the environment —
required for the 'openai-small' embedding profile`.

**Cause**: the credential for that provider was never set, or was set on a
different machine/environment. griot never guesses a credential is present.

**Fix**:

```bash
griot auth set <provider>          # hidden input; keychain, or <config>/.env at 0600
griot auth list                    # see what's currently configured (masked)
```

**The key was set, and the API still refuses it (401/403).** A variable
exported in the shell (`export GITHUB_TOKEN=...` in `~/.zshrc`, say) wins
over what `griot auth set` stores, in every terminal that sets it. The
refusal names where the key came from; `griot auth list` and `griot
doctor` (check `credentials`) say the same, with the shell file and line.
Remove that export, and `unset` the variable in terminals already open.

If you're upgrading from an older setup where credentials only ever lived
in the plaintext `.env` file (before the OS-keychain integration existed),
`griot auth migrate` moves everything already in that file into the
keychain in one pass. If you just ran `griot auth set` and a *running* MCP
server still reports the key missing, that's expected — the server only
resolves `.env` once, at process start, so it won't see a newly-set
credential until restarted.

## Weak or empty search results

**Symptom**: `griot search`/`griot_search` returns nothing, or returns
results with visibly low scores that don't answer the query.

**Cause**: this is a retrieval-quality question, not necessarily a bug —
but it can also mean the index itself is broken (wrong embedding dimension,
model swapped without reindexing, an empty/corrupted collection), or simply
behind: `griot_search` answers with `behind` (the repositories among its
results whose index is behind them, with the commits made since) and
`griot_index_status` says the same per registered repository and per
source (`behind_sources`, `missing_sources` for a source that never ran).
Behind means re-index that repository (`griot index all --repo <name>`),
not that retrieval is weak; "code" among `missing_sources` means a search
has nothing of its files at all.

**Fix**: run the self-check first — it doesn't require any curated data and
catches pipeline-level breakage:

```bash
griot quality-check --skip-golden-set
```

This re-searches for a sample of already-indexed points' own content and
confirms each one finds *itself* near the top with a high score — if this
fails, the problem is the index, not the query. If the self-check passes
but real questions still come back weak, that's a genuine retrieval-quality
question rather than a broken pipeline: curate a `griot golden-set add`
case around the query you expect to work, and use `griot quality-check`
(without `--skip-golden-set`) going forward to track whether retrieval
quality holds or regresses across changes (e.g. switching embedding
profiles).

## `griot stats` says "point count unavailable (collection in use)"

**Symptom**: the `Index:` line in `griot stats` reads `point count
unavailable (collection in use)` instead of a number.

**Cause**: this is not an error and does not mean the index is empty —
another process (typically a running `griot mcp` that was used within the
last 30 seconds, or, in `single` mode, at any point since it started) has
the collection open right now, so `griot stats` cannot read the point
count without colliding with it.

**Fix**: nothing is broken. If you need the real count, wait for the
idle window and run `griot stats` again, or stop the process holding the
collection (see the first entry above).
