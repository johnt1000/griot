import pytest

from conftest import GitRepo
from griot import index_commits


def test_list_commits_empty_repo(git_repo):
    assert index_commits.list_commits(git_repo.path) == []


def test_list_commits_reads_subject_and_body(git_repo):
    git_repo.commit("Fix bug X", body="Details of the bug and the fix.")
    commits = index_commits.list_commits(git_repo.path)
    assert len(commits) == 1
    assert commits[0]["subject"] == "Fix bug X"
    assert commits[0]["body"] == "Details of the bug and the fix."


def test_list_commits_all_covers_every_branch_without_duplicating(git_repo):
    """git log --all: a commit reachable from two branches appears only once."""
    git_repo.commit("commit on main")
    git_repo.branch("feature-x")
    # without checkout: creates a commit directly on the feature-x branch pointing at the same history
    import subprocess
    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "feature-x"], check=True)
    git_repo.commit("commit on feature-x", filename="feature.txt")

    commits = index_commits.list_commits(git_repo.path)
    assert len(commits) == 2  # does not duplicate the commit shared by both branches
    subjects = {c["subject"] for c in commits}
    assert subjects == {"commit on main", "commit on feature-x"}


def test_build_documents_ids_are_stable_per_commit_hash(git_repo):
    sha = git_repo.commit("single commit")
    docs = index_commits.build_documents(git_repo.path)
    assert docs[0]["id"] == f"{git_repo.path.name}:commit:{sha}"
    assert docs[0]["metadata"]["commit_hash"] == sha


def test_build_documents_chunks_a_huge_commit_body(git_repo):
    """[real bug fix] A commit body over ~8k tokens (release/promote
    commits routinely run this large) gets rejected outright by
    OpenAI-compatible embedding APIs (HTTP 400) — this used to embed the
    WHOLE body as one oversized chunk, failing every single reindex,
    forever (a one-off backfill script fixed the on-disk DATA once; the
    code itself never actually chunked). Mirrors index_code.py's
    chunk_text() usage exactly."""
    huge_body = "x" * 5000  # well over chunk_text()'s default max_chars=1500
    sha = git_repo.commit("huge release notes", body=huge_body)

    docs = index_commits.build_documents(git_repo.path)

    assert len(docs) > 1  # actually chunked, not one oversized document
    assert all(d["metadata"]["commit_hash"] == sha for d in docs)
    assert all(len(d["content"]) <= 1500 for d in docs)
    assert [d["id"] for d in docs] == [f"{git_repo.path.name}:commit:{sha}:{i}" for i in range(len(docs))]
    assert [d["metadata"]["chunk_index"] for d in docs] == list(range(len(docs)))


def test_build_documents_single_chunk_id_format_is_unchanged(git_repo):
    """[real bug guard] A short commit (the overwhelming majority) must
    keep producing the EXACT SAME id it always had, with no chunk suffix
    — unconditionally appending ':0' to every id would silently break
    idempotency for every commit ever indexed before this fix, which is
    precisely the class of bug this whole investigation started from
    (see the --path/--repo id-mismatch fix in jobs.py)."""
    sha = git_repo.commit("short commit", body="small body")
    docs = index_commits.build_documents(git_repo.path)
    assert len(docs) == 1
    assert docs[0]["id"] == f"{git_repo.path.name}:commit:{sha}"
    assert docs[0]["metadata"]["chunk_index"] == 0


# --- --path (mutually exclusive with --repo, skips repos.json) -------------

def test_main_with_path_indexes_without_reading_repos_json(git_repo, monkeypatch):
    git_repo.commit("commit via --path")
    calls = {}

    def fake_index_documents(documents, desc="Indexing"):
        calls["documents"] = documents
        return (len(documents), 0, 0)

    def fail_load_repos():
        raise FileNotFoundError("repos.json should not be read when --path is used")

    monkeypatch.setattr(index_commits.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_commits.common, "load_repos", fail_load_repos)

    index_commits.main(["--path", str(git_repo.path)])

    assert calls["documents"]
    assert calls["documents"][0]["metadata"]["repo"] == git_repo.path.name


def test_path_and_repo_together_is_argparse_error(git_repo):
    with pytest.raises(SystemExit):
        index_commits.main(["--path", str(git_repo.path), "--repo", "some-repo"])


def test_path_disambiguates_ids_for_same_basename_different_location(git_repo, tmp_path, monkeypatch):
    """Natural-id collision guard: two repos with the same final basename ('repo'), indexed via
    --path (outside repos.json curation), must not end up with the same id
    prefix — otherwise upserting one would silently overwrite the other."""
    (tmp_path / "elsewhere").mkdir()
    repo2 = GitRepo(tmp_path / "elsewhere" / "repo")
    git_repo.commit("commit em repo1", filename="a.py")
    repo2.commit("commit em repo2", filename="b.py")

    calls = []

    def fake_index_documents(documents, desc="Indexing"):
        calls.append(documents)
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_commits.common, "index_documents", fake_index_documents)

    index_commits.main(["--path", str(git_repo.path)])
    index_commits.main(["--path", str(repo2.path)])

    prefix1 = calls[0][0]["id"].split(":", 1)[0]
    prefix2 = calls[1][0]["id"].split(":", 1)[0]
    assert prefix1 != "repo" and prefix2 != "repo"  # no longer just the raw basename
    assert prefix1 != prefix2  # different paths -> different keys
    # metadata still has the human-readable name — only the id was disambiguated.
    assert calls[0][0]["metadata"]["repo"] == "repo"
    assert calls[1][0]["metadata"]["repo"] == "repo"


# --- repos.json missing ----------------------------------------

def test_main_without_repos_json_prints_friendly_error(monkeypatch, capsys):
    def fail_load_repos():
        raise FileNotFoundError()

    monkeypatch.setattr(index_commits.common, "load_repos", fail_load_repos)

    index_commits.main([])

    out = capsys.readouterr().out
    assert "not found" in out
