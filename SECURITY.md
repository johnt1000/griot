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
- Each logged search and tool call also carries the **name** of the project it came from: the folder griot ran in (never its path), or exactly what you set in `GRIOT_PROJECT`. The home directory is not recorded as a project, and the name is stripped of control characters and cut at 100 characters. It is stored locally in `logs.db` and is never sent anywhere.

## Encryption at rest — deliberate position

griot does **not** implement application-level encryption of the vector store, and this is a considered decision rather than an omission:

- The embedded engine has no encryption-at-rest support; encrypting payloads ourselves would break retrieval, and wrapping the store in a decrypt-on-open layer would leave the plaintext in memory anyway for any attacker who can already read the process.
- The data is single-user and local. The correct layer for at-rest protection here is the OS: **full-disk encryption** (FileVault on macOS, LUKS on Linux) plus the `0700`/`0600` modes griot enforces.

If your threat model includes other users on a shared machine reading your files, the file modes cover it. If it includes someone with your OS user or root, no application-level scheme griot could implement would help.

## Indexing a repository you do not trust

A repository is input, and so is its git config. griot reads history with `git`, and git obeys the repository's own config, where several keys name a program to run (`log.showSignature` with `gpg.program`, `core.fsmonitor`, hooks). griot overrides those on every git call, never lets a read turn into a fetch (a repository that claims to be a partial clone would otherwise pick the program that fetches), does not read bare repositories at all (one can sit inside another project's files with a config of its own), and runs git without its own credentials in the environment. For file contents, `griot index code` reads, in a work tree, only what the repository does not ignore (tracked files and new ones), so a file matched by `.gitignore` (where local settings and notes with secrets tend to live) is not sent to an embedding API; if git cannot list a work tree, nothing is read from it rather than everything. It does not follow a symlink, in the file or in a directory on the way to it, and it skips files over 1 MB. Limits worth knowing: a secret in a file that is **not** ignored, tracked or new, is indexed like any other file; a hard link to a file outside the repository is an ordinary file as far as griot can tell; and what an older version already indexed from files that are no longer read (ignored ones included) stays searchable until that repository is indexed again as a registered repository, which removes the points whose source is gone (when the run holds the removal back it says why, and `--prune` overrides the proportion check). Points written by a `--path` run are never removed by indexing; deleting the profile's collection (`griot profiles delete`) drops everything. The git part covers what is known to run a program during the read-only commands griot uses; it is not a sandbox, and a repository you have reason to distrust is still better indexed with a local embedding profile, which sends nothing anywhere.

## Credentials in what is indexed

What is indexed is embedded (on a paid profile, sent to the provider) and returned verbatim by search to any agent that asks. A token in a tracked file, a commit message or a pull request body would travel both ways.

- **When indexing**, every text goes through detectors before it is cut into chunks and before it is embedded: private key material, a fixed list of provider token formats (`redaction.DETECTORS` is the list: AWS, GitHub, GitLab, Slack, Stripe, OpenAI, Anthropic, Google, npm, PyPI and a few more), JWTs, passwords inside URLs, bearer and basic authorization headers, and a random-looking value assigned to a name such as `API_KEY`, `token` or `password`, quoted or not. A match is replaced with `[REDACTED:<rule>]`; the rest of the file stays searchable. The run lists where, never what.
- **When reading**, the same replacement is applied to whatever search, `griot ask` and the quality check take out of the store, so a value indexed by an older version does not reach an agent or a chat model while it waits to be indexed again. One exception: a value that an older version happened to cut in two, across a chunk boundary, matches nothing on either side; indexing that repository again fixes it.
- **Names** (a file path, a branch, a tag) are stored as they are, because ids are built from them; a credential-shaped name is replaced where griot names a result or a finding (search results, `griot ask --show-sources`, the run report, `griot audit`, the sources recorded in the usage log), and what the CLI prints there carries no control characters. Not covered: the list of documents that failed in a run's record, which holds ids as they are.
- **`griot audit`** lists where the index of the active profile still holds such values (locations and rule names only) and exits with status 1 when it finds any.

This does not decide whether a value is real, and it is built for precision: it will not blank out ordinary code that merely mentions a key. It does **not** find a password written in prose, a short or wordy password (`password: hunter2`), personal data, a credential split across lines or encoded, or a provider format that is not on the list. The text of a search query is not scanned either: what you or an agent type as a query goes to the embedding provider as typed. For those the protection is not indexing the file at all (`.gitignore`). And replacing a value here does not make it safe: if it was real it is still in the file or in the history, and on a paid profile an older version already sent it out. Rotate it.

## MCP server considerations

- Most tools are read-only. `griot_index_repo` is **disabled by default** (`GRIOT_MCP_ENABLE_INDEX`), and even when enabled only accepts paths registered in `repos.json` or under `GRIOT_MCP_INDEX_ROOTS` (resolved, symlink-safe, fail-closed). This exists because a prompt-injected agent must not be able to index — and thereby exfiltrate to an embedding API — arbitrary filesystem paths.
- The tools that change state (`griot_repos_add/remove`, `griot_profiles_delete`, `griot_golden_set_add/remove`, `griot_assist_install`, `griot_index_repo`) never act on the first call. They ask the client to collect a human confirmation, and where it can, that answer is the only thing that counts: a `confirm=true` argument is ignored and a decline is final. Where the client cannot ask, they refuse and answer with the equivalent CLI command, and a `confirm=true` argument re-runs them without a human — **except** for the three operations where that would be unsound: `griot_repos_add` (widens the set of paths that may be indexed), `griot_profiles_delete` (irreversible), and `griot_assist_install` (writes Skill/Agent files that a future AI coding session in that location loads and follows automatically — a compromised agent installing its own standing instructions is exactly the scenario this guards against). Those accept nothing but a real human answer, because confirmation protects against a *mistake*, not against a compromised agent — `confirm` is an argument the agent itself supplies. Those three also carry the `anthropic/requiresUserInteraction` marker, which Claude Code honors: run headless (verified on 2.1.285, with an allow rule and with `bypassPermissions`) it denies the call before it reaches griot. What it shows in an interactive session was not verified here. It is an addition to griot's own dialog, not a replacement. (Full policy table, including read-only tools: [docs/mcp-capability-coverage.md § The management surface](docs/mcp-capability-coverage.md#the-management-surface).)
- **The CLI asks before it destroys or widens.** `griot repos add` and `griot profiles delete` ask at an interactive terminal and have no flag that answers instead, so the plain command run from a shell with no terminal, which is how agents usually run commands, exits with status 2 and changes nothing. `repos remove`, `golden-set remove` and `auth remove` ask the same way and accept `--yes`. This stops the easy path, and it is **not** a boundary against an agent that can run arbitrary shell commands: such a process can allocate a pseudo-terminal or edit `repos.json` directly. What an agent's shell may run is decided by your agent's own permission settings, not by griot.
- **No MCP tool edits your global agent instructions, and no non-interactive command does.** `griot assist install --scope global` can offer to add a short block to `~/.claude/CLAUDE.md`; it shows the exact text and writes only after you type `y` at an interactive prompt. That keeps an MCP tool, a pipe or a script from writing a file that is loaded into every project. It does not stop an agent that already has a shell on your machine from editing the file directly or driving a pseudo-terminal: that is outside what griot can defend.
- **No tool accepts, returns, or asks for a credential**, in any form, masked or not. `griot_auth_guidance` reports only whether each provider is configured and answers with the `griot auth` command to run in a terminal. A chat channel reaches the model provider, the session transcript on disk, and later context windows; confirmation does not change that, so secrets stay out of it by construction rather than by policy.
- **Only you can let griot's tools run without being asked.** A harness asks before each tool call unless its settings allow the tool. `griot assist install` can add allow rules for griot's read-only tools (the ones the server marks read-only; not the quality check, which spends more than a search), and only after you type `y` at an interactive prompt, with the rules and the file on screen. There is no flag that answers yes and no MCP tool that does it: an agent must not be able to widen what an agent may do. Like the instructions block, this does not stop a process that already has a shell from editing the file itself. A rule or pattern of yours under `deny` or `ask` is never overridden, and in a project the file is refused if it, or `.claude`, is a link that leads elsewhere; a project file that already exists is pointed out before the question, with what it holds and whether git tracks it, because a repository can ship one. Limits worth knowing: a rule names a server, not griot itself, so it applies to any MCP server registered as `griot`, including one a project defines under that name; the set of tools is computed from the server, so a tool a later version marks read-only is offered the next time you run the installer (never added on its own); and the allowed tools still record each call in griot's own log. Once allowed, a search runs without a prompt: what it returns is still retrieved content, not instructions, and on a paid embedding profile each search costs a query embedding, bounded by the spend ceilings.
- **`griot_assist_install` names the directories before it asks.** Where a global install writes depends on the environment the server was started with: Claude Code keeps its user files in the directory `CLAUDE_CONFIG_DIR` names, and griot follows it. That environment comes from whoever configured the server, which can be a project's `.mcp.json`. So the question a person answers gives the skills and agents directories in full and resolved (a directory on the way can be a link: what is shown is where the files land), and says when `CLAUDE_CONFIG_DIR` chose them. A value that does not name one place is refused before anyone is asked: a relative path, which would be read against the project the server runs in, or the home of a user that does not exist. Installing no longer changes the mode of a directory that was already there.
- **`griot assist install` writes only where it says.** The destination is a directory griot does not own (a project's `.claude/`, or yours). A destination file that is a symbolic link is refused, and in local scope so is a directory that leads out of the project; everything is checked before the first file is written, and `griot_assist_install` refuses before asking. In global scope the directories may be links (a `~/.claude` kept in a dotfiles checkout); only the skill and agent files may not. A file is written beside its destination and moved over it, so an existing file that shares its content with another name (a hard link) is replaced rather than written into. The global instructions file is the exception to all of this: it is yours, it is edited in place through a link if it is one, and only after you answer at a terminal (see the point above). The check and the write are two steps: it protects against what a repository ships, not against a process that swaps a directory for a link while the install runs.
- **The spend ceilings must be numbers.** `GRIOT_SPEND_CEILING_USD`, `GRIOT_SPEND_VELOCITY_CEILING_USD` and the chat price variables are refused at start unless finite and zero or more. They are still read from the environment and from `<config>/.env`: whoever can set those can raise a ceiling, so the breaker bounds accidents, not someone who controls the configuration. `griot config set` asks at an interactive terminal, with no flag that answers, before it raises a ceiling, turns on indexing through MCP, adds a directory an agent may index, or points a platform token at another host; like the other CLI confirmations that is a guard against the easy path for an agent with a shell, not a boundary. No MCP tool changes a setting.
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
