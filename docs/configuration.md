# Configuration

Which embedding and chat profile griot uses, every setting and how to change
it, and where griot keeps its files. Credentials have their own page:
[Credentials](credentials.md).

## Embedding profiles

Each profile gets its own collection (vectors from different models aren't
comparable). Make one the active profile with `griot profiles use <name>`,
which writes `GRIOT_EMBED_PROFILE` to `<config>/.env` (it asks first when the
profile calls an API; `--yes` answers), or pick one for a single run with
`--profile`. A profile switched to has its own, empty index until you run
`griot index all`, and an MCP server that is already running keeps the profile
it started with. A `GRIOT_EMBED_PROFILE` exported in the environment, or set
in a server's own `env`, wins over the file.

| Profile | Backend | Cost | Notes |
|---|---|---|---|
| `jina-code` (default) | local ONNX | free | code-specialist, 768-dim |
| `bge-small`, `nomic-q`, `mxbai-large`, `bge-large-en` | local ONNX | free | RAM-tiered alternatives — `griot profiles list` shows what fits your machine |
| `openai-small` | OpenAI API | paid | needs `GRIOT_OPENAI_API_KEY` |
| `gemini` | Gemini API | paid | needs `GEMINI_TOKEN` |

`griot profiles list` shows every profile with RAM estimates and credential
status. `griot profiles delete <profile>` permanently deletes that profile's
on-disk collection to reclaim disk space (refuses the active profile, and
refuses while any indexing run is in progress). It asks for confirmation at an
interactive terminal and has no `--yes`.

## Chat profiles (`griot ask` only)

Search is controlled by the *embedding* profile above; the chat profile only
decides which LLM writes the final answer. Select with `GRIOT_CHAT_PROFILE` or
`--chat-profile`.

| Profile | Credential | Default model | Price config |
|---|---|---|---|
| `gemini` (default) | `GEMINI_TOKEN` | gemini flash | built-in |
| `openai` | `GRIOT_OPENAI_API_KEY` | gpt mini tier | **required**: `GRIOT_OPENAI_CHAT_PRICE_PER_1M_TOKENS` |
| `deepseek` | `GRIOT_DEEPSEEK_API_KEY` | deepseek-chat | **required**: `GRIOT_DEEPSEEK_CHAT_PRICE_PER_1M_TOKENS` |
| `groq` | `GRIOT_GROQ_API_KEY` | llama-3.3-70b-versatile | defaults to $0 (free tier — verify current limits in Groq's console) |

griot never assumes an unverified price: `openai`/`deepseek` refuse to run
until you set their price
(`griot config set openai-chat-price <USD per 1M tokens>`, or
`deepseek-chat-price`), so the spend circuit breaker always tracks real cost.

How `griot ask` finds its context, and the flags that narrow it:
[Search](search.md#griot-ask).

## Settings: `griot config`

The first time you run any `griot` command, `<config>/.env` is generated for
you with every setting listed, mode 0600: most with their default written out,
those whose default may still change commented out, and credentials empty. You
rarely need to open it: `griot config list` shows each setting, the value in
force and where it comes from, and `griot config set` changes one.

`griot config list` shows every setting, the value in force and where it comes
from (the environment, `<config>/.env`, or the default).
`griot config set <name> <value>` checks a value and writes it to the file,
`griot config unset <name>` goes back to the default, and
`griot config get <name>` prints one value. A change that widens something, or
deletes history, is asked about at an interactive terminal, with no flag that
answers: raising a spend ceiling, turning on indexing through MCP, adding a
directory an agent may index, pointing a platform token at another host,
shortening `log-retention-days` or lowering `run-retention` (the question says
how many searches and MCP tool calls, or indexing runs, the next prune
deletes; the set itself deletes nothing). A variable exported in the
environment wins over the file, and a running MCP server keeps the values it
started with: the `griot_config_list` tool answers for the server being asked,
with the value each setting has there, where it came from, and whether the
file has changed since. The embedding profile has its own command
(`griot profiles use`), and credentials have `griot auth`.

## Environment variables

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
| `GRIOT_MCP_CONCURRENCY_MODE` | `multi` (default) or `single` — see [Running multiple sessions](mcp.md#running-multiple-sessions) |
| `GRIOT_MCP_IDLE_RELEASE_SECONDS` | idle window before releasing the collection handle in `multi` mode (default 30) |
| `GRIOT_GITLAB_API_BASE` | self-hosted GitLab API base (default `https://gitlab.com/api/v4`) |
| `GRIOT_GITEA_HOSTS` | comma-separated Gitea/Forgejo hosts to recognize |

## Where things live

| Path | Contents |
|---|---|
| `~/.config/griot/` | `.env` (credentials, 0600), `repos.json`, `quality_golden_set.json`, `golden_set_rejected.json` (questions rejected in `golden-set review`, as digests) |
| `~/.local/share/griot/` | `qdrant_data/` (vectors + indexed content), `models/` (local embedding models), `logs/`, `.spend_state.json`, `.index_jobs.json` (indexing runs started from the MCP server) |

Override with `GRIOT_CONFIG_DIR` / `GRIOT_DATA_DIR` (XDG variables are also
honored). Everything griot writes is chmod 0600 (files) / 0700 (dirs).
