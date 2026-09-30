# Changelog

Notable changes to griot. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While griot is `0.x`, the CLI surface and the on-disk layout may change
between minor versions. Breaking changes are called out explicitly.

## [Unreleased]

### Added

- **`griot-operations` skill** in the `griot assist install` bundle — a
  runbook for indexing from inside an agent session: when the attached MCP
  server's own open collection makes a shell `griot index` fail with
  `WouldBlock`, when to use `griot_index_repo` instead, and how to find
  which process holds the collection.

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
