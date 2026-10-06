"""A repository being indexed is untrusted input, and so is its git config.

`git log` obeys the repository's own config, and several keys name a program
to run: with `log.showSignature` set, git runs `gpg.program` for every signed
commit. A repository received as an archive, or an embedded bare repository in
tracked files, can therefore run code as the user on `griot index`. Every git
call in the package goes through common.run_git(), which overrides those keys
on the command line and gives git an environment without credentials."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from griot import common, index_branches, index_commits, index_platform, index_tags, jobs, retrieval_eval

SRC = Path(common.__file__).parent


def _write_stand_in_signer(program, marker):
    """A program that signs as gpg does (`--status-fd=2 -bsau <key>`) and,
    asked to verify, leaves a marker instead: standing in for anything.

    Signing reads all of stdin first, as gpg does: git writes the object to
    sign there, and a program that exits without reading it makes git's
    write fail with EPIPE whenever the program finishes first, which turned
    `git tag -s` into exit 128 on a loaded CI runner."""
    program.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  *--verify*) echo ran > "{marker}"; exit 1 ;;\n'
        '  *) cat > /dev/null; echo "[GNUPG:] SIG_CREATED " >&2; '
        "printf -- '-----BEGIN PGP SIGNATURE-----\\n\\nZmFrZQ==\\n-----END PGP SIGNATURE-----\\n' ;;\n"
        "esac\n")
    program.chmod(0o755)


@pytest.fixture
def hostile_repo(tmp_path):
    """A repository whose own config tells git to run a program when it shows
    a signed commit. The program leaves a marker, standing in for anything."""
    marker = tmp_path / "marker"
    program = tmp_path / "program.sh"
    _write_stand_in_signer(program, marker)
    repo = tmp_path / "repo"

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "gpg.program", str(program))
    (repo / "a.py").write_text("x = 1\n")
    git("add", "a.py")
    git("commit", "-q", "-S", "-m", "signed commit")
    git("tag", "-s", "v1", "-m", "signed tag")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("remote", "add", "origin", "git@github.com:group/project.git")
    git("config", "log.showSignature", "true")
    git("config", "core.fsmonitor", str(program) + " --verify")
    marker.unlink(missing_ok=True)
    return repo, marker


def test_the_stand_in_signer_takes_everything_git_hands_it(tmp_path):
    """git writes the object to sign on the program's stdin. A program that
    exits without reading it races git's write: when the program wins, git
    gets EPIPE, says "gpg failed to sign the data" and `git tag -s` exits 128,
    which failed the fixture above once on a loaded CI runner. A message
    larger than a pipe buffer makes the program win every time, so this holds
    the fixture's signer to reading its input, as gpg does."""
    program = tmp_path / "program.sh"
    _write_stand_in_signer(program, tmp_path / "marker")
    repo = tmp_path / "repo"

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "gpg.program", str(program))
    git("commit", "-q", "--allow-empty", "-m", "base")
    message = tmp_path / "message"
    message.write_text("m" * (1 << 20) + "\n")
    signed = git("tag", "-s", "big", "-F", str(message))
    assert signed.returncode == 0, signed.stderr
    assert "-----BEGIN PGP SIGNATURE-----" in git("cat-file", "tag", "big").stdout


READERS = {
    "commits": lambda repo: index_commits.list_commits(repo),
    "retrieval_eval": lambda repo: retrieval_eval._commits_with_files(repo),
    "tags": lambda repo: index_tags.list_tags(repo),
    "branches": lambda repo: index_branches.build_documents(repo),
    "platform": lambda repo: index_platform._remote_url(repo),
    "job_refusal": lambda repo: jobs.index_job_refusal(str(repo)),
}


@pytest.mark.parametrize("reader", list(READERS))
def test_reading_a_repository_never_runs_a_program_its_config_names(hostile_repo, reader):
    repo, marker = hostile_repo
    READERS[reader](repo)
    assert not marker.exists(), f"{reader} ran a program chosen by the repository's config"


def test_the_readers_still_read(hostile_repo):
    repo, _ = hostile_repo
    assert [c["subject"] for c in index_commits.list_commits(repo)] == ["signed commit"]
    assert [t["name"] for t in index_tags.list_tags(repo)] == ["v1"]
    assert index_platform._remote_url(repo) == "git@github.com:group/project.git"


def test_a_signature_placeholder_cannot_run_the_repositorys_program(hostile_repo):
    """`%G?` verifies signatures whatever log.showSignature says, so the
    program itself is overridden, not only the switch that usually calls it."""
    repo, marker = hostile_repo
    common.run_git(repo, ["log", "--pretty=format:%G?"], timeout=30)
    assert not marker.exists()


@pytest.fixture(params=["ext-transport", "ssh-command"])
def partial_repo(request, tmp_path):
    """A repository that claims to be a partial clone and is missing an
    object. Reading history makes git fetch the object on demand, from a
    "remote" and over a transport the repository itself chooses: an `ext::`
    URL is a command line, and core.sshCommand is the program run for ssh."""
    marker = tmp_path / "marker"
    program = tmp_path / "fetch.sh"
    program.write_text(f'#!/bin/sh\necho ran > "{marker}"\nexit 1\n')
    program.chmod(0o755)
    repo = tmp_path / "repo"

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "d").mkdir()
    (repo / "d" / "a.py").write_text("x = 1\n")
    git("add", "d/a.py")
    git("commit", "-q", "-m", "one")
    (repo / "d" / "a.py").write_text("x = 2\n")
    git("commit", "-q", "-am", "two")
    git("tag", "-a", "v1", "-m", "tag")
    tree = git("rev-parse", "HEAD:d")
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    git("config", "extensions.partialClone", "origin")
    git("config", "remote.origin.promisor", "true")
    git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    if request.param == "ext-transport":
        git("config", "remote.origin.url", f"ext::{program}")
        git("config", "protocol.ext.allow", "always")
    else:
        git("config", "remote.origin.url", "ssh://host.invalid/x")
        git("config", "core.sshCommand", str(program))
    return repo, marker


PARTIAL_READERS = {
    "commits": lambda repo: index_commits.list_commits(repo),
    "retrieval_eval": lambda repo: retrieval_eval._commits_with_files(repo),
    "tags": lambda repo: index_tags.list_tags(repo),
    "name_only": lambda repo: common.run_git(repo, ["log", "--all", "--name-only", "--pretty=format:%H"],
                                             timeout=30, check=False),
}


@pytest.mark.parametrize("reader", list(PARTIAL_READERS))
def test_reading_never_fetches_a_missing_object_through_the_repositorys_transport(partial_repo, reader):
    repo, marker = partial_repo
    PARTIAL_READERS[reader](repo)
    assert not marker.exists(), f"{reader} let the repository choose and run a fetch program"


def test_no_module_runs_a_command_through_a_shell():
    """Same rule from the other side: a shell string would hide a git call
    from the check below, and nothing in the package needs one."""
    offenders = [path.name for path in sorted(SRC.glob("*.py"))
                 if re.search(r"shell\s*=\s*True|os\.system\(|os\.popen\(", path.read_text())]
    assert offenders == []


def test_every_git_call_in_the_package_goes_through_the_one_helper():
    """The rule, not the list of call sites known today: no module builds a
    git command line of its own."""
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text()
        for match in re.finditer(r"""[\[(]\s*["']git["']""", text):
            line = text.count("\n", 0, match.start()) + 1
            if path.name == "common.py" and "def run_git" in text and _inside_run_git(text, match.start()):
                continue
            offenders.append(f"{path.name}:{line}")
    assert offenders == []


def _inside_run_git(text: str, pos: int) -> bool:
    start = text.index("def run_git")
    end = text.index("\ndef ", start + 1)
    return start <= pos < end


def test_git_runs_without_the_users_credentials(monkeypatch, git_repo):
    """Defence in depth: if a program did run, it would find no key to take."""
    git_repo.commit("one", filename="a.py")
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test-not-real")
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    common.run_git(git_repo.path, ["rev-parse", "HEAD"], timeout=30)
    assert seen["env"] is not None, "git inherited the whole environment"
    # The rule is an allowlist, so it is checked as one: nothing reaches git
    # that was not named, whatever a credential happens to be called.
    fixed = {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_NO_LAZY_FETCH", "GIT_ALLOW_PROTOCOL"}
    assert set(seen["env"]) <= set(common._GIT_ENV_ALLOWED) | fixed
    assert "PATH" in seen["env"]


def test_a_bare_repository_is_not_accepted_as_a_repository(tmp_path, monkeypatch):
    """A bare repository can sit in a project's TRACKED files (a fixture
    directory, say) and carries its own config. `rev-parse` answers "false"
    with exit 0 inside one, so the exit code alone accepted it. Git cannot
    tell an embedded one from one kept on purpose, so every bare repository
    is refused; there were no files in it for the `code` source anyway."""
    root = tmp_path / "allowed"
    bare = root / "project" / "docs" / "fixture"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(root))
    refusal = jobs.index_job_refusal(str(bare))
    assert refusal is not None and "git repository" in refusal


def test_a_real_work_tree_is_still_accepted(git_repo, monkeypatch):
    git_repo.commit("one", filename="a.py")
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(git_repo.path.parent))
    assert jobs.index_job_refusal(str(git_repo.path)) is None


def test_commit_text_that_is_not_utf8_does_not_take_the_source_down(git_repo, tmp_path):
    """One commit in a legacy encoding used to raise UnicodeDecodeError and
    stop the commits source for every repository in the run. The object is
    written raw: `git commit` would convert the message on the way in."""
    git_repo.commit("plain", filename="a.py")

    def git(*args):
        return subprocess.run(["git", "-C", str(git_repo.path), *args], check=True,
                              capture_output=True).stdout.decode().strip()

    raw = tmp_path / "commit"
    raw.write_bytes(
        f"tree {git('rev-parse', 'HEAD^{tree}')}\nparent {git('rev-parse', 'HEAD')}\n".encode()
        + b"author T <t@example.com> 1700000000 +0000\ncommitter T <t@example.com> 1700000000 +0000\n\n"
        + b"caf\xe9 in latin-1\n")
    git("update-ref", "HEAD", git("hash-object", "-t", "commit", "-w", str(raw)))

    subjects = [c["subject"] for c in index_commits.list_commits(git_repo.path)]
    assert len(subjects) == 2 and "plain" in subjects
