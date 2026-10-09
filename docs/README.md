# griot documentation

The [README](../README.md) is the short version: what griot is, how to install
it and a first run. These pages hold the rest.

## Using griot

| | |
|---|---|
| [Getting started](getting-started.md) | Installing, a first index and search, `griot stats`, `griot doctor` and `griot update` |
| [Configuration](configuration.md) | Embedding and chat profiles, every environment variable, where things live, `griot config` |
| [Credentials](credentials.md) | `griot auth`, the OS keychain and `.env`, `griot auth migrate`, and credentials in indexed content |
| [Search](search.md) | Search modes, filters, grouping, whether the index is behind, and `griot ask` |
| [Quality](quality.md) | The golden set, `griot quality-check` and `griot golden-set review` |
| [MCP server](mcp.md) | Registering the server, the skills and agent for Claude Code and opencode, and running several sessions |
| [Platforms](platforms.md) | The five platform adapters, their tokens, and what has run against a real platform |
| [indexing-model.md](indexing-model.md) | What a point is keyed by, what its payload carries, and why re-indexing is idempotent |

## The project

| | |
|---|---|
| [SECURITY.md](../SECURITY.md) | What leaves your machine, what is protected on disk, and the MCP threat model |
| [ROADMAP.md](../ROADMAP.md) | What is planned — and what was considered and rejected, with reasons |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | What the code expects from a change |
| [CHANGELOG.md](../CHANGELOG.md) | Released changes |

## For maintainers

Written for whoever works on griot itself rather than for its users.

| | |
|---|---|
| [mcp-capability-coverage.md](mcp-capability-coverage.md) | Which MCP protocol capabilities griot uses, the CLI↔MCP parity table, and the confirmation policy |
| [lessons-and-debts.md](lessons-and-debts.md) | Decisions this project learned the hard way, and what it knowingly left undone |
