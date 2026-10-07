# Lessons and open debts

What this project decided the hard way, and what it knowingly left undone.

Two kinds of thing outlive a decision once it has been carried out: the
reasoning someone would otherwise re-derive (or re-litigate), and the work
that was consciously skipped. Both are here so neither has to be rediscovered
from the code.

---

## Open debts

Things deliberately not done. Each was a decision, not an oversight; each is
still true today unless noted.

### 1. ~~Credentials in `.env` move to the keychain only when asked~~ — resolved

Still opt-in by design, now visible: when a keychain is reachable, `griot doctor` and `griot auth list` name each credential still in plaintext and the command that moves it (#34).

### 2. ~~Keychain protection is opportunistic, not universal~~ — resolved

Still a fallback, no longer silent: `griot auth list`, `griot auth set` and `griot doctor` say where each credential is kept and whether a keychain backend exists (#34).

### 3. ~~`_keychain_delete()` cannot tell "nothing stored" from "backend error"~~ — resolved

Resolved through keyring's own `PasswordDeleteError`, without coupling to a backend: `griot auth remove` says when the keychain could not be reached (#31).

### 4. ~~Permission repair reruns on every cold collection open in `multi` mode~~ — resolved

Resolved: a reopen walks only the directories that changed, with a full check after every write and at least every ten minutes (#35).

### 5. ~~Permission repair is best-effort per file~~ — resolved

Resolved: a failed chmod is retried once, and what is still open to others shows in `griot doctor` (#35).

### 6. ~~`griot stats` renders trends as text, not charts~~ — resolved

Resolved: terminal charts without a new dependency, plain text where a chart cannot be drawn (#42).

### 7. ~~The MCP confirmation policy is explained in three places, not one~~ — resolved

The three statements stay, each for its reader; tests/test_confirmation_policy_docs.py now checks all three against the server (#21, #29).

### 8. ~~`griot stats` mixes two clocks and two scopes~~ — resolved

Resolved: local days everywhere, and the active collection by default with `--all-profiles` for every one (#41).

### 9. ~~A search for readers returns up to `limit`, not exactly `limit`~~ — resolved

Resolved: a short list asks the store for a wider window, a bounded number of times, in every search mode (#36, #40).

### 10. ~~The self-check counts a sample with no text as passed~~ — resolved

Fixed in #16; what changed is in the changelog.

### 11. ~~Two threads can build the local model twice~~ — resolved

Fixed in #17; what changed is in the changelog.

### 12. ~~Writing a setting replaces a symlinked `.env` with a regular file~~ — resolved

Fixed in #18; what changed is in the changelog.

### 13. ~~The confirmations in the CLI are a guard, not a boundary~~ — resolved

Resolved as a recorded decision: a process that can run arbitrary commands as the user can answer any terminal question, so the confirmations are a guard against the plain command. SECURITY.md, each command's help and its refusal say so, and a test holds the wording (#27).

### 14. ~~`uv.lock` is written by more than one version of uv~~ — resolved

Regenerated with the pinned uv (#22); CONTRIBUTING.md says to lock with it, and a test keeps that note in step with the workflows (#28). It recurs after each Dependabot lock update until the lock is written again: see debt 27.

### 15. ~~Where a credential comes from is read from shell files only~~ — resolved

Resolved for direnv: the `.envrc` (or the `.env` it loads) and the line are named (#33). A desktop session or an IDE run configuration still reads as "this shell".

### 16. ~~The attribution rule stops where the hooks stop~~ — resolved

Resolved: a required check reads every pull request's title and description with the hooks' pattern (#23, #30). What is left is debt 26.

### 17. ~~One repository refused entirely does not stop a platform run~~ — resolved

Resolved: the run exits 1 naming it (#24), and `griot doctor` and `griot stats` name it too (#32). What is left is debt 19.

### 18. ~~The mutation evidence of 2026-10-06 needs checking again~~ — resolved

Resolved: the guards of #12 to #42 were mutated again with specs that select their tests; 15 survivors got a test and the rest are argued equivalent (#44, #45).

### 19. ~~A platform run where everything was refused records no `refused_repos`~~ — resolved

Resolved: the run records them, and doctor and stats read the newest platform run's refusals whatever ran after it (#59).

### 20. ~~A shorter log retention deletes history without asking~~ — resolved

Resolved: shortening the retention says what the next prune will delete and asks; without a terminal it is refused (#52).

### 21. ~~`GRIOT_UPDATE_CHECK` can be turned on by a server's environment~~ — resolved

Resolved: a server's environment may turn it off, never on (#46).

### 22. ~~The progress of an index run does not survive a server restart~~ — resolved

Resolved: the job registry is kept in a private file and a restarted server picks up a run still going, checked against pid reuse (#57).

### 23. ~~`griot index keywords` starts over after an interruption~~ — resolved

Resolved: an interrupted copy resumes, re-copying only missing or changed points, and the swap still needs the whole copy verified (#56).

### 24. ~~The MCP resources read a private attribute of the SDK~~ — resolved

Resolved for the resources (#53); `tools_safe_to_preapprove` still uses a private one, see debt 31.

### 25. ~~`griot stats` counts resource reads under "MCP tools"~~ — resolved

Resolved: tool calls and resource reads are counted apart (#50).

### 26. ~~A Dependabot description quoting a release note can fail the text check~~ — resolved

Resolved: a quoted line passes, and a line that credits an assistant is still refused (#54).

### 27. ~~The lock goes back to Dependabot's form after each of its updates~~ — resolved

Resolved: Dependabot's lock pull requests get the lock rewritten with the pinned uv by a workflow that runs no pull request code with write rights; its commit needs one approval click to start the checks (#55).

### 28. ~~Where a credential came from is worded three ways~~ — resolved

Resolved: one helper words it everywhere, with direnv advice for a direnv file (#51).

### 29. ~~The management-surface table checks examples, not every read-only tool~~ — resolved

Resolved: the read-only row lists every read-only tool and is checked both ways; `griot_index_repo`'s confirmation is proven by behaviour (#58).


### 30. ~~The release build caches what it downloads~~ — resolved

Resolved: the build sets up uv without a cache, and every zizmor finding in release.yml is fixed or justified (#70). Two newer GitHub features (`cache-mode`, the `$/` form) were left out on purpose: no run had proven them.

### 31. ~~`tools_safe_to_preapprove` lists tools through a private SDK attribute~~ — resolved

Resolved: it reads the server's public tool list (#66). Its test still used the private one as a reference: debt 38.

### 32. ~~Below Python 3.13 the server sends tool descriptions indented~~ — resolved

Resolved: descriptions are cleaned at registration and the same on every Python (#67). What they contain is debt 37.

### 33. ~~A platform run refused entirely is reported twice~~ — resolved

Resolved: once, by the line naming its repositories (#68).

### 34. ~~`griot index keywords` does not say that it resumed~~ — resolved

Resolved: it says so and how much was already copied (#69).

### 35. ~~Two pieces of code the mutations proved unreachable~~ — resolved

Resolved: removed, with the equivalent re-checks beside them (#65).

### 36. ~~The resources heading of `griot stats` is out of line~~ — resolved

Resolved: every heading's value starts at the same column (#64).

### 37. ~~MCP prompt descriptions carry notes meant for maintainers~~ — resolved

Resolved: a tool's or prompt's description stops at a `Maintainer notes:` line, every prompt and argument has a description for agents, and a test reads what a client receives (#77).

### 38. ~~The pre-approval test still reads a private SDK attribute~~ — resolved

Resolved: the test reads what a client is offered (#76), and then what each tool does when called, not its markers (#84, debt 45).

### 39. ~~`griot update` reaches into `griot doctor`'s private helpers~~ — resolved

Resolved: the three helpers are public and update.py calls them by those names (#75).

### 40. ~~The note that keyword search is not built repeats on every search~~ — resolved

Resolved: the MCP server gives it once per process and collection; the CLI, read by a person one command at a time, keeps it (#79). How it is remembered: debt 46 (#88).

### 41. ~~The measurement that made hybrid the default cannot be re-run~~ — resolved

Resolved: `scripts/measure-search-modes.py` and this repository's queries are committed (#81); its scoring is tested (#89, debt 48) and it runs on repositories in any language (#93, debt 52).

### 42. ~~The documentation still says keyword is better for identifiers~~ — resolved

Resolved: every text that recommends a mode says keyword is for a commit hash, an error code or where a name is used, and a test holds them to it (#80).

### 43. ~~Readers' searches are diversified, the golden set's are not~~ — resolved

Resolved: the golden set is searched as readers search (#82), and a case met by a folded copy passes and says so (#90).

### 44. ~~The kept HTTP session has a private name other modules use~~ — resolved

`griot doctor` reached `common._http_session` across a module boundary. Resolved: it is public as `common.http_session`, and a test fails if another module names the private one (#83).

### 45. ~~The pre-approval test reads the markers it checks~~ — resolved

Its expected set was filtered by the same read-only hint, human-only marker and exception list the server uses, so a misreading both shared would pass. Resolved: every tool is called through an in-memory client in a throwaway world, and is safe when the call changes no file, asks no one, embeds at most one text and reads only part of the index (#84).

### 46. ~~The fallback note's memory is unlocked and never reset~~ — resolved

Two first searches at once could both give the note, and a collection told once was never told again after losing keyword vectors a second time. Resolved: it is checked and recorded under a lock, and forgotten once a default search finds keyword vectors (#88).

### 47. ~~Other modules reach common.py's private names~~ — resolved

auth, config, mcp_server, quality_check and redaction read private names of common.py. Resolved: those are public, and a guard fails on any private name of common.py used outside it, in tests too unless listed with a reason (#87).

### 48. ~~`measure()` of the measurement script is untested~~ — resolved

The loop that produces MRR@10 and recall@10 had no test. Resolved: it runs on a tiny real index with a stand-in embedding whose ranks are known (#89).

### 49. ~~Cases from `golden-set suggest` have no mode~~ — resolved

`suggest` wrote cases without a mode, so they were vector cases while `add` made hybrid ones, and the default on an empty collection was untested. Resolved: suggest goes through the same default and write path as add, and an empty collection gets hybrid (#92).

### 50. ~~The README calls the golden-set check ungrouped~~ — resolved

The README and the indexing model still described the check as an ungrouped search, and only a docstring kept the folded copies' whole payload away from readers. Resolved: both describe the check as it runs, and a test checks every reader for a copy's fields (#91).

### 51. ~~Index progress files pile up in the system temporary directory~~ — resolved

Each job's progress file was made there and nothing removed what a dead host or the test suite left (about 1,200 files). Resolved: they live in the data directory, and a new process removes old ones no job names, scratch files of an interrupted write included (#94).

### 52. ~~The measurement script finds only Python definitions~~ — resolved

It judged name queries by `.py` files only, a child crashing at exit after a complete run ended the measurement, and the model download wrote logs under the home directory. Resolved: definitions in any language griot indexes, the crash reported and passed over, caches and logs in the throwaway directory (#93).

---

## Lessons

### An agent that has to be told, and has to ask, does not search

Six weeks of real use showed a server that was connected for days while the
agent searched with grep. Nothing told it when griot was the right tool, and
every call waited on a person. Retrieval quality was not the problem;
adoption was. What changed the picture travels with the server and the
installer, not with a document someone has to read: instructions the server
sends at connection, a registration that covers every project, and an offer
to let the read-only tools run without a prompt.

### A filter that cannot match is an error, not an empty result

An empty list reads as "nothing was found". A search narrowed to a
repository with nothing indexed, or to a kind of source that does not exist,
used to be indistinguishable from a search that found nothing, and an agent
would conclude absence. The same rule runs through the quality check (a case
whose repository is not indexed says so instead of "expected and not
found"), through `griot stats` (a golden set file that cannot be read is
reported, not taken for none) and through the indexers (no directory to
index is a failure, not "nothing to index").

### Measure a widening against what persists, not against what is in force

`griot config set` asks before a change that widens something. Measured
against the value in force, exporting a high ceiling in the command's own
environment made a raise look like a reduction, and it was written to the
file without a question. What a write changes is the file, so the file (or
the default) is what the change is compared with. The environment wins while
it is set, but it belongs to the caller and is gone with the shell.

### A validator and its reader have to agree

The command that sets Gitea hosts accepted `Git.Example.com:3000`, asked for
confirmation, and wrote a value the reader could never match: it compares the
host of a remote in lower case and without a port. A value accepted at the
door and silently useless inside is worse than one refused. Each validator in
`griot config` is written from what the code that reads the setting does
with it, and the tests hold copies of lists (chat profiles, kinds of source)
to their originals.

### A question has to show where the write lands

The MCP install tool asked "install at global scope?" while the destination
came from an environment variable that a project's own configuration can
set, and the directory it named could be a link to somewhere else. The
question now gives the resolved directories and says when the environment
chose them. Naming the option is not naming the consequence.

### Only what counts its users may close what they use

The function that hands out the open collection used to close and reopen it
once it had gone unused for a while. It cannot know who still holds the
handle it gave out: in a server with two calls at once, the second closed
the collection under the first. Letting go of an idle handle moved to the
one place that counts the calls in flight, and the two tools that need the
collection to themselves wait for the others instead of closing it.

### What decides embedding does not decide rewriting

A document is embedded again when its text changes, and only then. The
fields stored beside the text (the state of a pull request, the commit a tag
points at) change for other reasons, and they were never written on their
own: a pull request merged without a change of wording kept saying it was
open. They are now compared separately and written without embedding.

### A separator is safe only if the source cannot hold it

The git log was read with two control characters between fields and
records, chosen because nobody types them. Git accepts both in a commit
message, and one commit that held one stopped a whole source on every run.
"Unlikely" is not a property of the format; NUL, which git refuses in those
places, is.

### A test that always fails kills every mutant

A mutation run reported no survivors while one of the tests it relied on was
failing for an unrelated reason, so every mutant "died". The run now checks
that the unmutated tests pass before it mutates anything, and a commit waits
for the suite's result to be read, not merely for the suite to have run.

### Measure before making every user pay again

Putting the file path in the embedded text looked like an obvious
improvement, and would have re-embedded every code chunk of every user. It
was measured first: a clear gain on a small general model, within noise on
the default one, and worse for queries about content only. It was not
adopted (the numbers are in the [roadmap](../ROADMAP.md)). The same habit
removed the graphical front end, in the next entry.

### The suite must not be able to touch whoever runs it

Several features here write to a place that is not griot's: the harness's
settings, its instructions file, its registry of servers. A test that
simulates a terminal and answers "y" with the real harness and the real home
would change the configuration of the person running the tests. Each of
those writes goes through one function, and the suite replaces it: harness
commands cannot run, a settings file cannot be written outside the test's
own directory, and the variable that relocates the harness's directory is
removed from the environment.

### Measure before deciding whether a component earns its place

griot once had a graphical front end. It was removed on numbers rather than
taste:
**~5,500 lines — about 47% of the codebase — and 41% of the test suite**,
against **no capability that the CLI did not already have**. Every page had a
direct command equivalent.

Two questions decided it, and both are answerable rather than arguable: *does
this add capability?* and *what does carrying it cost?* The second is the one
that gets skipped — the cost that hurt was never writing the thing, it was
that 41% of the suite ran on every unrelated change, and every one of its bugs
still had to be fixed to keep that suite green. Sunk cost is not a reason to
keep something; ongoing cost is a reason to remove it.

### Redundancy decides what needs protecting at rest

Indexed content is a derived copy of data already sitting unencrypted on the
same disk, readable by the same user, at their own git checkouts —
`repos.json` even records where. Encrypting the copy while the original sits
in the clear next to it does not reduce real risk.

A credential is the one category where that argument does not apply: it is
not a copy of anything, and it grants ongoing account and billing access
independent of whether the attacker can read the source. That asymmetry — not
a general "encrypt everything" principle — is what justified adopting OS
keychain storage while continuing to decline encryption of the vector store.

### Adopt security layers additively, never as a hard requirement

Keychain support degrades to the pre-existing behaviour on any failure, with
the credential injected into `os.environ` once at import so that every
existing `os.getenv()` call site works unchanged and is unaware of which
backend held the value. A security improvement that breaks the tool in
environments where it used to work will be turned off, not adopted.

### A default written out explicitly is an override in disguise

The generated `.env` template used to list every setting with its built-in
default written out ("behaviorally identical to leaving it unset"). That is only
true until the default changes: a file generated while
`GRIOT_MCP_CONCURRENCY_MODE` defaulted to `single` kept pinning `single` after
the default became `multi`, and would have silently defeated the change for
every existing install. Settings whose default the project may still change
belong in the template commented out, so the file documents the value without
freezing it.

### Keep the long-lived host process out of expensive, exclusive work

An invariant learned from a long-lived host process that violated it, and it
applies **today** to the MCP server: a long-lived host must never load the
embedding model or open a vector-store handle to do work that belongs in a
subprocess. The model costs hundreds of megabytes to a gigabyte, and the
store's handle is a process-level exclusive lock — a host holding either
starves everything else.

This is why `griot_index_repo` spawns `griot index` rather than calling it
in-process, and why `jobs.py` exists as a module importable without the MCP
SDK. It has already been violated once in practice, by a poll that opened the
active collection once per second.

### A test that derives its answer from the markers it checks agrees with them

The test of which tools may be pre-approved filtered a client's view with
the server's own reading of each tool: its read-only hint, its human-only
marker and its list of exceptions. A tool marked read-only by mistake would
have been both offered and expected. The answer now comes from what each
tool does when called. A test whose expected value is computed from the
thing under test cannot catch a mistake the two share.


---

## Where this came from

These entries were distilled from the project's architecture decision
records once each decision had been carried out and verified against the
code. The records themselves were then retired: a design document describing
software that no longer exists, or a roadmap for work already shipped, ages
into a trap for the next reader. What survives here is what does not age —
why a thing was decided, and what was left undone on purpose.
