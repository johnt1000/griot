# What griot stores, and why re-indexing is safe

Two things worth knowing before you trust an index: how a point is
identified, and what each point carries. The first is why running an
indexer twice costs almost nothing; the second is what you get back from
`griot search` and from the `griot_search` MCP tool.

## Stable ids

Every chunk is stored under an id derived from a **natural key** — not a
random one. `common.stable_id()` hashes the key (md5 → UUID), so the same
content always lands on the same id:

| Source | Key |
|---|---|
| code | `repo:code:file_path:chunk_index` |
| commit | `repo:commit:hash` — plus `:chunk_index` only when the message was long enough to split |
| tag | `repo:tag:name` |
| branch | `repo:branch:name` |
| merge/pull request | `repo:mr:iid:chunk_index` |
| release | `repo:release:tag:chunk_index` |
| issue | `repo:issue:iid:chunk_index` |

The commit exception is deliberate and load-bearing. Almost every commit
message fits in one chunk, and those keep the unsuffixed form they have
always had. Appending `:0` unconditionally would have changed the id of every
commit ever indexed — every one of them re-embedded once, and the old point
left behind as a duplicate.

An upsert onto an existing id **overwrites** rather than duplicating, so
re-running any indexer over the same repository is safe with no cleanup
first. Combined with a content hash — a chunk whose content has not changed
is skipped before it reaches the embedding model — a second run over
unchanged repositories embeds nothing and costs nothing.

The `repo` component is the repository's directory name, which is what makes
the same file path in two different repositories two different points.

### Why this is worth stating

An id scheme that looks equivalent but is not will silently double your
index and quietly stop reuse from working. That happened here: one code
path passed a repository by filesystem path while another passed it by
name, producing two different keys for identical content. Nothing failed —
the index simply grew, every run re-embedded everything, and the only
visible symptom was a reuse rate near zero. If you extend griot with a new
source type, the natural key is the part to get right first.

## Payload schema

Every point carries `source_type` and `content` (the indexed text), plus
fields specific to its kind:

| `source_type` | Additional fields |
|---|---|
| `code` | `repo`, `file_path`, `chunk_index` |
| `commit` | `repo`, `commit_hash`, `author`, `date` |
| `tag` | `repo`, `tag_name`, `commit_hash`, `date` |
| `branch` | `repo`, `branch_name`, `last_commit_hash`, `last_commit_date` |
| `merge_request` | `repo`, `mr_iid`, `state`, `author`, `created_at`, `source_branch`, `target_branch`, `chunk_index` |
| `release` | `repo`, `tag_name`, `released_at`, `chunk_index` |
| `issue` | `repo`, `issue_iid`, `state`, `author`, `created_at`, `chunk_index` |

`griot search` always labels each excerpt from these fields —
`commit a1b2c3d4 — my-service`, `MR !245 (merged) — my-api` — and
`griot ask --show-sources` prints the same labels for the context it used.

### Reading this from an agent

`griot_search` returns `source_type` on every result, and it is the field
worth branching on: the same question is answered differently by code (what
the implementation does now), a commit (when it changed), a merge request
(what was argued before it was accepted) and an issue (what problem started
it).

There is no filter parameter — the tool takes a query and a limit — so
covering several kinds of source means varying the query and sorting the
results yourself by `source_type`. The `/mcp__griot__history` prompt exists
to drive exactly that.

`group_by_document=true` collapses a document's chunks to its best-scoring
one, which makes a given number of results reach more distinct files,
commits and PRs. It is off by default: repeated hits on one file are
*different* chunks, not redundancy, so grouping trades depth for breadth
rather than removing waste.

## Stale points

A run upserts what it read and then removes, per repository and source, the
points it did not produce (`common.prune_orphans`). The store is asked what it
holds (a filtered scroll on `repo` and `source_type`) and the answer is
compared with the ids of the documents just built; there is no second record
of what was indexed. Sources are `code`, `commit`, `tag` and `branch`; the
platform sources are never pruned, because a partial API listing is
indistinguishable from deleted items.

`payload.repo` is the directory name, which does not identify a point: a
`--path` run of another directory of the same name writes it too, under a
different id key. So a point counts as the repository's only when its id is
the one its own payload produces under the repository's key
(`_point_ids_written_under`); anything else is left alone.

Removal is skipped for a `--path` run, for a repository that is not registered
or whose directory name another registered repository shares, when nothing was
read for that source, when a file could not be read (`code`), when a document
failed, when the index lock is taken, and when more than half of the
repository's points (above 100) would go, unless `--prune`. The run prints why.

## Chunking

Text is split at 1500 characters with a 200-character overlap. The overlap
is why two adjacent chunks of one document share a little text, and why a
match near a boundary is still retrievable from either side.

Commit bodies are chunked like any other text. They were not always — a
large commit message used to be sent whole and rejected by the embedding
API, which showed up as a stable count of failures that reappeared on every
re-index.
