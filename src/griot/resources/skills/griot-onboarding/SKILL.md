---
name: griot-onboarding
description: Use when the user has just installed griot (or is about to) and hasn't indexed anything yet — covers what griot actually is, the real first-run cost, registering a repo, a zero-cost dry run, running a real index, and the first search/ask. Not for day-to-day usage of an existing index (see griot-workflows) or for indexing decisions on an already-set-up project (see griot-indexing).
---

# griot onboarding

## What griot actually is

griot is local-first RAG over your git repositories. It indexes source code,
git history (commits, tags, branches) and code-platform data (PRs/MRs,
releases, issues) into an **embedded** vector store — no database server, no
Docker — and lets you search it or ask questions about it, from the CLI or
from an AI agent via MCP. There is no web UI; everything happens through the
`griot` CLI and, if you're reading this from inside an agent, through the
MCP tools this skill pack sits alongside.

## Install

```bash
git clone https://github.com/johnt1000/griot && cd griot
pipx install .
```

Requires Python >= 3.10 and `git`. If griot is already installed and
attached as an MCP server in this project, you can skip straight to
registering a repo below.

## The real first-run cost (read this before you index anything)

griot's default embedding profile, `jina-code`, runs locally via ONNX — no
API key, no per-query cost. But **the first time you use any local profile,
griot downloads that profile's ONNX model from Hugging Face** and caches it
under your data directory. For the default `jina-code` this is about
**1.1 GB**. This is not configurable or optional — it's a one-time network
call to `huggingface.co` that carries none of your data, and every run after
that is fully offline. Budget a few minutes and some disk space for it
before your first real index.

Nothing else leaves your machine in the default configuration. Paid
profiles (OpenAI, Gemini) only send data when you explicitly select them.

## Make the tools available to your agent

griot's MCP server has to be registered with the agent before its tools show
up in a session. `griot assist install` offers to register it for every
project (`--scope local` for this project only). It looks first at what is
registered already and offers to replace a registration whose command is
gone; otherwise it shows
the command and runs it only after you type `y`, and prints how to undo it.
With a local embedding profile, remember that each open session runs its own
server, and each one loads the model on its first search (`griot profiles
list` shows how much memory each profile is estimated to take).

The same command then offers to let the agent call griot's read-only tools
(search, status, lists, usage) without asking the user each time: it shows
the allow rules and the settings file, and adds them only after the user
types `y` at a terminal. This step is the user's: there is no flag that
answers yes and no tool that does it, so do not try to do it for them. griot
adds no rule for the tools that change anything, nor for the quality check.

## Register a repo

```bash
griot repos add ~/code/my-app
```

It asks you to confirm, and needs an interactive terminal to do so: there is
no flag that answers for you, because a registered path is one whose contents
may be sent to an embedding API. Run from a shell with no terminal, as an
agent would, it exits with status 2 and changes nothing: this step is yours.

This adds the resolved path to `<config>/repos.json`, the list `griot index
all` (without `--path`) walks. You can register as many repos as you want;
`griot repos list` shows what's registered and flags any path that no
longer exists on disk.

## Dry run first — this never spends anything

Before indexing for real, especially if you're on a paid profile or just
want to know what you're about to pay for in local compute time:

```bash
griot index all --repo my-app --dry-run
```

`--dry-run` only counts how many chunks would need to be (re)embedded — no
local CPU spent embedding, no API call, no cost of any kind. This is the
safe way to answer "how big is this going to be" before committing to a
real run. It works for any single source too (e.g. `griot index code --repo
my-app --dry-run`).

## Index for real

```bash
griot index all --repo my-app
```

`griot index all` runs five sources in a fixed order: `code`, `commits`,
`tags`, `branches`, `platform` (code first because it's the largest volume;
platform last because it's the only one that depends on an external
API — see the `griot-indexing` skill for adapter setup). It stops at the
first source that fails, so you always know which one broke. Filter with
`--sources code,commits` if you only want part of the pipeline.

Re-running this over unchanged content costs nothing — griot skips any
chunk whose content hash hasn't changed. See `griot-indexing` for the full
incremental-indexing model.

## First search

```bash
griot search "where is the retry logic for the payment API?"
```

This is vector search only — free, local, no LLM call, even on a paid
embedding profile (aside from embedding the query itself). It prints each
hit's source label, score, and a one-line content preview.

## `griot ask` — plainly, this one is paid

```bash
griot ask "how does authentication work in this codebase?" --show-sources
```

`griot ask` is search **plus LLM synthesis** — it requires a working chat
provider (`gemini` is the default chat profile, needing `GEMINI_TOKEN`; see
`griot auth set gemini`). Do not treat this as a free convenience layer:
every call to `griot ask` spends money against your chat provider, bounded
by the spend circuit breaker (daily + 5-minute ceilings — see
`griot-troubleshooting` if it ever refuses a call). `griot search` costs
nothing; `griot ask` does. Reach for `search` by default and `ask` when you
specifically want a synthesized, cited answer.

## What to read next

- **`griot-indexing`** — the five sources in depth, incremental behavior,
  choosing an embedding profile, setting up a platform adapter (GitHub
  etc.), reclaiming disk space.
- **`griot-workflows`** — day-to-day usage once an index exists: search vs.
  ask, reading `source_type`, the MCP prompts if you're working through
  Claude Code with griot's MCP server attached.
- **`griot-troubleshooting`** — what to do when indexing, search, or `ask`
  fail, with the real cause and fix for each.
