# griot

[![CI](https://github.com/johnt1000/griot/actions/workflows/ci.yml/badge.svg)](https://github.com/johnt1000/griot/actions/workflows/ci.yml)

Local-first RAG over your git repositories. griot indexes source code, git history (commits, tags, branches) and code-platform data (PRs/MRs, releases, issues) into an **embedded** vector store — no database server, no Docker — and lets you search it or ask questions about it, from the CLI or from an AI agent via [MCP](https://modelcontextprotocol.io/).

Named after the West African storyteller who keeps a community's history: griot remembers what your repositories have been through.

## Highlights

- **Fully local storage** — vectors live in an in-process [Qdrant Edge](https://qdrant.tech/edge/) shard under your XDG data dir. No services to run.
- **Free by default** — the default embedding profile (`jina-code`) runs locally via ONNX. Paid profiles (OpenAI, Gemini) are opt-in.
- **Spend circuit breaker** — daily ceiling + 5-minute velocity ceiling on every paid call, with atomic on-disk state. The `openai` and `deepseek` chat profiles refuse to run until you set their price (`griot config set openai-chat-price <USD per 1M tokens>`, or `deepseek-chat-price`), so the breaker always tracks real cost.
- **Five platforms** — GitHub, GitLab (incl. self-hosted), Bitbucket Cloud, Azure DevOps and Gitea/Forgejo adapters for PRs/releases/issues, detected from each repo's `origin` remote. These are built against each provider's documented API and covered by tests with mocked HTTP; the GitHub path has been exercised against a live account, and GitLab against gitlab.com, with a token and without one (self-hosted GitLab and the other three, not yet).
- **MCP server** — expose search/status/quality tools to Claude Code, opencode or any MCP client. Indexing via agent is off by default and path-allowlisted.
- **Security-hardened** — API keys never in URLs or logs, 0600/0700 file modes on everything it writes, no credential ever follows a redirect. See [SECURITY.md](SECURITY.md).

**Status:** v0.5.0, beta. One maintainer, used daily by its author. The CLI surface and the on-disk layout may still change between 0.x releases.

## Installation

```bash
pipx install griot-rag    # the distribution is griot-rag; the command is griot
```

Or from a checkout: `git clone https://github.com/johnt1000/griot && cd griot && pipx install .` (`pipx install -e .` for an editable install).

Requires Python ≥ 3.10 and `git`. **The first run of a local profile downloads its ONNX model from Hugging Face** (~1.1 GB for the default `jina-code`) and caches it under your data directory; `griot profiles list` shows each profile's RAM tier against the RAM you actually have. CI runs the suite on 3.10 and 3.13, scans for committed secrets, and installs the built wheel in a clean environment on every push.

## Quickstart

```bash
# 1. register the repos you want to index (asks you to confirm, in a terminal)
griot repos add ~/code/my-app
griot repos add ~/code/my-lib

# 2. see what would be indexed (never spends anything)
griot index all --repo my-app --dry-run

# 3. index for real (default profile is local & free)
griot index all --repo my-app

# 4. search (no LLM: by meaning and exact words; --mode keyword for a hash or an error code)
griot search "where is the retry logic for the payment API?"
griot search "ERR_CONNECTION_REFUSED" --mode keyword

# 5. ask (search + LLM synthesis — requires a chat provider, see below)
griot ask "how does authentication work in this codebase?" --show-sources

# 6. keep an eye on usage and spend
griot stats

# 7. when something does not work: every check at once, reads only
griot doctor

# 8. upgrade griot to the newest release (shows the command, asks first)
griot update
```

`griot doctor` checks the whole setup in one go and says what to do about each finding. It changes no setting, index or file of yours; two things happen on the way and are said: loading the configuration closes a `.env` left open to other users, as every griot command does, and asking the harness which server it has registered may start that server for a moment, as `griot assist install` does. It checks settings the file holds that griot cannot start with, the configuration file, the directories and everything the data directory holds (the model cache aside) closed to other users, the active profile and its credential, the collection, the registered repositories and whether their index is behind, today's spend against the ceiling, the MCP registration and which read-only tools still ask before every call, the variables a server would ignore, git, the log, and whether a newer griot was released. That last check asks PyPI for the newest version of `griot-rag` and, when yours is behind, prints the command that upgrades it for how griot seems to be installed (it never runs it; for an editable or development install it says to update the checkout instead, since an installer would replace it); offline it says it could not check. `griot config set update-check false` turns it off; the only other command that asks PyPI is `griot update`, which you run to upgrade. Exit status 1 only when a check fails; a warning is something to know.

`griot update` runs that upgrade: it asks PyPI for the newest `griot-rag` (always: `update-check` governs only the doctor's check, and asking is what this command is for), works out how griot was installed (`pipx`, `uv tool`, or `pip` in a virtual environment), shows the exact command and asks before running it; `--yes` answers, and with no terminal and no `--yes` it runs nothing. Up to date, it says so and runs nothing; offline, it says it could not check and exits non-zero. It refuses an editable or development install (update the checkout instead) and an installation whose method it cannot tell (it prints the candidate commands instead of guessing). The installer's output is shown as it runs and its exit status is the command's; afterwards it prints the version now installed. MCP servers already running keep the old version until they are restarted. It is a command for you, not an MCP tool: an agent does not upgrade the tool it is using.

`griot stats --days N` covers today and the N-1 days before it, in local days (midnight local time, the day the spend ceiling counts). Its counts are the active profile's collection, the one its state lines describe; `--all-profiles` counts every profile. Spend is always the whole account's.

`griot index all` runs the sources in order: `code`, `commits`, `tags`, `branches`, `platform`. Filter with `--sources code,commits`. Index an unregistered directory directly with `--path <dir>`.

In a git work tree `index code` reads what git does not ignore (tracked files and new ones), skips symlinks and files over 1 MB, and replaces credential-looking values before anything is embedded. After each run the points whose source is gone are removed: a deleted file, a deleted branch. That removal is held back when more than half of a repository would go (`--prune` overrides) and never happens for a `--path` run, so register a repository you index regularly. `--dry-run` says what a run would embed and remove, at no cost.

## Embedding profiles

Each profile gets its own collection (vectors from different models aren't comparable). Make one the active profile with `griot profiles use <name>`, which writes `GRIOT_EMBED_PROFILE` to `<config>/.env` (it asks first when the profile calls an API; `--yes` answers), or pick one for a single run with `--profile`. A profile switched to has its own, empty index until you run `griot index all`, and an MCP server that is already running keeps the profile it started with. A `GRIOT_EMBED_PROFILE` exported in the environment, or set in a server's own `env`, wins over the file.

| Profile | Backend | Cost | Notes |
|---|---|---|---|
| `jina-code` (default) | local ONNX | free | code-specialist, 768-dim |
| `bge-small`, `nomic-q`, `mxbai-large`, `bge-large-en` | local ONNX | free | RAM-tiered alternatives — `griot profiles list` shows what fits your machine |
| `openai-small` | OpenAI API | paid | needs `GRIOT_OPENAI_API_KEY` |
| `gemini` | Gemini API | paid | needs `GEMINI_TOKEN` |

`griot profiles list` shows every profile with RAM estimates and credential status. `griot profiles delete <profile>` permanently deletes that profile's on-disk collection to reclaim disk space (refuses the active profile, and refuses while any indexing run is in progress). It asks for confirmation at an interactive terminal and has no `--yes`.

## Chat profiles (`griot ask` only)

Search is controlled by the *embedding* profile above; the chat profile only decides which LLM writes the final answer. Select with `GRIOT_CHAT_PROFILE` or `--chat-profile`.

`griot ask` finds its context the way `griot search` does, with the same flags: `--repo` and `--source-type` (each repeatable) keep the context to some repositories and kinds of source, and `--mode` picks how it is ranked. A repository with nothing indexed, or a kind that does not exist, is an error before anything is embedded or the chat model is called, so it costs nothing. When the search finds nothing (filters that each exist but match nothing together, such as commits of a repository with none indexed, or an empty index), `ask` prints `No results.` and the filters that narrowed it, as `griot search` does, and does not call the chat model (nor names one in the query log). `--limit` takes what `griot search` takes, a whole number from 1:

```bash
griot ask "why do we retry on 409?" --repo my-app --source-type commit --source-type merge_request --show-sources
```

| Profile | Credential | Default model | Price config |
|---|---|---|---|
| `gemini` (default) | `GEMINI_TOKEN` | gemini flash | built-in |
| `openai` | `GRIOT_OPENAI_API_KEY` | gpt mini tier | **required**: `GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS` |
| `deepseek` | `GRIOT_DEEPSEEK_API_KEY` | deepseek-chat | **required**: `GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS` |
| `groq` | `GRIOT_GROQ_API_KEY` | llama-3.3-70b-versatile | defaults to $0 (free tier — verify current limits in Groq's console) |

griot never assumes an unverified price: `openai`/`deepseek` refuse to run until you set their price (`griot config set openai-chat-price <USD per 1M tokens>`, or `deepseek-chat-price`), so the spend circuit breaker always tracks real cost.

## Credentials

```bash
griot auth set openai      # hidden input; OS keychain when available, else <config>/.env at 0600
griot auth list            # status per provider, keys always masked
griot auth remove openai   # asks first; --yes skips the question
griot auth migrate         # moves every credential already in the plaintext file into the keychain
```

Credentials go to the OS keychain — macOS Keychain, Linux Secret Service, Windows Credential Manager — through `keyring`, which `pipx install griot-rag` installs with griot. Where no keychain backend is reachable (Linux without a running, unlocked Secret Service provider such as GNOME Keyring, KWallet or KeePassXC; a headless server, a container, CI), griot falls back to `<config>/.env` in plaintext at mode 0600, and `griot auth set`, `griot auth list` and `griot doctor` say so, why, and where each credential is. A credential stored in the file earlier (with no backend reachable, or by a griot that did not install `keyring`) stays there until you run `griot auth migrate` (or re-run `griot auth set` for that one provider).

Platform tokens (only needed for `griot index platform`): `GITHUB_TOKEN`, `GITLAB_PERSONAL_ACCESS_TOKEN`, `BITBUCKET_ACCESS_TOKEN`, `AZURE_DEVOPS_PAT`, `GITEA_TOKEN`. GitLab is the exception: without `GITLAB_PERSONAL_ACCESS_TOKEN`, griot reads a public project's merge requests, releases and issues anonymously and says so in the run's output; a private project then fails that run with an error naming the token and `griot auth set gitlab`.

A credential exported in your shell (`export GITHUB_TOKEN=...` in `~/.zshrc`, say) wins over the one griot stores. When the two differ, `griot auth set`, `griot auth list`, `griot auth remove`, `griot doctor` and an API that refuses the key say so, naming the shell file and line, or the direnv `.envrc` (never the value): remove the export to use the stored key.

The first time you run any `griot` command, `<config>/.env` is generated for you with every setting listed, mode 0600: most with their default written out, those whose default may still change commented out, and credentials empty. You rarely need to open it: `griot config list` shows each setting, the value in force and where it comes from, and `griot config set` changes one.

## MCP server

The server has to be registered with your agent before its tools exist in a session. The installer can do it for you:

```bash
griot assist install                      # for every project: copies the skills, then asks about the rest
griot assist install --scope local        # for this project only
```

It first asks the harness what is registered already. A server that runs this griot is left alone, and so is one that runs anything else that is still there. One whose command no longer exists is offered to be replaced, showing the two commands it would run (remove, then add), and only at the scope being installed: an install for one project never removes what is registered for every project, and an install for every project never touches a project's own registration (it says so when that one is broken, since it takes precedence there). Otherwise it shows the exact command and runs it only after you type `y` (`--mcp` answers yes and makes the command fail if the registration does, `--no-mcp` skips the question). For Claude Code that command is `claude mcp add --scope user griot -- <path to griot> mcp`, and the way back is `claude mcp remove --scope user griot` (`--scope local` for a per-project registration); the installer prints it. Install griot as a tool first (`pipx` or `uv tool`): what gets registered is the path of the griot you ran, and one inside a project's virtual environment stops working when that environment goes.

The installer then **offers** to let the agent call griot's read-only tools without asking each time: an agent that has to ask before every search mostly does not search. For Claude Code it shows the rules (`mcp__griot__griot_search` and the other read-only tools, as the server itself marks them) and the file, and adds them to `permissions.allow` only after you type `y`: in `~/.claude/settings.json` with `--scope global`, otherwise in the project's personal `.claude/settings.local.json`. (Wherever this page says `~/.claude`, read the directory `CLAUDE_CONFIG_DIR` names when you have set it: Claude Code keeps its user files there, and the installer follows it for the skills, the agent, the instructions block and these rules.) Every other setting keeps its value (the file is written back as indented JSON, so its layout may change), a rule or pattern you already have under `deny` or `ask` wins and is left out, and a file griot cannot edit safely is not touched: not plain JSON settings, a key given twice, read-only. griot adds no rule for the tools that change anything, nor for the four read-only tools that cost or read far more than a search does (listed under the MCP server below). A rule matches any MCP server named `griot`, whoever defines it. There is no flag that answers yes, an MCP tool never does this, and `--no-allow-tools` skips the question. To undo, remove the rules from that file.

Registered for every project, each open session starts its own griot server. With a paid embedding profile that is light. With a local one each server loads the model on its first search, from about 100 MB to a few GB depending on the profile (`griot profiles list` shows each profile's estimate). What may be indexed does not change with where the server is registered: that is decided by `repos.json` and `GRIOT_MCP_INDEX_ROOTS`.

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

Leave `env` out unless you want this project to differ from your own configuration. A variable set there wins over `griot config` for that server only where it narrows what your configuration says: one that would turn on indexing through MCP, add a directory an agent may index, raise a spend ceiling, send a platform token to another host, switch to a profile that calls an API or let an index run fail for longer is ignored (the server says so on stderr and in `griot_config_list`). Those are set with `griot config set` and `griot profiles use`, or, for a profile, with `--profile` in the server's `args`. `GRIOT_PROJECT` is the one that belongs in `env`. A server also refuses to start with its configuration or data directory inside the project it was started in.

The server sends instructions when it connects: what griot covers, when to search it first and when to read or grep instead. A client that passes server instructions on to the agent (Claude Code does) needs nothing installed for that.

`griot_search` takes an optional `group_by_document`: off by default (up to three chunks of one document, so it can answer in some depth without taking every slot), on when you want breadth (the best chunk of each document, so the same number of results reaches more files, commits and PRs). `repos` and `source_types` narrow a search to some repositories and to some kinds of source (`code`, `commit`, `tag`, `branch`, `merge_request`, `release`, `issue`); a repository with nothing indexed, or a kind that does not exist, is an error rather than an empty result. `mode` picks how results are ranked: `hybrid` (the default) by meaning and by the exact words, the two rankings fused by rank; `vector` by meaning alone; `keyword` by the exact words, BM25 over the text plus file path, commit hash and ref names, for a commit hash, an error code, or where a name is used (it embeds nothing, so it is free on any profile, and returns only chunks holding a word of the query). Measured on two repositories (MRR@10), `keyword` ranked commit hashes far above `vector` (1.00 against 0.01) but put the file that defines a function or class name first less often (0.61 against 0.72), since the chunks that mention a name are mostly where it is used; `hybrid`, the default, is the mode for the rest, a name's definition included. Scores are on each mode's own scale, and every result says which mode ran (`mode` on `griot_search`'s output, a `Mode:` line from `griot search`). An index made before keyword search existed needs `griot index keywords` once (local, embeds nothing, needs about as much free disk as the collection while it runs); until then the default searches by meaning and says so with that command (with every `griot search` and `griot ask`; on the first default search of an MCP server process, after which `mode` alone says it), an explicit `keyword` or `hybrid` is refused with that command in the message, and `griot doctor` and `griot_index_status` (`keyword_search`) say whether it has run. `griot search` takes the same as `--repo`, `--source-type`, `--group-by-document` and `--mode`, and `griot ask` takes `--repo`, `--source-type` and `--mode`. Each indexing run records what every repository looked like (its HEAD, and the tag and remote-branch refs), so `griot_index_status` says, per registered repository, whether its index is behind the repository and how: commits made since the code and commits sources ran (or that the indexed commit is no longer in the history: rewritten, or another branch checked out), whether the tags or remote branches changed (the base branch counts, since each branch is described against it), and which sources never ran; `griot_search` names the repositories among its results that are behind (`behind`), and `griot stats` lists them under attention. Pull requests and issues live on the platform, so nothing local can say whether that source is behind.

The server's own tool list is the inventory (your client shows it), and [docs/mcp-capability-coverage.md](docs/mcp-capability-coverage.md) maps each tool to its CLI command. The read-only ones cover search, index and spend status, the usage report, the lists of repositories, profiles and curated cases, the settings the server is running with, and which credentials are configured. Four more read without changing anything and have no confirmation of their own, but `griot assist install` offers no allow rule for them, because they cost or read far more than a search does, so your client asks before each call unless you allow them yourself: the quality check (`griot_quality_check`: the self-check and the curated golden set, one embedding per sampled point, billed on a paid profile), a preview of what an index run would embed and remove (`griot_index_preview`: free, and available whether or not indexing through MCP is enabled, but it reads every file of the repository), an audit of where the index holds credential-looking values (`griot_audit`: it reads every stored chunk), and candidate golden-set cases from a repository's git log (`griot_golden_set_suggest`).

Four prompts, which clients surface as slash commands:

| Command | What it does |
|---|---|
| `/mcp__griot__stats` | The same usage report `griot stats` prints, read for you. |
| `/mcp__griot__history` | Investigates a question across every source type — code, commits, PRs, issues — and answers as a cited history, oldest cause first. |
| `/mcp__griot__health` | Says whether the index is worth trusting, and which kind of failure it is if not. |
| `/mcp__griot__overview` | What is indexed here, and which registered repos are no longer usable. |

A prompt injects text; the work is still a tool call, so nothing here runs in the background or spends anything on its own.

Some of the read-only data is also offered as MCP resources (the server's resource list is the inventory): the registered repositories, the usage report and the index status, as JSON, for clients that read context by URI or let you attach it. Each is the matching tool at its default arguments, produced by the same code, so the two never disagree; the tools stay, and take the arguments a resource cannot.

The tools that change something register or remove a repository (`griot_repos_add`, `griot_repos_remove`), delete a profile (`griot_profiles_delete`), curate the golden set (`griot_golden_set_add`, `griot_golden_set_remove`), install the skills (`griot_assist_install`, below) and index a repository (`griot_index_repo`, **off by default** — it can spend money on paid profiles; you enable it with `griot config set mcp-index true`, which asks at a terminal, and even then it only accepts paths registered via `griot repos add` or under `GRIOT_MCP_INDEX_ROOTS`). None of them act unasked. Where your client can show a confirmation dialog they ask you, and only your answer counts: a `confirm=true` argument from the agent is ignored there, and a "no" is final. Where the client cannot ask, they refuse and hand back the equivalent `griot` command, and an explicit `confirm=true` argument re-runs them — which means that on such a client an agent that passes `confirm=true` up front executes without a human. That fallback is deliberate: it is the only thing that works on a client that cannot prompt. **Registering a new repo (`griot_repos_add`), deleting a profile (`griot_profiles_delete`), and installing assist skills/agents (`griot_assist_install`) do not have it** — those accept nothing but a real human answer, because they widen what may be indexed, destroy data irreversibly, or install files a future AI session will auto-load and follow, and `confirm` is an argument the agent supplies to itself. Those three are also marked as requiring user interaction, which Claude Code honors: run headless, it denies the call before it reaches griot, even with an allow rule for the tool. (Full policy table: [docs/mcp-capability-coverage.md § The management surface](docs/mcp-capability-coverage.md#the-management-surface).)

### Claude Code / opencode skills and agent

```bash
griot assist install                      # every project: detects Claude Code and/or opencode, installs into whichever is present
griot assist install --scope local        # this project only, into ./.claude or ./.opencode
griot assist install --harness opencode   # skip detection, target one harness explicitly
griot assist install --skills-only        # copy the skills and the agent, and ask nothing
```

After the files, the installer asks up to three things, in this order, each `[y/N]` with no as the default: registering the MCP server, letting the read-only tools run without a prompt, and (for every project only) the instructions block. The last two are offered only when the server is registered, since they are about its tools. It ends with a summary of what was done and what comes next. With no supported harness on the machine it installs nothing and exits with an error.

Copies a small bundle — five Skills (onboarding, indexing, day-to-day search/ask workflows, operations — running and recovering index runs from inside an agent session — and troubleshooting) and a setup Agent — written for whoever uses griot in **their own** project, not for contributing to griot itself. Claude Code gets `.claude/skills/`+`.claude/agents/`, opencode gets `.opencode/skills/`+`.opencode/agents/` (at `--scope global`: Claude Code's user directory, `~/.claude` or the one `CLAUDE_CONFIG_DIR` names, and opencode's, `$XDG_CONFIG_HOME/opencode` or `~/.config/opencode` when that variable is unset, the rule opencode itself follows on every platform; `OPENCODE_CONFIG_DIR` does not move it, because opencode reads that directory in addition to this one, and the command says which variable chose the place); Skills are one shared file per skill (both harnesses read the same `SKILL.md` layout), the setup Agent ships as two variants because the two harnesses use different frontmatter for a subagent definition. opencode also loads skills from Claude Code's directory, `~/.claude/skills` (always under the home directory, whatever `CLAUDE_CONFIG_DIR` says) or the project's `.claude/skills`, and from `.agents/skills`, so a skill griot puts there for Claude Code, or one already there, is not copied for opencode: it would show each one twice. The command says which skills it left out and why; opencode still gets its own agent file, which it does not read from `~/.claude`. Copies an earlier install left in opencode's own directory are now duplicates, also after `--harness claude-code` alone, but they may hold your edits: at a terminal the command lists them and asks before deleting them (default no); with no terminal, or with `--skills-only`, it keeps them and prints their paths, why they are redundant and how to remove them. There is no flag that answers. Run with `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` (or `OPENCODE_DISABLE_CLAUDE_CODE`, or `OPENCODE_DISABLE_EXTERNAL_SKILLS` for both directories) set, as you start opencode, and opencode gets its own copy again. griot can only read those variables in its own environment, not in the one opencode is started with, so the output names each one and its value: if opencode runs with different values, run the install again with the same ones. The same thing is also `griot_assist_install`, an MCP tool an already-connected agent can request on your behalf — it still needs a real human answer, for the same reason `repos_add`/`profiles_delete` do; it never deletes those earlier copies, and its result lists them under `copies_kept`. Re-running it overwrites a file it installed before if griot's bundled version changed — including any edits you made to that file yourself; the command lists which files it overwrote.

At global scope, `griot assist install` also **offers** to add a short block to the harness's global instructions file (`~/.claude/CLAUDE.md` for Claude Code) that tells agents in every project when to use `griot_search`. It shows you the exact text and the file, and writes only if you type `y` at the prompt. With no interactive terminal (a script, a pipe) it writes nothing, and there is deliberately no flag that answers for you. That keeps the question from being skipped by accident or by an MCP tool. It is not a defence against a process that already has a shell on your machine: that process can edit the file directly, or drive a pseudo-terminal. `--no-instructions` skips the question. The block sits between `griot:begin` and `griot:end` markers, nothing outside them is touched, a symlinked file is written through, and deleting the block removes it. `griot_assist_install` (MCP) never touches this file.

Secrets are never an MCP operation. `griot_auth_guidance` tells an agent which providers are configured and which command you should run yourself; no tool takes or returns a key, masked or otherwise.

There is deliberately no `griot_ask` MCP tool: an agent calling MCP already has its own LLM — it needs retrieval, not a second synthesis layer.

### Running multiple sessions

The vector store is embedded (no server), so only one process can hold a given collection open at a time. Subagents and workflow-spawned agents share their parent session's MCP connection and never collide with each other. A genuinely separate session (another window, another project) using the **same** embedding profile can collide, though, and so can a `griot index` run from a shell while a server is attached. By default (`GRIOT_MCP_CONCURRENCY_MODE=multi`) the server releases the collection once it has gone `GRIOT_MCP_IDLE_RELEASE_SECONDS` (default 30) without a tool call and retries with backoff when it reopens, so another session or a shell command gets in after that window. The cost is one reopen, roughly 90 ms on a 15,000-point collection, on the first call after an idle stretch. Set `GRIOT_MCP_CONCURRENCY_MODE=single` to hold the collection for the server's whole life instead: no reopen cost, but any other process on that profile gets a hard error until the session ends. To index from inside a session without waiting for the idle window, enable `griot_index_repo` (`GRIOT_MCP_ENABLE_INDEX=true`): it lets go of the server's handle before starting the run, once no other griot tool call is using the index (it waits a few seconds for one to finish, and otherwise asks to be called again). While it runs, `griot_index_status` shows how far it got (`job`: per source, chunks done of the total once known), and `griot_index_wait` waits for it for up to five minutes, sending progress notifications to a client that asks for them; a session restart does not lose a run still going. The `griot-operations` skill walks through this. Sessions on different embedding profiles never collide, since each profile is a separate collection.

## Documentation

| | |
|---|---|
| [SECURITY.md](SECURITY.md) | What leaves your machine, what is protected on disk, and the MCP threat model |
| [ROADMAP.md](ROADMAP.md) | What is planned — and what was considered and rejected, with reasons |
| [docs/indexing-model.md](docs/indexing-model.md) | What a point is keyed by, what its payload carries, and why re-indexing is idempotent |
| [docs/mcp-capability-coverage.md](docs/mcp-capability-coverage.md) | Which MCP protocol capabilities griot uses, the CLI↔MCP parity table, and the confirmation policy |
| [docs/lessons-and-debts.md](docs/lessons-and-debts.md) | Decisions this project learned the hard way, and what it knowingly left undone |
| [CONTRIBUTING.md](CONTRIBUTING.md) | What the code expects from a change |
| [CHANGELOG.md](CHANGELOG.md) | Released changes |

## Where things live

| Path | Contents |
|---|---|
| `~/.config/griot/` | `.env` (credentials, 0600), `repos.json`, `quality_golden_set.json`, `golden_set_rejected.json` (questions rejected in `golden-set review`, as digests) |
| `~/.local/share/griot/` | `qdrant_data/` (vectors + indexed content), `models/` (local embedding models), `logs/`, `.spend_state.json`, `.index_jobs.json` (indexing runs started from the MCP server) |

Override with `GRIOT_CONFIG_DIR` / `GRIOT_DATA_DIR` (XDG variables are also honored). Everything griot writes is chmod 0600 (files) / 0700 (dirs).

## Environment variables

`griot config list` shows every setting, the value in force and where it comes from (the environment, `<config>/.env`, or the default). `griot config set <name> <value>` checks a value and writes it to the file, `griot config unset <name>` goes back to the default, and `griot config get <name>` prints one value. A change that widens something, or deletes history, is asked about at an interactive terminal, with no flag that answers: raising a spend ceiling, turning on indexing through MCP, adding a directory an agent may index, pointing a platform token at another host, shortening `log-retention-days` or lowering `run-retention` (the question says how many searches and MCP tool calls, or indexing runs, the next prune deletes; the set itself deletes nothing). A variable exported in the environment wins over the file, and a running MCP server keeps the values it started with: the `griot_config_list` tool answers for the server being asked, with the value each setting has there, where it came from, and whether the file has changed since. The embedding profile has its own command (`griot profiles use`), and credentials have `griot auth`.

The most used ones; `griot config list` shows them all.

| Variable | Purpose |
|---|---|
| `GRIOT_EMBED_PROFILE` | active embedding profile (default `jina-code`) |
| `GRIOT_CHAT_PROFILE` | active chat profile (default `gemini`) |
| `GRIOT_SPEND_CEILING_USD` | daily spend ceiling (default $3) |
| `GRIOT_SPEND_VELOCITY_CEILING_USD` | 5-minute window ceiling (default $1) |
| `GRIOT_LOG_QUESTIONS` | `false` omits question text from the query log |
| `GRIOT_UPDATE_CHECK` | `false` keeps `griot doctor` from asking PyPI whether a newer griot was released (`griot update`, which you run to upgrade, asks regardless) |
| `GRIOT_LOG_RETENTION_DAYS` | days of searches and MCP tool calls kept in `logs/logs.db` (default 365); older ones are deleted, at most once a day, by the next search or tool call. Indexing runs have `GRIOT_RUN_RETENTION`; quality checks are kept. Shortening it with `griot config set` asks first |
| `GRIOT_RUN_RETENTION` | indexing runs kept in `logs/logs.db` for each repository and source (default 100, at least 1): the newest ones, pruned in the same daily pass, also after an index run. A count, not a day window: the last run of every repository and source, the last one that did not fail, and the last that changed the index always stay, so the freshness report, `griot doctor`, `griot_index_status` and "index last changed" read the same after a prune. `griot stats` counts the runs still kept: over a window with more than that many runs of one repository and source, the older ones are not in its totals. Quality checks (the pass-rate trend) are never pruned. Lowering it with `griot config set` asks first |
| `GRIOT_PROJECT` | name recorded with each `griot ask`, `griot_search` and MCP tool call so `griot stats` can show usage by project (default: the folder `CLAUDE_PROJECT_DIR` names, else the folder `griot` runs in; set it in the `env` of that project's MCP server entry, not in `.env`) |
| `GRIOT_MCP_ENABLE_INDEX` | `true` enables the MCP indexing tool |
| `GRIOT_MCP_INDEX_ROOTS` | `:`-separated dir prefixes allowed for MCP indexing |
| `GRIOT_MCP_CONCURRENCY_MODE` | `multi` (default) or `single` — see "Running multiple sessions" above |
| `GRIOT_MCP_IDLE_RELEASE_SECONDS` | idle window before releasing the collection handle in `multi` mode (default 30) |
| `GRIOT_GITLAB_API_BASE` | self-hosted GitLab API base (default `https://gitlab.com/api/v4`) |
| `GRIOT_GITEA_HOSTS` | comma-separated Gitea/Forgejo hosts to recognize |

## Quality tooling

```bash
griot quality-check              # self-check: sampled points must find themselves
griot golden-set suggest ~/code/my-app  # derive curated test cases from that repository's git log (human-approved)
griot golden-set review          # curate cases from the questions actually asked (at a terminal)
griot golden-set add "query"     # curate a case from a real search
```

`griot quality-check` scores retrieval against your curated golden set — useful before/after switching embedding profiles. Each case is searched in the mode it was made in, over every repository and the way readers search: at most three chunks of one document, and the same text found in several places comes back once, the other places named in the result's `also_in`. A case whose document came back only as such a copy passes, and says so (`met_by_copy`: which result carried it, under which name). `griot golden-set list` shows each case's mode. `griot golden-set add`, `griot golden-set suggest` and the `griot_golden_set_add` tool make a `hybrid` case by default, like `griot search`, so a case measures what a reader gets; `add --mode vector|keyword|hybrid` picks another. That holds with nothing indexed yet too: an index run builds keyword vectors. On a collection without keyword vectors the default makes a `vector` case instead, says so and names `griot index keywords` (a hybrid case there would only ever be skipped); on one whose config cannot be read it makes a `vector` case and says that. A case in the file without a mode, as every case before modes was, is a vector case. A keyword or hybrid case on a collection without keyword vectors is reported as skipped, naming `griot index keywords`, neither passed nor failed and left out of the pass rate ("8 of 8 passed, 2 skipped"); the report says how many cases were searched in each mode.

`griot golden-set review` grows the golden set from real use. It reads the query log (`griot ask` and the `griot_search` tool record each question) and offers, at most `--limit` (10) at a time: first the questions asked more than once, most asked first, in any session or project, including the same session (an agent retrying a search counts too; the same words count as the same question, whatever the case, punctuation or word order); then the vector searches whose best result scored in the bottom quarter of that collection's vector searches (once there are at least 20 of them; keyword and hybrid scores are not similarity, so they are not used); then, newest first, the hybrid searches whose two rankings disagreed: the first result by meaning (vector) is not in the first 10 by the words (keyword), and the first by the words is not in the first 10 by meaning, both among the results the search returned; then, newest first, the searches that were reworded: the next search of the same session came within 5 minutes, is not the same question, shares at least half of the shorter question's words of four letters or more (normalised as for asked more than once; shorter words such as "how" or "the" say nothing about the subject), and brought back other results, an implicit sign the first list did not serve. The first search is the one offered, with the rewording shown beside it, since the rule cannot tell a rewording from a question about another facet of the same thing and you can. Each logged search records its session as an opaque digest, never the value it came from: of the conversation id the agent client exports, when it exports one (Claude Code's `CLAUDE_CODE_SESSION_ID`, which it gives both to `griot mcp` and to every command its shell tool runs, so a `griot_search` and a later `griot ask` of the same conversation pair, although each shell command runs in a new shell), and otherwise of the process that holds the conversation (the agent session that started `griot mcp`, or the shell `griot ask` ran in), never the process number. opencode exports no conversation id, so there a `griot ask` run through its shell tool is a session of its own. A search logged before griot recorded sessions is never paired, and neither is one from a process with no parent of its own and no client id. A hybrid search logs, beside each result, its rank in each ranking (numbers only, from the two lists the fusion already has: nothing more is embedded or searched); a search logged before that, or one whose keyword ranking matched nothing, is never offered as a disagreement. A result one ranking did not place at all counts as past the first 10 only when that ranking looked at least 10 deep, since otherwise it could have been 6th or 9th there, which is not a disagreement; every search `griot ask` and `griot_search` log looks that deep from `--limit 2` up (each ranking is fetched several times wider than the list returned), and a one-result search has nothing to disagree about. Only `griot_search` logs a best score, so a `griot ask` question can be offered as asked more than once or as a disagreement but never as scoring low. For each it shows the question, why it is a candidate, and the results logged for it (with each result's rank by meaning and by the words, for a hybrid search, and the rewording, for a reworded one); you type the number of the right one (or several), `n` when none of them was, `s` to skip, `r` to reject it for good, `q` to stop. A pick becomes a case exactly as `golden-set add` makes one, asserting that result comes back. Questions already in the golden set and ones you rejected are not offered again; rejections are kept as digests, not text, in `golden_set_rejected.json` beside the golden set. Nothing is written to the index. A case keeps the search mode its results came from (the mode that ran: vector, keyword or hybrid), and is checked by a search in that mode over every repository, the way readers search and not grouped by document (see `griot quality-check`), so only a search like that can become one: one narrowed with `repos` or `source_types`, or one grouped by document, is shown, with the reason, but cannot be picked (when the same question was also asked unnarrowed and not grouped by document, that asking is the one offered). The same goes for a search logged before griot recorded what a case needs, and for a result whose name looked like a credential. With `GRIOT_LOG_QUESTIONS=false` there is nothing to review, and the command says so.

### Credentials in indexed content

```bash
griot audit                      # where the index holds credential-looking values (never the values)
```

Text is scanned for credential-shaped values (private keys, a list of provider token formats, JWTs, passwords in URLs, random-looking values assigned to names like `API_KEY`) before it is embedded and stored, and they are replaced with a marker; the run tells you where. `griot audit` looks for the same shapes in what is already indexed. What this can and cannot find is in [SECURITY.md](SECURITY.md#credentials-in-what-is-indexed).

## Development

```bash
uv sync --locked --extra dev   # the versions CI runs (or: pip install -e ".[dev]")
uv run pytest -q               # no test ever calls a real API or needs credentials
```

## License

[Apache-2.0](LICENSE)
