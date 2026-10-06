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

### 1. Credentials in `.env` move to the keychain only when asked

Setting a credential before the keychain support existed leaves it in
`<config>/.env` in plaintext until you re-run `griot auth set <provider>`
for that provider.

Silently moving a secret is a bigger risk than leaving it where it already
was, so the migration is opt-in by design. The practical consequence is that
the security improvement is **not automatic for an existing install**.

To take the improvement on an existing install:
`pip install "griot[keychain]"`, then run `griot auth migrate` (2026-09-02
— moves every credential currently in the plaintext file into the
keychain in one explicit, user-invoked command; a no-op for any provider
the keychain backend can't reach). Still not automatic by design — the
principle above is unchanged, `migrate` is just the bulk version of the
same opt-in action `griot auth set` already performed one provider at a
time.

### 2. Keychain protection is opportunistic, not universal

An environment with no reachable keychain backend — headless Linux without a
Secret Service provider, most containers — falls back silently to the
plaintext `.env` behaviour. Intentional: griot must keep working everywhere
it works today. But it means the guarantee is best-effort, and
`SECURITY.md` should never be worded as if it were absolute.

### 3. `_keychain_delete()` cannot tell "nothing stored" from "backend error"

Both return `False`. `remove_provider_key()` ORs this with the file-based
check, so what the user is told stays correct; the ambiguity lives only
inside that one return value. Distinguishing the two would mean coupling to
backend-specific exception types across macOS Keychain, Linux Secret Service
and Windows Credential Manager — more coupling to an optional dependency's
internals than the distinction is worth.

### 4. Permission repair reruns on every cold collection open in `multi` mode

`_secure_collection_dir()` walks the collection directory recursively. It was
originally called only after writes; a security review extended it to
`get_client()`, which means that in `GRIOT_MCP_CONCURRENCY_MODE=multi` it
also runs on every reopen after an idle release, not just on writes. A real,
bounded, per-open cost on large collections — accepted, not a correctness
bug.

**2026-09-02 update**: the `os.chmod` write syscall itself is now skipped
per file/directory when its mode is already correct (`_repair_mode()`) —
most files, most reopens, once a collection has been repaired at least
once. The walk and the `os.stat()` verification of every file still run
every time on purpose: skipping those would mean trusting that nothing
changed since this process last held the collection, which is exactly
what `multi` mode's whole premise (another process reopening the same
collection while this one was released) makes unsafe to assume. The debt
is smaller, not gone.

**2026-09-29 update**: `multi` is now the default, so this applies to every
install rather than to those who opted in. Measured on a copy of a real
collection (244 MB, about 15,000 points, 70 files): a full reopen, walk
included, takes about 87 ms (worst of seven, 133 ms). At that size the walk is
not the dominant cost; it would only matter for a collection with many
thousands of files.

### 5. Permission repair is best-effort per file

A single `chmod` failure (a transient race with the engine's own I/O
mid-write) is swallowed rather than raised, so a permission repair can never
fail a real indexing run. A rare individual file could be missed in one run;
the next `index_documents()` corrects it.

**2026-09-02 update**: swallowed no longer means untraceable — a failure now
logs a warning (`common.log_and_print(..., level="warning", echo=False)`,
naming the path) to `griot.log` instead of vanishing with a bare `pass`.
Still never raises, and still doesn't echo to stdout (which is the MCP
server's JSON-RPC transport) — only what's observable changed.

### 6. `griot stats` renders trends as text, not charts

Spend over time, source breakdown and quality trend are numbers and unicode
sparklines in a terminal. That is a real limit for anyone who reads a shape
faster than a column of figures. Accepted knowingly: `griot stats --json`
exposes the same data for anyone who wants to plot it.

### 7. The MCP confirmation policy is explained in three places, not one

README.md, SECURITY.md and `docs/mcp-capability-coverage.md`'s "The
management surface" table each restate, in their own prose, which MCP tools
require a human answer versus a `confirm=true` bypass. A 2026-09-02
documentation audit flagged the real risk this creates: the policy could
change and only some of the three restatements get updated, and nothing
would catch that but another manual read-through.

Not consolidated into a single canonical paragraph on purpose — each
restatement serves a different reader in place (a newcomer skimming
README, someone doing a security review, someone about to add a new
state-changing tool), and a bare "see the table" would degrade all three
into a worse read. The mitigation applied instead: README and SECURITY.md
now link directly to `docs/mcp-capability-coverage.md#the-management-surface`
as the canonical table, so a future edit at least has one obvious place a
reviewer would think to check against. The duplication itself remains.

### 8. `griot stats` mixes two clocks and two scopes

The window (`--days`) is cut in UTC, while spend is grouped by the local day,
because that is the day the circuit breaker counts. The first local day of a
window can therefore come in partial. And the state lines (last indexed, last
quality check, golden set) are about the active profile's collection, while
the counts of runs, embedded and skipped chunks add up every profile. Both
are accepted for now: each number is right for what it says, and the report
says which collection the state is about.

### 9. A search for readers returns up to `limit`, not exactly `limit`

Search for a reader (the MCP tool, `griot search`, the context of
`griot ask`) asks the store for six times `limit`, holds one document to
three results and folds copies of the same thing. When the whole window is
chunks of two long documents, six results come back for a `limit` of eight,
and `also_in` names the copies found in that window, not every copy in the
index. Asking the store again until the list is full was not worth the
second round trip for a case that needs a very thin slice of the index.

### 10. The self-check counts a sample with no text as passed

`run_self_check()` skips a sampled point whose content is blank, and counts
it in `sampled` and in `passed`. A collection whose samples were all blank
would read as healthy. Not reachable with what the indexers write today (a
chunk is never empty), which is why it was left.

### 11. Two threads can build the local model twice

`get_embed_model()` is an unlocked check-then-set. Two MCP worker threads
that embed for the first time at the same moment each build a model; the
second replaces the first and the memory of one is wasted until collected.
It predates the lazy import and was not made worse by it.

### 12. Writing a setting replaces a symlinked `.env` with a regular file

`griot auth set`, `griot profiles use`, `griot config set` and
`griot config unset` write through python-dotenv's `set_key`/`unset_key`,
which rewrite the file by renaming a new one over it. A `<config>/.env` that is a link into a dotfiles checkout stops
being a link. The installer treats the harness's files more carefully (it
writes through a link in the user's own directory); griot's own file was
left as dotenv does it.

### 13. The confirmations in the CLI are a guard, not a boundary

`griot repos add`, `griot profiles delete`, raising a ceiling with
`griot config set` and the installer's questions need an interactive
terminal and have no flag that answers. That stops the plain command an
agent would run from a shell with no terminal. It does not stop a process
that can run arbitrary commands: it can fake a terminal, set the variable in
its own environment, or edit the file. SECURITY.md says so wherever it
describes one of them; it is listed here because it is the most likely thing
to be mistaken for a security boundary.

### 14. `uv.lock` is written by more than one version of uv

CI and the release install with uv pinned in the workflows; Dependabot
updates `uv.lock` with whatever version of uv it runs. The two write the
same resolution differently (where a dependency's Python marker goes), so
both lockfiles pass `--locked`, but the next `uv lock` on the pinned version
rewrites a few dozen unrelated lines. Releasing 0.2.1 met it: the bump of
one version line came with 32 lines of marker changes, edited back by hand.
`[tool.uv] required-version` would not reach Dependabot. Left until the
noise lands in a change that matters; the fix is then to regenerate the
lock on the pinned uv in a commit of its own.

### 15. Where a credential comes from is read from shell files only

`credential_origin()` knows the value in the environment came from outside
griot's own files, and names WHERE only when it finds an export in the
shell's startup files (`.zshrc`, `.bashrc`, `config.fish` and the rest of
the list). A value set by direnv, a file sourced from one of those, the
login environment of the desktop session or an IDE's run configuration is
reported as exported in "this shell (no shell file griot knows sets it)".
Reading every mechanism that can set a variable is open-ended; the message
says what it does not know rather than guessing.

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


---

## Where this came from

These entries were distilled from the project's architecture decision
records once each decision had been carried out and verified against the
code. The records themselves were then retired: a design document describing
software that no longer exists, or a roadmap for work already shipped, ages
into a trap for the next reader. What survives here is what does not age —
why a thing was decided, and what was left undone on purpose.
