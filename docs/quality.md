# Quality tooling

Checking that retrieval finds what it should: the self-check, the curated
golden set, and growing that set from the questions actually asked.

```bash
griot quality-check              # self-check: sampled points must find themselves
griot golden-set suggest ~/code/my-app  # derive curated test cases from that repository's git log (human-approved)
griot golden-set review          # curate cases from the questions actually asked (at a terminal)
griot golden-set add "query"     # curate a case from a real search
```

## `griot quality-check`

`griot quality-check` scores retrieval against your curated golden set —
useful before/after switching embedding profiles. Each case is searched in the
mode it was made in, over every repository and the way readers search: at most
three chunks of one document, and the same text found in several places comes
back once, the other places named in the result's `also_in`. A case whose
document came back only as such a copy passes, and says so (`met_by_copy`:
which result carried it, under which name). `griot golden-set list` shows each
case's mode. `griot golden-set add`, `griot golden-set suggest` and the
`griot_golden_set_add` tool make a `hybrid` case by default, like
`griot search`, so a case measures what a reader gets;
`add --mode vector|keyword|hybrid` picks another. That holds with nothing
indexed yet too: an index run builds keyword vectors. On a collection without
keyword vectors the default makes a `vector` case instead, says so and names
`griot index keywords` (a hybrid case there would only ever be skipped); on
one whose config cannot be read it makes a `vector` case and says that. A case
in the file without a mode, as every case before modes was, is a vector case.
A keyword or hybrid case on a collection without keyword vectors is reported
as skipped, naming `griot index keywords`, neither passed nor failed and left
out of the pass rate ("8 of 8 passed, 2 skipped"); the report says how many
cases were searched in each mode.

## `griot golden-set review`

`griot golden-set review` grows the golden set from real use. It reads the
query log (`griot ask` and the `griot_search` tool record each question) and
offers, at most `--limit` (10) at a time: first the questions asked more than
once, most asked first, in any session or project, including the same session
(an agent retrying a search counts too; the same words count as the same
question, whatever the case, punctuation or word order); then the vector
searches whose best result scored in the bottom quarter of that collection's
vector searches (once there are at least 20 of them; keyword and hybrid scores
are not similarity, so they are not used); then, newest first, the hybrid
searches whose two rankings disagreed: the first result by meaning (vector) is
not in the first 10 by the words (keyword), and the first by the words is not
in the first 10 by meaning, both among the results the search returned; then,
newest first, the searches that were reworded: the next search of the same
session came within 5 minutes, is not the same question, shares at least half
of the shorter question's words of four letters or more (normalised as for
asked more than once; shorter words such as "how" or "the" say nothing about
the subject), and brought back other results, an implicit sign the first list
did not serve.

The first search is the one offered, with the rewording shown beside it, since
the rule cannot tell a rewording from a question about another facet of the
same thing and you can.

Each logged search records its session as an opaque digest, never the value it
came from: of the conversation id the agent client exports, when it exports
one (Claude Code's `CLAUDE_CODE_SESSION_ID`, which it gives both to
`griot mcp` and to every command its shell tool runs, so a `griot_search` and
a later `griot ask` of the same conversation pair, although each shell command
runs in a new shell), and otherwise of the process that holds the conversation
(the agent session that started `griot mcp`, or the shell `griot ask` ran in),
never the process number. opencode exports no conversation id, so there a
`griot ask` run through its shell tool is a session of its own. A search
logged before griot recorded sessions is never paired, and neither is one from
a process with no parent of its own and no client id.

A hybrid search logs, beside each result, its rank in each ranking (numbers
only, from the two lists the fusion already has: nothing more is embedded or
searched); a search logged before that, or one whose keyword ranking matched
nothing, is never offered as a disagreement. A result one ranking did not
place at all counts as past the first 10 only when that ranking looked at
least 10 deep, since otherwise it could have been 6th or 9th there, which is
not a disagreement; every search `griot ask` and `griot_search` log looks that
deep from `--limit 2` up (each ranking is fetched several times wider than the
list returned), and a one-result search has nothing to disagree about. Only
`griot_search` logs a best score, so a `griot ask` question can be offered as
asked more than once or as a disagreement but never as scoring low.

For each it shows the question, why it is a candidate, and the results logged
for it (with each result's rank by meaning and by the words, for a hybrid
search, and the rewording, for a reworded one); you type the number of the
right one (or several), `n` when none of them was, `s` to skip, `r` to reject
it for good, `q` to stop. A pick becomes a case exactly as `golden-set add`
makes one, asserting that result comes back. Questions already in the golden
set and ones you rejected are not offered again; rejections are kept as
digests, not text, in `golden_set_rejected.json` beside the golden set.
Nothing is written to the index.

A case keeps the search mode its results came from (the mode that ran: vector,
keyword or hybrid), and is checked by a search in that mode over every
repository, the way readers search and not grouped by document (see
`griot quality-check`), so only a search like that can become one: one
narrowed with `repos` or `source_types`, or one grouped by document, is shown,
with the reason, but cannot be picked (when the same question was also asked
unnarrowed and not grouped by document, that asking is the one offered). The
same goes for a search logged before griot recorded what a case needs, and for
a result whose name looked like a credential. With `GRIOT_LOG_QUESTIONS=false`
there is nothing to review, and the command says so.
