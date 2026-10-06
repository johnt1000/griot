"""Dependabot's uv.lock is rewritten by the uv the workflows pin.

Dependabot writes uv.lock with its own uv, which places Python markers
differently from the pinned one: both forms pass `--locked`, and the next
`uv lock` on the pinned uv rewrites dozens of unrelated lines (releasing
0.2.1 met it). A workflow, on Dependabot's pull requests that change the
lock, rewrites it with the pinned uv and commits it to the pull request.

What the workflow may do is narrow on purpose, and these tests hold it so:

- It runs on `pull_request`, never `pull_request_target` or `workflow_run`:
  the pull request's code never runs with the base repository's rights.
- Only for a pull request Dependabot opened and Dependabot last pushed to,
  from a branch of this repository (a fork's head never gets a write token).
- The job that runs uv holds a read-only token and builds nothing
  (`--no-build`: no build backend, no setup.py from an sdist). A second job,
  the only one that can write, runs no uv and no Python: it copies the file
  the first job produced and pushes it, without force, so a branch that moved
  meanwhile is left alone.
- The rewrite may change how the lock is written, never what it installs:
  `scripts/lock-versions-unchanged.py` compares every package's version,
  source and hashes before and after, and the run stops when one differs.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "dependabot-lock.yml"
SCRIPT = ROOT / "scripts" / "lock-versions-unchanged.py"
DEPENDABOT = "dependabot[bot]"


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true


def _jobs():
    return _workflow()["jobs"]


def _names():
    """(the job that can write, the job that runs uv), by name."""
    jobs = _jobs()
    writers = [name for name, job in jobs.items() if (job.get("permissions") or {}).get("contents") == "write"]
    assert len(writers) == 1, f"exactly one job may write: {writers}"
    readers = [name for name in jobs if name not in writers]
    assert len(readers) == 1, f"exactly one job runs uv: {readers}"
    return writers[0], readers[0]


def _writer_and_reader():
    jobs = _jobs()
    writer, reader = _names()
    return jobs[writer], jobs[reader]


def _runs(job):
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _commands(job):
    return [" ".join(line.split()) for script in _runs(job)
            for line in re.sub(r"\\\n\s*", " ", script).splitlines() if line.strip() and not line.strip().startswith("#")]


# --- when it runs ---------------------------------------------------------------------------------


def test_it_runs_only_on_pull_requests_that_change_the_lock():
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request"}
    assert triggers["pull_request"]["paths"] == ["uv.lock"]


def test_the_workflow_itself_holds_a_read_only_token():
    assert _workflow()["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("which", ["writer", "reader"])
def test_every_job_runs_only_for_dependabot_on_a_branch_of_this_repository(which):
    """The author of the pull request AND whoever pushed last: a person
    pushing to Dependabot's branch gets no rewrite on top of their commit, and
    the rewrite's own push (by github-actions) does not start another one."""
    writer, reader = _writer_and_reader()
    condition = " ".join(str({"writer": writer, "reader": reader}[which].get("if", "")).split())
    assert f"github.event.pull_request.user.login == '{DEPENDABOT}'" in condition
    assert f"github.actor == '{DEPENDABOT}'" in condition
    assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
    assert " || " not in condition, "every part has to hold, not one of them"


def test_the_writer_waits_for_the_reader_and_runs_only_when_the_lock_changed():
    writer, _ = _writer_and_reader()
    _, reader_name = _names()
    assert writer["needs"] == reader_name
    assert f"needs.{reader_name}.outputs.changed == 'true'" in " ".join(str(writer["if"]).split())


# --- the job that runs uv ---------------------------------------------------------------------------


def test_the_job_that_runs_uv_cannot_write():
    _, reader = _writer_and_reader()
    wider = {what: level for what, level in (reader.get("permissions") or {}).items() if level not in ("read", "none")}
    assert not wider


def test_the_rewrite_builds_nothing_and_re_resolves_only_the_project():
    """`--upgrade-package griot-rag` is CONTRIBUTING's recipe: it re-resolves
    the project itself, so the lock comes back in the pinned uv's form with no
    pin moved. `--no-build`: uv may not run any build backend."""
    _, reader = _writer_and_reader()
    locks = [command for command in _commands(reader) if command.startswith("uv lock")]
    assert locks == ["uv lock --upgrade-package griot-rag --no-build"]


def test_the_rewrite_is_checked_to_change_no_version_before_anything_leaves_the_job():
    _, reader = _writer_and_reader()
    commands = _commands(reader)
    lock = next(i for i, command in enumerate(commands) if command.startswith("uv lock"))
    check = next(i for i, command in enumerate(commands) if "scripts/lock-versions-unchanged.py" in command)
    assert check > lock
    uploads = [i for i, step in enumerate(reader["steps"]) if "actions/upload-artifact@" in str(step.get("uses", ""))]
    check_step = next(i for i, step in enumerate(reader["steps"]) if "lock-versions-unchanged.py" in step.get("run", ""))
    assert uploads and all(i > check_step for i in uploads)


def test_the_job_that_runs_uv_uses_the_uv_the_other_workflows_pin():
    _, reader = _writer_and_reader()
    setup = [step for step in reader["steps"] if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    pinned = next(step["with"]["version"] for step in ci["jobs"]["test"]["steps"]
                  if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"))
    assert [step["with"]["version"] for step in setup] == [pinned]


# --- the job that writes ------------------------------------------------------------------------------


def test_the_job_that_writes_holds_only_contents_write():
    writer, _ = _writer_and_reader()
    assert writer["permissions"] == {"contents": "write"}


def test_the_job_that_writes_runs_no_uv_and_no_python():
    """It holds the only token that can push: nothing it runs may come from
    a package index or from the pull request's code."""
    writer, _ = _writer_and_reader()
    used = {str(step.get("uses", "")).split("@")[0] for step in writer["steps"] if "uses" in step}
    assert used <= {"actions/checkout", "actions/download-artifact"}
    # Every program it starts, wherever a command can start: the beginning
    # of a line, after a pipe or a separator, inside `$(...)`.
    programs = {piece.split()[0] for command in _commands(writer)
                for piece in re.split(r"[|;&]+|\$\(|\b(?:then|else|do)\b", command) if piece.split()}
    assert programs <= {"cp", "git", "printf", "base64"}, programs


def test_the_checkout_of_the_job_that_writes_keeps_no_credential():
    writer, _ = _writer_and_reader()
    checkouts = [step for step in writer["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts and all(step["with"]["persist-credentials"] is False for step in checkouts)


def test_the_branch_name_reaches_the_shell_through_the_environment_only():
    """A branch name is text someone typed; inside `run:` GitHub would paste
    it into the shell code."""
    writer, reader = _writer_and_reader()
    for job in (writer, reader):
        for script in _runs(job):
            assert "${{" not in script, script


def test_the_push_is_never_forced():
    writer, _ = _writer_and_reader()
    pushes = [command for command in _commands(writer) if re.search(r"\bgit\b.*\bpush\b", command)]
    assert pushes
    for push in pushes:
        assert not re.search(r"(^|\s)(-f|--force\S*)(\s|$)|\s\+\S*:", push), push


def test_contributing_says_the_bot_commit_needs_its_checks_approved():
    """A push made with the workflow's token starts the required checks only
    once someone approves them: without saying so, the pull request just sits
    there waiting for checks that never seem to come."""
    text = " ".join((ROOT / "CONTRIBUTING.md").read_text().split())
    assert ".github/workflows/dependabot-lock.yml" in text
    assert '"Approve workflows to run"' in text


def test_the_commit_lets_dependabot_keep_rebasing():
    """Dependabot stops rebasing a pull request someone else pushed to,
    unless the commit message says `[dependabot skip]`."""
    writer, _ = _writer_and_reader()
    assert any("[dependabot skip]" in command for command in _commands(writer) if "commit" in command)


# --- the check that the rewrite changed no version ------------------------------------------------


LOCK = """\
version = 1
revision = {revision}
requires-python = ">=3.10"

[[package]]
name = "alpha"
version = "{alpha}"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [
    {{ name = "beta"{marker} }},
]
sdist = {{ url = "https://example.invalid/alpha.tar.gz", hash = "sha256:{sdist}", size = 1 }}
wheels = [
    {{ url = "https://example.invalid/alpha.whl", hash = "sha256:{wheel}", size = 1 }},
]

[[package]]
name = "beta"
version = "2.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [
    {{ url = "https://example.invalid/beta.whl", hash = "sha256:{beta}", size = 1 }},
]
{extra}"""

BASE = dict(revision=3, alpha="1.0", marker="", sdist="a" * 64, wheel="b" * 64, beta="c" * 64, extra="")


def _check(tmp_path, **changes):
    (tmp_path / "before.lock").write_text(LOCK.format(**BASE))
    (tmp_path / "after.lock").write_text(LOCK.format(**{**BASE, **changes}))
    return subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "before.lock"), str(tmp_path / "after.lock")],
                          capture_output=True, text=True)


def test_the_same_lock_passes(tmp_path):
    done = _check(tmp_path)
    assert done.returncode == 0, done.stderr


def test_a_lock_written_in_another_form_passes(tmp_path):
    """What the two versions of uv disagree on: the revision line and where
    a dependency's marker goes."""
    done = _check(tmp_path, revision=5, marker=", marker = \"python_full_version < '3.11'\"")
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("changes, named", [
    (dict(alpha="1.1"), "alpha"),
    (dict(sdist="d" * 64), "alpha"),
    (dict(wheel="d" * 64), "alpha"),
    (dict(beta="d" * 64), "beta"),
    (dict(extra='\n[[package]]\nname = "gamma"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'),
     "gamma"),
], ids=["version", "sdist-hash", "wheel-hash", "another-package", "added-package"])
def test_a_rewrite_that_changes_what_is_installed_is_refused(tmp_path, changes, named):
    done = _check(tmp_path, **changes)
    assert done.returncode == 1 and named in done.stderr, done.stderr


def test_a_package_the_rewrite_dropped_is_refused(tmp_path):
    (tmp_path / "before.lock").write_text(LOCK.format(**BASE))
    after = LOCK.format(**BASE).split('[[package]]\nname = "beta"')[0]
    (tmp_path / "after.lock").write_text(after)
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "before.lock"), str(tmp_path / "after.lock")],
                          capture_output=True, text=True)
    assert done.returncode == 1 and "beta" in done.stderr, done.stderr


def test_a_source_that_moved_is_refused(tmp_path):
    (tmp_path / "before.lock").write_text(LOCK.format(**BASE))
    (tmp_path / "after.lock").write_text(LOCK.format(**BASE).replace("https://pypi.org/simple", "https://elsewhere.invalid/simple", 1))
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "before.lock"), str(tmp_path / "after.lock")],
                          capture_output=True, text=True)
    assert done.returncode == 1 and "alpha" in done.stderr, done.stderr


def test_the_repository_lock_passes_against_itself(tmp_path):
    done = subprocess.run([sys.executable, str(SCRIPT), str(ROOT / "uv.lock"), str(ROOT / "uv.lock")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_an_unreadable_lock_is_an_error_not_a_pass(tmp_path):
    (tmp_path / "before.lock").write_text(LOCK.format(**BASE))
    (tmp_path / "after.lock").write_text("this is not [ toml")
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "before.lock"), str(tmp_path / "after.lock")],
                          capture_output=True, text=True)
    assert done.returncode not in (0, 1)
