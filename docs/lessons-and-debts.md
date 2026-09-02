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

### 1. Credentials already in `.env` do not migrate to the keychain

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

---

## Lessons

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
