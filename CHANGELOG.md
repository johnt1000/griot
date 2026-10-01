# Changelog

Notable changes to griot. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While griot is `0.x`, the CLI surface and the on-disk layout may change
between minor versions. Breaking changes are called out explicitly.

## [Unreleased]

### Added

- **`griot config` shows and changes the settings.** Every setting was a
  line in `<config>/.env` that took an editor, the variable's name and a
  guess at what a valid value is. `griot config list` shows each one, the
  value in force and where it comes from (environment, file or default);
  `get` prints one; `set` checks the value before writing it; `unset` goes
  back to the default. A change that widens something is asked about at an
  interactive terminal, with no flag that answers: raising a spend ceiling
  (also by unsetting a lower one), turning on indexing through MCP, adding a
  directory an agent may index, pointing the GitLab or Gitea token at
  another host. The GitLab API base must be https. No MCP tool changes a
  setting. A chat profile or a concurrency mode that does not exist is now
  one line that says how to fix it instead of a traceback, and `config set`
  works with a file griot cannot start with, which is how it is repaired.
- **`griot profiles use <name>` makes a profile the active one.** It writes
  `GRIOT_EMBED_PROFILE` to `<config>/.env`, which used to take an editor and
  knowing the variable's name. It asks first when the profile calls an API
  (`--yes` answers), and says what follows: the credential that is missing,
  that the profile has its own index to build, that a running MCP server
  keeps its profile until restarted, and that a variable exported in the
  environment wins over the file. A `GRIOT_EMBED_PROFILE` that names no
  profile is now one line that says how to fix it (exit status 2) instead
  of a traceback on every command, and `profiles use` repairs it.
- **`griot assist install` offers to pre-approve griot's read-only tools.**
  A harness asks a person before each tool call unless its settings allow
  the tool, and an agent that has to ask before every search mostly does not
  search. For Claude Code the installer shows the allow rules (one per
  read-only tool, as the server itself marks them; the quality check keeps
  asking) and the file, and adds them to `permissions.allow` after a typed
  `y`: `~/.claude/settings.json` with `--scope global`, otherwise the
  project's personal `.claude/settings.local.json`. Every other setting
  keeps its value, a rule or pattern already under `deny` or `ask` is left
  alone, and a file griot cannot edit safely (not plain JSON settings, a key
  given twice, read-only) is not touched. A rule matches any MCP server
  named `griot`. No flag answers yes, no MCP tool does it, and
  `--no-allow-tools` skips the question.
- **`griot stats` reports the state of the index, not only activity in a
  window.** It opens with an `Attention:` block when something needs someone
  (the last indexing run died, today's spend reached the ceiling, the
  collection cannot be read) and says how long ago the index was last
  written and last searched, whatever `--days` is. Quality is a line even
  when it was never checked, and says when it was checked before the index
  last changed (for the active profile's collection). The golden set appears
  with its size, the result and age of its last run, and the cases that
  expect a repository that is not in `repos.json`; a golden set file that
  cannot be read is reported instead of being taken for none. Indexing shows
  how many stale points were removed and credential-looking values replaced,
  and latency is given as p50 and p90 instead of an average. `griot_stats`
  returns the same fields (`attention`, `last_indexed_at`, `last_query_at`,
  `golden_set` and the rest), and the `stats` prompt starts from them.
- **A search result says where, when and who.** Each `griot_search` result
  carries `metadata`, what was stored with the source: the path and chunk
  number of a file, the whole hash, author and date of a commit, the
  identifier and date of a tag, branch, pull request, release or issue. The
  label alone cut the hash to eight characters and had no date, so "when did
  this change" could not be answered from a search. An agent now also sees
  the author name stored with a commit, a pull request or an issue.
- **`griot search` takes the filters the MCP tool has.** `--repo NAME` and
  `--source-type KIND` (each repeatable) and `--group-by-document`, with the
  same rule: a repository with nothing indexed or a kind that does not exist
  is an error (exit status 2), not "No results.". A `--limit` below 1 is a
  usage error instead of a traceback. `griot ask` is not narrowed: it still
  searches everything.
- **`griot_search` can be narrowed.** `repos` keeps a search inside the named
  repositories and `source_types` to kinds of source (`code`, `commit`,
  `tag`, `branch`, `merge_request`, `release`, `issue`). A repository with
  nothing indexed or a kind that does not exist is an error, not an empty
  result, and is refused before the query is embedded. `griot_repos_list`
  now gives each repository's `name`, which is what `repos` takes. The
  filters used are recorded with the search in the query log, also when
  `GRIOT_LOG_QUESTIONS=false` withholds the question itself (the names of
  what was returned were already recorded).
- **The MCP server tells the agent when to use it.** It now sends
  instructions at connection: what griot covers (every registered repository,
  not only the current one), when to search it first, when not to (an exact
  string or value, a known path), how to write a query, and to open what a
  result points at. Before, an agent saw only tool names, and sessions
  with the server connected searched with grep instead. The description of
  `griot_search` now leads with the same things.
- **`griot assist install` offers to register griot's MCP server.** With
  `--scope global` for every project (the harness's user scope), otherwise for
  this project only. It shows the exact command and runs it, through the
  harness's own CLI, only after a typed `y`, and says how to undo it; `--mcp`
  answers yes (and the command fails if the registration does) and `--no-mcp`
  skips the question. Without a terminal, or without the harness CLI on the
  PATH, it prints the command instead. A griot that has no absolute path is
  not registered, and one inside a virtual environment is pointed out. Registration used to be documented per
  project only, from when one server held the index for as long as it ran.
- **`griot-operations` skill** in the `griot assist install` bundle — a
  runbook for indexing from inside an agent session: when the attached MCP
  server's own open collection makes a shell `griot index` fail with
  `WouldBlock`, when to use `griot_index_repo` instead, and how to find
  which process holds the collection.
- **`griot assist install --scope global` offers to add griot's instructions to your
  global agent instructions file** (`~/.claude/CLAUDE.md` for Claude Code): a short
  block telling agents in every project when to use `griot_search`. It shows the exact
  text and the file, and writes only when you type `y` at the prompt; with no interactive
  terminal it writes nothing, and there is no flag that answers for you. `--no-instructions` skips
  the question. The block sits between `griot:begin` and `griot:end` markers, text
  outside them is never touched, a symlinked file is written through, and re-running the
  command updates the block in place. The MCP tool `griot_assist_install` never touches
  this file.
- **The project a search or tool call came from is now recorded**: the name of the folder
  `CLAUDE_PROJECT_DIR` names, else of the one griot runs in (never a path), or exactly what you set in `GRIOT_PROJECT`; the home directory counts as unknown.
  `griot stats` shows `by project: ...` under the query count, `griot stats --json` and the
  `griot_stats` tool carry `queries_by_project`, and `tool_calls` gained a `project` column
  that is added in place to an existing `logs.db`, history kept. Records from before are
  counted as `unknown`.

### Changed

- **One document no longer takes every result.** `griot_search`,
  `griot search` and the context `griot ask` builds hold one document to
  three chunks (ungrouped; `group_by_document` still means one), and the
  slots it gives up go to the next best results, so `limit` is now a
  ceiling. A file copied between repositories, or the same commit in a fork,
  comes back once and names the other places found (`also_in`); different
  commits with the same message stay separate. The quality check and the golden set
  still get every point: they measure retrieval, not reading.
- **BREAKING: CLI commands that destroy or widen something now ask first.**
  `griot repos add` and `griot profiles delete` ask `[y/N]` at an interactive
  terminal and have no flag that answers instead: run without a terminal they
  exit with status 2 and change nothing. `griot repos remove`,
  `griot golden-set remove` and `griot auth remove` ask the same way and accept
  `--yes` for scripts. Anything that would be refused anyway (a bad path, an
  unknown profile, an index out of range) is reported before the question.
- **BREAKING: in a git work tree, `griot index code` no longer reads files
  the repository ignores.** It reads tracked files and new ones, and leaves
  out whatever `.gitignore` (or `.git/info/exclude`, or your global excludes)
  matches; a directory outside git is walked as before. Files
  over 1 MB are skipped and reported, and `pnpm-lock.yaml`, `*.min.*` and tool
  caches are left out. If git cannot list a work tree, nothing is read from
  it. Points already indexed from files that are no longer read, ignored
  files included, are removed the next time that repository is indexed (see
  the next entry).
- **Indexing now removes stale points.** A deleted or renamed file, the tail
  of a file that shrank, a file that is now ignored, a deleted branch or tag:
  their points used to stay forever and search kept returning text that no
  longer exists. After a run, the points it did not produce are removed, for
  the `code`, `commits`, `tags` and `branches` sources. It is skipped whenever
  in doubt: with `--path`, for an unregistered repository or an ambiguous
  name, for points another directory of the same name wrote, when nothing was
  read for the source (the last tag or branch of a repository keeps its
  point), when a file could not be read or a document failed, and when more
  than half of a repository's points (above 100) would go at once, unless
  `--prune`. `--dry-run` reports how many would be removed. The index now
  reflects the working tree at the time of the run: indexing on another
  branch, or in a sparse checkout, removes what is not on disk.
- **More file types are indexed**: `.tsx .jsx .mjs .cjs .mdx .sql .yml .yaml
  .tf .toml`. The next `griot index code` embeds them; on a paid profile that
  costs money, and `--dry-run` shows how many chunks first.
- **Bare repositories are no longer read.** `griot index` on a bare clone
  used to index its commits, tags and branches; it is now refused, because a
  bare repository can sit inside another project's files with a git config of
  its own and git cannot tell the two apart. Index a normal clone instead.
- **MCP confirmations reach the person in Claude Code.** The state-changing
  tools ask through the SDK's resolver mechanism, which works on the protocol
  revision Claude Code negotiates; before, the question never arrived and the
  tools fell back to `confirm=true`. Where a client can ask, `confirm=true` is
  now ignored and a decline is final; it still works in a client that cannot
  ask. Questions state the consequence and whether it can be undone.
- `griot_repos_add`, `griot_profiles_delete` and `griot_assist_install` carry
  the `anthropic/requiresUserInteraction` marker. Claude Code run headless
  denies a marked call before it reaches griot, even with an allow rule.
- **`griot_search` returns 8 results by default** (it was 5). Agents overrode the
  old default on about nine calls in ten and asked for 6 to 8, so the default now
  starts there. The `griot search` CLI keeps 5. Pass `limit` to change it, up to 50.
- **`GRIOT_MCP_CONCURRENCY_MODE` now defaults to `multi`** (it was `single`).
  Set `single` to keep the previous behavior. A `<config>/.env` generated by an
  earlier version may pin `GRIOT_MCP_CONCURRENCY_MODE=single` explicitly, which
  overrides the new default: `griot config unset mcp-concurrency` removes it. New `.env` templates
  leave the line commented out.
- With `multi`, anything that needs the collection (`griot index`, `search`,
  `ask`, `quality-check` and the `griot_search`/`griot_quality_check` MCP
  tools) waits up to about 12 seconds for it to free up before failing with
  `Could not open collection ... another griot process still has it open`,
  instead of failing at once with the raw engine error. `griot stats` and
  `griot_index_status` never wait: a held collection is a normal answer for
  them (`point count unavailable`).

- When the collection is held by another process, the CLI now prints one
  explanation instead of a Python traceback: which process holds it (PID,
  command, start time, found with `lsof` when available) and what to do. A
  griot process is shown with its command line, any other process by name
  only. The new `CollectionBusyError` (a `RuntimeError`) carries the
  condition; only the engine's lock error is translated, so a corrupt
  collection still surfaces as itself.

### Fixed

- **`griot assist install --scope global` follows `CLAUDE_CONFIG_DIR`.**
  Claude Code keeps its user files in the directory that variable names and
  does not read `~/.claude` when it is set; the installer wrote to
  `~/.claude` regardless and reported success for skills, an agent, an
  instructions block and tool rules that nothing loaded. All of them now go
  where the variable points (a leading `~` is the home directory), and the
  harness's own `claude mcp add` already did. A value that is not an
  absolute path is refused. Because the MCP tool reads the variable from the
  server's environment, which a project's `.mcp.json` can set, its question
  now names the resolved directories and says when the variable chose them.
  Installing no longer tightens the mode of a directory that already
  existed, and a destination under something that is not a directory is
  refused before anyone is asked. opencode is unchanged.
- **`griot stats` no longer counts an evening's spend twice.** The daily
  total resets at local midnight and the report grouped it by UTC date, so a
  local day that crossed midnight UTC was summed under two dates. Spend is
  grouped by the local day. A search recorded without a duration no longer
  breaks the report.
- **A golden-set case that cannot pass says so.** A case expecting a
  repository with nothing indexed was reported as "expected and not found",
  which read as a search failure. `griot quality-check` now names the
  repository and says the case cannot pass until it is indexed (it still
  counts as failed), and does not embed the query for it.
- **A search whose query could not be embedded says why.** A failed embedding
  call (a bad key, a rate limit, no network) surfaced as a type error about
  vector kinds, in the terminal, to an agent and in the usage log. It now
  says the query could not be embedded and gives the provider's reason.
- **The MCP server no longer writes notices into its own protocol stream.**
  Warnings that a terminal run prints (a collection held by another process,
  an embedding call being retried) went to stdout, which over stdio is the
  JSON-RPC stream. They go to stderr.
- **Indexers exit with an error when they cannot start.** A repository name
  that matches nothing, a missing or empty `repos.json`, or a registry in
  which no path is a directory any more, printed a line and exited with
  status 0; `griot index all` then announced that every source had
  completed. Both now exit with status 1 and write to stderr, also when
  an indexer is run as `python -m griot.index_<source>`.
- **A quality check over nothing no longer passes.** "0 of 0 samples
  recovered" was reported as "Quality OK" with exit status 0 by
  `griot quality-check`, and as zeros with no error by the MCP tool
  `griot_quality_check`, which also created the empty collection it was
  checking. Both now say that nothing was measured; the tool returns an error.
- **A damaged collection is no longer reported as "in use".** It was retried
  for several seconds and then blamed on another process. It now fails at
  once with the real error, and `griot stats` and `griot_index_status` say the
  collection could not be read (`points_error`) instead of "in use".
- **`--path .` names the repository.** A relative path gave the repository an
  empty name in its points, its ids and its labels. The path is now resolved
  first, so a repository indexed through a symlink with `--path` is named
  after the real directory from now on: its next run embeds it again under
  the new name, and the points under the old name stay until the collection
  is deleted.
- **Two registered repositories can no longer share a directory name.**
  `griot repos add` refuses the second one: both wrote the same ids, so each
  run overwrote the other's points and embedded them again.
- The CLI command suggested when `griot_index_repo` is refused now indexes a
  registered repository by name. With `--path` it would have embedded
  everything again under different ids.
- **`--dry-run` no longer leaves a failed run in the history.** A dry-run
  writes nothing when it succeeds, but a dry-run that died on a held
  collection was recorded as a failed indexing run: `griot stats` counted it,
  and it could shadow the last real run in `griot_index_status`. Abbreviated
  flags (`--dry`) count as dry-runs, as argparse treats them.
- **`GRIOT_MCP_CONCURRENCY_MODE=multi` now actually releases an idle
  collection.** The idle check only ran on the server's next tool call,
  which reopened the collection at once, so an idle `griot mcp` held it
  forever and other sessions or a shell `griot index` died with
  `WouldBlock`. The server now runs a reaper thread that releases the handle
  after `GRIOT_MCP_IDLE_RELEASE_SECONDS` without a call, and never while a
  tool is running.

### Security

- **Credential-looking values are no longer embedded, stored or returned.**
  Indexed text used to go to the embedding provider and into the index as it
  was, and search returned it verbatim: a token in a tracked file, a commit
  message or a pull request body travelled both ways. Every text now goes
  through detectors (private key material, a list of provider token formats,
  JWTs, passwords in URLs, authorization headers, random-looking values
  assigned to names like `API_KEY`) before it is chunked and embedded; a match
  is replaced with `[REDACTED:<rule>]` and the run lists where. The same
  replacement is applied to what search, `griot ask` and the quality check
  read from the store, and to the names shown for a result, which covers most
  of what an older version indexed until that repository is indexed again.
  `griot audit` lists where the index still holds such values. It does not
  find passwords in prose, short passwords, personal data or formats that are
  not on the list, and a search query is not scanned.
- A remote URL is no longer printed with the credentials in it, and the names
  of results and findings (search, `ask --show-sources`, the run report,
  `griot audit`) are printed without control characters.
- **Symlinks in a repository are not followed.** A repository could ship
  `notes.md -> ~/.aws/credentials` and have the target indexed, and on a paid
  profile sent to the embedding API. The file and the directories on the way
  to it are checked when listed and again when opened.
- **Indexing a repository no longer runs programs named in that repository's
  git config.** `git log` obeys the repository's own config, and keys such as
  `log.showSignature` with `gpg.program` make it run a program of the
  repository's choosing; a repository received as an archive could therefore
  run code on `griot index`. Every git call now goes through one helper that
  overrides those keys, never fetches (a repository posing as a partial clone
  chose the program that fetched a missing object), and runs git without
  griot's credentials in the environment. A commit message in a legacy
  encoding no longer stops the commits source.
- **The installer does not write through a symbolic link.**
  `griot assist install` opened each destination as it found it, so a project
  that shipped `.claude/skills/<name>/SKILL.md` as a link to another file had
  that file overwritten on install, and `.claude` as a link to another
  directory had the files written there. A destination file that is a link is
  refused in both scopes; in local scope the write must also land inside the
  project. Every destination is checked before the first file is written, and
  the MCP tool refuses before it asks anyone. A `~/.claude` that is itself a
  link (a dotfiles checkout) still works.
- **`griot_index_status` accepts only the collection of a known profile.**
  The name was joined to the data directory as it came, so an absolute path
  or `..` made the server open a directory of the agent's choosing as a
  collection. A collection name that is not a plain name is now refused
  wherever a name becomes a path.
- **A spend ceiling or a price that is not a number is refused at start.**
  `GRIOT_SPEND_CEILING_USD=nan` turned the circuit breaker off without a
  word, because no amount compares as having reached it; an infinite ceiling
  and a negative price did the same. The two ceilings and the chat prices
  must be finite and zero or more, and griot says which variable is wrong
  instead of starting, for every command except `--help` and `--version`.
  A paid call whose cost does not come out as an amount (not a number, or
  negative) stops the run; a negative one used to be dropped as free.
- The CLI command suggested in an MCP refusal is quoted for the shell and for
  the CLI's own parser, and is withheld when a value contains non-printable
  characters. Values chosen by the agent are shown escaped and truncated in the
  question a person reads.

### Performance

- **griot starts in less than half the time.** The local embedding library
  (fastembed, with onnxruntime under it) was imported by every command and
  every MCP server start: about 0.45 s of the 0.7 s that importing griot took
  on the machine it was measured on, 0.3 s without it. It is now loaded when
  a local model is first used; `griot stats`, `griot repos list`
  and anything on an API profile never load it.

## [0.1.0] — unreleased

First public release.

### Added

- **Indexing** — `griot index code|commits|tags|branches|platform|all` over
  registered repositories, into an embedded [Qdrant Edge](https://qdrant.tech/edge/)
  store under the XDG data directory. Incremental by content hash: unchanged
  chunks are never re-embedded.
- **Platform adapters** — GitHub, GitLab (including self-hosted), Bitbucket
  Cloud, Azure DevOps and Gitea/Forgejo, for pull/merge requests, releases
  and issues, detected from each repository's `origin` remote.
- **Search and ask** — `griot search` (vector search, no LLM) and
  `griot ask` (search plus synthesis), with the chat provider selectable
  between Gemini, OpenAI, DeepSeek and Groq.
- **MCP server** — `griot mcp` exposes fifteen tools by default (sixteen
  with indexing enabled) and four prompts over stdio. State-changing tools
  confirm before acting; three of them accept nothing but a human answer. No
  tool accepts or returns a credential.
- **Claude Code / opencode integration** — `griot assist install` detects
  Claude Code and/or opencode on the machine and copies a bundled Skill set
  (onboarding, indexing, workflows, troubleshooting) plus a setup Agent into
  each detected harness's own config directory, local or global scope. Also
  available as the `griot_assist_install` MCP tool, which — like registering
  a repo or deleting a profile — never runs on `confirm=true` alone.
- **Embedding profiles** — six local ONNX profiles (free) and two paid ones
  (OpenAI, Gemini), each in its own collection. `griot profiles delete`
  reclaims a collection's disk space, refusing the active profile and any
  profile currently being indexed.
- **Spend circuit breaker** — daily and five-minute velocity ceilings on
  paid calls, with atomic on-disk state safe across concurrent processes.
- **Credential handling** — `griot auth set/list/remove`, reading keys with
  `getpass` and storing them in the OS keychain when the optional `keychain`
  extra is installed, otherwise in `<config>/.env` at mode 0600.
  `griot auth migrate` moves every credential already sitting in the
  plaintext file into the keychain in one explicit command, a no-op for
  any provider the keychain backend can't reach.
- **Quality checks** — `griot quality-check` (mechanical self-check plus a
  curated golden set) and `griot golden-set` to curate it.
- **Usage reporting** — `griot stats`, with spend against ceiling, reuse
  rate, query latency, and per-tool MCP call counts.

### Security

- Hardening pass across the secret-leak paths, the MCP path allowlist, file
  permissions on everything griot writes, and the platform API adapters.
  Details in [SECURITY.md](SECURITY.md).
- OS keychain storage for credentials via the optional `keychain` extra,
  falling back to `<config>/.env` at mode 0600 where no backend is reachable.
- A permission-repair failure on the vector store's on-disk files is now
  logged (`griot.log`) instead of silently swallowed; still never fails an
  indexing run.

### Housekeeping

- Apache-2.0 license, packaging metadata, and an English README.
- Code, CLI-facing strings and tests are entirely in English (2026-08-20).

[Unreleased]: https://github.com/johnt1000/griot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/johnt1000/griot/releases/tag/v0.1.0
