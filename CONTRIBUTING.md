# Contributing

griot has one maintainer and is not looking to grow a large contributor
base. Issues and pull requests are welcome anyway — this page exists so a
change that took you an evening does not get rejected for a reason nobody
told you.

## Before writing code

**Open an issue first** for anything beyond a typo or an obvious bug fix. A
short description of the problem is enough. This is not process for its own
sake: some things have been deliberately decided against, and the reasoning
is in [docs/lessons-and-debts.md](docs/lessons-and-debts.md) and
[ROADMAP.md](ROADMAP.md) rather than obvious from the code.

## What the code expects

- **Tests first, and they must fail for the right reason.** A test that
  passes before the fix, or fails on an import error, proves nothing.
- **Comments explain *why*, not *what*.** The code already says what it
  does. Most comments here exist because something went wrong once — that
  context is the valuable part, so keep it when you touch the surrounding
  code.
- **English everywhere** — code, comments, docstrings, test names, and every
  string a user sees.
- **Never commit a secret, or anything that belongs to your own machine.**
  That covers API keys and tokens, `.env` files, anything copied from
  `~/.config/griot` or `~/.local/share/griot`, and absolute paths under your
  home directory: tests and docs use placeholders such as `/Users/you/code`.
  CI scans the full history with [gitleaks](https://github.com/gitleaks/gitleaks)
  using `.gitleaks.toml`; before you push, run `gitleaks dir --config
  .gitleaks.toml .` on your working tree and `gitleaks git --config
  .gitleaks.toml .` on the history, and you will see what it will see.
  `scripts/install-git-hooks.sh` does this for you on every commit: it enables
  a `pre-commit` hook that scans what you staged with gitleaks, refuses files
  that never belong in the repository (`.env`, databases, logs, collection
  data) and, if you list them in `.git/sensitive-terms.txt`, private names of
  your own, plus a `commit-msg` hook that applies the same list to the message.
  The list lives inside `.git`, so it is never committed. It is matched
  against file contents and against file and directory names, but not against
  binary files (gitleaks cannot read those either), and a missing list only
  prints a warning. The hooks need gitleaks installed and block the commit
  when it is missing. `scripts/install-git-hooks.sh --check` reports whether
  they are active and able to run.
  The hooks only look at what you are about to commit. `scripts/audit-history.sh`
  looks back over every commit reachable from any branch or tag, merges
  included, for the same things (secrets, files that never belong, your
  private terms in contents, file names, commit and tag messages, and branch
  and tag names) and prints locations only. It does not cover binary files,
  unreachable commits, the reflog, or remote branches you never fetched. It
  exits 0 only when every check ran and found nothing, 1 on findings, and 2
  when it could not run a check (for example when your private list is
  empty), so a result of 2 is not a green light. Run it after you change your
  list and before you make a repository public. A commit's author name and email
  are public once the repository is, so check `git config user.email` too.
- **No new runtime dependency without discussing it first.** griot installs
  with `pip` and runs with no services; each dependency is a constraint on
  that. Optional extras are the escape hatch.
- **Never widen what may be indexed without a human in the loop.** Anything
  that can add a path to the indexing allowlist is a security boundary, not
  a convenience — see [SECURITY.md](SECURITY.md).

## Running the tests

```bash
uv sync --locked --extra dev    # the versions CI runs, from uv.lock
uv run pytest -q
```

or, without uv, `pip install -e ".[dev]"` and `pytest -q` (which resolves
whatever versions are current, not the locked ones).

The suite is hermetic: it never calls a paid API, never reads a real
credential, and asserts after every test that it did not touch your real
config or data directories. If a change makes a test need the network or a
key, that is a design problem with the change.

CI runs the suite on Python 3.10 and 3.13 — the floor the package declares
and a current release. It also scans for committed secrets and installs the
built wheel in a clean environment.

What CI runs is pinned, and `tests/test_supply_chain.py` holds it so: a
GitHub Action is referenced by commit (with its version in a comment), and
everything installed comes from `uv.lock`, the build backend included.
When you change a dependency in `pyproject.toml`, run `uv lock` and commit
the lock with it — `--locked` fails the build otherwise. A new command in a
workflow that fetches or installs anything has to be added to the list in
that test, which is the moment to check that it installs from the lock.
Dependabot opens the routine updates.

**Manual verification is not hermetic.** If you run the `griot` binary
directly (not through pytest) to smoke-test a change, export
`GRIOT_CONFIG_DIR` and `GRIOT_DATA_DIR` to a scratch directory first — on
every command, since a shell `&&` chain does not propagate an env var
prefix to the next command. Without them, `griot` reads and writes your
real `~/.config/griot` and `~/.local/share/griot`, which hold a live
config and any credentials you've set.

## Pull requests

Keep them small and one-topic. Say what problem the change solves and how
you verified it. If it changes behaviour a user can see, add a line to
[CHANGELOG.md](CHANGELOG.md) under `Unreleased`.

## Reporting a security issue

Don't describe the vulnerability in a regular issue or PR — see
[SECURITY.md](SECURITY.md) for the two channels that go to the maintainer
first (a GitHub security advisory, or an issue marked `security`).
