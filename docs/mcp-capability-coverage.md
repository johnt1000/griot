# MCP capability coverage

What the Model Context Protocol offers, what griot uses today, and what
adopting each unused capability would concretely mean here.

Kept as a reference to return to — not a plan. Nothing below is scheduled;
see the reasoning at the end for why.

**Last verified**: 2026-08-22 (updated the same day, after the management surface landed), against the `mcp` SDK installed in this
repo's venv; the resources rows on 2026-10-06.

## How to re-verify

The SDK's surface changes between versions, so check rather than trust this
table:

```bash
python -c "from mcp.server.mcpserver import MCPServer; print(sorted(a for a in dir(MCPServer) if not a.startswith('_')))"
python -c "from mcp.server.mcpserver import Context; print(sorted(a for a in dir(Context) if not a.startswith('_')))"
```

Count what the protocol actually exposes, not what the source mentions —
grepping the decorators lumps tools in with prompts and also matches the
module docstring's own prose about them, which is how the count in this
file was wrong once already:

```bash
python -c "
import anyio
from mcp.client.client import Client
from griot import mcp_server
async def m():
    async with Client(mcp_server.mcp) as c:
        print(len((await c.list_tools()).tools), 'tools')
        print([p.name for p in (await c.list_prompts()).prompts], 'prompts')
        print([str(r.uri) for r in (await c.list_resources()).resources], 'resources')
anyio.run(m)"
```

(`griot_index_repo` is absent from that count unless `GRIOT_MCP_ENABLE_INDEX`
is set — it is registered conditionally.)

## Coverage

| Capability | In SDK | griot uses | Verdict |
|---|---|---|---|
| Tools | yes | **15** (16 with `GRIOT_MCP_ENABLE_INDEX`) | Covered |
| Prompts | yes | **4** | Covered |
| Resources | yes | **yes** (see `list_resources()`) | Covered: duplicates of read-only tools |
| Resource templates | yes | no | Follows from resources |
| Progress (`ctx.report_progress`) | yes | no | Gap worth revisiting |
| Client-side logging (`ctx.log`) | yes | no | Minor |
| Elicitation — form (via `Resolve`/`Elicit`) | yes | **yes** (`_confirmed`) | Covered |
| Elicitation — URL (`ctx.elicit_url`) | yes | no | Registered; not the right fit today |
| Change notifications (`ctx.notify_*_changed`) | yes | no | Does not apply |
| Argument completion (`@mcp.completion`) | yes | no | Marginal |
| HTTP / SSE transports, custom routes | yes | no | Does not apply |

Four of ten — but the raw count misleads. Most absences are correct for what
griot is, and are recorded below so nobody re-derives the reasoning.

## Used today

**Tools** (`@mcp.tool`), in two groups. The server's `list_tools()` is the
inventory, and the table under "CLI and MCP, side by side" maps each tool to
its command; a list kept here in prose went stale more than once.

*Read-only*: search, index and spend status, the usage report, the lists of
repositories, profiles, settings and curated cases, which credentials are
configured, what an index run would do, and the quality check. Each carries `readOnlyHint`, which is also what the
installer reads to decide which tools it may offer to pre-approve.

*State-changing, behind `_confirmed()`*: registering and removing a
repository, deleting a profile, curating the golden set, installing the
skills, and `griot_index_repo`, registered only when `GRIOT_MCP_ENABLE_INDEX`
is set.

All return `TypedDict`s so the SDK generates a real `output_schema` and
clients receive `structured_content`.

**Resources** (`@mcp.resource`): read-only data the agent fetches by URI, as
JSON, without a tool call. Each one is a read-only tool at its default
arguments — the repositories, the usage report, the index status — and
`list_resources()` is the inventory (each description names its tool). See
[Resources](#resources) for why they duplicate the tools rather than replace
them, and how the two are kept from disagreeing.

**Elicitation** is used, but never alone — see `_confirmed()` in
`mcp_server.py` for the three-layer policy and why no single layer is
sufficient.

**Prompts** (`@mcp.prompt`) — four. A prompt computes nothing: it injects
text, and the work is still a tool call. Each one exists because it carries
a judgement or a strategy that is not in any tool's output.

| Slash command | Carries |
|---|---|
| `/mcp__griot__stats` | How to read the usage report: start from what does not depend on the window (`attention`, how long ago the index was last written and searched, whether the last quality check is older than the index, the golden set's last result), then trust `recent_reuse_rate` over `reuse_rate` when they disagree (a window spanning a fix averages two eras and describes neither), read a falling quality trend as an index regression, put spend against the ceiling, say so when the window is too thin to conclude anything. |
| `/mcp__griot__history` | A multi-source search strategy. griot indexes seven source types and "why is this like this" is rarely answered by one: code says what, the commit says when, the merge request says who argued, the issue says what problem started it. Left alone an agent searches once and stops. |
| `/mcp__griot__health` | The distinction between the two checks — the self-check is mechanical (indexed points retrieve themselves; proves the pipeline, not usefulness), the golden set is curated (real questions). Either can pass while the other fails, and the failures mean opposite things. `griot_quality_check` returns both, apart; the prompt says how to read each, and that a golden set that was not run (`golden_check` null) is not a pass. Also warns that the check bills on a paid profile. |
| `/mcp__griot__overview` | Orientation, plus reading `exists`/`is_git` as the warnings they are — a registered path that is gone still lists, and indexing it fails or quietly indexes nothing. |

`history` is also what closes a gap the `griot_ask` decision left open
without meaning to. Refusing it as a tool was right — the caller already
has an LLM, so a second paid synthesis adds nothing — but it dropped the
retrieval *strategy* along with the synthesis, and the strategy was the
part worth keeping. A prompt puts it back: the search plan travels with the
server, the synthesis happens in the model already in the room.

Named without a `griot_` prefix on purpose: the client composes
`mcp__<server>__<prompt>`, so the server segment already says it, and only
that last segment is ours to choose — which is why a shape like
`griot:stats` is not available to a server at all (see the plugin note in
[ROADMAP.md](../ROADMAP.md) for where that form actually comes from).

### Elicitation (form) — how critical operations are confirmed

MCP has no "confirm" primitive. griot asks through an elicitation: the
server puts a question to the client, the client shows it to the person, and
the answer decides. It is what lets griot expose state-changing operations
(`repos add/remove`, `profiles delete`, `golden-set add/remove`,
`index`, `assist install`) over MCP without executing them unasked.

**Not through `ctx.elicit()`.** That call sends a server-to-client request in
the middle of the tool call, and the protocol revision Claude Code negotiates
(2026-07-28) has no place for one: it raised `NoBackChannelError`, so every
confirmed tool silently fell back to `confirm=true`. Each confirmed tool
instead declares a `Resolve` parameter (`answer`) that the SDK fills before
the body runs, using whichever round trip the negotiated protocol supports.
That parameter is not part of the tool's input schema, so an agent cannot
supply it.

**Verified against the SDK, both protocol revisions** (legacy and
2026-07-28), and against Claude Code itself:

| Scenario | What the tool receives |
|---|---|
| Person accepts | an accept whose data is the confirmation model |
| Person declines | a decline: nothing changes, no CLI hint, no `confirm=true` hint |
| Person dismisses the dialog | a cancel: same, worded differently |
| Client never declared the capability | an accept **whose data is not the confirmation model** |
| Claude Code in `-p` mode (nobody to ask) | a cancel |

The fourth row is the dangerous one. The SDK wraps whatever a resolver returns
as an *accepted* outcome, and "nobody can be asked" is returned by the
resolver, so it reaches the tool looking like a yes. `_confirmed()` therefore
authorizes only an accept whose data is a `_Ask`, never merely an accept.

Three consequences that shape any design here:

1. **Branch on declared capability, not on client identity.**
   `ctx.client_capabilities.elicitation` is `None` when unsupported and an
   `ElicitationCapability(form=…, url=…)` when supported. No registry of
   client names, and it keeps working for clients that do not exist yet.
2. **Where a person can be asked, the person decides.** The resolver asks
   whatever `confirm` says, and `_confirmed()` does not consult `confirm`
   after a decline or a dismissal: the argument comes from the agent, and an
   agent that was just told no could otherwise call again with
   `confirm=true`. A dismissal (which is also what a headless Claude Code
   answers by itself) gets the terminal command, a decline gets nothing.
3. **Ask last.** The resolver runs before the tool body, so it repeats the
   cheap validation (an unknown harness, a path that would be refused, an
   invalid limit) and skips the question when the call cannot succeed. A
   confirmation whose answer cannot change the outcome only teaches people to
   click through.

The shape that follows, implemented in `_confirmed()`: when the client can
ask, ask; when it cannot, fall back to an explicit `confirm=true` argument,
*except* where the operation widens a security boundary or destroys data
irreversibly, which refuses outright with the equivalent CLI command, because
an argument the agent supplies is not a human answer.

Tests for this live in `tests/test_confirmation.py` and drive a real MCP
client on both protocol revisions, since the resolver path is invisible to
unit tests that call the tool function directly.

### Resources

A tool means "do something"; a resource means "read this", addressed by URI
and fetched when the agent wants context — without spending a tool call.
Some clients also let a *person* attach a resource to the context
explicitly, and clients may cache them.

The open question was whether the read-only tools that are really just data
should *move* to resources or be *duplicated*. Decided on 2026-10-06:
**duplicated**. The tools stay, because they are what agents are known to
call and they take arguments a static resource cannot (`griot_stats`'s
window, `griot_index_status`'s collection); a resource is the tool at its
defaults. `griot://collections` was not added: no tool lists collections,
and a resource with nothing behind it would be a second implementation, not
a copy. `griot_profiles_list` already names each profile's collection.

A tool and a resource exposing the same data is two surfaces to keep in
agreement, so neither has its own code. In `mcp_server.py`, each resource
runs the function its tool runs (`_repos_list()`, `_stats()`,
`_index_status()`) and is serialized by the output model the SDK built for
that tool: the same model that turns the tool's dict into the structured
content a client receives, which drops undeclared keys and renders values as
JSON. `tests/test_mcp_resources.py` compares a resource read with the tool
call through a real client.

Two behaviours a resource gets from the SDK that a tool does not, both
handled:

- An error raised while reading becomes a bare "Error reading resource";
  the message (a corrupt `repos.json`, and which file to fix) would be lost.
  The read raises an `MCPError` carrying the tool's own message instead.
- A read is not a tool call, so it would bypass `_records_call`. It goes
  through it: recorded in `tool_calls` under its URI, and counted as a call
  in flight, so the idle reaper does not close the collection under it.

## Gaps worth revisiting

### Progress

`griot_index_repo` starts an indexing run that takes minutes and returns
immediately with `{started: true}`. The agent is then blind: its only
option is to poll `griot_index_status`. `ctx.report_progress()` exists for
exactly this.

The obstacle is real and architectural, not cosmetic: indexing runs in a
**subprocess**, because the MCP server's stdout *is* the JSON-RPC transport
and any stray `print()` from the indexer would corrupt the protocol (see
`mcp_server.py`'s module docstring). Progress would have to travel back
from the subprocess to a tool call that has already returned. That is a
design change, not a decorator.

### Elicitation (URL) — the sanctioned channel for secrets

`ctx.elicit_url()` is documented in the SDK as directing the user to an
external URL for *"out-of-band interactions that **must not pass through
the MCP client**"* — the OAuth-style pattern. It is the only capability in
the protocol designed for data that must never enter the conversation.

This matters because the alternatives are actively unsafe for a token:

- **A prompt is the worst possible channel.** It injects text into the
  conversation by design, so the secret would reach the model provider,
  land in the session transcript on disk, and can resurface in later
  context windows and summaries.
- **Form elicitation is also in-band**: the value returns through the
  MCP client and the orchestrating agent sees the tool result.

**Why griot does not use it today.** `elicit_url` needs a URL that actually
collects the value, which for a local tool means griot serving one — a
listener on a port — a network surface this project deliberately does not
have. An ephemeral,
single-endpoint, loopback-only listener that dies after one request is a
genuinely small thing, so this is not a hard
"no". But it buys little: griot's providers use **static API keys**, not
OAuth, and `griot auth set <provider>` already reads the key with
`getpass` (never echoed, never in argv) and stores it in the OS keychain.
Routing the same secret through a browser and a local socket adds moving
parts without removing an exposure.

**When to revisit**: if griot ever supports a provider that authenticates
by OAuth rather than by a pasted key. Then the redirect *is* the flow,
`elicit_url` is exactly the right mechanism, and the local listener earns
its place instead of merely duplicating `getpass`.

Until then the policy stands: **secrets are set from the CLI, and the MCP
side guides the user there with the concrete command — it never collects
them.**

### Client-side logging

`ctx.log()` sends log lines to the client instead of only to `griot.log`.
Would make a failing tool call self-explanatory in the agent's own
transcript. Small, and largely subsumed by the fact that tool errors
already surface as `isError` with an actionable message.

## Absences that are correct

- **Change notifications** — the tool list is static; it changes only when
  `GRIOT_MCP_ENABLE_INDEX` flips, which is a restart.
- **HTTP/SSE transports and custom routes** — griot is local and
  single-user; stdio is the right transport, and anything network-facing
  would reopen the threat-model questions settled in [lessons and debts](lessons-and-debts.md).
- **Argument completion** — most arguments are few and simple (`days`,
  `limit`, `collection`). The two that take names, `repos` and
  `source_types` of `griot_search`, are the natural candidates: the kinds are
  seven fixed values listed in the description, and a repository name that
  matches nothing is an error that says so. Completion would save that round
  trip, at the cost of protocol surface few clients use. Still marginal.

## CLI and MCP, side by side

Verified by reading the CLI's subparsers and calling `list_tools()` on a
live server; last on 2026-10-01. The table is the mapping; counts are left
out on purpose, because every one written here went stale.

| Operation | CLI | MCP | Confirmation |
|---|---|---|---|
| Vector search | `search` | `griot_search` (`group_by_document`, `repos`, `source_types`; `--group-by-document`, `--repo`, `--source-type` on the CLI) | — |
| Index status | *(part of `stats`)* | `griot_index_status` | — |
| Today's spend | *(part of `stats`)* | `griot_spend_status` | — |
| Usage report | `stats` | `griot_stats`, prompt `stats` | — |
| Guided investigation | `ask` *(paid synthesis)* | prompt `history` *(synthesis in the caller's own LLM)* | — |
| Quality check | `quality-check` *(self-check **and** golden set)* | `griot_quality_check` *(both; `golden_set=false` for the self-check alone)* | — |
| List repos | `repos list` | `griot_repos_list` | — |
| List profiles | `profiles list` | `griot_profiles_list` | — |
| List golden set | `golden-set list` | `griot_golden_set_list` | — |
| Audit of stored values | `audit` *(the whole index)* | `griot_audit` *(a bounded number of stored points per call; `repos` reads one repository at a time; asked about each time)* | — |
| Settings | `config list`, `config get` *(what a new process would use)* | `griot_config_list` *(what this server runs with, and whether the file changed since it started)* | — |
| Register a repo | `repos add` | `griot_repos_add` | **human only** |
| Unregister a repo | `repos remove` | `griot_repos_remove` | dialog, or `confirm` where nobody can be asked |
| Delete a profile | `profiles delete` | `griot_profiles_delete` | **human only** |
| Candidate cases from a git log | `golden-set suggest` *(asks about each and writes the approved ones)* | `griot_golden_set_suggest` *(returns the candidates and writes nothing; a bounded stretch of the log; asked about each time)* | — |
| Curate a case | `golden-set add` | `griot_golden_set_add` | dialog, or `confirm` where nobody can be asked |
| Remove a case | `golden-set remove` | `griot_golden_set_remove` | dialog, or `confirm` where nobody can be asked |
| Index | `index all\|code\|commits\|tags\|branches\|platform` | `griot_index_repo` (off by default) | dialog, or `confirm` where nobody can be asked |
| What an index run would do | `index ... --dry-run` | `griot_index_preview` (always there; one repository per call) | — (read-only, not pre-approved: it reads the whole repository) |
| Install Claude Code/opencode skills+agent | `assist install` | `griot_assist_install` | **human only** |
| Add griot's block to the GLOBAL instructions file (`~/.claude/CLAUDE.md`, or under `CLAUDE_CONFIG_DIR`) | `assist install --scope global` (asks; needs a terminal) | none, by design | CLI only |
| Register griot's MCP server with the harness | `assist install` (asks; `--mcp` answers) | none, by design | CLI only |
| Let the harness call the read-only tools without asking | `assist install` (asks; no flag answers) | none, by design | CLI only |

### CLI only — and why each one stays there

| Operation | Why there is no tool |
|---|---|
| `ask` | Whoever calls an MCP tool already has an LLM. It needs retrieval, not a second synthesis it pays for. |
| `auth set`, `auth remove` | A secret does not travel through a chat channel. `griot_auth_guidance` answers with the command to run instead. |
| `auth migrate` | It moves secrets between stores: same reason as `auth set`. |
| `auth list` | Even a masked value lets someone confirm a stolen key is the right one, and gives an agent nothing beyond the `configured` boolean it already has. |
| `mcp` | It is the command that starts this server. |
| `config set`, `config unset` | Settings decide how much may be spent, what an agent may index and where tokens are sent. A person changes them; the widening ones only at a terminal. |
| `doctor` | A checklist for the person setting griot up: most of what it reads a tool already answers (`griot_config_list`, `griot_index_status`, `griot_repos_list`, `griot_spend_status`, `griot_profiles_list`), and what it adds (the file's permissions, the MCP registration, which tools still ask) is about the machine and the harness the agent runs in, which the agent cannot change. |
| `profiles use` | Which profile is active decides where everything indexed and searched is sent, and whether it is billed. The user's call, at a terminal; a running server would keep its profile anyway. |

Three tools expose less than their CLI counterpart, and the differences
matter enough to state:

- `griot_index_repo`: no `--repo`, no `--profile`, no `--prune`, and one
  `path` per call rather than "every registered repo". Its dry run is a tool
  of its own, `griot_index_preview`.
- `griot_assist_install`: copies the skills and the agent, and nothing else.
  Registering the server, the instructions block and the pre-approval of
  tools are the three steps of the installer that decide what an agent may
  do, so each is a question at a terminal.
- `griot_quality_check`: runs the self-check and the curated golden set,
  and returns them apart. Two bounds the terminal command does not have:
  the sample of the self-check and the number of curated cases are capped
  per call (each embeds a query, which a paid profile bills), and a case's
  `limit` is held to what `griot_search` allows (the case then carries
  `limit_reduced_from`, since with fewer results it can fail here and pass
  at a terminal). Past the cap on cases the
  curated half is not run and `golden_set_not_run` says so; `griot
  quality-check` in a terminal runs them all. It is not among the tools the
  installer offers to pre-approve, for the same reason.

### MCP only

`griot_index_status` and `griot_spend_status` are slices of `griot stats`,
split out because an agent usually wants one of them. `griot_auth_guidance`
has no CLI counterpart at all — it exists to say "not through here" and
name the command that does work.

The gap, then, is not coverage. It is a set of deliberate choices, each
listed above, and two read-only views that are planned.

## The management surface

Settled on 2026-08-22 and implemented: how much of the CLI is manageable
over MCP, and under what mechanism.

| Kind of operation | Over MCP | Mechanism |
|---|---|---|
| Read-only (`search`, `stats`, `repos list`, `profiles list`, `golden-set list`) | yes | plain tool |
| State-changing or costly (`index`, `repos remove`, `golden-set add/remove`) | yes, confirmed | a confirmation dialog when the client can ask (then `confirm=true` is ignored and a decline is final), `confirm=true` otherwise |
| Widens a security boundary (`repos add`), destroys irreversibly (`profiles delete`), or installs standing instructions a future AI session auto-loads (`assist install`) | yes, confirmed | a confirmation dialog only — `human_required=True`, no argument bypasses it; plus the `anthropic/requiresUserInteraction` marker |
| **Secrets** (`auth set/list/remove`) | **never** | MCP answers with the CLI command to run |

The third row exists because confirmation protects against a *mistake*,
not against a compromised agent: `confirm` is an argument the agent
itself supplies. Where the operation would widen what may be indexed —
and therefore what may reach a paid embedding API — only a real human
answer counts. Classification is by **effect**, not by whether the
operation reads as destructive: `repos remove` narrows the allowlist, so
its worst case is inconvenience, and it keeps the fallback.

The secrets row is not a stricter version of the row above it. Confirmation
does not make it safe to type a token into a chat — the problem is the
channel, not the absence of a confirmation step.

Relying on the host's own permission prompt INSTEAD of a confirmation dialog was
considered and rejected: `destructiveHint` is a hint a client MAY act on,
and in Claude Code's auto mode there is no prompt at all — which is the
mode this project is actually used in. The three human-only tools do carry
`_meta["anthropic/requiresUserInteraction"]`, as an addition. Verified
against Claude Code 2.1.285 run headless, with an allow rule and with
`bypassPermissions`: the call was denied and the tool never reached the
server. Its documentation says an interactive session prompts on every call;
that was not observed here. The marker says nothing about the consequence of
the call, so griot's own dialog stays: two confirmations for three rare
operations, on purpose.

**The same classification holds in the CLI.** `repos add` and
`profiles delete` ask at an interactive terminal and have no `--yes`;
`repos remove`, `golden-set remove` and `auth remove` ask and accept `--yes`.
Without that, an agent told "this needs a person" by the MCP tool could run
the equivalent command from its shell and nobody would be asked. It guards
the easy path only: a process that can run arbitrary commands can fake a
terminal or edit the files, and what an agent's shell may do is the agent
host's permission system's job.

## Why none of this is scheduled

The remaining gaps — progress, `elicit_url` — are improvements
to the *shape* of functionality that has not yet met a real agent. Most
of the tools were added on 2026-08-21 and 2026-08-22 and have
never been called outside tests.
The same held for resources, which is why they were added as copies of
three tools rather than in their place: nothing is taken away from the
surface agents use, and `tool_calls` (which records a resource read under
its URI) now shows which of the two gets used.

The end-to-end MCP validation (see ROADMAP) produces exactly the evidence
that decides this, and as of 2026-08-22 griot records it: the `tool_calls`
table captures which tools are actually invoked, how often, and which fail, and (since 2026-09-30) which project
the call came from.
Revisit this document with that data in hand.
