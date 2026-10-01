"""A control character in what git stores does not stop a source.

The git log, the tags and the branches were read with the "unit separator"
(0x1f) between fields and the "record separator" (0x1e) between records,
and git lets both into a commit message, a tag message and an author name.
One such commit made the split come out with the wrong number of fields:
a ValueError that ended the commits source, and with it `griot index all`,
on every run. NUL is the one character git refuses in those places."""

import subprocess

import pytest

from griot import common, index_branches, index_commits, index_tags, retrieval_eval

UNIT, RECORD = "\x1f", "\x1e"


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo.path), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def odd(git_repo):
    """A history with both separators in a subject, in a body and in an
    author's name, between two ordinary commits."""
    git_repo.commit("first ordinary commit", filename="a.py")
    (git_repo.path / "b.py").write_text("x = 1")
    _git(git_repo, "add", "b.py")
    _git(git_repo, "-c", f"user.name=An{UNIT}Author", "commit", "-q", "-m",
         f"Fix the{UNIT}parser{RECORD}again\n\nA body with {UNIT} and {RECORD} in it.")
    git_repo.commit("last ordinary commit", filename="c.py")
    return git_repo


# --- commits ----------------------------------------------------------------------------------


def test_the_commits_source_reads_every_commit(odd):
    commits = index_commits.list_commits(odd.path)
    assert [c["subject"] for c in commits] == ["last ordinary commit", f"Fix the{UNIT}parser{RECORD}again", "first ordinary commit"]
    middle = commits[1]
    assert middle["body"] == f"A body with {UNIT} and {RECORD} in it." and middle["author"] == f"An{UNIT}Author"
    assert all(len(c["hash"]) == 40 and c["date"] for c in commits)


def test_the_documents_of_the_commits_source_are_built(odd):
    docs = index_commits.build_documents(odd.path)
    assert len(docs) == 3 and all(d["metadata"]["commit_hash"] for d in docs)


def test_a_body_that_ends_with_blank_lines_is_read_as_before(git_repo):
    git_repo.commit("subject", body="line one\n\nline two\n\n")
    (commit,) = index_commits.list_commits(git_repo.path)
    assert (commit["subject"], commit["body"]) == ("subject", "line one\n\nline two")


# --- tags -------------------------------------------------------------------------------------


def test_the_tags_source_reads_every_tag(odd):
    odd.tag("v1", message=f"Release{UNIT}one{RECORD}\n\nNotes with {UNIT} and {RECORD}.")
    odd.tag("v2", message="Release two")
    tags = {t["name"]: t for t in index_tags.list_tags(odd.path)}
    assert set(tags) == {"v1", "v2"}
    assert tags["v1"]["subject"] == f"Release{UNIT}one{RECORD}" and tags["v1"]["contents"] == f"Notes with {UNIT} and {RECORD}."
    assert tags["v2"]["subject"] == "Release two"
    assert len(index_tags.build_documents(odd.path)) == 2


def test_a_lightweight_tag_on_such_a_commit_is_read(odd):
    _git(odd, "tag", "light", "HEAD~1")
    (tag,) = [t for t in index_tags.list_tags(odd.path) if t["name"] == "light"]
    assert tag["subject"] == f"Fix the{UNIT}parser{RECORD}again"


# --- branches ---------------------------------------------------------------------------------


def test_the_last_commit_of_a_branch_is_read(odd):
    _git(odd, "branch", "feature", "HEAD~1")
    commit = index_branches.last_commit(odd.path, "feature")
    assert commit["subject"] == f"Fix the{UNIT}parser{RECORD}again" and commit["author"] == f"An{UNIT}Author"
    assert len(commit["hash"]) == 40


# --- the commits with their files (golden-set suggest, the retrieval ruler) --------------------


def test_the_commits_with_their_files_are_read(odd):
    commits = retrieval_eval._commits_with_files(odd.path)
    assert [c["files"] for c in commits] == [["c.py"], ["b.py"], ["a.py"]]
    assert commits[1]["subject"] == f"Fix the{UNIT}parser{RECORD}again"
    assert commits[1]["body"] == f"A body with {UNIT} and {RECORD} in it."


def test_a_commit_that_touched_several_files_keeps_them_all(git_repo):
    for name in ("one.py", "two.py", "three.py"):
        (git_repo.path / name).write_text("x = 1")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "Add three files\n\nwith a body")
    (commit,) = retrieval_eval._commits_with_files(git_repo.path)
    assert sorted(commit["files"]) == ["one.py", "three.py", "two.py"] and commit["body"] == "with a body"


def test_a_commit_without_files_between_others_does_not_shift_the_rest(git_repo):
    git_repo.commit("with a file", filename="a.py")
    _git(git_repo, "commit", "-q", "--allow-empty", "-m", "an empty one")
    git_repo.commit("with another", filename="b.py")
    commits = retrieval_eval._commits_with_files(git_repo.path)
    assert [(c["subject"], c["files"]) for c in commits] == [
        ("with another", ["b.py"]), ("an empty one", []), ("with a file", ["a.py"])]


# --- output that is not what was asked for ----------------------------------------------------


def test_a_record_cut_short_is_left_out_and_the_rest_is_kept():
    """Whatever git prints, a source does not end in a traceback."""
    whole = "\x00".join(["a" * 40, "author", "date", "subject", "body"]) + "\x00"
    assert common.git_records(whole + "\n" + "b" * 40 + "\x00only two\x00", 5) == [["a" * 40, "author", "date", "subject", "body"]]
    assert common.git_records("", 5) == []


def test_the_separator_between_records_is_not_part_of_a_field():
    one = "\x00".join(["a" * 40, "s1"]) + "\x00"
    two = "\x00".join(["b" * 40, "s2"]) + "\x00"
    assert common.git_records(one + "\n" + two, 2) == [["a" * 40, "s1"], ["b" * 40, "s2"]]


def test_whole_records_are_read_without_a_warning(monkeypatch):
    said = []
    monkeypatch.setattr(common, "log_and_print", lambda message, **kw: said.append(message))
    one = "\x00".join(["a" * 40, "s1"]) + "\x00"
    assert len(common.git_records(one + "\n" + one, 2)) == 2 and said == []
    common.git_records(one + "\n" + "b" * 40 + "\x00", 2)
    assert len(said) == 1 and "left out" in said[0]


@pytest.mark.parametrize("value,expected", [("a" * 40, True), ("0123456789abcdef" * 4, True), ("a" * 39, False),
                                            ("a" * 41, False), ("A" * 40, False), ("not a hash", False), ("", False)])
def test_what_counts_as_the_name_of_an_object(value, expected):
    assert common.is_git_hash(value) is expected


def test_fields_that_do_not_line_up_are_never_returned_as_a_commit(git_repo, monkeypatch):
    """A NUL where git allows none (an object written by hand) shifts every
    field after it. What is read from then on begins with no hash and is
    left out, instead of turning text into "commits"."""
    def record(*fields):
        return "".join(field + "\x00" for field in fields)

    crafted = (record("a" * 40, "Ann", "2026-01-01T00:00:00+00:00", "good", "body") + "\n"
               + record("b" * 40, "Bob", "2026-01-02T00:00:00+00:00", "broken", "first half", "second half") + "\n"
               + record("c" * 40, "Cid", "2026-01-03T00:00:00+00:00", "after", "body"))
    monkeypatch.setattr(common, "run_git", lambda repo_path, args, **kw: type("Done", (), {"stdout": crafted})())
    commits = index_commits.list_commits(git_repo.path)
    assert [c["hash"] for c in commits] == ["a" * 40, "b" * 40]
    assert all(common.is_git_hash(c["hash"]) for c in commits)
