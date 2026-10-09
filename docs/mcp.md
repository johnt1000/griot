# MCP server

griot's MCP server gives an agent in Claude Code, opencode or any MCP client
its search, status and quality tools. How those tools search:
[Search](search.md).

## Registering the server

The server has to be registered with your agent before its tools exist in a
session. The installer can do it for you:

```bash
griot assist install                      # for every project: copies the skills, then asks about the rest
griot assist install --scope local        # for this project only
```

It first asks the harness what is registered already. A server that runs this
griot is left alone, and so is one that runs anything else that is still
there. One whose command no longer exists is offered to be replaced, showing
the two commands it would run (remove, then add), and only at the scope being
installed: an install for one project never removes what is registered for
every project, and an install for every project never touches a project's own
registration (it says so when that one is broken, since it takes precedence
there). Otherwise it shows the exact command and runs it only after you type
`y` (`--mcp` answers yes and makes the command fail if the registration does,
`--no-mcp` skips the question). For Claude Code that command is
`claude mcp add --scope user griot -- <path to griot> mcp`, and the way back
is `claude mcp remove --scope user griot` (`--scope local` for a per-project
registration); the installer prints it. Install griot as a tool first (`pipx`
or `uv tool`): what gets registered is the path of the griot you ran, and one
inside a project's virtual environment stops working when that environment
goes.

The installer then **offers** to let the agent call griot's read-only tools
without asking each time: an agent that has to ask before every search mostly
does not search. For Claude Code it shows the rules
(`mcp__griot__griot_search` and the other read-only tools, as the server
itself marks them) and the file, and adds them to `permissions.allow` only
after you type `y`: in `~/.claude/settings.json` with `--scope global`,
otherwise in the project's personal `.claude/settings.local.json`. (Wherever
this page says `~/.claude`, read the directory `CLAUDE_CONFIG_DIR` names when
you have set it: Claude Code keeps its user files there, and the installer
follows it for the skills, the agent, the instructions block and these rules.)
Every other setting keeps its value (the file is written back as indented
JSON, so its layout may change), a rule or pattern you already have under
`deny` or `ask` wins and is left out, and a file griot cannot edit safely is
not touched: not plain JSON settings, a key given twice, read-only. griot adds
no rule for the tools that change anything, nor for the four read-only tools
that cost or read far more than a search does (listed under the MCP server
below). A rule matches any MCP server named `griot`, whoever defines it. There
is no flag that answers yes, an MCP tool never does this, and
`--no-allow-tools` skips the question. To undo, remove the rules from that
file.

Registered for every project, each open session starts its own griot server.
With a paid embedding profile that is light. With a local one each server
loads the model on its first search, from about 100 MB to a few GB depending
on the profile (`griot profiles list` shows each profile's estimate). What may
be indexed does not change with where the server is registered: that is
decided by `repos.json` and `GRIOT_MCP_INDEX_ROOTS`.

To register it yourself, per project:

```bash
claude mcp add --transport stdio griot --scope project -- griot mcp
```

or in `.mcp.json`:

```json
{
  "mcpServers": {
    "griot": {
      "command": "griot",
      "args": ["mcp"]
    }
  }
}
```

Leave `env` out unless you want this project to differ from your own
configuration. A variable set there wins over `griot config` for that server
only where it narrows what your configuration says: one that would turn on
indexing through MCP, add a directory an agent may index, raise a spend
ceiling, send a platform token to another host, switch to a profile that calls
an API or let an index run fail for longer is ignored (the server says so on
stderr and in `griot_config_list`). Those are set with `griot config set` and
`griot profiles use`, or, for a profile, with `--profile` in the server's
`args`. `GRIOT_PROJECT` is the one that belongs in `env`. A server also
refuses to start with its configuration or data directory inside the project
it was started in.

## What the server offers

The server sends instructions when it connects: what griot covers, when to
search it first and when to read or grep instead. A client that passes server
instructions on to the agent (Claude Code does) needs nothing installed for
that.

The server's own tool list is the inventory (your client shows it), and
[docs/mcp-capability-coverage.md](mcp-capability-coverage.md) maps each tool
to its CLI command. The read-only ones cover search, index and spend status,
the usage report, the lists of repositories, profiles and curated cases, the
settings the server is running with, and which credentials are configured.
Four more read without changing anything and have no confirmation of their
own, but `griot assist install` offers no allow rule for them, because they
cost or read far more than a search does, so your client asks before each call
unless you allow them yourself: the quality check (`griot_quality_check`: the
self-check and the curated golden set, one embedding per sampled point, billed
on a paid profile), a preview of what an index run would embed and remove
(`griot_index_preview`: free, and available whether or not indexing through
MCP is enabled, but it reads every file of the repository), an audit of where
the index holds credential-looking values (`griot_audit`: it reads every
stored chunk), and candidate golden-set cases from a repository's git log
(`griot_golden_set_suggest`).

The server's prompt list is the inventory of prompts (your client shows each
one as a slash command); these are the ones it offers:

| Command | What it does |
|---|---|
| `/mcp__griot__stats` | The same usage report `griot stats` prints, read for you. |
| `/mcp__griot__history` | Investigates a question across every source type — code, commits, PRs, issues — and answers as a cited history, oldest cause first. |
| `/mcp__griot__health` | Says whether the index is worth trusting, and which kind of failure it is if not. |
| `/mcp__griot__overview` | What is indexed here, and which registered repos are no longer usable. |

A prompt injects text; the work is still a tool call, so nothing here runs in
the background or spends anything on its own.

Some of the read-only data is also offered as MCP resources (the server's
resource list is the inventory): the registered repositories, the usage report
and the index status, as JSON, for clients that read context by URI or let you
attach it. Each is the matching tool at its default arguments, produced by the
same code, so the two never disagree; the tools stay, and take the arguments a
resource cannot.

## Tools that change something

The tools that change something register or remove a repository
(`griot_repos_add`, `griot_repos_remove`), delete a profile
(`griot_profiles_delete`), curate the golden set (`griot_golden_set_add`,
`griot_golden_set_remove`), install the skills (`griot_assist_install`, below)
and index a repository (`griot_index_repo`, **off by default** — it can spend
money on paid profiles; you enable it with `griot config set mcp-index true`,
which asks at a terminal, and even then it only accepts paths registered via
`griot repos add` or under `GRIOT_MCP_INDEX_ROOTS`). None of them act unasked.
Where your client can show a confirmation dialog they ask you, and only your
answer counts: a `confirm=true` argument from the agent is ignored there, and
a "no" is final. Where the client cannot ask, they refuse and hand back the
equivalent `griot` command, and an explicit `confirm=true` argument re-runs
them — which means that on such a client an agent that passes `confirm=true`
up front executes without a human. That fallback is deliberate: it is the only
thing that works on a client that cannot prompt. **Registering a new repo
(`griot_repos_add`), deleting a profile (`griot_profiles_delete`), and
installing assist skills/agents (`griot_assist_install`) do not have it** —
those accept nothing but a real human answer, because they widen what may be
indexed, destroy data irreversibly, or install files a future AI session will
auto-load and follow, and `confirm` is an argument the agent supplies to
itself. Those three are also marked as requiring user interaction, which
Claude Code honors: run headless, it denies the call before it reaches griot,
even with an allow rule for the tool. (Full policy table:
[docs/mcp-capability-coverage.md § The management surface](mcp-capability-coverage.md#the-management-surface).)

## Claude Code / opencode skills and agent

```bash
griot assist install                      # every project: detects Claude Code and/or opencode, installs into whichever is present
griot assist install --scope local        # this project only, into ./.claude or ./.opencode
griot assist install --harness opencode   # skip detection, target one harness explicitly
griot assist install --skills-only        # copy the skills and the agent, and ask nothing
```

After the files, the installer asks up to three things, in this order, each
`[y/N]` with no as the default: registering the MCP server, letting the
read-only tools run without a prompt, and (for every project only) the
instructions block. The last two are offered only when the server is
registered, since they are about its tools. It ends with a summary of what was
done and what comes next. With no supported harness on the machine it installs
nothing and exits with an error.

Copies a small bundle — five Skills (onboarding, indexing, day-to-day
search/ask workflows, operations — running and recovering index runs from
inside an agent session — and troubleshooting) and a setup Agent — written for
whoever uses griot in **their own** project, not for contributing to griot
itself.

Claude Code gets `.claude/skills/`+`.claude/agents/`, opencode gets
`.opencode/skills/`+`.opencode/agents/` (at `--scope global`: Claude Code's
user directory, `~/.claude` or the one `CLAUDE_CONFIG_DIR` names, and
opencode's, `$XDG_CONFIG_HOME/opencode` or `~/.config/opencode` when that
variable is unset, the rule opencode itself follows on every platform;
`OPENCODE_CONFIG_DIR` does not move it, because opencode reads that directory
in addition to this one, and the command says which variable chose the place);
Skills are one shared file per skill (both harnesses read the same `SKILL.md`
layout), the setup Agent ships as two variants because the two harnesses use
different frontmatter for a subagent definition. opencode also loads skills
from Claude Code's directory, `~/.claude/skills` (always under the home
directory, whatever `CLAUDE_CONFIG_DIR` says) or the project's
`.claude/skills`, and from `.agents/skills`, so a skill griot puts there for
Claude Code, or one already there, is not copied for opencode: it would show
each one twice. The command says which skills it left out and why; opencode
still gets its own agent file, which it does not read from `~/.claude`.

Copies an earlier install left in opencode's own directory are now duplicates,
also after `--harness claude-code` alone, but they may hold your edits: at a
terminal the command lists them and asks before deleting them (default no);
with no terminal, or with `--skills-only`, it keeps them and prints their
paths, why they are redundant and how to remove them. There is no flag that
answers.

Run with `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` (or
`OPENCODE_DISABLE_CLAUDE_CODE`, or `OPENCODE_DISABLE_EXTERNAL_SKILLS` for both
directories) set, as you start opencode, and opencode gets its own copy again.
griot can only read those variables in its own environment, not in the one
opencode is started with, so the output names each one and its value: if
opencode runs with different values, run the install again with the same ones.

The same thing is also `griot_assist_install`, an MCP tool an
already-connected agent can request on your behalf — it still needs a real
human answer, for the same reason `repos_add`/`profiles_delete` do; it never
deletes those earlier copies, and its result lists them under `copies_kept`.

Re-running it overwrites a file it installed before if griot's bundled version
changed — including any edits you made to that file yourself; the command
lists which files it overwrote.

At global scope, `griot assist install` also **offers** to add a short block
to the harness's global instructions file (`~/.claude/CLAUDE.md` for Claude
Code) that tells agents in every project when to use `griot_search`. It shows
you the exact text and the file, and writes only if you type `y` at the
prompt. With no interactive terminal (a script, a pipe) it writes nothing, and
there is deliberately no flag that answers for you. That keeps the question
from being skipped by accident or by an MCP tool. It is not a defence against
a process that already has a shell on your machine: that process can edit the
file directly, or drive a pseudo-terminal. `--no-instructions` skips the
question. The block sits between `griot:begin` and `griot:end` markers,
nothing outside them is touched, a symlinked file is written through, and
deleting the block removes it. `griot_assist_install` (MCP) never touches this
file.

## Secrets and synthesis

Secrets are never an MCP operation. `griot_auth_guidance` tells an agent which
providers are configured and which command you should run yourself; no tool
takes or returns a key, masked or otherwise.

There is deliberately no `griot_ask` MCP tool: an agent calling MCP already
has its own LLM — it needs retrieval, not a second synthesis layer.

## Running multiple sessions

The vector store is embedded (no server), so only one process can hold a given
collection open at a time. Subagents and workflow-spawned agents share their
parent session's MCP connection and never collide with each other. A genuinely
separate session (another window, another project) using the **same**
embedding profile can collide, though, and so can a `griot index` run from a
shell while a server is attached.

By default (`GRIOT_MCP_CONCURRENCY_MODE=multi`) the server releases the
collection once it has gone `GRIOT_MCP_IDLE_RELEASE_SECONDS` (default 30)
without a tool call and retries with backoff when it reopens, so another
session or a shell command gets in after that window. The cost is one reopen,
roughly 90 ms on a 15,000-point collection, on the first call after an idle
stretch. Set `GRIOT_MCP_CONCURRENCY_MODE=single` to hold the collection for
the server's whole life instead: no reopen cost, but any other process on that
profile gets a hard error until the session ends.

To index from inside a session without waiting for the idle window, enable
`griot_index_repo` (`GRIOT_MCP_ENABLE_INDEX=true`): it lets go of the server's
handle before starting the run, once no other griot tool call is using the
index (it waits a few seconds for one to finish, and otherwise asks to be
called again). While it runs, `griot_index_status` shows how far it got
(`job`: per source, chunks done of the total once known), and
`griot_index_wait` waits for it for up to five minutes, sending progress
notifications to a client that asks for them; a session restart does not lose
a run still going. The `griot-operations` skill walks through this.

Sessions on different embedding profiles never collide, since each profile is
a separate collection.
