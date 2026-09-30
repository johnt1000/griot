"""The versioned git hooks (scripts/git-hooks/) exercised for real, in
throwaway repositories: a hook that silently stops blocking is the failure
worth guarding against, and only running `git commit` proves it blocks.

gitleaks itself is replaced by a stub in most tests, so the path, term and
message logic is covered on any machine; the tests that need the real binary
are skipped where it is not installed.
"""

import os
import secrets
import shutil
import stat
import string
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the hooks are bash scripts")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
HOOKS = SCRIPTS / "git-hooks"

REAL_GITLEAKS = shutil.which("gitleaks") or next(
    (p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks") if os.access(p, os.X_OK)), None)
needs_gitleaks = pytest.mark.skipif(REAL_GITLEAKS is None, reason="gitleaks is not installed")


def _env(gitleaks_bin, author_email="maintainer@example.com"):
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Maintainer", "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": "Maintainer", "GIT_COMMITTER_EMAIL": author_email,
        "GITLEAKS_BIN": gitleaks_bin,
    }


class Repo:
    """A throwaway repository plus the isolated environment every git call in it uses."""

    def __init__(self, path, env):
        self.path, self.env = path, env

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def __truediv__(self, other):
        return self.path / other


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


@pytest.fixture
def stub_gitleaks(tmp_path):
    stub = tmp_path / "gitleaks-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


@pytest.fixture
def repo(tmp_path, stub_gitleaks):
    path = tmp_path / "repo"
    path.mkdir()
    env = _env(stub_gitleaks)
    assert _git(path, "init", "-q", env=env).returncode == 0
    assert _git(path, "config", "core.hooksPath", str(HOOKS), env=env).returncode == 0
    shutil.copy(REPO_ROOT / ".gitleaks.toml", path / ".gitleaks.toml")
    return Repo(path, env)


def _terms(repo, text):
    (repo / ".git" / "sensitive-terms.txt").write_text(text)


def _commit(repo, message="change", env=None):
    return _git(repo, "commit", "-q", "-m", message, env=env or repo.env)


def _stage(repo, name, content="hello\n"):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    assert _git(repo, "add", "-f", name, env=repo.env).returncode == 0


def _commits(repo):
    out = _git(repo, "rev-list", "--all", "--count", env=repo.env)
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def test_the_hooks_are_executable():
    for name in ("pre-commit", "commit-msg"):
        assert os.access(HOOKS / name, os.X_OK), name
    assert os.access(SCRIPTS / "install-git-hooks.sh", os.X_OK)
    assert not os.access(HOOKS / "sensitive-terms.example", os.X_OK)  # a template, not a script


def test_a_clean_commit_goes_through(repo):
    _stage(repo, "notes.md")

    result = _commit(repo)

    assert result.returncode == 0, result.stderr
    assert _commits(repo) == 1


@pytest.mark.parametrize("name", [".env", ".env.local", "data.db", "collection.sqlite3", "server.pem", "logs/griot.log",
                                  "qdrant_data/x/segment.dat", "repos.json", "quality_golden_set.json",
                                  ".spend_state.json", ".griot.lock",
                                  ".ENV", "config/.Env.production", ".envrc", "id_rsa", "home/.ssh/id_ed25519", "bundle.p12"])
def test_files_that_never_belong_are_blocked(repo, name):
    _stage(repo, name)

    result = _commit(repo)

    assert result.returncode != 0
    assert "never belong" in result.stderr and name in result.stderr
    assert _commits(repo) == 0


@pytest.mark.parametrize("name", ["docs/logs.md", "keyring.py", "id_rsa.pub", "src/environment.py", "notes/database.md",
                                  ".ENV.EXAMPLE"])
def test_files_that_only_resemble_forbidden_ones_are_allowed(repo, name):
    _stage(repo, name)

    assert _commit(repo).returncode == 0


def test_a_blocked_path_with_spaces_is_reported_intact(repo):
    _stage(repo, "my dir/.env")
    _stage(repo, "we ird.db")

    result = _commit(repo)

    assert result.returncode != 0
    assert "my dir/.env" in result.stderr and "we ird.db" in result.stderr


def test_an_env_example_is_allowed(repo):
    _stage(repo, ".env.example", "KEY=\n")

    assert _commit(repo).returncode == 0


def test_a_private_term_in_a_file_is_blocked_whatever_its_case_and_the_content_is_not_echoed(repo):
    _terms(repo, "acme-private\n")
    _stage(repo, "docs/note.md", "line one\nsee the ACME-Private repository\n")

    result = _commit(repo)

    assert result.returncode != 0
    assert "docs/note.md:2" in result.stderr
    assert "repository" not in result.stderr  # only file:line, never the line itself
    assert _commits(repo) == 0


@pytest.mark.parametrize("name", ["acme-private-notes.txt", "docs/Acme-Private/readme.md", "Acme-Private"])
def test_a_private_term_in_a_file_or_directory_name_is_blocked(repo, name):
    # git grep reads contents only; a private name can just as well be a path.
    _terms(repo, "acme-private\n")
    _stage(repo, name, "nothing private inside\n")

    result = _commit(repo)

    assert result.returncode != 0
    assert name in result.stderr
    assert _commits(repo) == 0


def test_a_term_hit_in_a_path_with_spaces_is_reported_intact(repo):
    _terms(repo, "acme-private\n")
    _stage(repo, "my notes/note.md", "acme-private\n")

    result = _commit(repo)

    assert result.returncode != 0
    assert "my notes/note.md:1" in result.stderr


def test_a_private_term_in_an_untouched_file_still_blocks_a_later_commit(repo):
    # The check covers the whole staged tree, so a leak that got in earlier is caught, not only new lines.
    _stage(repo, "old.md", "acme-private\n")
    assert _commit(repo).returncode == 0
    _terms(repo, "acme-private\n")
    _stage(repo, "other.md")

    assert _commit(repo).returncode != 0


def test_blank_lines_and_comments_in_the_terms_file_do_not_match_everything(repo):
    # GNU grep (the CI runner) treats an empty pattern as matching every line, the classic way to block
    # every commit. BSD grep and git grep do not, so this is only sensitive on Linux.
    _terms(repo, "# a comment\n\n   \nacme-private\n\n")
    _stage(repo, "clean.md", "nothing private here\n")

    assert _commit(repo).returncode == 0


def test_a_terms_file_with_only_comments_checks_nothing_and_passes(repo):
    # The staged file holds the comment's own text, so a comment used as a pattern would match it.
    _terms(repo, "# acme-private\n")
    _stage(repo, "clean.md", "# acme-private\n")

    assert _commit(repo).returncode == 0


def test_a_missing_terms_file_warns_but_does_not_block(repo):
    _stage(repo, "clean.md")

    result = _commit(repo)

    assert result.returncode == 0
    assert "warning" in result.stderr and "NOT being checked" in result.stderr


def test_a_private_term_in_the_commit_message_is_blocked(repo):
    _terms(repo, "acme-private\n")
    _stage(repo, "clean.md")

    result = _commit(repo, message="docs for the Acme-Private project")

    assert result.returncode != 0
    assert "commit message" in result.stderr or "message" in result.stderr
    assert _commits(repo) == 0
    assert _commit(repo, message="docs").returncode == 0


def test_git_template_comments_in_the_message_are_not_scanned(repo, tmp_path):
    _terms(repo, "acme-private\n")
    message = tmp_path / "MSG"
    message.write_text("docs: fine\n# Changes to be committed: acme-private/file.md\n")

    result = subprocess.run([str(HOOKS / "commit-msg"), str(message)], capture_output=True, text=True, cwd=repo, env=repo.env)

    assert result.returncode == 0


def test_the_commit_is_blocked_when_gitleaks_cannot_be_found(repo):
    _stage(repo, "clean.md")
    env = {**repo.env, "GITLEAKS_BIN": "/nonexistent/gitleaks"}

    result = _commit(repo, env=env)

    assert result.returncode != 0
    assert "gitleaks not found" in result.stderr
    assert _commits(repo) == 0


def test_a_failing_gitleaks_blocks_the_commit(repo, tmp_path):
    failing = tmp_path / "gitleaks-fail"
    failing.write_text("#!/bin/sh\necho 'Finding: something' >&2\nexit 1\n")
    failing.chmod(0o755)
    _stage(repo, "clean.md")

    result = _commit(repo, env={**repo.env, "GITLEAKS_BIN": str(failing)})

    assert result.returncode != 0
    assert "gitleaks reported a finding" in result.stderr
    assert _commits(repo) == 0


def test_the_maintainers_own_email_is_never_a_reason_to_block(repo):
    # An author email is normal commit metadata, and an address in a file is normal documentation.
    _terms(repo, "acme-private\n")
    _stage(repo, "SECURITY.md", "Contact: someone.real@gmail.com\n")

    result = _commit(repo, env=_env(repo.env["GITLEAKS_BIN"], author_email="someone.real@gmail.com"))

    assert result.returncode == 0, result.stderr


@needs_gitleaks
def test_a_realistic_token_is_blocked_by_the_real_scanner(repo):
    token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    env = {**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS}
    _stage(repo, "leak.py", f'TOKEN = "{token}"\n')

    result = _commit(repo, env=env)

    assert result.returncode != 0
    assert token not in result.stderr  # findings are redacted
    assert _commits(repo) == 0


@needs_gitleaks
def test_a_clean_commit_goes_through_the_real_scanner(repo):
    _stage(repo, "notes.md")

    assert _commit(repo, env={**repo.env, "GITLEAKS_BIN": REAL_GITLEAKS}).returncode == 0


# --- the installer -------------------------------------------------------------


@pytest.fixture
def clone(tmp_path, stub_gitleaks):
    path = tmp_path / "clone"
    path.mkdir()
    env = _env(stub_gitleaks)
    assert _git(path, "init", "-q", env=env).returncode == 0
    shutil.copytree(SCRIPTS, path / "scripts")
    return Repo(path, env)


def _install(clone):
    return subprocess.run([str(clone / "scripts" / "install-git-hooks.sh")], capture_output=True, text=True,
                          cwd=clone, env=clone.env)


def test_the_installer_points_git_at_the_versioned_hooks_and_creates_the_private_list(clone):
    result = _install(clone)

    assert result.returncode == 0, result.stderr
    assert _git(clone, "config", "core.hooksPath", env=clone.env).stdout.strip() == "scripts/git-hooks"
    terms = clone / ".git" / "sensitive-terms.txt"
    assert terms.exists() and stat.S_IMODE(terms.stat().st_mode) == 0o600


def test_the_installer_never_overwrites_an_existing_private_list(clone):
    (clone / ".git" / "sensitive-terms.txt").write_text("my-real-term\n")

    assert _install(clone).returncode == 0
    assert _install(clone).returncode == 0

    assert (clone / ".git" / "sensitive-terms.txt").read_text() == "my-real-term\n"


def test_hooks_enabled_by_the_installer_actually_block(clone):
    assert _install(clone).returncode == 0
    (clone / ".git" / "sensitive-terms.txt").write_text("acme-private\n")
    (clone / "note.md").write_text("acme-private\n")
    assert _git(clone, "add", "note.md", env=clone.env).returncode == 0

    result = _git(clone, "commit", "-q", "-m", "x", env=clone.env)

    assert result.returncode != 0 and "private terms" in result.stderr
