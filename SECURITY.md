# Security

griot indexes the contents of your repositories — which may be private — and can send data to third-party APIs you configure. This document states plainly what goes where, what griot protects against, and what it deliberately does not.

## What leaves your machine

**No repository content, in the default configuration.** The default embedding profile (`jina-code`) runs locally; `griot search` and the MCP tools never call a chat LLM.

One exception is not configurable: the **first run of any local profile downloads its ONNX model from Hugging Face** and caches it. That request carries no data of yours — it is a model download, and every run afterwards is offline — but it is a network call to a third party, so it belongs in this table rather than in a footnote.

Data leaves your machine when **you** configure it to, plus that one download:

| Action | What is sent | To |
|---|---|---|
| first run of a local embedding profile | nothing of yours — the model is downloaded | huggingface.co |
| indexing with a paid embedding profile | chunked content of code, commit messages, tags, branches, PRs/issues | OpenAI or Google, per your profile |
| `griot search` / `griot_search` with a paid profile | the query text | same provider |
| `griot ask` | the question + retrieved context chunks | the chat provider you selected |
| `griot index platform` | authenticated API reads only | the platform (GitHub/GitLab/…) |

The spend circuit breaker (daily + 5-minute velocity ceilings) bounds how much paid traffic can happen before griot refuses further calls.

## What stays on disk, and how it's protected

`~/.local/share/griot/qdrant_data/` holds a **recoverable full copy of everything you indexed** (payloads are LZ4-compressed, not encrypted). Logs record run metadata and, unless disabled, your questions. Credentials live in `~/.config/griot/.env`.

Protections applied:

- Every file griot writes is mode `0600`; every directory `0700`. Pre-existing files are repaired to `0600` on the next write.
- API keys are sent in headers only — never in URLs — and error/log messages never interpolate provider exception text that could contain them.
- Credentialed HTTP requests never follow redirects, and server-provided pagination URLs are refused if they point to a different host.
- `GRIOT_LOG_QUESTIONS=false` keeps question text out of the persistent query log.

## Encryption at rest — deliberate position

griot does **not** implement application-level encryption of the vector store, and this is a considered decision rather than an omission:

- The embedded engine has no encryption-at-rest support; encrypting payloads ourselves would break retrieval, and wrapping the store in a decrypt-on-open layer would leave the plaintext in memory anyway for any attacker who can already read the process.
- The data is single-user and local. The correct layer for at-rest protection here is the OS: **full-disk encryption** (FileVault on macOS, LUKS on Linux) plus the `0700`/`0600` modes griot enforces.

If your threat model includes other users on a shared machine reading your files, the file modes cover it. If it includes someone with your OS user or root, no application-level scheme griot could implement would help.

## MCP server considerations

- Most tools are read-only. `griot_index_repo` is **disabled by default** (`GRIOT_MCP_ENABLE_INDEX`), and even when enabled only accepts paths registered in `repos.json` or under `GRIOT_MCP_INDEX_ROOTS` (resolved, symlink-safe, fail-closed). This exists because a prompt-injected agent must not be able to index — and thereby exfiltrate to an embedding API — arbitrary filesystem paths.
- The tools that change state (`griot_repos_add/remove`, `griot_profiles_delete`, `griot_golden_set_add/remove`, `griot_assist_install`, `griot_index_repo`) never act on the first call. They ask the client to collect a human confirmation; where the client cannot, they refuse and answer with the equivalent CLI command. A `confirm=true` argument re-runs them without a human — **except** for the three operations where that would be unsound: `griot_repos_add` (widens the set of paths that may be indexed), `griot_profiles_delete` (irreversible), and `griot_assist_install` (writes Skill/Agent files that a future AI coding session in that location loads and follows automatically — a compromised agent installing its own standing instructions is exactly the scenario this guards against). Those accept nothing but a real human answer, because confirmation protects against a *mistake*, not against a compromised agent — `confirm` is an argument the agent itself supplies. (Full policy table, including read-only tools: [docs/mcp-capability-coverage.md § The management surface](docs/mcp-capability-coverage.md#the-management-surface).)
- **No tool accepts, returns, or asks for a credential**, in any form, masked or not. `griot_auth_guidance` reports only whether each provider is configured and answers with the `griot auth` command to run in a terminal. A chat channel reaches the model provider, the session transcript on disk, and later context windows; confirmation does not change that, so secrets stay out of it by construction rather than by policy.
- `griot_search` results carry an explicit note that retrieved content is **data, not instructions**. This is a mitigation, not a guarantee: content you indexed (commit messages, issues) is untrusted input to whatever agent reads it. Treat search output accordingly in your agent design.
- Enabling `GRIOT_MCP_ENABLE_INDEX` in a committed config file means every clone of that project gets the tool enabled — prefer setting it outside version control.

## Automated checks

Every push and pull request runs the full test suite on the oldest and a
current supported Python, plus a [gitleaks](https://github.com/gitleaks/gitleaks)
scan over the **full history** — a credential removed in a later commit still
sits in the repository until the history is rewritten, so scanning only the
current tree would report a clean state that isn't one.

The scan's allowlist (`.gitleaks.toml`) exempts specific literal fixtures,
never whole files: allowing a path would let a real credential pasted into
that file ship silently, which is the exact failure the scan exists to catch.
Its behaviour is verified in both directions — the repository reports clean,
and a realistic token planted in it is still reported.

A secret scan is a backstop, not a control. Nothing in this repository should
ever contain a credential in the first place: `griot auth set` reads keys with
`getpass` (never echoed, never in argv) and writes them to `<config>/.env` at
mode 0600, outside the working tree, or to the OS keychain when `keyring` is
installed.

## Reporting a vulnerability

Open a [GitHub security advisory](https://github.com/johnt1000/griot/security/advisories/new) or an issue marked `security`. Please include reproduction steps. As a single-maintainer project there is no SLA, but security reports get priority over everything else.
