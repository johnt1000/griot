# Getting started

Installing griot, a first index and search, and what to do when something does
not work. Profiles and settings are in [Configuration](configuration.md).

## Installation

```bash
pipx install griot-rag    # the distribution is griot-rag; the command is griot
```

Or from a checkout:
`git clone https://github.com/johnt1000/griot && cd griot && pipx install .`
(`pipx install -e .` for an editable install).

Requires Python ≥ 3.10 and `git`. **The first run of a local profile downloads
its ONNX model from Hugging Face** (~1.1 GB for the default `jina-code`) and
caches it under your data directory; `griot profiles list` shows each
profile's RAM tier against the RAM you actually have. CI runs the suite on
Python 3.10, 3.13 and 3.14 on Linux and on 3.13 on macOS, scans for committed
secrets, and installs the built wheel in a clean environment. It runs on
every pull request and every push to `main`, when started by hand, and on the
tagged commit before a release builds.

## A first index and search

```bash
# 1. register the repos you want to index (asks you to confirm, in a terminal)
griot repos add ~/code/my-app
griot repos add ~/code/my-lib

# 2. see what would be indexed (never spends anything)
griot index all --repo my-app --dry-run

# 3. index for real (default profile is local & free)
griot index all --repo my-app

# 4. search (no LLM: by meaning and exact words; --mode keyword for a hash or an error code)
griot search "where is the retry logic for the payment API?"
griot search "ERR_CONNECTION_REFUSED" --mode keyword

# 5. ask (search + LLM synthesis — requires a chat provider, see configuration.md)
griot ask "how does authentication work in this codebase?" --show-sources

# 6. keep an eye on usage and spend
griot stats

# 7. when something does not work: every check at once, reads only
griot doctor

# 8. upgrade griot to the newest release (shows the command, asks first)
griot update
```

`griot index all` runs the sources in order: `code`, `commits`, `tags`,
`branches`, `platform`. Filter with `--sources code,commits`. Index an
unregistered directory directly with `--path <dir>`.

In a git work tree `index code` reads what git does not ignore (tracked files
and new ones), skips symlinks and files over 1 MB, and replaces
credential-looking values before anything is embedded. After each run the
points whose source is gone are removed: a deleted file, a deleted branch.
That removal is held back when more than half of a repository would go
(`--prune` overrides) and never happens for a `--path` run, so register a
repository you index regularly. `--dry-run` says what a run would embed and
remove, at no cost.

## Usage and spend: `griot stats`

`griot stats --days N` covers today and the N-1 days before it, in local days
(midnight local time, the day the spend ceiling counts). Its counts are the
active profile's collection, the one its state lines describe;
`--all-profiles` counts every profile. Spend is always the whole account's.

## When something does not work: `griot doctor`

`griot doctor` checks the whole setup in one go and says what to do about each
finding. It changes no setting, index or file of yours; two things happen on
the way and are said: loading the configuration closes a `.env` left open to
other users, as every griot command does, and asking the harness which server
it has registered may start that server for a moment, as
`griot assist install` does. It checks settings the file holds that griot
cannot start with, the configuration file, the directories and everything the
data directory holds (the model cache aside) closed to other users, the active
profile and its credential, the collection, the registered repositories and
whether their index is behind, today's spend against the ceiling, the MCP
registration and which read-only tools still ask before every call, the
variables a server would ignore, git, the log, and whether a newer griot was
released.

That last check asks PyPI for the newest version of `griot-rag` and, when
yours is behind, prints the command that upgrades it for how griot seems to be
installed (it never runs it; for an editable or development install it says to
update the checkout instead, since an installer would replace it); offline it
says it could not check. `griot config set update-check false` turns it off;
the only other command that asks PyPI is `griot update`, which you run to
upgrade. Exit status 1 only when a check fails; a warning is something to
know.

## Upgrading: `griot update`

`griot update` runs that upgrade: it asks PyPI for the newest `griot-rag`
(always: `update-check` governs only the doctor's check, and asking is what
this command is for), works out how griot was installed (`pipx`, `uv tool`, or
`pip` in a virtual environment), shows the exact command and asks before
running it; `--yes` answers, and with no terminal and no `--yes` it runs
nothing. Up to date, it says so and runs nothing; offline, it says it could
not check and exits non-zero. It refuses an editable or development install
(update the checkout instead) and an installation whose method it cannot tell
(it prints the candidate commands instead of guessing). The installer's output
is shown as it runs and its exit status is the command's; afterwards it prints
the version now installed. MCP servers already running keep the old version
until they are restarted. It is a command for you, not an MCP tool: an agent
does not upgrade the tool it is using.
