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
| tag | `repo:tag:name` — plus `:chunk_index` only when the message was long enough to split |
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
unchanged repositories embeds nothing and costs nothing. The fields stored
beside the text are compared as well: when one changed and the text did not
(a pull request that was merged, a tag that got the hash of its commit), it
is written without embedding anything, and the run says how many points
that was.

The `repo` component is the repository's directory name, which is what makes
the same file path in two different repositories two different points. For a
`--path` run the id key is the name followed by a short hash of the path, so
that two directories of the same name indexed that way do not share ids; the
`repo` stored with the point is still the directory name.

### Why this is worth stating

An id scheme that looks equivalent but is not will silently double your
index and quietly stop reuse from working. That happened here: one code
path passed a repository by filesystem path while another passed it by
name, producing two different keys for identical content. Nothing failed —
the index simply grew, every run re-embedded everything, and the only
visible symptom was a reuse rate near zero. If you extend griot with a new
source type, the natural key is the part to get right first.

## Payload schema

Every point carries `source_type`, `content` (the indexed text) and
`content_hash` (what decides whether it is embedded again), plus fields
specific to its kind:

| `source_type` | Additional fields |
|---|---|
| `code` | `repo`, `file_path`, `chunk_index` |
| `commit` | `repo`, `commit_hash`, `author`, `date`, `chunk_index` |
| `tag` | `repo`, `tag_name`, `commit_hash` (the commit the tag leads to, not the tag object), `date`, `chunk_index` |
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

Each result also carries `metadata`: the fields of the table above, except
`content`, `content_hash`, `repo` and `source_type`, which are beside it or
not for a reader. That is what to act on (open that file, show that commit,
say when). `also_in` is added when the same thing was found in another place.

`griot_search` takes `source_types` (and `repos`) to narrow a search to
some kinds of source: commits and pull requests when the question is why
something changed, code when it is how something works. The
`/mcp__griot__history` prompt drives a question across the kinds.

What comes back is arranged for a reader. One document fills at most three
results, so a long file does not take every slot, and a file or a commit
that is indexed in more than one place (a copied file, a fork) comes back
once, naming the other places it was found; two different commits with the
same message stay two results. `group_by_document=true` goes further and keeps only the
best-scoring chunk of each document, which makes a given number of results
reach more distinct files, commits and PRs: repeated hits on one file are
*different* chunks, not redundancy, so grouping trades depth for breadth.
The quality check and the golden set do not get this arrangement: they
measure retrieval itself, point by point.

## Two vectors per point

Each point carries two vectors. `dense` is the embedding of the text by the
active profile's model: it is what a search by meaning (`--mode vector`)
compares. `bm25` is a sparse BM25 vector of the same text, computed
locally by the vector store's own BM25 model (English stemming and
stopwords), plus the identifying fields the text does not hold: `file_path`,
`commit_hash` and `last_commit_hash` (whole, and abbreviated to 7 to 12
characters, as git abbreviates them), `tag_name`, `branch_name`,
`source_branch` and `target_branch`. It is what a search by the exact words
compares (`--mode keyword`): a function name, an error code, a file name, a
commit hash. `--mode hybrid` runs both and fuses the two rankings (reciprocal
rank fusion, k=60); it is the default of `griot search`, `griot ask` and
`griot_search`, because it ranks descriptive questions as well as vector does
and identifiers and commit hashes far better. Where it cannot run, the
default runs vector instead of failing: on a collection without keyword
vectors (and then it says so, naming `griot index keywords`), or for a query
with no word keyword search can match. Every result says which mode ran. The
quality check, the golden set and the retrieval evaluation measure vector
search explicitly, whatever the default. A keyword search embeds nothing, so it costs nothing on
any profile; scores are on each mode's own scale and are not comparable
across modes.

A collection indexed before keyword search has only the dense vector, and the
store cannot add a vector to points it already holds. `griot index keywords`
copies such a collection into a new one with both vectors, reading the dense
vectors back rather than embedding anything, and puts it in the old one's
place: while it runs it needs about as much free disk as the collection, and
it holds the index lock and the collection, so nothing else can index or
search that profile until it finishes. Run again, it writes nothing; run
after an interruption, it starts the copy over (and a collection left aside
halfway through the swap is put back the next time anything opens it). Until
it has run, an explicit `--mode keyword` or `--mode hybrid` is refused with
that command in the message, and the default searches by meaning as before
and says so. New points are
written with both vectors once the collection has room for them.

## What is stored is not always what was read

Credential-looking values are replaced before chunking and before the
content hash is taken (`common.chunk_text`, `common._split_pending`), so the
hash is of the stored text. A point written by a version that did not do this
has the hash of the raw text: the next run sees it as changed, embeds the
replaced text and overwrites it. Readers go through `common.stored_text`.

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
or whose directory name another registered repository shares, for a directory
registered twice (by a symlink and by its real path), when nothing was
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
