"""scripts/audit-history.sh exercised for real, in throwaway repositories whose
history is built to contain what the hooks would have refused.

The hooks only guard new commits; the audit is what looks back, so every case
here hides the problem somewhere the current tree does not show it (a file that
was deleted later, an old message, a name that no longer exists).

gitleaks is a stub in most tests, so the term, path and message logic is covered
on any machine; the tests that need the real binary skip where it is missing.
"""

import os
import secrets
import shutil
import string
import subprocess
import sys

import pytest
from hook_support import REPO_ROOT, SCRIPTS, Repo, git, make_env, write_script

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the audit is a bash script")

AUDIT = SCRIPTS / "audit-history.sh"

REAL_GITLEAKS = shutil.which("gitleaks") or next(
    (p for p in ("/opt/homebrew/bin/gitleaks", "/usr/local/bin/gitleaks") if os.access(p, os.X_OK)), None)
needs_gitleaks = pytest.mark.skipif(REAL_GITLEAKS is None, reason="gitleaks is not installed")


@pytest.fixture
def repo(tmp_path):
    stub = write_script(tmp_path / "gitleaks-stub", "exit 0\n")
    path = tmp_path / "repo"
    path.mkdir()
    env = make_env(stub)
    assert git(path, "init", "-q", env=env).returncode == 0
    shutil.copy(REPO_ROOT / ".gitleaks.toml", path / ".gitleaks.toml")
    (path / ".git" / "sensitive-terms.txt").write_text("acme-private\n")
    return Repo(path, env)


def commit(repo, files=None, message="change", remove=()):
    for name, content in (files or {}).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for name in remove:
        (repo / name).unlink()
    assert git(repo, "add", "-A", "-f", env=repo.env).returncode == 0
    result = git(repo, "commit", "-q", "--allow-empty", "-m", message, env=repo.env)
    assert result.returncode == 0, result.stderr


def add_empty_commits(repo, count):
    """`count` empty commits on the current branch in one process (git commit per commit is slow)."""
    branch = git(repo, "symbolic-ref", "--short", "HEAD", env=repo.env).stdout.strip()
    stream = ""
    for i in range(count):
        message = f"empty {i}"
        stream += (f"commit refs/heads/{branch}\ncommitter M <m@example.com> {1_700_000_000 + i} +0000\n"
                   f"data {len(message)}\n{message}\n")
        if i == 0:  # only the first names its parent; the rest continue the branch tip fast-import already holds
            stream += f"from refs/heads/{branch}^0\n"
        stream += "\n"
    result = subprocess.run(["git", "-C", str(repo), "fast-import", "--quiet"], input=stream, text=True,
                            capture_output=True, env=repo.env)
    assert result.returncode == 0, result.stderr


def evil_merge(repo, files):
    """A real two-parent merge whose merge commit alone adds `files`."""
    branch = git(repo, "symbolic-ref", "--short", "HEAD", env=repo.env).stdout.strip()
    assert git(repo, "checkout", "-q", "-b", "side", env=repo.env).returncode == 0
    commit(repo, {"side.txt": "s\n"}, "side")
    assert git(repo, "checkout", "-q", branch, env=repo.env).returncode == 0
    commit(repo, {"main.txt": "m\n"}, "main")
    assert git(repo, "merge", "-q", "--no-commit", "--no-ff", "side", env=repo.env).returncode == 0
    for name, content in files.items():
        (repo / name).write_text(content)
    assert git(repo, "add", "-A", "-f", env=repo.env).returncode == 0
    assert git(repo, "commit", "-q", "-m", "merge side", env=repo.env).returncode == 0


def audit(repo, **env_extra):
    return subprocess.run([str(AUDIT)], capture_output=True, text=True, cwd=repo, env={**repo.env, **env_extra})


def test_the_audit_script_is_executable():
    assert os.access(AUDIT, os.X_OK)


def test_a_clean_history_passes(repo):
    commit(repo, {"a.md": "fine\n"}, "first")
    commit(repo, {"b.md": "also fine\n"}, "second")

    result = audit(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 revisions" in result.stdout and "clean" in result.stdout


def test_a_term_that_was_deleted_from_the_tree_is_still_found_in_history(repo):
    commit(repo, {"docs/old.md": "line\nwork for acme-private\n"}, "add doc")
    commit(repo, remove=["docs/old.md"], message="drop doc")

    result = audit(repo)

    assert result.returncode == 1
    assert "docs/old.md" in result.stdout
    assert "work for" not in result.stdout + result.stderr  # locations only, never the content


def test_a_term_is_found_whatever_its_case(repo):
    commit(repo, {"a.md": "the ACME-Private repo\n"}, "add")

    assert audit(repo).returncode == 1


def test_a_term_in_a_file_name_that_no_longer_exists_is_found(repo):
    commit(repo, {"acme-private-notes.txt": "nothing inside\n", "docs/Acme-Private/x.md": "nothing\n"}, "add")
    commit(repo, remove=["acme-private-notes.txt", "docs/Acme-Private/x.md"], message="drop")

    result = audit(repo)

    assert result.returncode == 1
    assert "acme-private-notes.txt" in result.stdout and "docs/Acme-Private/x.md" in result.stdout


def test_a_term_in_an_old_commit_message_is_found_and_the_message_is_not_echoed(repo):
    commit(repo, {"a.md": "fine\n"}, "docs for the Acme-Private project")
    commit(repo, {"b.md": "fine\n"}, "unrelated later change")

    result = audit(repo)

    assert result.returncode == 1
    short = git(repo, "log", "--format=%h", "--grep=Acme-Private", "-i", env=repo.env).stdout.strip()
    assert short and short in result.stdout
    assert "docs for the" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", [".env", "config/.Env.production", "data.db", "id_rsa", "logs/griot.log", "repos.json"])
def test_a_forbidden_file_that_was_committed_once_is_found(repo, name):
    commit(repo, {name: "x\n"}, "oops")
    commit(repo, remove=[name], message="remove it")

    result = audit(repo)

    assert result.returncode == 1 and name in result.stdout


@pytest.mark.parametrize("name", ["docs/logs.md", "keyring.py", "id_rsa.pub", ".env.example"])
def test_files_that_only_resemble_forbidden_ones_are_not_reported(repo, name):
    commit(repo, {name: "x\n"}, "add")

    assert audit(repo).returncode == 0


def test_a_failing_gitleaks_is_reported_as_a_secrets_problem(repo, tmp_path):
    commit(repo, {"a.md": "x\n"})
    # The audit asks gitleaks for a dedicated exit code for leaks (see LEAK_EXIT), because exit 1 is also its fatal-error code.
    failing = write_script(tmp_path / "gitleaks-fail", "echo 'Finding: something' >&2\nexit 3\n")

    result = audit(repo, GITLEAKS_BIN=failing)

    assert result.returncode == 1
    assert "secrets" in result.stdout.lower() and "FOUND" in result.stdout


def test_the_audit_asks_gitleaks_for_a_dedicated_leak_exit_code(repo, tmp_path):
    args_file = tmp_path / "gitleaks-args"
    recorder = write_script(tmp_path / "gitleaks-recorder", f'echo "$@" > {args_file}\nexit 0\n')
    commit(repo, {"a.md": "x\n"})

    audit(repo, GITLEAKS_BIN=recorder)

    assert "--exit-code 3" in args_file.read_text()


def test_a_gitleaks_fatal_error_that_exits_one_is_not_reported_as_a_leak(repo, tmp_path):
    # gitleaks exits 1 for a leak by default AND for a fatal error (a malformed config, say).
    fatal = write_script(tmp_path / "gitleaks-fatal", "echo 'unable to load gitleaks config' >&2\nexit 1\n")
    commit(repo, {"a.md": "x\n"})

    result = audit(repo, GITLEAKS_BIN=fatal)

    assert result.returncode == 2
    assert "gitleaks failed" in result.stdout and "FOUND" not in result.stdout


@needs_gitleaks
def test_a_malformed_gitleaks_config_is_an_error_not_a_leak_for_the_real_scanner(repo):
    (repo / ".gitleaks.toml").write_text("this is [not valid toml\n")
    commit(repo, {"a.md": "fine\n"})

    result = audit(repo, GITLEAKS_BIN=REAL_GITLEAKS)

    assert result.returncode == 2
    assert "FOUND" not in result.stdout


def test_a_file_name_with_a_tab_is_reported_intact(repo):
    commit(repo, {"a\tb.txt": "acme-private\n"}, "add")

    result = audit(repo)

    assert result.returncode == 1
    assert "a\tb.txt (1 revision" in result.stdout


def test_the_audit_cannot_run_without_gitleaks(repo):
    commit(repo, {"a.md": "x\n"})

    result = audit(repo, GITLEAKS_BIN="/nonexistent/gitleaks")

    assert result.returncode == 2
    assert "gitleaks not found" in result.stdout + result.stderr


def test_an_audit_that_could_not_check_private_names_is_incomplete_not_clean(repo):
    # Exit 0 would read as approval to publish while the private names were never looked at.
    (repo / ".git" / "sensitive-terms.txt").unlink()
    commit(repo, {"a.md": "acme-private\n"})

    result = audit(repo)

    assert result.returncode == 2
    assert "NOT checked" in result.stdout and "incomplete" in result.stdout
    assert "audit-history: clean" not in result.stdout


def test_a_comment_in_the_list_is_not_used_as_a_pattern(repo):
    # The history holds the comment's own text, so a comment used as a pattern would match it.
    (repo / ".git" / "sensitive-terms.txt").write_text("# acme-private\n\n   \n")
    commit(repo, {"note.md": "# acme-private\n"}, "clean")

    result = audit(repo)

    assert "FOUND" not in result.stdout  # the comment was never a pattern
    assert result.returncode == 2 and "no active terms" in result.stdout  # and with nothing active the audit is incomplete


def test_blank_lines_in_the_list_do_not_match_everything(repo):
    # GNU grep treats an empty pattern as matching every line; BSD grep does not, so this bites on Linux.
    (repo / ".git" / "sensitive-terms.txt").write_text("\n   \nacme-private\n\n")
    commit(repo, {"a.md": "nothing to see\n"}, "clean")

    assert audit(repo).returncode == 0


def test_the_maintainers_email_is_never_a_finding(repo):
    env = make_env(repo.env["GITLEAKS_BIN"], author_email="someone.real@gmail.com")
    (repo / "SECURITY.md").write_text("Contact: someone.real@gmail.com\n")
    assert git(repo, "add", "-A", env=env).returncode == 0
    assert git(repo, "commit", "-q", "-m", "docs", env=env).returncode == 0

    assert audit(repo).returncode == 0


def test_history_longer_than_one_batch_is_fully_searched(repo):
    commit(repo, {"old.md": "acme-private\n"}, "first")
    commit(repo, remove=["old.md"], message="second")
    add_empty_commits(repo, 210)

    result = audit(repo)

    assert result.returncode == 1
    assert "212 revisions" in result.stdout  # two real commits plus 210 empty ones, all read
    assert "old.md" in result.stdout  # it sits in the oldest batch


def test_a_history_that_fills_the_batches_exactly_does_not_fall_through_to_the_index(repo):
    # 200 revisions is one full batch and then an EMPTY one; `git grep` with no revisions searches the
    # index instead of history, so a staged term would be reported as if it were in history.
    commit(repo, {"a.md": "fine\n"}, "first")
    add_empty_commits(repo, 199)
    (repo / "staged.md").write_text("acme-private\n")
    assert git(repo, "add", "staged.md", env=repo.env).returncode == 0

    result = audit(repo)

    assert "200 revisions" in result.stdout
    assert result.returncode == 0, result.stdout


def test_an_empty_repository_is_clean_and_the_index_is_not_mistaken_for_history(repo):
    (repo / "staged.md").write_text("acme-private\n")
    assert git(repo, "add", "staged.md", env=repo.env).returncode == 0

    result = audit(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 revisions" in result.stdout


def test_the_audit_never_changes_the_repository(repo):
    commit(repo, {"a.md": "acme-private\n"}, "add")
    (repo / "untracked.txt").write_text("x\n")
    before = (git(repo, "rev-parse", "HEAD", env=repo.env).stdout, git(repo, "status", "--porcelain", env=repo.env).stdout,
              git(repo, "for-each-ref", env=repo.env).stdout)

    audit(repo)

    assert (git(repo, "rev-parse", "HEAD", env=repo.env).stdout, git(repo, "status", "--porcelain", env=repo.env).stdout,
            git(repo, "for-each-ref", env=repo.env).stdout) == before


def test_findings_in_several_revisions_of_one_file_are_summarised_once(repo):
    for i in range(3):  # two matching lines per revision: revisions are counted, not lines
        commit(repo, {"notes.md": f"acme-private {i}\nacme-private again\n"}, f"edit {i}")

    result = audit(repo)

    assert result.stdout.count("notes.md") == 1
    assert "notes.md (3 revisions" in result.stdout  # the summary line itself: the header also says "3 revisions"


@needs_gitleaks
def test_a_token_removed_long_ago_is_found_by_the_real_scanner_and_redacted(repo):
    token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
    commit(repo, {"leak.py": f'TOKEN = "{token}"\n'}, "oops")
    commit(repo, remove=["leak.py"], message="remove")

    result = audit(repo, GITLEAKS_BIN=REAL_GITLEAKS)

    assert result.returncode == 1
    assert token not in result.stdout + result.stderr


@needs_gitleaks
def test_a_clean_history_passes_the_real_scanner(repo):
    commit(repo, {"a.md": "fine\n"})

    assert audit(repo, GITLEAKS_BIN=REAL_GITLEAKS).returncode == 0


def test_a_forbidden_file_that_exists_only_in_a_merge_commit_is_found(repo):
    # `git log --name-only` omits merge commits by default, so a change made only in the merge was invisible.
    commit(repo, {"a.md": "fine\n"}, "first")
    evil_merge(repo, {".env": "K=v\n"})

    result = audit(repo)

    assert result.returncode == 1 and ".env" in result.stdout


def test_a_term_in_a_file_name_that_exists_only_in_a_merge_commit_is_found(repo):
    commit(repo, {"a.md": "fine\n"}, "first")
    evil_merge(repo, {"acme-private-plan.txt": "nothing inside\n"})

    result = audit(repo)

    assert result.returncode == 1 and "acme-private-plan.txt" in result.stdout


def test_a_file_name_with_a_colon_is_reported_intact(repo):
    commit(repo, {"c:d.txt": "acme-private\n"}, "add")

    result = audit(repo)

    assert result.returncode == 1
    assert "c:d.txt (1 revision" in result.stdout  # not truncated to "c"


def test_a_term_in_an_annotated_tag_message_is_found_and_the_message_is_not_echoed(repo):
    commit(repo, {"a.md": "fine\n"}, "first")
    assert git(repo, "tag", "-a", "v1", "-m", "release for Acme-Private", env=repo.env).returncode == 0

    result = audit(repo)

    assert result.returncode == 1
    assert "v1" in result.stdout
    assert "release for" not in result.stdout + result.stderr


def test_a_lightweight_tag_is_not_mistaken_for_a_tag_message(repo):
    commit(repo, {"a.md": "fine\n"}, "first")
    assert git(repo, "tag", "v1", env=repo.env).returncode == 0

    assert audit(repo).returncode == 0


def test_a_lightweight_tag_on_a_flagged_commit_is_reported_once_as_the_commit(repo):
    commit(repo, {"a.md": "fine\n"}, "docs for Acme-Private")
    assert git(repo, "tag", "light", env=repo.env).returncode == 0

    result = audit(repo)

    assert result.returncode == 1
    assert "commit " in result.stdout and "tag light" not in result.stdout


@pytest.mark.parametrize("kind", ["branch", "tag"])
def test_a_term_in_a_branch_or_tag_name_is_found(repo, kind):
    # Ref names are published with a push just like file names.
    commit(repo, {"a.md": "fine\n"}, "first")
    assert git(repo, kind, "Acme-Private-fix", env=repo.env).returncode == 0

    result = audit(repo)

    assert result.returncode == 1 and "Acme-Private-fix" in result.stdout


def test_a_gitleaks_that_errors_is_not_reported_as_a_finding(repo, tmp_path):
    commit(repo, {"a.md": "x\n"})
    broken = write_script(tmp_path / "gitleaks-broken", "echo 'fatal: cannot read repository' >&2\nexit 2\n")

    result = audit(repo, GITLEAKS_BIN=broken)

    assert result.returncode == 2
    assert "gitleaks failed" in result.stdout and "FOUND" not in result.stdout


def test_a_long_history_says_it_may_take_a_while(repo):
    commit(repo, {"a.md": "fine\n"}, "first")
    add_empty_commits(repo, 1000)

    result = audit(repo)

    assert "1001 revisions" in result.stdout
    assert "may take" in result.stdout
    assert result.returncode == 0, result.stdout
