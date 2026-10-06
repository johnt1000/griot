# Changelog

Notable changes to griot. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While griot is `0.x`, the CLI surface and the on-disk layout may change
between minor versions. Breaking changes are called out explicitly.

## [Unreleased]

### Changed

- **The git hooks refuse a commit message that credits an AI assistant.**
  `commit-msg` refuses a co-author line naming an assistant, a session link
  or a "Generated with" line, and `pre-push` refuses them in the commits a
  push sends (a cherry-pick or a rebase runs no commit hook). It does not
  depend on `.git/sensitive-terms.txt`. Naming an assistant otherwise is
  fine. See CONTRIBUTING.md.

### Fixed

- **`griot doctor` no longer fails a new installation.** The `.env` griot
  writes on first use leaves `GRIOT_MCP_INDEX_ROOTS` and `GRIOT_GITEA_HOSTS`
  empty, which their readers take as "none"; the settings check judged them
  with the rules of `griot config set`, which refuse an empty value, and
  reported FAIL on every installation after its first command. An empty
  value now passes the check for the settings whose readers take it as
  their default (the two lists, `log-questions` and `mcp-index`), and still
  fails it where griot cannot use it (a number, a choice, a model, a URL).

- **A platform that refuses a fetch is a failure of the run, not only a
  warning.** `griot index platform` printed a warning for a fetch of pull
  requests, releases or issues the platform refused, and counted nothing:
  when an expired token was refused everything, the run said "No platform
  items to index.", exited 0 and left no record, so `griot index all`,
  `griot stats` and the freshness report saw nothing wrong. Now each refused
  fetch counts in the run's `failed` and is listed among its failures by
  HTTP status (never the error's text, which can hold the request URL); a
  run where everything was refused exits 1, which also stops `griot index
  all`, and is recorded as a run that did not do its job, so `griot doctor`
  and `griot stats` report it and the source does not read as indexed. A
  run where only some fetches were refused exits 0 and shows them as
  failures, like a document that failed to embed; a dry run records nothing.

## [0.2.1] — 2026-10-05

### Security

- **What reaches `main`, and what reaches PyPI, goes through the same
  checks.** A release used to need only a tag: it now runs the whole CI on
  the tagged commit before building, and refuses a tag on a commit that is
  not on `main`. The secret scan runs gitleaks itself, installed by version
  and checksum, and reads the changes of merge commits too (the action did
  not, and ran on a Node version GitHub is retiring). The tests run on
  Python 3.14 and on macOS as well, the runners are named by version
  (`ubuntu-24.04`, `macos-15`) instead of `-latest`, and the tests that need
  the real scanner run instead of being skipped.
- **The locked dependencies are past known vulnerabilities.** `uv.lock`
  moves `pyjwt` to 2.15.0 and `urllib3` to 2.8.0. The published package does
  not pin them, so an install from PyPI already picked up the fixed releases;
  this is what CI and a development checkout run.

### Fixed

- **A credential exported in the shell no longer hides silently behind the
  one griot stores.** A key exported in `~/.zshrc` (or any shell file) wins
  over `<config>/.env` and the keychain, as it should, and nothing said so:
  `griot auth set github` wrote the new token and reported success, every
  new terminal kept using the old exported one, and the platform answered
  401 with nothing saying which token it had been given. Now `griot auth
  set` warns when an export with another value will keep winning, naming
  the shell file and line (never the value); `griot auth list` says when
  the key in use comes from the shell and differs from the stored one (it
  compared with the `.env` only, not the keychain); `griot auth remove`
  says when an export is still there; a platform, embedding or chat API
  that refuses a key (401/403) says where the key came from and what to do;
  and `griot doctor` has a `credentials` check for the same.

## [0.2.0] — 2026-10-05

The first version published to PyPI, as `griot-rag` (the name `griot`
there is somebody else's; the command and the import stay `griot`).

### Added

- **`griot doctor`.** One command that checks what the scattered error
  messages checked one at a time. It changes no setting, index or file of
  yours (loading the configuration closes a `.env` left open to others, as
  every command does, and asking the harness about its registration may
  start the registered server for a moment; both are said). It checks:
  settings the file
  holds that griot cannot start with (the one check that runs even when the
  configuration cannot load), the configuration file and directories closed
  to other users, the active profile and its credential, the collection,
  the registered repositories and whether their index is behind, today's
  spend against the ceiling, the MCP registration (and a registration whose
  command is gone), which read-only tools still ask before every call, the
  variables a server started with this environment would ignore, git, the
  log. Each says ok / warn / FAIL / skip with what to do; `--json` for one
  document; exit status 1 only for a failure.
- **Which repository is behind its own repository.** `griot stats` said
  when the index was last written, for the whole collection; the question
  an agent has before trusting a result is whether what griot holds about
  THIS repository is behind what the repository holds now. Every indexing
  run now records what each repository it covered looked like (its HEAD,
  and a fingerprint of its tag and remote-branch refs), and griot compares
  that with the repository of the moment, each source by what it indexes:
  code and commits by the commits made since (or "the indexed commit is
  not in this history" when it is no longer an ancestor of HEAD: rewritten,
  or another branch checked out), tags and branches by whether their refs
  changed (all remote branches for the latter, the base included, since
  each branch is described against it), and the platform source not at all
  (pull requests live on the platform). `griot_index_status` carries, per
  registered repository, `behind`, `behind_sources`, `commits_behind`,
  `missing_sources` (never indexed) and the per-source detail;
  `griot_search` names the repositories among its results that are behind
  (`behind`, and a sentence in `note`); `griot stats` lists them under
  attention and in its state lines, and names repositories whose code was
  never indexed. A run from before this says nothing about freshness.
- **`griot_golden_set_suggest` gives an agent candidate cases from a git
  log.** What `griot golden-set suggest` offers at a terminal: a commit's
  message as the question and the files it touched as what must come
  back, for one registered repository, over a bounded stretch of its log.
  It writes nothing: each candidate carries what `griot_golden_set_add`
  takes, and a person confirms it there. It says how many commits it left
  out and why, and whether the repository has anything indexed. Read-only,
  and asked about each time.
- **`griot_audit` tells an agent where the index holds credential-looking
  values.** The same places `griot audit` lists at a terminal, with the
  rule that matched and a count per repository, and never the values. One
  call reads a bounded number of stored points and says so when it stopped
  there; `repos` reads one repository at a time. Over nothing indexed it is
  an error, not a clean result. It changes nothing, and it is still asked
  about each time: the installer does not offer to pre-approve it.
- **`griot_config_list` shows the settings an MCP server is running with.**
  `griot config list` answers for a new process; a server read the file
  once, when it started, and its environment can come from where it was
  registered. Per setting the tool gives the value in that server, where it
  came from (the environment, the file, or the default), what the file says
  now, and `restart_needed` when the two differ and a restart would apply
  the change. It lists no credential, replaces one written inside a URL,
  and changes nothing; the installer offers it among the read-only tools
  that may run without a prompt.
- **`griot_quality_check` runs the curated golden set too.** It ran the
  self-check only, so an agent asked whether the index can be trusted had
  half an answer and the `health` prompt sent the rest to a terminal. The
  tool now returns both, apart: the self-check's counts, and `golden_check`
  with each curated case, whether it passed, what was missing, and a reason
  when it could not pass at all. When the cases are not run (`golden_set=false`,
  none curated, or more than one call runs) `golden_check` is null and
  `golden_set_not_run` says why. The run is recorded for the trend, as the
  terminal's is.
- **`griot_index_preview` says what an index run would do.** Per source:
  how many chunks would be embedded, how many are up to date, how many
  stale points a plain run would remove and how many it would hold back
  (more than half of a repository), and on a profile that bills an estimate
  of the cost. It runs the same `--dry-run` the CLI has, in a subprocess:
  nothing is embedded, removed or recorded. A dry run no longer creates an
  empty collection for a profile that was never indexed. It is
  there whether or not indexing through MCP is enabled, so that an agent can
  say what a reindex would cost before anyone decides. The same paths are
  allowed as for a real run. Read-only, and not among the tools the
  installer offers to pre-approve: it reads the whole repository.
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

- **BREAKING: `griot assist install` installs for every project by default.**
  `--scope` now defaults to `global` (the harness's user directory); pass
  `--scope local` for the current project only. A bare install used to go
  into whatever directory it was run in, and never offered the instructions
  block.
- **The installer asks in the order things depend on each other, and looks
  before it asks.** The MCP server comes first: the installer asks the
  harness what is registered under the name griot (`claude mcp get`). A
  server that runs this griot is left alone, one that runs anything else
  that is still there is pointed out and left alone, and one whose command
  no longer exists is offered to be replaced, at the scope being installed
  only (`--mcp` replaces it without asking). A broken registration at
  another scope is never removed, only pointed out. The tool rules and the
  instructions block come after it and are offered only when the server is
  there: rules for a server that is not registered, and instructions to use
  tools that do not exist, were worse than nothing. `--no-mcp` still means
  "I look after the server myself", and the two are offered; they are also
  offered when the harness's command line is not installed, since then
  nothing could be looked at. It ends with a
  summary of what was done, what was left out and what comes next.
  `--skills-only` copies the files and asks nothing. With no supported
  harness on the machine the command now exits with status 1.
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

- **`griot mcp` starts on Python 3.10 again.** `griot_stats` and
  `griot_index_status` each declared a field of their answer as
  `NotRequired[...]`. The MCP SDK reads an answer's fields with
  `typing.get_type_hints`, which from Python 3.11 takes that qualifier off
  and on 3.10 leaves it there, and the model it builds is then refused: on
  the oldest Python the package supports, importing the server failed and
  it did not start. Nothing showed it: the tests had only run on a newer
  Python. The field is declared in
  a `total=False` base instead, which every version reads the same way; the
  schema a client sees is unchanged.
- **A branch named like a path in the work tree is listed.** With a
  directory `origin/feat` in the work tree, git stopped at the branch
  `origin/feat` with "ambiguous argument: both revision and filename", and
  the branches source skipped the branch without a word. The name is now
  given as a revision and nothing else.
- **The index is not closed under a call that is using it.** In a server
  with more than one tool call at a time, three things could take the
  collection from under a call or leave it held for good. A call arriving
  after the idle window closed and reopened the handle, whoever was using
  it. Two calls that both found it closed both opened it, and one of the
  two handles was never closed. And `griot_index_repo` closed it to make
  room for the index run whatever else was in flight. Opening and closing
  are now one at a time; only the idle reaper, which counts the calls in
  flight, lets go of an idle handle; and the index tool, like the preview,
  waits a few seconds for the other call to finish and otherwise answers
  "another call is using the index" instead of closing it. It checks
  before asking for confirmation as well as after.
- **A control character in a commit does not stop a source.** The git log,
  the tags and the branches were read with the control characters 0x1f and
  0x1e between fields and records, and git accepts both in a commit
  message, a tag message and an author's name. One such commit ended the
  commits source with a ValueError, and `griot index all` with it, on every
  run; the same went for the tags, for the last commit of a branch and for
  `griot golden-set suggest`. They are now read with NUL between fields,
  the one character git refuses there, and output that does not divide
  into whole records is left out with a warning instead of a traceback.
- **An annotated tag carries the hash of its commit.** What was stored as
  `commit_hash` was the hash of the tag object, which names no commit and
  matches nothing in the commits source. A tag of a tag is followed to the
  commit at the end.
- **A long tag message is cut into chunks.** A release note kept in a tag
  was one document whatever its length: the model read only its start, and
  an API that refuses an oversized input failed that tag on every run. It
  is cut the way a commit message is; a tag that fits in one chunk keeps
  its id. The subject is no longer repeated in the text of a tag that has
  a body, and a signed tag's signature block is no longer part of it:
  those tags are embedded again once.
- **A detail that changed while the text stayed is written.** What decides
  whether a document is embedded again is its text, so the fields stored
  beside it were never updated on their own: a pull request merged without
  a change of wording kept saying `opened`, and a tag corrected by the
  entry above would have kept the wrong hash. They are now compared and
  written without embedding anything, and the run reports how many points
  that was.
- **`griot golden-set suggest` offers only cases that can pass.** A
  candidate required every file its commit touched that still existed:
  files `griot index code` never reads (an image, a lock file, a file the
  repository ignores) included, and any number of them, when a case is
  searched with five results. Approved, such a case failed forever. A file
  is now required only if the index would hold a chunk of it (read by
  `griot index code`, and not empty); a commit that touched more files
  than a search returns, or that has no message, is left out; and the
  command says how many commits it left out and why. A file whose name is
  not ASCII is no longer dropped as missing, a directory inside a work
  tree gets its own paths instead of the tree's, a directory that is not a
  git work tree is an error instead of "no candidates", and `--limit`
  counts candidates, not commits.
- **The golden set file is written through a rename.** It was written in
  place, by every command that changes it: a write that stopped half-way
  left a file nobody could read, and every curated case with it.
- **A yes/no setting is read with the spellings `griot config set` takes.**
  The command accepts yes/no/on/off and writes true/false, but the readers
  knew two spellings each: a line written by hand as
  `GRIOT_LOG_QUESTIONS=no` kept logging questions, and
  `GRIOT_MCP_ENABLE_INDEX=yes` did not enable the tool. Both now read
  true/yes/on/1 and false/no/off/0.
- **A golden set file that cannot be run names the file and the case.**
  `quality_golden_set.json` is edited by hand; a case without its `query`
  or `must_include` ended `griot quality-check` in a traceback naming the
  missing key, after the self-check had run. It is now checked before
  anything is embedded (a `limit` that is not a whole number of at least 1
  is refused the same way), and when a search fails in the middle of the
  curated cases the self-check that was already done is still recorded.
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

- **An MCP server obeys its environment only where that narrows
  (BREAKING for a server configured through its `env`).** A server is
  started by whoever registered it, and a registration can come with a
  project: a `.mcp.json` in a cloned repository could name `griot mcp`
  with an `env` that turned on indexing through MCP, added directories an
  agent may index, raised the spend ceilings, pointed a platform token at
  another host or switched a local embedding profile for one that calls
  an API, and the server did as told. For a server, a variable that would
  widen what your own `<config>/.env` says is now ignored, with a line on
  stderr and an `environment_ignored` field in `griot_config_list`; one
  that narrows is obeyed, in the form it was measured in (a directory as
  the real path it names), and so is `--profile` on the command line. A
  value the setting cannot take no longer stops the server: it is ignored
  like one that widens (a profile name that does not exist still stops
  it, as before). A server also refuses to start when its configuration or data
  directory is inside the project it was started in, so that a repository
  cannot bring its own settings and index and have them served with your
  credentials. If your own registration turned something on through
  `env`, set it with `griot config set` (or `griot profiles use`) instead.
  Commands at a terminal are unchanged: they obey the shell they run in.
  Not covered: variables that are not griot's settings (a proxy, a
  certificate bundle, a credential) in a server's `env`.
- **What the git hooks refuse is refused by every route into the history.**
  Three things went past them. A line of a commit message that starts with
  `#` was taken for one of git's template comments and not read, although a
  message given with `-m` or `-F` has no template and git keeps the line.
  A merge that makes a commit runs `pre-merge-commit`, not `pre-commit`, so
  nothing looked at what the other branch brought. And the list of files
  that never belong knew `.env` but not `prod.env`, `.env-prod`, `.env~` or
  `app.env.local`, nor a tracked link replaced by a real file; it goes by
  the `.env` in a name now (a name without one, such as a bare `env`, is
  still not recognised), and `.env.sample`, `.env.template` and `.env.dist`
  are allowed beside `.env.example`. All three are closed. A cherry-pick, a
  fast-forward merge, `git am` and most of a rebase run no commit hook at
  all, so there is now a `pre-push` hook: it looks at every commit a push
  would send that the remote does not have, for secrets, files that never
  belong and private terms (in added lines, file names, commit and tag
  messages, branch and tag names), and prints locations only. It refuses to
  push when it could not read the changes, and a tag that points at a file
  or a directory instead of a commit. A private list saved with Windows
  line endings matched nothing, in every hook, and one that starts with a
  byte order mark lost its first term; both match now. And every hook reads
  bytes: in a UTF-8 locale a file in another encoding (or a file name) was
  enough for a term on the same line, or the name itself, to go unseen at
  commit time as well. A file git calls binary is still not read.
- **A secret that only a merge commit holds is found.** gitleaks reads
  `git log -p`, which prints no diff for a merge unless asked, so whatever a
  merge itself added was never scanned, by `scripts/audit-history.sh` or by
  anything else. The audit and the new `pre-push` hook ask for the changes
  of merge commits as well.
- **What the build runs and installs is pinned.** The CI named its actions
  by a tag, which the owner of an action can move to other code, and
  installed the newest version of every dependency on each run although the
  repository carries a lock file with a hash per package. Actions are now
  referenced by commit, the checkout leaves no token behind for later
  steps, and everything installed comes from `uv.lock` with its hash
  checked: the dependencies, the wheel's smoke test, the build backend and
  the metadata checker (the build fails when the lock is out of date).
  Dependabot proposes the updates of the actions and of the lock. For an installation from
  the package index, `qdrant-edge-py` and `mcp`, which griot calls
  directly, are held below the version that may change their interface
  (`<0.9`, `<3`).
- **The directory that holds griot's data is closed to other users.** The
  index, the logs and the configuration were each written 0600 inside 0700
  directories, but `<data>/griot` itself was made on the way to `logs/` by
  whichever command ran first, with the permissions of the day (0755), and
  only an index run closed it afterwards. The model cache was open too.
  Every directory griot itself makes is now 0700, the ones made on the
  way included, and griot closes its own data and configuration
  directories again when it finds them open, as an earlier version left
  them. (What the embedding library makes inside the model cache keeps the
  library's modes, under a closed directory.)
- **A paid call whose answer reports no tokens is still counted.** The
  cost of a call was the token count the provider reported times the
  price. An answer without one (the field missing, null or zero) cost
  nothing: the day's total did not move and the spend ceiling never came
  closer, for embeddings and for `griot ask` alike. Such a call is now
  counted from the size of the text sent and received, in bytes, at a rate
  that errs on the side of counting more, and never as nothing; griot says
  once that the figure is an estimate. A chat answer that cannot be read
  is counted before it fails.
- **A token glued to an underscore is recognised.** The formats known by
  their prefix (GitHub, GitLab, Slack, Stripe, OpenAI, AWS key ids and the
  rest) were matched from a word boundary, and an underscore is a word
  character: `fix_<token>` as a branch or file name, or `<token>_old`, was
  not seen at all, so it was stored and shown as it was. They now begin
  and end wherever a letter or a digit does not continue them. What an
  earlier version indexed this way is still in the store: `griot audit`
  lists it, and indexing the repository again rewrites it.
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

- **Calls to an embedding or chat API go over a connection that is kept.**
  Every call opened its own: a TCP and a TLS handshake before each batch of
  an index run, and before each search of a server that stays up for days.
  Measured against a real endpoint, a call took about 385 ms on a new
  connection and about 240 ms on a kept one. A kept connection that the
  other end closed while it sat idle is tried again at once on a fresh one,
  instead of falling into the ten or twenty seconds the calls wait when an
  API cannot be reached. The session keeps nothing but the connection: no
  cookie is stored, and a credential goes in its own call's headers.
- **Local indexing is faster and takes a fraction of the memory.** A local
  model was handed 128 texts at once, a number taken from a rule of thumb
  and never measured on the chunks griot cuts (up to 1500 characters). It
  is handed 8 now. Measured on chunks of this repository: with `bge-small`,
  128 chunks took 56 s and 3.0 GB before and 31 s and 0.7 GB after; with
  `jina-code`, the default, 2.0 chunks per second and 1.4 GB at 8 against
  0.35 chunks per second and 2.8 GB at 64, and no answer in fifteen
  minutes at 128. Throughput is flat from 4 to 16 texts and falls after
  that; memory grows with every step. Only two of the local models were
  measured: the others get the same size, which is the cautious one.
- **An OpenAI-compatible profile sends 128 texts per request instead of
  50.** 50 is what Gemini's batch call was tuned for and applied to every
  API profile. The embeddings endpoint takes 2048 inputs and 300,000 tokens
  per request: 128 chunks of code or English are well under that, and an
  index run makes two and a half times fewer requests. A round of text
  that costs more than a token per character (Chinese, emoji) is cut into
  requests by its size in bytes, which bounds its tokens, so it cannot go
  over the limit; a request that fails takes only its own texts with it.
  Each profile now says its own batch sizes.
- **The branches source no longer runs two git processes per branch.** It
  ran one for the last commit of every remote branch and one for what each
  has ahead of the default branch. The last commits of all branches are
  now read in two calls, and one more says which branches have anything
  ahead: the list of those commits is asked for only where there are any,
  and on most repositories nearly every remote branch was merged long ago.
  Measured on a repository with 300 remote branches, 60 of them ahead: 602
  git calls and 14.2 s before, 65 calls and 1.5 s after. The documents are
  the same as before, so nothing is embedded again: the names are resolved
  and the commits formatted by git itself, as before, only for all
  branches at once. Where git cannot answer for all of them together, each
  branch is asked on its own as it was.
- **griot starts in less than half the time.** The local embedding library
  (fastembed, with onnxruntime under it) was imported by every command and
  every MCP server start: about 0.45 s of the 0.7 s that importing griot took
  on the machine it was measured on, 0.3 s without it. It is now loaded when
  a local model is first used; `griot stats`, `griot repos list`
  and anything on an API profile never load it.

## [0.1.0] — never published

What the repository held when it was made public; the first version on
PyPI is 0.2.0.

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

[Unreleased]: https://github.com/johnt1000/griot/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/johnt1000/griot/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/johnt1000/griot/releases/tag/v0.2.0
