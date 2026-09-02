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
- **No new runtime dependency without discussing it first.** griot installs
  with `pip` and runs with no services; each dependency is a constraint on
  that. Optional extras are the escape hatch.
- **Never widen what may be indexed without a human in the loop.** Anything
  that can add a path to the indexing allowlist is a security boundary, not
  a convenience — see [SECURITY.md](SECURITY.md).

## Running the tests

```bash
pip install -e ".[dev]"
pytest -q
```

The suite is hermetic: it never calls a paid API, never reads a real
credential, and asserts after every test that it did not touch your real
config or data directories. If a change makes a test need the network or a
key, that is a design problem with the change.

CI runs the suite on Python 3.10 and 3.13 — the floor the package declares
and a current release. It also scans for committed secrets and installs the
built wheel in a clean environment.

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
