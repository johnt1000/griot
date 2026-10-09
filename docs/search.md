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
`griot_search`'s output, a `Mode:` line from `griot search`).

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
```

## Whether the index is behind

Each indexing run records what every repository looked like (its HEAD, and the
tag and remote-branch refs), so `griot_index_status` says, per registered
repository, whether its index is behind the repository and how: commits made
since the code and commits sources ran (or that the indexed commit is no
longer in the history: rewritten, or another branch checked out), whether the
tags or remote branches changed (the base branch counts, since each branch is
described against it), and which sources never ran; `griot_search` names the
repositories among its results that are behind (`behind`), and `griot stats`
lists them under attention. Pull requests and issues live on the platform, so
nothing local can say whether that source is behind.

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
