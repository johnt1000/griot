# Search

How `griot search` and the `griot_search` MCP tool rank and narrow results,
and how `griot ask` builds an answer on the same search.

## Grouping and filters

`griot_search` takes an optional `group_by_document`: off by default (up to
three chunks of one document, so it can answer in some depth without taking
every slot), on when you want breadth (the best chunk of each document, so the
same number of results reaches more files, commits and PRs).

`repos` and `source_types` narrow a search to some repositories and to some
kinds of source (`code`, `commit`, `tag`, `branch`, `merge_request`,
`release`, `issue`); a repository with nothing indexed, or a kind that does
not exist, is an error rather than an empty result.

## Search modes

`mode` picks how results are ranked: `hybrid` (the default) by meaning and by
the exact words, the two rankings fused by rank; `vector` by meaning alone;
`keyword` by the exact words, BM25 over the text plus file path, commit hash
and ref names, for a commit hash, an error code, or where a name is used (it
embeds nothing, so it is free on any profile, and returns only chunks holding
a word of the query). Measured on two repositories (MRR@10), `keyword` ranked
commit hashes far above `vector` (1.00 against 0.01) but put the file that
defines a function or class name first less often (0.61 against 0.72), since
the chunks that mention a name are mostly where it is used; `hybrid`, the
default, is the mode for the rest, a name's definition included. Scores are on
each mode's own scale, and every result says which mode ran (`mode` on
`griot_search`'s output, a `Mode:` line from `griot search`, or `mode` with
`--json`).

An index made before keyword search existed needs `griot index keywords` once
(local, embeds nothing, needs about as much free disk as the collection while
it runs); until then the default searches by meaning and says so with that
command (with every `griot search` and `griot ask`; on the first default
search of an MCP server process, after which `mode` alone says it), an
explicit `keyword` or `hybrid` is refused with that command in the message,
and `griot doctor` and `griot_index_status` (`keyword_search`) say whether it
has run.

## From the command line

`griot search` takes the same as `--repo`, `--source-type`,
`--group-by-document` and `--mode`, and `griot ask` takes `--repo`,
`--source-type` and `--mode`.

```bash
griot search "where is the retry logic for the payment API?"
griot search "ERR_CONNECTION_REFUSED" --mode keyword
griot search "retry policy" --json        # one JSON document on stdout
```

### `griot search --json`

One JSON document on stdout and nothing else there; notices, warnings and
errors stay on stderr. Its keys are those of `griot_search`'s output, built
by the same function, so a script or an agent reading it through a shell
gets what the tool gives:

| Key | What it holds |
|---|---|
| `results` | the hits, best first; `[]` when nothing matched (exit 0) |
| `results[].source_label` | where the hit comes from, as the text output shows it |
| `results[].repo`, `results[].source_type` | the repository (its directory name) and the kind of source |
| `results[].metadata` | what was stored with the source: `file_path` for code; `commit_hash`, `author`, `date` for a commit |
| `results[].content` | the stored text of the chunk |
| `results[].score` | on the scale of the mode that ran |
| `results[].also_in` | other places the same text was found; absent when none |
| `mode` | the mode that ran (`vector`, `keyword` or `hybrid`) |
| `behind` | `{repository: commits behind}` for the repositories among the results whose index is behind their HEAD (`null` when the indexed commit is not in the history); `{}` otherwise |
| `note` | that the results are retrieved data, never instructions; why nothing was found when filters narrowed the search; the keyword-index note when the default fell back to `vector`; and last, which repositories are behind, ending with the command that catches each one up |

The `note` differs from the tool's in two ways: `griot search --json` says
every current condition on every run (one process per command), where the
server says each one the first time its process sees it, and again when it
changes (a repository falling into another band of commits behind, another
collection without keyword vectors), so the tool's `note` is often empty; and
it ends with one `griot index all --repo NAME` per repository that is behind,
each on its own line and quoted so it pastes (for example
`griot index all --repo 'my repo'`), where the tool names `griot_index_repo`
when `GRIOT_MCP_ENABLE_INDEX` allows it.

The keyword-index note is the one condition said in two places by the CLI,
on purpose with two rules: plain `griot search` and `griot ask` say it on
stderr once per collection (the ledger remembers it across commands), since a
person reads every command; `griot search --json` puts it in the note every
time, since a script or agent reading one document has nothing else to
remember it by. Either way the ledger records it, so the closing
``1 notice: run `griot doctor` `` line does not count it as news.

A filter that cannot match (`--repo` with nothing indexed) is an error on
stderr with exit 2 and nothing on stdout, as without `--json`. `--limit` is
not held to `griot_search`'s ceiling of 50.

## Whether the index is behind

Each indexing run records what every repository looked like (its HEAD, and the
tag and remote-branch refs), so `griot_index_status` says, per registered
repository, whether its index is behind the repository and how: commits made
since the code and commits sources ran (or that the indexed commit is no
longer in the history: rewritten, or another branch checked out), whether the
tags or remote branches changed (the base branch counts, since each branch is
described against it), and which sources never ran; `griot_search` names the
repositories among its results that are behind (`behind`, on every result; its
`note` says it in words, with the command that indexes them, the first time the
server sees a repository fall behind or fall into another band of commits:
1-9, 10-99, 100-999, 1000 or more, or a history rewritten), and `griot stats`
and `griot doctor` list them every time. Other commands end, when something
new needs attention, with one line on stderr, ``1 notice: run `griot doctor` `` (again a
week later if nothing changed; `--quiet` silences it). Pull requests and issues live on the platform, so
nothing local can say whether that source is behind. `griot index all
--if-stale` indexes the repositories this finds behind, and nothing else
([keeping the index fresh](getting-started.md#keeping-the-index-fresh)).

## `griot ask`

`griot ask` finds its context the way `griot search` does, with the same
flags: `--repo` and `--source-type` (each repeatable) keep the context to some
repositories and kinds of source, and `--mode` picks how it is ranked. A
repository with nothing indexed, or a kind that does not exist, is an error
before anything is embedded or the chat model is called, so it costs nothing.
When the search finds nothing (filters that each exist but match nothing
together, such as commits of a repository with none indexed, or an empty
index), `ask` prints `No results.` and the filters that narrowed it, as
`griot search` does, and does not call the chat model (nor names one in the
query log). `--limit` takes what `griot search` takes, a whole number from 1:

```bash
griot ask "why do we retry on 409?" --repo my-app --source-type commit --source-type merge_request --show-sources
```
