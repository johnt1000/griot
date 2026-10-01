"""The branches source asks git about all branches at once, not twice per branch.

It ran `git log -1` for the last commit of every remote branch and `git log
base..branch` for what each has ahead of the default branch: two processes
per branch, about fourteen seconds for a repository with three hundred
branches, most of them merged long ago and with nothing ahead.

The last commits of all branches are now read in two calls (the names
resolved together, the commits shown together), and one more call says
which of them the default branch does not contain: the list of commits
ahead is asked for only where there are any. What is built from it has to
be EXACTLY what one call per branch built: the text of a document decides
whether it is embedded again."""

import subprocess

import pytest

from griot import common, index_branches


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo.path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _remote_branch(repo, name: str, at: str = "HEAD") -> None:
    _git(repo, "update-ref", f"refs/remotes/origin/{name}", at)


def _commit_on(repo, branch: str, message: str, base: str = "main", *extra_config) -> None:
    """One commit on a new local branch off `base`, published as origin/<branch>."""
    _git(repo, "checkout", "-q", "-b", f"local-{branch}", base)
    (repo.path / f"{branch.replace('/', '-')}.txt").write_text(message)
    _git(repo, "add", ".")
    _git(repo, *extra_config, "commit", "-q", "-m", message)
    _remote_branch(repo, branch)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", f"local-{branch}")  # only the remote one stays


@pytest.fixture
def repo(git_repo):
    """main with three commits; branches that are merged, ahead, and odd."""
    for n in range(3):
        git_repo.commit(f"main commit {n}", filename=f"m{n}.txt")
    for n in range(6):
        _remote_branch(git_repo, f"merged-{n}", f"HEAD~{n % 3}")
    _commit_on(git_repo, "feature/one", "Add the first feature")
    _commit_on(git_repo, "feature/two", "A subject\nthat runs over two lines\n\nand has a body")
    _commit_on(git_repo, "spaces", "  padded subject  ")
    _commit_on(git_repo, "accents", "Corrige a paginação — café")
    _commit_on(git_repo, "control", "Fix the\x1fparser\x1eagain")
    _commit_on(git_repo, "other-author", "By someone else", "main", "-c", "user.name=Zoë Ünïcode")
    git_repo.set_remote_head("main")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "main")
    return git_repo


def _the_old_way(repo_path) -> list[dict]:
    """What build_documents() did before: two git calls per branch."""
    base = index_branches.default_branch(repo_path)
    documents = []
    for branch in index_branches.remote_branches(repo_path):
        if branch == base:
            continue
        commit = index_branches.last_commit(repo_path, branch)
        if commit is None:
            continue
        ahead = index_branches.ahead_commits(repo_path, base, branch) if base else []
        text = f"Branch: {branch}\nLast commit: {commit['subject']} ({commit['author']}, {commit['date']})"
        if ahead:
            text += "\n\nCommits ahead of " + (base or "default") + ":\n" + "\n".join(ahead)
        documents.append({"id": f"{repo_path.name}:branch:{branch}", "content": text,
                          "metadata": {"source_type": "branch", "repo": repo_path.name, "branch_name": branch,
                                       "last_commit_hash": commit["hash"], "last_commit_date": commit["date"]}})
    return documents


@pytest.fixture
def git_calls(monkeypatch):
    calls = []
    real = common.run_git

    def counting(repo_path, args, **kw):
        calls.append(list(args))
        return real(repo_path, args, **kw)

    monkeypatch.setattr(common, "run_git", counting)
    return calls


# --- the same documents -----------------------------------------------------------------------


def test_the_documents_are_exactly_what_two_calls_per_branch_built(repo):
    expected = _the_old_way(repo.path)
    assert len(expected) == 12
    assert index_branches.build_documents(repo.path) == expected


def test_a_commit_in_another_encoding_reads_as_it_did(git_repo):
    """`git log` re-encodes a message written in a legacy encoding;
    `for-each-ref` gives the bytes as they are. Such a branch is asked about
    the old way, so that its text does not change."""
    git_repo.commit("main commit", filename="m.txt")
    _git(git_repo, "checkout", "-q", "-b", "local-legacy", "main")
    (git_repo.path / "l.txt").write_text("x")
    _git(git_repo, "add", ".")
    message = git_repo.path / "message.bin"
    message.write_bytes("Corrige a paginação".encode("latin-1"))
    subprocess.run(["git", "-C", str(git_repo.path), "-c", "i18n.commitEncoding=ISO-8859-1", "commit", "-q",
                    "-F", str(message)], check=True)
    _remote_branch(git_repo, "legacy")
    _git(git_repo, "checkout", "-q", "main")
    _git(git_repo, "branch", "-q", "-D", "local-legacy")
    git_repo.set_remote_head("main")
    expected = _the_old_way(git_repo.path)
    assert "paginação" in expected[0]["content"]
    assert index_branches.build_documents(git_repo.path) == expected


def test_without_a_default_branch_nothing_is_listed_as_ahead(git_repo, git_calls):
    git_repo.commit("only commit")
    _remote_branch(git_repo, "feature")
    _remote_branch(git_repo, "other")
    expected = _the_old_way(git_repo.path)
    assert [d["metadata"]["branch_name"] for d in expected] == ["origin/feature", "origin/other"]
    git_calls.clear()
    assert index_branches.build_documents(git_repo.path) == expected
    assert _lists_asked(git_calls) == [] and not [call for call in git_calls if call[0] == "rev-list"], \
        "ahead of nothing: no list is asked for"


def test_a_tag_named_like_the_default_branch_changes_nothing(repo):
    """`origin/main` then names the tag, to `git log` and to the count
    alike. Counting against one thing and listing against another would
    skip a list the old way had."""
    _git(repo, "tag", "origin/main", "HEAD~2")
    expected = _the_old_way(repo.path)
    assert any("Commits ahead" in d["content"] for d in expected if "merged" in d["id"]), "the tag is behind main"
    assert index_branches.build_documents(repo.path) == expected


def test_the_pointer_to_the_default_branch_is_not_a_branch(repo):
    names = [d["metadata"]["branch_name"] for d in index_branches.build_documents(repo.path)]
    assert "origin/main" not in names and not [n for n in names if "HEAD" in n or n == "origin"]


def test_the_branches_come_in_the_order_they_did(repo):
    assert [d["id"] for d in index_branches.build_documents(repo.path)] == [d["id"] for d in _the_old_way(repo.path)]


# --- with far fewer processes -----------------------------------------------------------------


def _lists_asked(calls) -> list[list[str]]:
    """The `git log --oneline base..branch` calls: one list of commits ahead."""
    return [call for call in calls if call[0] == "log" and "--oneline" in call]


def _one_at_a_time(calls) -> list[list[str]]:
    """The `git log -1 <branch>` calls of the old way."""
    return [call for call in calls if call[0] == "log" and "-1" in call]


def test_branches_with_nothing_ahead_cost_no_process_of_their_own(repo, git_calls):
    index_branches.build_documents(repo.path)
    assert len(_lists_asked(git_calls)) == 6, "one per branch that IS ahead; the six merged ones cost nothing"
    assert _one_at_a_time(git_calls) == []
    # The default branch, the list of branches, their commits (two calls),
    # which of them are ahead, and the six lists.
    assert len(git_calls) == 5 + 6, [call[0] for call in git_calls]


def test_the_number_of_processes_does_not_grow_with_merged_branches(git_repo, git_calls):
    git_repo.commit("main commit", filename="m.txt")
    for n in range(40):
        _remote_branch(git_repo, f"merged-{n}")
    git_repo.set_remote_head("main")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "main")
    git_calls.clear()
    assert len(index_branches.build_documents(git_repo.path)) == 40
    assert len(git_calls) == 5, [call[0] for call in git_calls]


def test_more_branches_than_fit_in_one_command_are_asked_in_a_few(git_repo, git_calls, monkeypatch):
    monkeypatch.setattr(index_branches, "_PER_CALL", 4)
    git_repo.commit("main commit", filename="m.txt")
    for n in range(9):
        _commit_on(git_repo, f"feature-{n}", f"Feature {n}")
    git_repo.set_remote_head("main")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "main")
    expected = _the_old_way(git_repo.path)
    git_calls.clear()
    assert index_branches.build_documents(git_repo.path) == expected and len(expected) == 9
    assert len([call for call in git_calls if call[0] == "rev-parse"]) == 3
    assert _one_at_a_time(git_calls) == []


# --- a git that cannot answer that way --------------------------------------------------------


def _refusing(monkeypatch, refuses):
    real = common.run_git
    calls = []

    def run(repo_path, args, **kw):
        calls.append(list(args))
        if refuses(args):
            raise subprocess.CalledProcessError(129, ["git", *args], stderr="fatal: not this way")
        return real(repo_path, args, **kw)

    monkeypatch.setattr(common, "run_git", run)
    return calls


def test_when_git_cannot_say_which_are_ahead_every_branch_is_asked(repo, monkeypatch):
    expected = _the_old_way(repo.path)
    calls = _refusing(monkeypatch, lambda args: args[0] == "rev-list")
    assert index_branches.build_documents(repo.path) == expected
    assert len(_lists_asked(calls)) == 12 and _one_at_a_time(calls) == []


def test_when_git_cannot_resolve_them_together_each_branch_is_asked(repo, monkeypatch):
    expected = _the_old_way(repo.path)
    calls = _refusing(monkeypatch, lambda args: args[0] == "rev-parse")
    assert index_branches.build_documents(repo.path) == expected
    assert len(_one_at_a_time(calls)) == 12


def test_an_answer_with_a_line_missing_is_not_paired_with_the_wrong_branches(repo, monkeypatch):
    """One hash per name, in order, is what pairs a branch with its commit.
    An answer of another length would pair every branch after the gap with
    its neighbour's commit, and say nothing."""
    expected = _the_old_way(repo.path)
    real = common.run_git

    def one_line_short(repo_path, args, **kw):
        done = real(repo_path, args, **kw)
        if args[0] == "rev-parse":
            return type("Done", (), {"stdout": "\n".join(done.stdout.splitlines()[1:]) + "\n", "returncode": 0})()
        return done

    monkeypatch.setattr(common, "run_git", one_line_short)
    assert index_branches.build_documents(repo.path) == expected


def test_when_git_cannot_show_them_together_each_branch_is_asked(repo, monkeypatch):
    expected = _the_old_way(repo.path)
    _refusing(monkeypatch, lambda args: args[0] == "log" and any(a.startswith("--no-walk") for a in args))
    assert index_branches.build_documents(repo.path) == expected


def test_a_remote_whose_name_begins_with_a_dash_is_never_given_to_a_command_as_an_option(git_repo, git_calls):
    git_repo.commit("main commit", filename="m.txt")
    _git(git_repo, "update-ref", "refs/remotes/-x/y", "HEAD")
    _remote_branch(git_repo, "feature")
    expected = _the_old_way(git_repo.path)
    git_calls.clear()
    assert index_branches.build_documents(git_repo.path) == expected and len(expected) == 2
    assert not [call for call in git_calls if call[0] == "rev-parse"]
    for call in git_calls:
        dashed = [i for i, arg in enumerate(call) if arg.startswith("-x/")]
        assert all(call.index("--end-of-options") < i for i in dashed), call


def test_a_default_branch_whose_name_git_cannot_put_in_the_format_is_not_a_reason_to_fail(git_repo):
    """A ref name may hold a parenthesis, which ends the format field early."""
    git_repo.commit("main commit", filename="m.txt")
    _git(git_repo, "update-ref", "refs/remotes/origin/rel(1)", "HEAD")
    _git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/rel(1)")
    _commit_on(git_repo, "feature", "Add a feature")
    expected = _the_old_way(git_repo.path)
    assert [d["metadata"]["branch_name"] for d in expected] == ["origin/feature"]
    assert index_branches.build_documents(git_repo.path) == expected


# --- what an independent review found: repositories where a different way of asking gave a different answer ----
# The first version read everything with `git for-each-ref`, which names a
# ref by its full name and prints a message as it is stored. `git log` (what
# the documents were always built from) resolves a SHORT name, which a tag or
# a local branch can shadow, peels a tag to its commit, re-encodes a message,
# and trims a subject. Each case below differed.


def _raw_commit(repo, message: bytes, author: str = "A U Thor <a@example.com> 1700000000 +0000",
                encoding: str | None = None) -> str:
    """A commit object written by hand, on top of HEAD."""
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    parent = _git(repo, "rev-parse", "HEAD")
    header = f"tree {tree}\nparent {parent}\nauthor {author}\ncommitter {author}\n"
    if encoding:
        header += f"encoding {encoding}\n"
    done = subprocess.run(["git", "-C", str(repo.path), "hash-object", "-t", "commit", "-w", "--stdin", "--literally"],
                          input=header.encode() + b"\n" + message, check=True, capture_output=True)
    return done.stdout.decode().strip()


@pytest.fixture
def odd(git_repo):
    for n in range(3):
        git_repo.commit(f"main commit {n}", filename=f"m{n}.txt")
    git_repo.set_remote_head("main")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "main")
    return git_repo


def _same_as_before(repo) -> list[dict]:
    expected = _the_old_way(repo.path)
    assert index_branches.build_documents(repo.path) == expected
    return expected


def test_a_tag_object_where_a_branch_should_be_reads_as_its_commit(odd, git_calls):
    _git(odd, "tag", "-a", "v1", "-m", "tag message", "HEAD~1")
    _git(odd, "update-ref", "refs/remotes/origin/tagged", _git(odd, "rev-parse", "v1"))
    expected = _the_old_way(odd.path)
    git_calls.clear()
    assert index_branches.build_documents(odd.path) == expected
    (doc,) = [d for d in expected if d["metadata"]["branch_name"] == "origin/tagged"]
    assert "main commit 1" in doc["content"] and doc["metadata"]["last_commit_date"]
    assert _one_at_a_time(git_calls) == [], "peeled to its commit when resolved: no need to ask one at a time"


def test_a_file_where_a_branch_should_be_is_not_a_branch(odd):
    _git(odd, "update-ref", "refs/remotes/origin/blob", _git(odd, "rev-parse", "HEAD:m0.txt"))
    _remote_branch(odd, "real")
    assert [d["metadata"]["branch_name"] for d in _same_as_before(odd)] == ["origin/real"]


def test_a_branch_whose_name_holds_an_arrow_is_left_out_as_it_was(odd):
    _git(odd, "update-ref", "refs/remotes/origin/a->b", "HEAD")
    _remote_branch(odd, "real")
    assert [d["metadata"]["branch_name"] for d in _same_as_before(odd)] == ["origin/real"]


def test_a_tag_with_the_name_of_a_branch_is_what_the_name_means(odd):
    """`origin/amb` names the tag, to `git log`: the document was built
    from the tag's commit, and it still is."""
    _commit_on(odd, "amb", "On the branch")
    _git(odd, "tag", "origin/amb", "HEAD~2")
    (doc,) = _same_as_before(odd)
    assert "main commit 0" in doc["content"] and "On the branch" not in doc["content"]


def test_a_local_branch_with_the_name_of_a_remote_one_is_what_the_name_means(odd):
    _commit_on(odd, "loc", "On the remote branch")
    _git(odd, "branch", "origin/loc", "HEAD~1")
    (doc,) = _same_as_before(odd)
    assert "main commit 1" in doc["content"]


def test_a_subject_keeps_the_shape_git_log_gives_it(odd):
    _git(odd, "checkout", "-q", "-b", "local-w", "main")
    (odd.path / "w.txt").write_text("x")
    _git(odd, "add", ".")
    _git(odd, "commit", "-q", "--cleanup=verbatim", "-m", "  lead and trail  \n")
    _remote_branch(odd, "white")
    _git(odd, "checkout", "-q", "main")
    _git(odd, "branch", "-q", "-D", "local-w")
    (doc,) = _same_as_before(odd)
    assert "Last commit:   lead and trail (" in doc["content"]


def test_a_legacy_message_whose_bytes_happen_to_be_utf8_reads_as_it_did(odd):
    _remote_branch(odd, "accident", _raw_commit(odd, "café accident\n".encode("utf-8"), encoding="ISO-8859-1"))
    (doc,) = _same_as_before(odd)
    assert "accident" in doc["content"]


@pytest.mark.parametrize("author", ["A<b> <a@example.com> 1700000000 +0000",
                                    "A B   <a@example.com> 1700000000 +0000",
                                    "A <a@example.com> -100 +0000",
                                    " <a@example.com> 1700000000 +0000"])
def test_an_author_line_written_by_hand_reads_as_it_did(odd, author):
    _remote_branch(odd, "odd-author", _raw_commit(odd, b"by hand\n", author=author))
    _remote_branch(odd, "ordinary")
    assert len(_same_as_before(odd)) == 2


def test_the_order_follows_the_repository_s_own_setting(odd):
    for name in ("b", "a", "c"):
        _remote_branch(odd, name)
    _git(odd, "config", "branch.sort", "-refname")
    names = [d["metadata"]["branch_name"] for d in _same_as_before(odd)]
    assert names == [d["metadata"]["branch_name"] for d in _the_old_way(odd.path)] and len(names) == 3


def test_a_default_branch_named_to_break_a_format_string_loses_no_branch(git_repo):
    """`origin/main)%00%(contents` is a name git accepts. Put inside a
    format string it closed the field early and shifted every record: the
    call succeeded and every branch was dropped, which reads as "this
    repository has no branches" and feeds the removal of stale points."""
    git_repo.commit("main commit", filename="m.txt")
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    hostile = "refs/remotes/origin/main)%00%(contents"
    _git(git_repo, "update-ref", hostile, "HEAD")
    _git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", hostile)
    _commit_on(git_repo, "feature", "Add a feature")
    expected = _same_as_before(git_repo)
    assert {"origin/feature", "origin/main"} <= {d["metadata"]["branch_name"] for d in expected}


def test_a_commit_git_did_not_show_sends_the_whole_repository_back_to_one_at_a_time(repo, monkeypatch):
    """Every resolved commit has to come back from the call that shows them.
    One that does not would leave its branch without a text."""
    expected = _the_old_way(repo.path)
    real = common.run_git

    def one_commit_short(repo_path, args, **kw):
        done = real(repo_path, args, **kw)
        if args[0] == "log" and any(a.startswith("--no-walk") for a in args):
            records = done.stdout.split("\n")
            return type("Done", (), {"stdout": "\n".join(records[1:]), "returncode": 0})()
        return done

    monkeypatch.setattr(common, "run_git", one_commit_short)
    assert index_branches.build_documents(repo.path) == expected


# --- a branch that was missing --------------------------------------------------------------------


@pytest.mark.parametrize("batch_available", [True, False])
def test_a_branch_named_like_a_path_in_the_work_tree_is_listed(odd, monkeypatch, batch_available):
    """`git log -1 origin/feat` stops with "ambiguous argument: both revision
    and filename" when the work tree has a path `origin/feat`, and the
    branch was skipped. Asked as a revision and nothing else, it is a
    branch like any other, whichever way it is read."""
    _commit_on(odd, "feat", "Add a feature")
    (odd.path / "origin").mkdir()
    (odd.path / "origin" / "feat").write_text("a file that happens to be named like the branch")
    if not batch_available:
        monkeypatch.setattr(index_branches, "branch_tips", lambda repo_path, branches: None)
    (doc,) = index_branches.build_documents(odd.path)
    assert doc["metadata"]["branch_name"] == "origin/feat" and "Add a feature" in doc["content"]
    assert "Commits ahead of origin/main" in doc["content"]
