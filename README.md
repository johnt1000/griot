# griot

[![CI](https://github.com/johnt1000/griot/actions/workflows/ci.yml/badge.svg)](https://github.com/johnt1000/griot/actions/workflows/ci.yml)

Local-first RAG over your git repositories. griot indexes source code, git
history (commits, tags, branches) and code-platform data (PRs/MRs, releases,
issues) into an **embedded** vector store — no database server, no Docker —
and lets you search it or ask questions about it, from the CLI or from an AI
agent via [MCP](https://modelcontextprotocol.io/).

Named after the West African storyteller who keeps a community's history:
griot remembers what your repositories have been through.

## Highlights

- **Fully local storage** — vectors live in an in-process
  [Qdrant Edge](https://qdrant.tech/edge/) shard under your XDG data dir. No
  services to run.
- **Free by default** — the default embedding profile (`jina-code`) runs
  locally via ONNX. Paid profiles (OpenAI, Gemini) are opt-in.
- **Spend circuit breaker** — daily ceiling + 5-minute velocity ceiling on
  every paid call, with atomic on-disk state. The `openai` and `deepseek` chat
  profiles refuse to run until you set their price
  (`griot config set openai-chat-price <USD per 1M tokens>`, or
  `deepseek-chat-price`), so the breaker always tracks real cost.
- **Five platforms** — GitHub, GitLab (incl. self-hosted), Bitbucket Cloud,
  Azure DevOps and Gitea/Forgejo adapters for PRs/releases/issues, detected
  from each repo's `origin` remote. What has run against a real platform:
  [Platforms](https://github.com/johnt1000/griot/blob/main/docs/platforms.md).
- **MCP server** — expose search/status/quality tools to Claude Code, opencode
  or any MCP client. Indexing via agent is off by default and
  path-allowlisted.
- **Security-hardened** — API keys never in URLs or logs, 0600/0700 file modes
  on everything it writes, no credential ever follows a redirect. See
  [SECURITY.md](https://github.com/johnt1000/griot/blob/main/SECURITY.md).

**Status:** v0.5.0, beta. One maintainer, used daily by its author. The CLI
surface and the on-disk layout may still change between 0.x releases.

## Installation

```bash
pipx install griot-rag    # the distribution is griot-rag; the command is griot
griot update              # later: upgrade griot to the newest release (shows the command, asks first)
```

Requires Python ≥ 3.10 and `git`. **The first run of a local profile downloads
its ONNX model from Hugging Face** (~1.1 GB for the default `jina-code`) and
caches it under your data directory; `griot profiles list` shows each
profile's RAM tier against the RAM you actually have.

Installing from a checkout, what the first run downloads, and what
`griot update` does:
[Getting started](https://github.com/johnt1000/griot/blob/main/docs/getting-started.md).

## Quickstart

```bash
# 1. register the repos you want to index (asks you to confirm, in a terminal)
griot repos add ~/code/my-app

# 2. see what would be indexed (never spends anything)
griot index all --repo my-app --dry-run

# 3. index for real (default profile is local & free)
griot index all --repo my-app

# 4. search (no LLM: by meaning and exact words; --mode keyword for a hash or an error code)
griot search "where is the retry logic for the payment API?"

# 5. ask (search + LLM synthesis — requires a chat provider, see Documentation below)
griot ask "how does authentication work in this codebase?" --show-sources
```

When something does not work, run `griot doctor`: every check at once, reads
only. See
[Getting started](https://github.com/johnt1000/griot/blob/main/docs/getting-started.md#when-something-does-not-work-griot-doctor).

## Documentation

| Guide | |
|---|---|
| [Getting started](https://github.com/johnt1000/griot/blob/main/docs/getting-started.md) | Installing, a first index and search, `griot stats`, `griot doctor` and `griot update` |
| [Configuration](https://github.com/johnt1000/griot/blob/main/docs/configuration.md) | Embedding and chat profiles, every environment variable, where things live, `griot config` |
| [Credentials](https://github.com/johnt1000/griot/blob/main/docs/credentials.md) | `griot auth`, the OS keychain and `.env`, `griot auth migrate`, and credentials in indexed content |
| [Search](https://github.com/johnt1000/griot/blob/main/docs/search.md) | Search modes, filters, grouping, whether the index is behind, and `griot ask` |
| [Quality](https://github.com/johnt1000/griot/blob/main/docs/quality.md) | The golden set, `griot quality-check` and `griot golden-set review` |
| [MCP server](https://github.com/johnt1000/griot/blob/main/docs/mcp.md) | Registering the server, the skills and agent for Claude Code and opencode, and running several sessions |
| [Platforms](https://github.com/johnt1000/griot/blob/main/docs/platforms.md) | The five platform adapters, their tokens, and what has run against a real platform |
| [All documentation](https://github.com/johnt1000/griot/blob/main/docs/README.md) | Every document: security, the roadmap, contributing, the changelog, and the maintainers' own |

## Development

```bash
uv sync --locked --extra dev   # the versions CI runs (or: pip install -e ".[dev]")
uv run pytest -q               # no test ever calls a real API or needs credentials
```

## License

[Apache-2.0](https://github.com/johnt1000/griot/blob/main/LICENSE)
