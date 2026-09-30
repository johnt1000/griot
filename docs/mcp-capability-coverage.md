# MCP capability coverage

What the Model Context Protocol offers, what griot uses today, and what
adopting each unused capability would concretely mean here.

Kept as a reference to return to — not a plan. Nothing below is scheduled;
see the reasoning at the end for why.

**Last verified**: 2026-08-22 (updated the same day, after the management surface landed), against the `mcp` SDK installed in this
repo's venv.

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
anyio.run(m)"
```

(`griot_index_repo` is absent from that count unless `GRIOT_MCP_ENABLE_INDEX`
is set — it is registered conditionally.)

## Coverage

| Capability | In SDK | griot uses | Verdict |
|---|---|---|---|
| Tools | yes | **15** (16 with `GRIOT_MCP_ENABLE_INDEX`) | Covered |
| Prompts | yes | **4** | Covered |
| Resources | yes | no | Gap worth revisiting |
| Resource templates | yes | no | Follows from resources |
| Progress (`ctx.report_progress`) | yes | no | Gap worth revisiting |
| Client-side logging (`ctx.log`) | yes | no | Minor |
| Elicitation — form (`ctx.elicit`) | yes | **yes** (`_confirmed`) | Covered |
| Elicitation — URL (`ctx.elicit_url`) | yes | no | Registered; not the right fit today |
| Change notifications (`ctx.notify_*_changed`) | yes | no | Does not apply |
| Argument completion (`@mcp.completion`) | yes | no | Marginal |
| HTTP / SSE transports, custom routes | yes | no | Does not apply |

Three of ten — but the raw count misleads. Most absences are correct for what
griot is, and are recorded below so nobody re-derives the reasoning.

## Used today

**Tools** (`@mcp.tool`) — fifteen by default, sixteen with indexing enabled, in two groups.

*Read-only* (nine): `griot_search`, `griot_spend_status`,
`griot_index_status`, `griot_quality_check`, `griot_repos_list`,
`griot_profiles_list`, `griot_golden_set_list`, `griot_stats`,
`griot_auth_guidance`.

*State-changing, behind `_confirmed()`* (seven): `griot_repos_add`,
`griot_repos_remove`, `griot_profiles_delete`, `griot_golden_set_add`,
`griot_golden_set_remove`, `griot_assist_install`, and `griot_index_repo`
(registered only when `GRIOT_MCP_ENABLE_INDEX` is set).

All return `TypedDict`s so the SDK generates a real `output_schema` and
clients receive `structured_content`.

**Elicitation** is used, but never alone — see `_confirmed()` in
`mcp_server.py` for the three-layer policy and why no single layer is
sufficient.

**Prompts** (`@mcp.prompt`) — four. A prompt computes nothing: it injects
text, and the work is still a tool call. Each one exists because it carries
a judgement or a strategy that is not in any tool's output.

| Slash command | Carries |
|---|---|
| `/mcp__griot__stats` | How to read the usage report: trust `recent_reuse_rate` over `reuse_rate` when they disagree (a window spanning a fix averages two eras and describes neither), read a falling quality trend as an index regression, put spend against the ceiling, say so when the window is too thin to conclude anything. |
| `/mcp__griot__history` | A multi-source search strategy. griot indexes seven source types and "why is this like this" is rarely answered by one: code says what, the commit says when, the merge request says who argued, the issue says what problem started it. Left alone an agent searches once and stops. |
| `/mcp__griot__health` | The distinction between the two checks — the self-check is mechanical (indexed points retrieve themselves; proves the pipeline, not usefulness), the golden set is curated (real questions). Either can pass while the other fails, and the failures mean opposite things. Also warns that the check bills on a paid profile. |
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

MCP has no "confirm" primitive, but `ctx.elicit()` is the mechanism: the
server asks the client to collect an answer, and the tool blocks until it
arrives. It is what lets griot expose state-changing operations
(`repos add/remove`, `profiles delete`, `golden-set add/remove`,
`index`, `assist install`) over MCP without executing them unasked.

**Verified empirically on 2026-08-22**, over a real stdio transport:

| Scenario | Result |
|---|---|
| Client supports elicitation, user accepts | works — `action=accept` |
| Client does NOT support it | `MCPError: Elicitation not supported` — **the tool fails** |
| In-memory client (what the test suite uses) | `NoBackChannelError` — no back-channel at all |

Two consequences that shape any design here:

1. **It does not degrade.** On a client without support the tool breaks
   rather than proceeding unconfirmed. Safe (fails closed) but unusable.
2. **Branch on declared capability, not on client identity.**
   `ctx.client_capabilities.elicitation` is `None` when unsupported and an
   `ElicitationCapability(form=…, url=…)` when supported — verified both
   ways. A capability check needs no registry of client names and keeps
   working for clients that do not exist yet.

The shape that follows, implemented in `_confirmed()`: read the
capability; confirm when present; when absent, fall back to an explicit
`confirm=true` argument — *except* where the operation widens a security
boundary, which refuses outright with the equivalent CLI command, because
an argument the agent supplies is not a human answer.

Anything relying on the `elicit()` path itself needs tests over **real
stdio** — the in-memory client the suite uses has no back-channel, so
unit tests exercise the fallback and the refusal, not the elicitation.

## Gaps worth revisiting

### Resources

A tool means "do something"; a resource means "read this", addressed by URI
and fetched when the agent wants context — without spending a tool call.
Some clients also let a *person* attach a resource to the context
explicitly, and clients may cache them.

In griot the natural candidates are the read-only tools that are really
just data: `griot://repos`, `griot://stats`, `griot://collections`.

```python
@mcp.resource("griot://repos", mime_type="application/json")
def repos_resource() -> str:
    return json.dumps(repos.repo_status())
```

The open question is not how, it is whether — and specifically whether
these should *move* or be *duplicated*. A tool and a resource exposing the
same data is two surfaces to keep in agreement.

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
- **`ctx.elicit()` (form) is also in-band**: the value returns through the
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
- **Argument completion** — the arguments are few and simple (`days`,
  `limit`, `collection`). Autocomplete would add protocol surface for no
  real ergonomic gain.

## CLI and MCP, side by side

Verified 2026-08-22 by reading the CLI's subparsers and calling
`list_tools()` on a live server; updated 2026-09-02 when `assist install`
landed on both sides. **24 CLI operations, 15 tools (16 with indexing
enabled) and 4 prompts; 18 of the 24 have an equivalent.**

| Operation | CLI | MCP | Confirmation |
|---|---|---|---|
| Vector search | `search` | `griot_search` (plus `group_by_document`, which the CLI has no flag for) | — |
| Index status | *(part of `stats`)* | `griot_index_status` | — |
| Today's spend | *(part of `stats`)* | `griot_spend_status` | — |
| Usage report | `stats` | `griot_stats`, prompt `stats` | — |
| Guided investigation | `ask` *(paid synthesis)* | prompt `history` *(synthesis in the caller's own LLM)* | — |
| Quality check | `quality-check` *(self-check **and** golden set)* | `griot_quality_check` *(self-check only)* | — |
| List repos | `repos list` | `griot_repos_list` | — |
| List profiles | `profiles list` | `griot_profiles_list` | — |
| List golden set | `golden-set list` | `griot_golden_set_list` | — |
| Register a repo | `repos add` | `griot_repos_add` | **human only** |
| Unregister a repo | `repos remove` | `griot_repos_remove` | elicit or `confirm` |
| Delete a profile | `profiles delete` | `griot_profiles_delete` | **human only** |
| Curate a case | `golden-set add` | `griot_golden_set_add` | elicit or `confirm` |
| Remove a case | `golden-set remove` | `griot_golden_set_remove` | elicit or `confirm` |
| Index | `index all\|code\|commits\|tags\|branches\|platform` | `griot_index_repo` (off by default) | elicit or `confirm` |
| Install Claude Code/opencode skills+agent | `assist install` | `griot_assist_install` | **human only** |
| Add griot's block to the GLOBAL instructions file (`~/.claude/CLAUDE.md`) | `assist install --scope global` (asks; needs a terminal) | none, by design | CLI only |

### CLI only — and why each one stays there

| Operation | Why there is no tool |
|---|---|
| `ask` | Whoever calls an MCP tool already has an LLM. It needs retrieval, not a second synthesis it pays for. |
| `auth set`, `auth remove` | A secret does not travel through a chat channel. `griot_auth_guidance` answers with the command to run instead. |
| `auth list` | Even a masked value lets someone confirm a stolen key is the right one, and gives an agent nothing beyond the `configured` boolean it already has. |
| `golden-set suggest` | Interactive by nature — it walks the git log asking for case-by-case approval. |
| `mcp` | It is the command that starts this server. |

Two tools expose less than their CLI counterpart, and both differences
matter enough to state:

- `griot_index_repo`: no `--dry-run`, no `--repo`, no `--profile`, and one
  `path` per call rather than "every registered repo".
- `griot_quality_check`: runs the **self-check only**. Nothing over MCP
  executes the curated golden set — `griot_golden_set_list` returns the
  cases, never a result of running them. An agent can replay a case by
  hand with `griot_search`, but the verdict comes from `griot
  quality-check` in a terminal. The `health` prompt says so rather than
  letting the agent infer a pass it never measured.

### MCP only

`griot_index_status` and `griot_spend_status` are slices of `griot stats`,
split out because an agent usually wants one of them. `griot_auth_guidance`
has no CLI counterpart at all — it exists to say "not through here" and
name the command that does work.

The gap, then, is not coverage. It is five deliberate choices.

## The management surface

Settled on 2026-08-22 and implemented: how much of the CLI is manageable
over MCP, and under what mechanism.

| Kind of operation | Over MCP | Mechanism |
|---|---|---|
| Read-only (`search`, `stats`, `repos list`, `profiles list`, `golden-set list`) | yes | plain tool |
| State-changing or costly (`index`, `repos remove`, `golden-set add/remove`) | yes, confirmed | `elicit()` when available, `confirm=true` otherwise |
| Widens a security boundary (`repos add`), destroys irreversibly (`profiles delete`), or installs standing instructions a future AI session auto-loads (`assist install`) | yes, confirmed | `elicit()` only — `human_required=True`, no argument bypasses it |
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

Relying on the host's own permission prompt instead of `elicit()` was
considered and rejected: `destructiveHint` is a hint a client MAY act on,
and in Claude Code's auto mode there is no prompt at all — which is the
mode this project is actually used in.

## Why none of this is scheduled

The remaining gaps — resources, progress, `elicit_url` — are improvements
to the *shape* of functionality that has not yet met a real agent. Most
of the tools were added on 2026-08-21 and 2026-08-22 and have
never been called outside tests.
Turning them into resources before knowing whether an agent calls them at
all would be choosing a format for protocol elegance rather than for use.

The end-to-end MCP validation (see ROADMAP) produces exactly the evidence
that decides this, and as of 2026-08-22 griot records it: the `tool_calls`
table captures which tools are actually invoked, how often, and which fail.
Revisit this document with that data in hand.
