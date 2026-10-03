"""What the hooks refuse is refused by every route into the history.

The hooks guarded `git commit`, and three things went past them:

- a line of the commit message that starts with `#` was taken for one of
  git's own template comments and not read, although a message given with
  `-m` or `-F` has no template and git keeps such a line;
- a merge that makes a commit runs `pre-merge-commit`, not `pre-commit`, so
  nothing looked at what the other branch brought;
- `prod.env` is not `.env`: the list of files that never belong went by a
  few exact names.

And a commit also arrives by cherry-pick, a fast-forward merge, `git am`,
most of a rebase or `--no-verify`, which run no commit hook. The one place every
route passes is the push, which is also the moment anything becomes public:
`pre-push` looks at the commits a push would send.

Run for real, in throwaway repositories, as tests/test_git_hooks.py does."""

import os
import secrets
import shutil
import string
import subprocess
import sys

import pytest
from hook_support import HOOKS, SCRIPTS, git as _git, make_repo, write_script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the hooks are bash scripts")

REAL_GITLEAKS = shutil.which("gitleaks") or next(
    (p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks") if os.access(p, os.X_OK)), None)
needs_gitleaks = pytest.mark.skipif(REAL_GITLEAKS is None, reason="gitleaks is not installed")
TERM = "acme-private"


@pytest.fixture
def stub_gitleaks(tmp_path):
    return write_script(tmp_path / "gitleaks-stub", "exit 0\n")


@pytest.fixture
def repo(tmp_path, stub_gitleaks):
    repo = make_repo(tmp_path / "repo", stub_gitleaks)
    (repo / ".git" / "sensitive-terms.txt").write_text(TERM + "\n")
    return repo


@pytest.fixture
def remote(repo, tmp_path):
    """`repo` with one clean commit on main and an empty remote, `origin`."""
    path = tmp_path / "remote.git"
    # -b main: git's default branch name differs between installations (it
    # was `master` on the CI runner), and a clone of a remote whose HEAD
    # names a branch nobody pushed is an empty checkout on another branch.
    assert _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path), env=repo.env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(path), env=repo.env).returncode == 0
    _commit_file(repo, "README.md", "hello\n", "first")
    return path


def _write(repo, name, content="hello\n"):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert _git(repo, "add", "-f", name, env=repo.env).returncode == 0


def _commit_file(repo, name, content="hello\n", message="change", hooks=True):
    """`hooks=False` stands for every route that runs no commit hook."""
    _write(repo, name, content)
    args = ["commit", "-q", "-m", message] + ([] if hooks else ["--no-verify"])
    done = _git(repo, *args, env=repo.env)
    assert done.returncode == 0, done.stderr
    return _git(repo, "rev-parse", "HEAD", env=repo.env).stdout.strip()


def _count(repo, ref="HEAD"):
    out = _git(repo, "rev-list", "--count", ref, env=repo.env)
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def _push(repo, *what, env=None):
    return _git(repo, "push", "-q", "origin", *(what or ("main",)), env=env or repo.env)


def _on_remote(repo, remote, ref):
    return _git(repo, "--git-dir", str(remote), "rev-parse", "-q", "--verify", ref, env=repo.env).stdout.strip()


def _merge_adding(repo, name, content):
    """A merge commit that itself adds `name` (neither side had it)."""
    assert _git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    _commit_file(repo, "side.md", "side\n", "side")
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side", env=repo.env).returncode == 0
    _write(repo, name, content)
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge side", env=repo.env).returncode == 0


def _token():
    return "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))


# === the commit message =========================================================================


@pytest.mark.parametrize("args", [
    ["-m", f"# notes for {TERM}"],
    ["-m", "docs: fine", "-m", f"# {TERM}"],
    ["-m", f"docs: fine\n\n#{TERM}"],
])
def test_a_line_that_starts_with_a_hash_is_read_when_the_message_came_from_the_command_line(repo, args):
    """git keeps it: nothing was a template, so nothing is a comment."""
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", *args, env=repo.env)

    assert done.returncode != 0 and "private term" in done.stderr
    assert _count(repo) == 0


def test_and_when_it_came_from_a_file(repo, tmp_path):
    _write(repo, "clean.md")
    (tmp_path / "message").write_text(f"docs: fine\n\n# about {TERM}\n")

    done = _git(repo, "commit", "-q", "-F", str(tmp_path / "message"), env=repo.env)

    assert done.returncode != 0 and _count(repo) == 0


def _editor(tmp_path, first_lines):
    """An editor that puts `first_lines` above whatever git offered."""
    text = tmp_path / "typed"
    text.write_text(first_lines)
    return write_script(tmp_path / "editor", f'cat "{text}" "$1" > "$1.new" && mv "$1.new" "$1"\n')


def test_the_comments_git_itself_adds_in_an_editor_are_still_not_read(repo, tmp_path):
    """They name the branch and the files, and git takes them out again: a
    branch called after a private name must not stop a clean message."""
    assert _git(repo, "checkout", "-q", "-b", f"{TERM}-topic", env=repo.env).returncode == 0
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", env={**repo.env, "GIT_EDITOR": _editor(tmp_path, "docs: fine\n")})

    assert done.returncode == 0, done.stderr
    assert TERM not in _git(repo, "log", "-1", "--format=%B", env=repo.env).stdout


def test_a_line_typed_in_the_editor_is_read(repo, tmp_path):
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", env={**repo.env, "GIT_EDITOR": _editor(tmp_path, f"docs for {TERM}\n")})

    assert done.returncode != 0 and _count(repo) == 0


def test_a_hash_line_is_read_when_git_was_told_that_comments_start_with_something_else(repo, tmp_path):
    assert _git(repo, "config", "core.commentChar", ";", env=repo.env).returncode == 0
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", env={**repo.env, "GIT_EDITOR": _editor(tmp_path, f"docs: fine\n\n# {TERM}\n")})

    assert done.returncode != 0 and _count(repo) == 0


def test_when_git_picks_the_comment_character_itself_every_line_is_read(repo, tmp_path):
    """Which one it picked cannot be known from the hook, so none is assumed
    (and "auto" is a setting, not a prefix: a line that starts with that
    word is a line of the message)."""
    assert _git(repo, "config", "core.commentChar", "auto", env=repo.env).returncode == 0
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", env={**repo.env, "GIT_EDITOR": _editor(tmp_path, f"automation for {TERM}\n")})

    assert done.returncode != 0 and _count(repo) == 0


@pytest.mark.parametrize("mode", ["verbatim", "whitespace", "scissors"])
def test_a_hash_line_is_read_when_git_was_told_to_keep_comments(repo, tmp_path, mode):
    assert _git(repo, "config", "commit.cleanup", mode, env=repo.env).returncode == 0
    _write(repo, "clean.md")

    done = _git(repo, "commit", "-q", env={**repo.env, "GIT_EDITOR": _editor(tmp_path, f"docs: fine\n\n# {TERM}\n")})

    assert done.returncode != 0 and _count(repo) == 0


# === a merge ====================================================================================


def _side_branch(repo, name, content, message="side work"):
    """A branch whose commit no hook saw (made elsewhere, or before the hooks)."""
    _commit_file(repo, "README.md", "hello\n", "first")
    assert _git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    _commit_file(repo, name, content, message, hooks=False)
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "more\n", "main moves on")


def _merge(repo, env=None):
    return _git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side", env=env or repo.env)


def test_a_merge_that_brings_a_file_that_never_belongs_is_blocked(repo):
    _side_branch(repo, "config/prod.env", "KEY=value\n")
    before = _count(repo)

    done = _merge(repo)

    assert done.returncode != 0 and "never belong" in done.stderr and "config/prod.env" in done.stderr
    assert _count(repo) == before


def test_a_merge_that_brings_a_private_term_is_blocked(repo):
    _side_branch(repo, "notes.md", f"the {TERM} rollout\n")
    before = _count(repo)

    done = _merge(repo)

    assert done.returncode != 0 and "notes.md" in done.stderr and "rollout" not in done.stderr
    assert _count(repo) == before


@needs_gitleaks
def test_a_merge_that_brings_a_secret_is_blocked(repo):
    token = _token()
    _side_branch(repo, "leak.py", f'TOKEN = "{token}"\n')
    before = _count(repo)

    done = _merge(repo, env={**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS})

    assert done.returncode != 0 and token not in done.stderr
    assert _count(repo) == before


def test_a_clean_merge_goes_through(repo):
    _side_branch(repo, "notes.md", "nothing to see\n")
    before = _count(repo)

    done = _merge(repo)

    assert done.returncode == 0, done.stderr
    assert _count(repo) == before + 2  # the side commit and the merge


def test_a_private_term_in_the_message_of_a_merge_is_blocked(repo):
    _side_branch(repo, "notes.md", "nothing to see\n")

    done = _git(repo, "merge", "-q", "--no-ff", "-m", f"merge the {TERM} work", "side", env=repo.env)

    assert done.returncode != 0 and "private term" in done.stderr


# === files that never belong ====================================================================


@pytest.mark.parametrize("name", ["prod.env", "config/staging.env", ".env-prod", ".env_backup", "app.env.local",
                                  ".env.d/secrets", ".env/settings", "deploy/PROD.ENV", "prod.envrc",
                                  ".env~", ".env.swp", ".envs/prod", '.env"', ".env copy", ".env\tx", ".env.test"])
def test_an_environment_file_under_any_of_its_usual_names_is_blocked(repo, name):
    _write(repo, name, "KEY=value\n")

    done = _git(repo, "commit", "-q", "-m", "change", env=repo.env)

    assert done.returncode != 0 and "never belong" in done.stderr and name in done.stderr
    assert _count(repo) == 0


@pytest.mark.parametrize("name", ["env.py", "migrations/env.py", "docs/environment.md", "sample.env.example",
                                  "src/dotenv_loader.py", "docs/tox.envs.md", "scripts/setenv.sh", "config/.env.example",
                                  ".env.sample", ".env.template", "deploy/.env.dist", "config/app.env.sample",
                                  ".envoy.yml", ".environment", "web/vite-env.d.ts"])
def test_a_name_that_only_resembles_one_is_allowed(repo, name):
    _write(repo, name)

    assert _git(repo, "commit", "-q", "-m", "change", env=repo.env).returncode == 0


def test_a_tracked_link_turned_into_a_real_file_is_still_refused(repo):
    """git reports that as a change of type, which the hook did not ask about."""
    os.symlink("elsewhere", repo / ".env")
    assert _git(repo, "add", "-f", ".env", env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "a link", env=repo.env).returncode == 0
    (repo / ".env").unlink()
    _write(repo, ".env", "KEY=value\n")

    done = _git(repo, "commit", "-q", "-m", "now a file", env=repo.env)

    assert done.returncode != 0 and "never belong" in done.stderr


# === the push ===================================================================================


def test_a_clean_push_goes_through(repo, remote):
    head = _commit_file(repo, "notes.md", "fine\n", "notes")

    done = _push(repo)

    assert done.returncode == 0, done.stderr
    assert _on_remote(repo, remote, "refs/heads/main") == head


@pytest.mark.parametrize("name", [".env", "prod.env", "data.db", "server.pem", "logs/griot.log", ".env~", '.env"', ".env\tx"])
def test_a_file_that_never_belongs_is_not_pushed_whatever_route_committed_it(repo, remote, name):
    _commit_file(repo, name, "x\n", "slipped in", hooks=False)

    done = _push(repo)

    assert done.returncode != 0 and "never belong" in done.stderr and name in done.stderr
    assert _on_remote(repo, remote, "refs/heads/main") == ""


def test_it_is_not_pushed_even_when_a_later_commit_removed_it(repo, remote):
    """The commit that added it is still sent, and still holds it."""
    _commit_file(repo, "prod.env", "KEY=value\n", "slipped in", hooks=False)
    assert _git(repo, "rm", "-q", "prod.env", env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "-m", "remove it", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "prod.env" in done.stderr


def test_a_commit_that_arrived_by_cherry_pick_is_looked_at(repo, remote):
    """cherry-pick runs neither pre-commit nor commit-msg."""
    assert _git(repo, "checkout", "-q", "-b", "elsewhere", env=repo.env).returncode == 0
    _commit_file(repo, "notes.md", f"the {TERM} rollout\n", "notes", hooks=False)
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    assert _git(repo, "cherry-pick", "elsewhere", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "notes.md" in done.stderr
    assert "rollout" not in done.stderr, "where it is, never the text itself"


def test_a_commit_that_arrived_by_a_fast_forward_is_looked_at(repo, remote):
    """A fast-forward merge runs no hook at all."""
    assert _git(repo, "checkout", "-q", "-b", "elsewhere", env=repo.env).returncode == 0
    _commit_file(repo, f"docs/{TERM}/plan.md", "plan\n", "plan", hooks=False)
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    assert _git(repo, "merge", "-q", "--ff-only", "elsewhere", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "file or directory names" in done.stderr


def test_a_private_term_in_a_commit_message_is_not_pushed(repo, remote):
    head = _commit_file(repo, "notes.md", "fine\n", f"notes for the {TERM} rollout", hooks=False)

    done = _push(repo)

    assert done.returncode != 0 and f"commit {head[:7]}" in done.stderr
    assert "rollout" not in done.stderr


def test_a_private_term_a_later_commit_took_out_again_is_still_not_pushed(repo, remote):
    _commit_file(repo, "notes.md", f"the {TERM} rollout\n", "notes", hooks=False)
    _commit_file(repo, "notes.md", "the rollout\n", "reword")

    done = _push(repo)

    assert done.returncode != 0 and "notes.md" in done.stderr


def test_a_term_the_remote_already_has_does_not_block_what_does_not_add_it(repo, remote):
    """What a push makes public is what its commits ADD. A name put on the
    list today may already be published: the push that removes it must go
    through, and so must one that does not touch it (the audit of the
    history is what reports it)."""
    _commit_file(repo, "old.md", f"the {TERM} rollout\nsecond line\n", "old", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "main", env=repo.env).returncode == 0
    _commit_file(repo, "old.md", f"the {TERM} rollout\nsecond line, edited\n", "touch another line", hooks=False)
    _commit_file(repo, "old.md", "the rollout\nsecond line, edited\n", "take the name out", hooks=False)

    done = _push(repo)

    assert done.returncode == 0, done.stderr


def test_a_branch_named_after_a_private_term_is_not_pushed(repo, remote):
    done = _push(repo, f"main:refs/heads/{TERM}-work")

    assert done.returncode != 0 and "branch or tag name" in done.stderr
    assert _on_remote(repo, remote, f"refs/heads/{TERM}-work") == ""


def test_a_tag_that_says_a_private_term_is_not_pushed(repo, remote):
    assert _push(repo).returncode == 0
    assert _git(repo, "tag", "-a", "v1", "-m", f"release for {TERM}", env=repo.env).returncode == 0

    done = _push(repo, "v1")

    assert done.returncode != 0 and "tag v1" in done.stderr and "release for" not in done.stderr
    assert _on_remote(repo, remote, "refs/tags/v1") == ""


def test_a_clean_tag_is_pushed(repo, remote):
    assert _push(repo).returncode == 0
    assert _git(repo, "tag", "-a", "v1", "-m", "first release", env=repo.env).returncode == 0

    assert _push(repo, "v1").returncode == 0


def test_a_file_only_a_merge_commit_adds_is_seen(repo, remote):
    """A merge can add what neither side had."""
    assert _git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    _commit_file(repo, "side.md", "side\n", "side")
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side", env=repo.env).returncode == 0
    _write(repo, "prod.env", "KEY=value\n")
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge side", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "prod.env" in done.stderr


def test_a_private_term_only_a_merge_commit_adds_is_seen(repo, remote):
    assert _git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    _commit_file(repo, "side.md", "side\n", "side")
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side", env=repo.env).returncode == 0
    _write(repo, "main.md", f"main\nthe {TERM} rollout\n")
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge side", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "main.md" in done.stderr and "rollout" not in done.stderr


def test_merging_what_the_remote_already_has_does_not_bring_its_old_terms_back_as_new(repo, remote):
    """A merge of a published branch adds nothing of that branch: only what
    the merge commit itself changes counts as its own."""
    assert _git(repo, "checkout", "-q", "-b", "published", env=repo.env).returncode == 0
    _commit_file(repo, "old.md", f"the {TERM} rollout\n", "old", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "published", env=repo.env).returncode == 0
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-verify", "-m", "merge published", "published", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode == 0, done.stderr


@needs_gitleaks
def test_a_secret_is_not_pushed_even_when_a_later_commit_removed_it(repo, remote):
    token = _token()
    _commit_file(repo, "leak.py", f'TOKEN = "{token}"\n', "slipped in", hooks=False)
    _commit_file(repo, "leak.py", "TOKEN = None\n", "remove it", hooks=False)

    done = _push(repo, env={**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS})

    assert done.returncode != 0 and "gitleaks" in done.stderr and token not in done.stderr
    assert _on_remote(repo, remote, "refs/heads/main") == ""


@needs_gitleaks
def test_a_secret_only_a_merge_commit_adds_is_seen(repo, remote):
    """gitleaks reads `git log -p`, which shows nothing for a merge unless asked."""
    token = _token()
    assert _git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    _commit_file(repo, "side.md", "side\n", "side")
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side", env=repo.env).returncode == 0
    _write(repo, "leak.py", f'TOKEN = "{token}"\n')
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge side", env=repo.env).returncode == 0

    done = _push(repo, env={**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS})

    assert done.returncode != 0 and token not in done.stderr


@needs_gitleaks
def test_a_clean_push_goes_through_the_real_scanner(repo, remote):
    _commit_file(repo, "notes.md", "fine\n", "notes")

    assert _push(repo, env={**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS}).returncode == 0


def test_a_push_is_blocked_when_gitleaks_cannot_be_found(repo, remote):
    done = _push(repo, env={**repo.env, "GITLEAKS_BIN": "/nonexistent/gitleaks"})

    assert done.returncode != 0 and "gitleaks not found" in done.stderr
    assert _on_remote(repo, remote, "refs/heads/main") == ""


def test_a_failing_gitleaks_blocks_the_push(repo, remote, tmp_path):
    failing = write_script(tmp_path / "gitleaks-fail", "echo 'Finding: something' >&2\nexit 1\n")

    done = _push(repo, env={**repo.env, "GITLEAKS_BIN": failing})

    assert done.returncode != 0 and _on_remote(repo, remote, "refs/heads/main") == ""


def test_only_what_the_remote_does_not_have_is_scanned(repo, remote, tmp_path):
    log = tmp_path / "gitleaks-calls"
    recording = write_script(tmp_path / "gitleaks-rec", f'echo "$@" >> "{log}"\nexit 0\n')
    env = {**repo.env, "GITLEAKS_BIN": recording}
    first = _git(repo, "rev-parse", "HEAD", env=env).stdout.strip()
    assert _push(repo, env=env).returncode == 0
    log.write_text("")
    second = _commit_file(repo, "notes.md", "fine\n", "notes")

    assert _push(repo, env=env).returncode == 0

    calls = log.read_text().splitlines()
    assert len(calls) == 1 and second in calls[0] and f"--not {first}" in calls[0]


def test_a_file_that_never_belonged_and_is_already_there_does_not_block_other_work(repo, remote):
    _commit_file(repo, "prod.env", "KEY=value\n", "long ago", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "main", env=repo.env).returncode == 0
    _commit_file(repo, "notes.md", "fine\n", "notes")

    assert _push(repo).returncode == 0


def test_the_commit_that_deletes_it_can_be_pushed(repo, remote):
    _commit_file(repo, "prod.env", "KEY=value\n", "long ago", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "main", env=repo.env).returncode == 0
    assert _git(repo, "rm", "-q", "prod.env", env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "-m", "remove it", env=repo.env).returncode == 0

    assert _push(repo).returncode == 0


def test_deleting_a_branch_on_the_remote_sends_nothing_and_needs_no_scanner(repo, remote):
    assert _push(repo, "main", "main:refs/heads/old").returncode == 0

    done = _push(repo, ":refs/heads/old", env={**repo.env, "GITLEAKS_BIN": "/nonexistent/gitleaks"})

    assert done.returncode == 0, done.stderr
    assert _on_remote(repo, remote, "refs/heads/old") == ""


def test_a_new_branch_is_scanned_from_where_it_leaves_what_the_remote_has(repo, remote, tmp_path):
    """The remote has no tip to report for a branch it never saw: what it
    has is what its other branches hold."""
    log = tmp_path / "gitleaks-calls"
    recording = write_script(tmp_path / "gitleaks-rec", f'echo "$@" >> "{log}"\nexit 0\n')
    env = {**repo.env, "GITLEAKS_BIN": recording}
    _commit_file(repo, "old.md", f"the {TERM} rollout\n", "published long ago", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "main", env=env).returncode == 0
    assert _git(repo, "checkout", "-q", "-b", "topic", env=env).returncode == 0
    tip = _commit_file(repo, "notes.md", "fine\n", "notes", hooks=False)
    log.write_text("")

    done = _push(repo, "topic", env=env)

    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 1 and f"{tip} --not --remotes=origin" in calls[0]


def test_the_first_commit_of_a_repository_is_looked_at_whatever_git_was_told_about_showing_it(repo, tmp_path):
    """`log.showRoot=false` makes `git log` print no changes for a commit
    without a parent."""
    path = tmp_path / "remote.git"
    assert _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path), env=repo.env).returncode == 0
    assert _git(repo, "remote", "add", "origin", str(path), env=repo.env).returncode == 0
    assert _git(repo, "config", "log.showRoot", "false", env=repo.env).returncode == 0
    _commit_file(repo, "prod.env", f"KEY={TERM}\n", "first", hooks=False)

    done = _push(repo)

    assert done.returncode != 0 and "never belong" in done.stderr and "lines the commits being pushed add" in done.stderr


def test_a_conflict_settled_by_keeping_a_published_line_is_not_the_merge_adding_it(repo, remote):
    """In the combined diff of a merge a line that came from ONE parent
    carries one +, and a line the merge wrote itself carries one per parent."""
    _commit_file(repo, "plan.md", "one\n", "plan")
    assert _git(repo, "checkout", "-q", "-b", "published", env=repo.env).returncode == 0
    _commit_file(repo, "plan.md", f"the {TERM} rollout\n", "theirs", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "published", env=repo.env).returncode == 0
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "plan.md", "ours\n", "ours")
    assert _git(repo, "merge", "-q", "published", env=repo.env).returncode != 0, "a conflict"
    (repo / "plan.md").write_text(f"ours\nthe {TERM} rollout\n")
    assert _git(repo, "add", "plan.md", env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge published", env=repo.env).returncode == 0

    assert _push(repo).returncode == 0

    _commit_file(repo, "more.md", "more\n", "more", hooks=False)
    assert _git(repo, "checkout", "-q", "-b", "side2", "HEAD~1", env=repo.env).returncode == 0
    _commit_file(repo, "plan.md", f"ours\nthe {TERM} rollout\ntheirs again\n", "side2", hooks=False)
    assert _git(repo, "checkout", "-q", "main", env=repo.env).returncode == 0
    _commit_file(repo, "plan.md", f"ours\nthe {TERM} rollout\nours again\n", "main again", hooks=False)
    assert _git(repo, "merge", "-q", "side2", env=repo.env).returncode != 0, "a conflict"
    (repo / "plan.md").write_text(f"ours\nthe {TERM} rollout\nwritten by the merge for {TERM}\n")
    assert _git(repo, "add", "plan.md", env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "merge side2", env=repo.env).returncode == 0

    done = _push(repo)

    assert done.returncode != 0 and "plan.md" in done.stderr


def test_a_remote_that_moved_without_this_clone_knowing_does_not_break_the_hook(repo, remote, tmp_path):
    """A forced push over commits never fetched: the old tip is not here to
    measure from, so everything not known to be on the remote is looked at."""
    assert _push(repo).returncode == 0
    other = tmp_path / "other"
    assert _git(tmp_path, "clone", "-q", str(remote), str(other), env=repo.env).returncode == 0
    (other / "theirs.md").write_text("theirs\n")
    assert _git(other, "add", "theirs.md", env=repo.env).returncode == 0
    assert _git(other, "commit", "-q", "-m", "theirs", env=repo.env).returncode == 0
    assert _git(other, "push", "-q", "origin", "HEAD:main", env=repo.env).returncode == 0
    _commit_file(repo, "prod.env", "KEY=value\n", "slipped in", hooks=False)

    done = _push(repo, "--force", "main")

    assert done.returncode != 0 and "prod.env" in done.stderr


# --- a scan that cannot be done is not a scan that found nothing ----------------------------------


def _commit_bytes(repo, name, content: bytes):
    (repo / name).write_bytes(content)
    assert _git(repo, "add", "-f", name, env=repo.env).returncode == 0
    assert _git(repo, "commit", "-q", "--no-verify", "-m", f"add {name}", env=repo.env).returncode == 0


@pytest.mark.parametrize("locale", ["en_US.UTF-8", "C"])
def test_a_line_that_is_not_valid_utf8_does_not_stop_the_search_for_terms(repo, remote, locale):
    """A file in another encoding made the program that reads the changes
    give up, in a UTF-8 locale, and the push went through with whatever
    came after it."""
    _commit_bytes(repo, "a_latin1.txt", b"caf\xe9 in latin1\n")
    _commit_bytes(repo, "z_after.txt", f"the {TERM} rollout\n".encode())

    done = _push(repo, env={**repo.env, "LC_ALL": locale, "LANG": locale})

    assert done.returncode != 0 and "z_after.txt" in done.stderr
    assert "latin1" not in done.stderr and "rollout" not in done.stderr


def _utf8_locale():
    """A UTF-8 locale this machine has, or None."""
    have = {name.lower().replace("utf8", "utf-8") for name in
            subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout.split()}
    return next((name for name in ("en_US.UTF-8", "C.UTF-8", "pt_BR.UTF-8") if name.lower() in have), None)


UTF8 = _utf8_locale()
in_utf8 = pytest.mark.skipif(UTF8 is None, reason="no UTF-8 locale on this machine")
# The same line holds the term and a byte that is not valid UTF-8, on either
# side of it. In a UTF-8 locale one tool stopped matching at that byte and
# another refused the whole line, with nothing said.
ODD_LINES = [b"caf\xe9 " + TERM.encode() + b"\n", TERM.encode() + b" caf\xe9\n"]


@in_utf8
@pytest.mark.parametrize("line", ODD_LINES)
def test_a_term_beside_a_byte_that_is_not_utf8_is_found_at_commit(repo, line):
    (repo / "latin1.txt").write_bytes(line)
    assert _git(repo, "add", "latin1.txt", env=repo.env).returncode == 0

    done = _git(repo, "commit", "-q", "-m", "notes", env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "latin1.txt" in done.stderr and _count(repo) == 0


@in_utf8
@pytest.mark.parametrize("line", ODD_LINES)
def test_a_term_beside_a_byte_that_is_not_utf8_is_found_at_push(repo, remote, line):
    _commit_bytes(repo, "latin1.txt", line)

    done = _push(repo, env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "latin1.txt" in done.stderr


@in_utf8
@pytest.mark.parametrize("line", ODD_LINES)
def test_a_term_beside_a_byte_that_is_not_utf8_is_found_in_a_tag_message(repo, remote, line, tmp_path):
    assert _push(repo).returncode == 0
    (tmp_path / "tag-message").write_bytes(line)
    assert _git(repo, "tag", "-a", "v1", "-F", str(tmp_path / "tag-message"), env=repo.env).returncode == 0

    done = _push(repo, "v1", env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "tag v1" in done.stderr


def _stage_a_name_in_bytes(repo, name: bytes):
    """Into the index alone: some file systems refuse such a name, git does not."""
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input="KEY=value\n",
                          capture_output=True, text=True, env=repo.env).stdout.strip()
    done = subprocess.run([b"git", b"-C", os.fsencode(str(repo)), b"update-index", b"--add", b"--cacheinfo",
                           b"100644," + blob.encode() + b"," + name], capture_output=True, env=repo.env)
    assert done.returncode == 0, done.stderr


@in_utf8
def test_a_file_that_never_belongs_is_seen_under_a_name_that_is_not_utf8(repo, remote):
    _stage_a_name_in_bytes(repo, b"caf\xe9.env")

    done = _git(repo, "commit", "-q", "-m", "change", env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "never belong" in done.stderr
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "change", env=repo.env).returncode == 0
    pushed = _push(repo, env={**repo.env, "LC_ALL": UTF8})
    assert pushed.returncode != 0 and "never belong" in pushed.stderr


@in_utf8
def test_a_private_term_is_seen_in_a_name_that_is_not_utf8(repo, remote):
    _stage_a_name_in_bytes(repo, b"caf\xe9-" + TERM.encode() + b".md")

    done = _git(repo, "commit", "-q", "-m", "change", env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "file or directory names" in done.stderr
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "change", env=repo.env).returncode == 0
    assert _push(repo, env={**repo.env, "LC_ALL": UTF8}).returncode != 0


# --- a name with an accent ------------------------------------------------------------------------
# Reading bytes costs the system's tools what they never had on every system:
# matching "AÇÃO" to a term written "ação". git's own searches do it, in the
# locale of whoever runs the hook, and those two are left in it.


@in_utf8
def test_git_s_own_search_still_ignores_the_case_of_an_accented_letter_at_commit(repo):
    (repo / ".git" / "sensitive-terms.txt").write_text("projeto ação\n", encoding="utf-8")
    _write(repo, "notes.md", "sobre o PROJETO AÇÃO\n")

    done = _git(repo, "commit", "-q", "-m", "notes", env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "notes.md" in done.stderr


@in_utf8
def test_and_in_the_messages_of_the_commits_a_push_sends(repo, remote):
    (repo / ".git" / "sensitive-terms.txt").write_text("projeto ação\n", encoding="utf-8")
    _commit_file(repo, "notes.md", "fine\n", "sobre o PROJETO AÇÃO", hooks=False)

    done = _push(repo, env={**repo.env, "LC_ALL": UTF8})

    assert done.returncode != 0 and "messages of these commits" in done.stderr


def test_a_private_list_that_starts_with_a_byte_order_mark_still_matches_its_first_term(repo):
    """Some editors put three bytes in front of the first line."""
    (repo / ".git" / "sensitive-terms.txt").write_bytes(b"\xef\xbb\xbf" + TERM.encode() + b"\n")
    _write(repo, "notes.md", f"the {TERM} rollout\n")

    assert _git(repo, "commit", "-q", "-m", "notes", env=repo.env).returncode != 0


def test_a_finding_in_a_merge_says_which_merges_to_look_at(repo, remote, tmp_path):
    """gitleaks cannot say where when it is handed lines."""
    finding = write_script(tmp_path / "gitleaks-merge", 'case "$1" in stdin) cat > /dev/null; exit 1 ;; esac\nexit 0\n')
    _merge_adding(repo, "extra.py", 'VALUE = "written by the merge"\n')
    merge = _git(repo, "rev-parse", "--short", "HEAD", env=repo.env).stdout.strip()

    done = _push(repo, env={**repo.env, "GITLEAKS_BIN": finding})

    assert done.returncode != 0 and merge in done.stderr


def test_a_term_after_a_nul_byte_on_the_same_line_is_found(repo, remote):
    """git calls a file text when its first 8000 bytes hold no NUL."""
    _commit_bytes(repo, "late_nul.txt", b"a" * 9000 + b"\n" + b"ok\x00 " + TERM.encode() + b"\n")

    done = _push(repo)

    assert done.returncode != 0 and "late_nul.txt" in done.stderr


def test_a_search_that_fails_blocks_the_push(repo, remote, tmp_path):
    """Whatever the reason: nothing found by a search that did not run is
    not a clean result."""
    broken = tmp_path / "broken-tools"
    broken.mkdir()
    write_script(broken / "awk", "exit 2\n")
    _commit_file(repo, "notes.md", "fine\n", "notes", hooks=False)

    done = _push(repo, env={**repo.env, "PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"})

    assert done.returncode != 0 and "could not be read" in done.stderr
    assert _on_remote(repo, remote, "refs/heads/main") == ""


def _awk_that(tmp_path, body):
    """An `awk` first on the PATH that does `body` ({awk} is the real one)."""
    tools = tmp_path / "odd-tools"
    tools.mkdir()
    write_script(tools / "awk", body.format(awk=shutil.which("awk")))
    return f"{tools}{os.pathsep}{os.environ['PATH']}"


def test_a_reader_that_says_it_went_well_and_saw_no_commit_blocks_the_push(repo, remote, tmp_path):
    """Its exit status is not the proof: every commit sent has to have been
    seen."""
    _commit_file(repo, "notes.md", "fine\n", "notes", hooks=False)

    done = _push(repo, env={**repo.env, "PATH": _awk_that(tmp_path, "cat > /dev/null\nexit 0\n")})

    assert done.returncode != 0 and "could not be read" in done.stderr


def test_a_reader_that_saw_every_commit_and_still_failed_blocks_the_push(repo, remote, tmp_path):
    _commit_file(repo, "notes.md", "fine\n", "notes", hooks=False)

    done = _push(repo, env={**repo.env, "PATH": _awk_that(tmp_path, '"{awk}" "$@"\nexit 3\n')})

    assert done.returncode != 0 and "could not be read" in done.stderr


@pytest.mark.parametrize("what", ["blob", "tree"])
def test_a_tag_that_points_at_something_other_than_a_commit_is_not_pushed(repo, remote, what):
    """A file or a directory can be tagged and pushed on its own, with no
    commit to look at."""
    assert _push(repo).returncode == 0
    if what == "blob":
        made = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=f"the {TERM} rollout\n",
                              capture_output=True, text=True, env=repo.env)
        target = made.stdout.strip()
    else:
        target = _git(repo, "rev-parse", "main^{tree}", env=repo.env).stdout.strip()
    assert _git(repo, "tag", "loose", target, env=repo.env).returncode == 0

    done = _push(repo, "loose")

    assert done.returncode != 0 and "not a commit" in done.stderr
    assert _on_remote(repo, remote, "refs/tags/loose") == ""


def test_a_private_list_saved_with_windows_line_endings_still_matches(repo, remote):
    """A carriage return at the end of every term made no term match
    anything, in every hook, with no word said."""
    (repo / ".git" / "sensitive-terms.txt").write_bytes(f"# names\r\n{TERM}\r\n".encode())
    _write(repo, "notes.md", f"the {TERM} rollout\n")

    assert _git(repo, "commit", "-q", "-m", "notes", env=repo.env).returncode != 0
    assert _git(repo, "commit", "-q", "--no-verify", "-m", "notes", env=repo.env).returncode == 0
    assert _push(repo).returncode != 0


def test_a_term_is_found_whatever_encoding_git_was_told_to_print_the_log_in(repo, remote):
    assert _git(repo, "config", "i18n.logOutputEncoding", "UTF-16", env=repo.env).returncode == 0
    _commit_file(repo, "notes.md", f"the {TERM} rollout\n", f"notes for {TERM}", hooks=False)

    done = _push(repo)

    assert done.returncode != 0 and "notes.md" in done.stderr and "messages of these commits" in done.stderr


# --- gitleaks and merges --------------------------------------------------------------------------


def test_what_a_merge_commit_itself_adds_is_handed_to_the_scanner(repo, remote, tmp_path):
    log = tmp_path / "gitleaks-calls"
    seen = tmp_path / "gitleaks-stdin"
    recording = write_script(tmp_path / "gitleaks-rec",
                             f'echo "$@" >> "{log}"\ncase "$1" in stdin) cat >> "{seen}" ;; esac\nexit 0\n')
    _merge_adding(repo, "extra.py", 'VALUE = "written by the merge"\n')

    assert _push(repo, env={**repo.env, "GITLEAKS_BIN": recording}).returncode == 0

    calls = log.read_text().splitlines()
    assert [call.split()[0] for call in calls] == ["git", "stdin"]
    assert 'VALUE = "written by the merge"' in seen.read_text() and "side" not in seen.read_text()


def test_a_push_without_a_merge_makes_one_call_to_the_scanner(repo, remote, tmp_path):
    log = tmp_path / "gitleaks-calls"
    recording = write_script(tmp_path / "gitleaks-rec", f'echo "$@" >> "{log}"\nexit 0\n')

    assert _push(repo, env={**repo.env, "GITLEAKS_BIN": recording}).returncode == 0

    assert [call.split()[0] for call in log.read_text().splitlines()] == ["git"]


@needs_gitleaks
def test_merging_a_published_branch_that_holds_a_finding_does_not_block_the_push(repo, remote):
    """What the remote already has is not this push's to answer for: only
    what the merge commit itself adds is the merge's own."""
    env = {**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS}
    assert _git(repo, "checkout", "-q", "-b", "published", env=env).returncode == 0
    _commit_file(repo, "old.py", f'TOKEN = "{_token()}"\n', "long ago", hooks=False)
    assert _git(repo, "push", "-q", "--no-verify", "origin", "published", env=env).returncode == 0
    assert _git(repo, "checkout", "-q", "main", env=env).returncode == 0
    _commit_file(repo, "main.md", "main\n", "main")
    assert _git(repo, "merge", "-q", "--no-ff", "--no-verify", "-m", "merge published", "published", env=env).returncode == 0

    done = _push(repo, env=env)

    assert done.returncode == 0, done.stderr


def test_a_missing_private_list_warns_and_still_checks_the_rest(repo, remote):
    (repo / ".git" / "sensitive-terms.txt").unlink()
    _commit_file(repo, "notes.md", f"the {TERM} rollout\n", "notes", hooks=False)

    done = _push(repo)

    assert done.returncode == 0 and "NOT being checked" in done.stderr
    _commit_file(repo, "prod.env", "KEY=value\n", "slipped in", hooks=False)
    assert _push(repo).returncode != 0


def test_a_path_with_spaces_is_reported_whole(repo, remote):
    _commit_file(repo, "my notes/the plan.md", f"the {TERM} rollout\n", "notes", hooks=False)

    done = _push(repo)

    assert done.returncode != 0 and "my notes/the plan.md" in done.stderr


# === the installer knows the new hooks ==========================================================


def test_every_hook_is_executable():
    for name in ("pre-commit", "commit-msg", "pre-merge-commit", "pre-push"):
        assert os.access(HOOKS / name, os.X_OK), name


def test_the_check_reports_all_four(tmp_path, stub_gitleaks):
    clone = make_repo(tmp_path / "clone", stub_gitleaks)
    shutil.copytree(SCRIPTS, clone / "scripts")
    install = str(clone / "scripts" / "install-git-hooks.sh")
    assert subprocess.run([install], capture_output=True, text=True, cwd=clone, env=clone.env).returncode == 0

    check = subprocess.run([install, "--check"], capture_output=True, text=True, cwd=clone, env=clone.env)

    assert check.returncode == 0, check.stdout
    for name in ("pre-commit", "commit-msg", "pre-merge-commit", "pre-push"):
        assert f"{name} is executable" in check.stdout
    (clone / "scripts" / "git-hooks" / "pre-push").chmod(0o644)
    broken = subprocess.run([install, "--check"], capture_output=True, text=True, cwd=clone, env=clone.env)
    assert broken.returncode == 1 and "pre-push is not executable" in broken.stdout
