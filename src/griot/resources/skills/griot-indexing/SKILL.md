---
name: griot-indexing
description: Deep guide to griot's indexing model — the five sources and what `griot index all` runs in what order, incremental/content-hash behavior, choosing an embedding profile, setting up a platform adapter (GitHub/GitLab/Bitbucket/Azure DevOps/Gitea), and reclaiming disk space with `griot profiles delete`. Use when deciding how or what to index, not for day-to-day search/ask usage (see griot-workflows).
---

# griot indexing

## The five sources

`griot index all` runs, in this fixed order:

1. **`code`** — reads each registered repo's files, chunks each one, embeds
   it. In a git work tree it reads **what the repository does not ignore**:
   tracked files and new ones, but never a file matched by `.gitignore`.
   Outside git it walks the directory. Extensions: `.py .md .mdx .js .mjs .cjs .jsx .ts
   .tsx .java .cs .php .cpp .go .rb .rs .scala .html .css .sol .sh .sql .yml
   .yaml .tf .toml`. Left out either way: `node_modules`, `dist`, `build`,
   `.git`, `__pycache__`, `.venv`/`venv`, `vendor`, `.next`, `target`,
   `coverage` and tool caches; `pnpm-lock.yaml` and `*.min.*`; symlinks
   (not followed); and any file over 1 MB, which is reported.
2. **`commits`** — git commit history.
3. **`tags`** — git tags.
4. **`branches`** — git branches.
5. **`platform`** — PRs/MRs, releases, issues from the repo's code hosting
   platform (see below). The only source that depends on an external
   network/API, which is why it runs last.

Code runs first because it's the largest volume; platform runs last because
it's the only one with an external dependency. `griot index all` stops at
the **first** source that fails, printing which one and why — it never
continues past a broken source, because doing so would mask the problem and
the next run would just re-pay the cost of discovering it again.

Run one source directly instead of all five with `griot index <source>`
(e.g. `griot index commits --repo my-app`) — each source module has its own
`--repo`/`--path`/`--dry-run` flags, shown by `griot index <source> --help`.

### Flags (verified against `src/griot/index_code.py`, representative of all five source modules)

- `--repo <name>` — indexes one repo by its directory name from
  `repos.json`, instead of every registered repo. Mutually exclusive with
  `--path`.
- `--path <dir>` — indexes an arbitrary directory directly, bypassing
  `repos.json` entirely. Useful for a one-off index you don't want
  registered.
- `--dry-run` — counts how many chunks would need to be (re)embedded
  without spending anything (no local CPU for embedding, no API call), and
  how many stale points would be removed.
- `--prune` — removes stale points even when they are more than half of
  what is indexed for a repository (see "Stale points" below).
- On `griot index all` specifically, `--sources code,commits` (a
  comma-separated subset of `code,commits,tags,branches,platform`) filters
  which sources run.

## Incremental behavior — why re-indexing is nearly free

Every chunk is stored under an id derived from a **natural key**, not a
random one (`common.stable_id()`, documented in full in
`docs/indexing-model.md`) — e.g. `repo:code:file_path:chunk_index` for code,
`repo:commit:hash` for a commit. The same content always lands on the same
id, so re-running an indexer over an unchanged repo **overwrites** existing
points rather than duplicating them.

On top of that, a **content hash** check skips any chunk whose content
hasn't changed before it ever reaches the embedding model. So a second run
over a repo with no real changes embeds nothing and costs nothing — this is
what `--dry-run` reports as "N chunks would need to be (re)embedded, M
already up to date."

This only holds if the *natural key* stays consistent across runs — this
project has been bitten once by passing the same repo by filesystem path in
one code path and by name in another, which silently produced two different
keys for identical content and made reuse collapse to near zero. If you
ever see indexing runs that never reuse anything for a repo you know hasn't
changed, that mismatch is the first thing to check (`griot stats` reports
the reuse rate — see `griot-troubleshooting`).

## Stale points — what is removed, and when it is not

After a run, points whose source is gone are removed: a deleted or renamed
file, the tail of a file that shrank, a file that is now ignored, a deleted
branch or tag (as long as at least one remains: see below), a commit no ref
reaches any more. Search stops returning text
that no longer exists. It compares what the run read with what the index
holds for that repository and source; nothing else is recorded.

Removing is the one destructive step, and a removed point costs an embedding
to bring back, so it is skipped whenever in doubt:

- with `--path` (only a repository registered in `repos.json` is pruned, and
  only if no other registered repository has the same directory name), and
  for points a `--path` run of another directory of the same name wrote,
  and for a directory that is registered more than once (by a symlink and by
  its real path, say);
- when the run read nothing for the repository and source (so the LAST tag
  or branch of a repository, once deleted, keeps its point), when a file
  could not be read (`code` source), or when a document failed to be written;
- when more than half of the repository's points (and more than 100) would
  go at once. That usually means another branch is checked out. The run says
  so; `--prune` goes ahead.
- never for the `platform` source: a failed or partial API listing looks the
  same as deleted items.

A registered name means the directory registered under it NOW: replace one
registered `api` with another directory called `api`, and the next run
removes what the first one left.

The index reflects the working tree at the time of the run. Indexing on one
branch and then on another removes what only the first had, and re-embeds it
if you switch back. The same goes for a sparse checkout: files that are not
on disk are gone as far as the run can tell. Below 100 points, or below half
of a repository, nothing holds the removal back.

## Choosing an embedding profile

```bash
griot profiles list
```

This detects your machine's real RAM (via `psutil`) and classifies each
**local** profile by RAM tier against it:

| Profile | Backend | Cost | Notes |
|---|---|---|---|
| `jina-code` (default) | local ONNX | free | code-specialist, 768-dim |
| `bge-small`, `nomic-q`, `mxbai-large`, `bge-m3`, `bge-large-en` | local ONNX | free | RAM-tiered alternatives |
| `openai-small` | OpenAI API | paid | needs `GRIOT_OPENAI_API_KEY` |
| `gemini` | Gemini API | paid | needs `GEMINI_TOKEN` |

RAM tiers are `light` (>=8GB), `medium` (>=16GB), `heavy` (>=32GB) — the
output flags a profile as "might be tight" if your machine is below its
tier's floor, but never blocks you from selecting it. For the two paid
profiles, `griot profiles list` also reports whether the relevant
credential is configured.

Select a profile with `GRIOT_EMBED_PROFILE` (persistent, via `.env`) or
per-invocation with `--profile <name>` on `index`/`search`/`ask`. **Each
profile gets its own collection** — vectors from different models aren't
comparable, so switching profiles means indexing from scratch under the new
one, not reusing the old collection.

The free-vs-paid tradeoff is straightforward: local profiles cost nothing
per call but use your own CPU/RAM at index and query time; paid profiles
offload that to an API and cost real money per embedded chunk and per
query, gated by the spend circuit breaker (`griot-troubleshooting`).

## Platform adapters

`griot index platform` (part of `index all`) reads PRs/MRs, releases and
issues from the repo's code hosting platform, auto-detected from the repo's
`origin` git remote. griot ships adapters for five platforms: GitHub,
GitLab (including self-hosted, via `GRIOT_GITLAB_API_BASE`), Bitbucket
Cloud, Azure DevOps, and Gitea/Forgejo (recognized hosts configured via
`GRIOT_GITEA_HOSTS`).

**State this plainly to the user**: these adapters are built against each
provider's documented API and covered by tests with mocked HTTP, but **only
the GitHub path has been exercised against a live account**. If you're
setting up GitLab/Bitbucket/Azure DevOps/Gitea for the first time, expect
to be the first real-world validation of that path and watch the first run
closely.

Platform tokens (only needed for `griot index platform`):
`GITHUB_TOKEN`, `GITLAB_PERSONAL_ACCESS_TOKEN`, `BITBUCKET_ACCESS_TOKEN`,
`AZURE_DEVOPS_PAT`, `GITEA_TOKEN`. Set via `griot auth set <provider>`
(hidden input, goes to the OS keychain when available or `<config>/.env` at
0600 otherwise).

## Reclaiming disk space

```bash
griot profiles delete <profile>
```

Permanently deletes that profile's on-disk collection. It asks for
confirmation at an interactive terminal and has no flag that answers instead,
so the user runs it, not an agent. It refuses to delete
the **active** profile (switch to a different one first with
`GRIOT_EMBED_PROFILE`/`--profile`) and refuses a profile that was never
indexed (nothing to delete). This is irreversible — there's no undo short
of re-indexing from scratch.
