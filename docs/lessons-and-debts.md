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

### 53. ~~`griot ask` calls the chat model over an empty context~~ — resolved

Filters that each exist but match nothing together, or an empty index, still sent the question to the chat model, and `--limit` took 0 and crashed on a negative number. Resolved: `ask` prints `No results.`, names the filters, calls no model, and its `--limit` takes what `griot search` takes (#105).

### 54. ~~`golden-set suggest` ends with a traceback on a refused case~~ — resolved

A candidate `add_case` refused stopped the run with a traceback. Resolved: the reason is printed under the candidate and the run goes on; an unreadable golden-set file stops it with one error (#102).

### 55. ~~The measurement script misses Java, C# and C++ methods~~ — resolved

A method declared as `<type> name(...)` with a body was not found as a definition. Resolved: it is, and calls, returns, conditions and prototypes are not (#102).

### 56. ~~The progress-file sweep test hard-codes the writer's scratch name~~ — resolved

The sweep and its test spelled the atomic writer's scratch name themselves, so a change to the writer could slip past both. Resolved: the writer and the sweep share `common.atomic_scratch_path`, and the test takes the name from a real write stopped before its rename (#102).

### 57. ~~The vector-fallback notes speak of one case~~ — resolved

`suggest` says them once for a run that can add several cases. Resolved: they are worded for one case or several (#102).

### 58. ~~The `bge-m3` profile is listed but cannot load~~ — resolved

fastembed has no BAAI/bge-m3, so the profile failed at its first embedding. Resolved: removed, a configuration naming it is told why, and every listed local profile is checked against fastembed's model list (#104).

### 59. ~~A server on a removed installation fails with a misleading TLS error~~ — resolved

A long-running server whose virtual environment was deleted answered with the error of whatever file a call read, such as a missing CA bundle. Resolved: a failed call checks whether the installation is gone and, if so, says so and asks for a restart (#103).

### 60. ~~A question that found nothing is logged with a chat model~~ — resolved

The query log named the active chat profile and model for a question that called none. Resolved: such a question is logged with neither (#109).

### 61. ~~`griot search` does not say which filters left it empty~~ — resolved

`griot ask` named the filters that narrowed an empty search; `griot search` and `griot_search` did not. Resolved: the three say the same sentence, from one function, and a test holds them to it (#109).

### 62. ~~The disagreement offer is blind to a narrow rank window~~ — resolved

The premise did not hold for the searches that are logged: `griot search`, the example given, logs nothing, and every search `griot ask` and `griot_search` log fetches each ranking several times wider than the list it returns, so from a limit of 2 the window already reaches the depth; only a one-result search is narrower, and it has nothing to disagree about. Resolved by pinning that at every such limit in the tests and writing the reason down; the depth of 10 was measured against 5 and 20 on a throwaway index of this repository, not on real logs (#108).

### 63. ~~opencode shows each griot skill twice when both harnesses are installed globally~~ — resolved

opencode also loads `~/.claude/skills`, so it got two copies of each skill. Resolved: a skill opencode already loads from another directory, or one being installed there in the same run, is not copied for it, and the copies an earlier install left are deleted (#110); debts 64 and 69 are what that left open.

### 64. ~~`griot assist install` deletes opencode's earlier skill copies without asking~~ — resolved

The install deleted the copies of griot's skills an earlier install left in opencode's own directory, so a hand edit there was lost. Resolved: at a terminal it lists them and asks (default no); otherwise it keeps them and says where they are, why they are redundant and how to remove them, and `griot_assist_install` never deletes and lists them under `copies_kept` (#115).

### 65. ~~A default `pipx install griot-rag` has no keychain~~ — resolved

`keyring` was an optional extra, so a default install kept credentials in plaintext and named the wrong command to leave it. Resolved: `keyring` is a dependency of `griot-rag` (the `keychain` extra kept, empty), and where no backend is reachable `griot auth` and `griot doctor` say the key is in plaintext, why, and how to get a backend (#113).

### 66. ~~Importing griot reads every credential from the keychain~~ — resolved

Every griot process asked the keychain for every known credential on import, one macOS prompt each. Resolved: a credential is read only when a command needs it, once per process, absence included, and is no longer put in the environment children inherit (#114); debt 71 is what that left open.

### 67. ~~A GitLab 404 with a token blames the token~~ — resolved

A project that does not exist or is not visible to the token ended the run with "Check the token". Resolved for every platform: a fetch answered 404 under a token is called not found, naming the project, platform and remote, and the run records `not_found_repos`, which `griot stats`, `griot doctor` and `griot_index_status` (`platform_not_found`) show apart (#117); debt 70 is what that left open.

### 68. ~~Agents do not learn the new result metadata~~ — resolved

The `griot_search` description had no room for new metadata fields. Resolved: the `griot://result-fields` resource lists the fields of each kind of source, served from the list every indexer's payload is checked against, and the description points to it (#116).

### 69. ~~opencode's skill switches are read from griot's environment~~ — resolved

The decision to skip a skill for opencode silently assumed opencode runs with griot's values of `OPENCODE_DISABLE_*`, and an install for Claude Code alone left opencode's copies as duplicates. Resolved: the note names each variable with its value and says it was read in griot's environment, and an install for Claude Code alone reports opencode's copies too (#115).

### 70. ~~A repository refused for mixed reasons still gets the token advice~~ — resolved

A repository whose fetches got some 404s and some 401 or 403 answers stayed among the token refusals, so the readers said to check the token and nothing about the project. Resolved: the run records which fetches were refused for which cause per refused repository (`refusal_causes`), and `griot stats`, `griot doctor` and `griot_index_status` (`platform_refusal_causes`) name it as refused for mixed reasons and give the token advice only for the fetches refused for the token; a run recorded before reads as it did (#122). Debt 72 is what that left open.

### 71. ~~A key added to the keychain does not reach a running server, and `griot auth set` does not say so~~ — resolved

`griot auth set` warned about a running server only when it replaced a key in `.env`. Resolved: `set` and `remove` say, whenever they store or remove a key, that running MCP servers keep what they read until restarted and how to restart them, and `griot_auth_guidance` says the same (#121).

### 72. ~~A platform failure under a token that is not the token's fault still gets the token advice~~ — resolved

A repository whose every fetch failed with no answer or a server error under a token was listed among the token refusals. Resolved: the run records the status or error kind of those fetches (`other_reasons`), and `griot index platform`, `griot stats`, `griot doctor` and `griot_index_status` (`platform_other_reasons`) say the platform did not answer or failed, that it is not the token, and to check the network or the platform's status and try again later, for the failed fetches of a mixed refusal too (#128).

### 73. ~~`griot ask` run through an agent's shell tool never pairs as reworded~~ — resolved

Every shell-tool call started a new shell, so each `griot ask` was a session of its own. Resolved: the session is the agent client's conversation id when the client exports one (Claude Code's `CLAUDE_CODE_SESSION_ID`, stored as a digest, never raw), which also pairs those asks with the same conversation's `griot_search` calls; opencode exports none, and without one the parent process decides as before (#126). Debt 83 is what that left unverified.

### 74. ~~Subagents sharing one MCP server share a session~~ — resolved as a documented limit

Not changed, documented: a subagent that reaches griot through its parent's `griot mcp` logs under the parent's session, which the quality guide (`docs/quality.md`) and the reworded rule's docstring now say, with the person reviewing as the check (#127). With #126 a subagent's shell `griot ask` may carry the same conversation id too; whether a subagent's id equals its parent's was not checked.

### 75. ~~Reworded candidates can be crowded out of a review~~ — resolved

The kinds were joined one after another and cut at `--limit`. Resolved: they take turns up to the limit, each kind in its own order, and each candidate names its kind (#127).

### 76. ~~A reworded candidate offers only the first search's results~~ — resolved

Resolved: a reworded candidate shows the follow-up's results beside the first search's, each marked with the search that returned it, and a pick from either list becomes a case of the first question in the first search's mode, when the search that returned it was neither narrowed nor grouped (#127).

### 77. ~~The documents contradicted the code in places~~ — resolved

A docs audit found statements the code contradicts: that `griot index keywords` starts an interrupted copy over, the MCP capability count, that `griot_index_wait` does not survive a server restart, that credentials live in `.env`, the CI matrix, the release steps, which read-only tools the installer leaves out of pre-approval, and no mention of the codeberg.org check of Gitea/Forgejo release authors. Resolved: each fixed and held to its source by a test (#129).

### 78. ~~Some flags griot reads are missing from `--help`~~ — resolved

`--profile`, `--chat-profile` and `--sources` are taken out of the command line before any parser runs, so no help showed them; `griot index all --help` ran every source's help, `griot mcp --help` started the server, and some usage lines read `griot [-h]`. Resolved: each command's help names the flags that change what it does, `griot mcp --help` prints a usage naming the server's settings, and the usage lines name their command (#130, #131).

### 79. ~~A redaction test's wall-clock limit failed under load~~ — resolved

The check that no input makes a redaction detector run away allowed 3 seconds of wall time, and failed once under full-suite load. Resolved: it limits CPU time in a child process, so waiting for a CPU does not count, and a release-race test takes its bound from the wait it guards instead of a fixed second (#132). Debt 86 is the one timing bound left.

### 80. ~~The README is too long to be the front page~~ — resolved

About 6,800 words mixed the pitch, a guide and the full reference, on GitHub and on PyPI alike. Resolved: a short README with an index of topic guides under `docs/`, linked by absolute URL so they work on PyPI, and a test that every link and anchor between the documents resolves (#133).

### 81. ~~Hand-written counts of the server's tools and prompts, and CI said to run on every push~~ — resolved

The `griot-workflows` skill and the coverage document counted prompts, tools and resources by hand, and the getting-started guide said CI runs on every push. Resolved: they point at the server's lists, a test fails on any such count in a bundled skill, agent or guide that no other test holds, and the guide says when CI runs, read against `ci.yml` (#134). Debt 82 narrowed what it let through; debt 85 is what it does not see.

### 82. ~~The inventory-count test exempts held counts in every file~~ — resolved

The test that refuses hand-written counts let "N human-only tools", "N read-only tools" and "N tools expose less" through in every skill, agent and guide, though a test held them in one passage of one file each. Resolved: a count is let through only inside the passage its holding test reads, taken from the function that test calls (#135).

### 83. ~~A conversation id that changes under a running server would stop pairing~~ — resolved as a documented limit

The `griot mcp` environment is fixed when the client starts it, while each
`griot ask` through the shell tool reads the conversation id afresh (#126).
If Claude Code changed `CLAUDE_CODE_SESSION_ID` within a conversation
without restarting its MCP servers (possibly on `/clear` or a resume), the
asks after the change would no longer pair with the server's searches until
it restarts. It was not verified when recorded. Resolved: Claude Code was observed to keep its MCP servers across `/clear` and an in-session `/resume`, and to send no conversation id with a tool call, so there is nothing per call to read; the limit and what was observed are documented in `log_session()` and `docs/quality.md` (#137).

### 84. ~~`griot doctor` reads private names of `stats`~~ — resolved

`doctor` builds its advice from `stats._FAILURE_CHECK`, the wording added
for a platform that did not answer or failed (#128), and from
`stats._behind_phrase`, which it has used since it was written. Debt 47
resolved the same pattern for `common.py` by making such names public, with
a guard against new ones; `stats` has no such guard. Resolved: `stats` exposes `FAILURE_CHECK` and `behind_phrase`, and the guard of debt 47 now covers every module of the package; it found twelve other uses of a module's private names across seven modules, each given a public name (#138).

### 85. ~~The hand-written count test sees at most two words between the number and the noun~~ — resolved

The test that refuses hand-written counts of the server's tools, prompts or
resources (#134, #135) matches a number word from "two", or digits, then
at most two words, then the noun: "three of griot's read-only MCP tools" or
"one resource" would pass it. It catches the counts the documents have
held so far, not every way to write one. Resolved: the scan reads one, zero, "a single" and digits, singular and plural nouns, and any describing words up to the noun within a clause; closed-class words and nouns that make a compound (a tool call, a tool use) do not count (#139).

### 86. ~~One release-race test still bounds a path by a fixed fraction of a second~~ — resolved

`test_a_status_read_does_not_wait_behind_a_slow_open` asserts the status
read takes under 0.25 s against a 0.3 s open (#132). The bound cannot be
raised toward the open's length without losing the check, and a longer
open would slow every test that uses the fixture, so it was left; it is
the one timing bound in that file a loaded machine can still trip. Resolved: the slow open lasts 10 s and the test asserts the read returned before it finished; two more fixed timings found by the sweep now take their bound from the wait they guard (#139).

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

Keychain support degrades to the pre-existing behaviour on any failure: an
unreachable backend leaves the credential in `<config_dir>/.env`, and every
call site reads it through one accessor that falls back from the
environment to the keychain, unaware of which backend held the value. A
security improvement that breaks the tool in environments where it used to
work will be turned off, not adopted.

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

### A guard written against the old shape guards the mechanism, not the harm

A test said that the credential status the MCP tools read must never touch
the keychain, because on macOS each read can ask for a password and those
tools run on every call. It held only while importing griot had already put
every key in the environment. Once keys were read on demand, obeying it
would have shown a key kept only in the keychain as not configured. The
harm was a prompt on every call, not a read; the guard now says that (once
per process). When the code a guard sits on changes shape, check the guard
against the harm it was written for, not against its wording.

### A measurement on constructed data shows the rule, not the use

The window for reworded searches was chosen by running the rule over
constructed logs, at the gaps between searches the script assumes (#123).
That shows how the rule behaves if those assumptions hold: which window
keeps nearly every rewording before returns to the subject come in. It
cannot say how often people and agents actually reword, how fast, or how
many follow-ups are about another facet of the subject, because those are
the inputs it was given. Such a number is recorded with its assumptions and
as pending until real use is read, not as a finding.

### A test's time limit has to bound the defect, not the machine

The check that no input makes a redaction detector run away allowed 3
seconds of wall time, and failed once when the full suite loaded the
machine (#132). Wall time counts waiting for a CPU, which is the machine's
state, not the code's; the defect the check guards against, backtracking
that never ends, burns CPU. The limit is now on CPU time, enforced by the
kernel in a child process. The same round's release-race test had a fixed
1-second bound against a 1.5-second wait; the defect there is sitting
through the wait, which takes at least the whole wait, so the wait itself
is the bound that still fails the defect and leaves a busy machine the most
room. Derive a timing bound from what the defect costs, and measure the
resource the defect consumes.


---

## Where this came from

These entries were distilled from the project's architecture decision
records once each decision had been carried out and verified against the
code. The records themselves were then retired: a design document describing
software that no longer exists, or a roadmap for work already shipped, ages
into a trap for the next reader. What survives here is what does not age —
why a thing was decided, and what was left undone on purpose.
