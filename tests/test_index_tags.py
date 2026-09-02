import pytest

from conftest import GitRepo
from griot import index_tags


def test_list_tags_empty_repo_has_no_tags(git_repo):
    assert index_tags.list_tags(git_repo.path) == []


def test_list_tags_reads_annotated_tag_message(git_repo):
    git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0\n\nCorpo da mensagem da tag.")

    tags = index_tags.list_tags(git_repo.path)
    assert len(tags) == 1
    assert tags[0]["name"] == "v1.0.0"
    assert tags[0]["subject"] == "Release 1.0.0"
    assert "Corpo da mensagem" in tags[0]["contents"]


def test_list_tags_lightweight_tag_has_empty_subject(git_repo):
    git_repo.commit("first commit")
    git_repo.tag("v0.0.1")  # lightweight tag, no message

    tags = index_tags.list_tags(git_repo.path)
    assert len(tags) == 1
    assert tags[0]["name"] == "v0.0.1"


def test_lightweight_tag_inherits_subject_from_underlying_commit(git_repo):
    """git for-each-ref dereferences a lightweight tag to the commit it
    points at — in practice 'subject' never comes back empty from a normal
    commit (git requires a message), so the fallback to the tag name (below)
    is a safety net, not the common path."""
    git_repo.commit("first commit")
    git_repo.tag("v0.0.1")

    docs = index_tags.build_documents(git_repo.path)
    assert len(docs) == 1
    assert docs[0]["content"] == "first commit"
    assert docs[0]["id"] == f"{git_repo.path.name}:tag:v0.0.1"
    assert docs[0]["metadata"]["source_type"] == "tag"


def test_build_documents_falls_back_to_tag_name_when_text_is_empty(monkeypatch, git_repo):
    """Safety net for build_documents: if subject/contents come back empty
    for some reason, never index an empty chunk — use the tag name instead."""
    monkeypatch.setattr(index_tags, "list_tags", lambda repo_path: [
        {"name": "v0.0.1", "commit_hash": "abc", "date": "2026-01-01", "subject": "", "contents": ""}
    ])
    docs = index_tags.build_documents(git_repo.path)
    assert len(docs) == 1
    assert docs[0]["content"] == "v0.0.1"


def test_build_documents_multiple_tags(git_repo):
    git_repo.commit("c1")
    git_repo.tag("v1")
    git_repo.commit("c2", content="different content")
    git_repo.tag("v2", message="Second version")

    docs = index_tags.build_documents(git_repo.path)
    names = {d["metadata"]["tag_name"] for d in docs}
    assert names == {"v1", "v2"}


# --- --path (mutually exclusive with --repo, skips repos.json) -------------

def test_main_with_path_indexes_without_reading_repos_json(git_repo, monkeypatch):
    git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0")
    calls = {}

    def fake_index_documents(documents, desc="Indexing"):
        calls["documents"] = documents
        return (len(documents), 0, 0)

    def fail_load_repos():
        raise FileNotFoundError("repos.json should not be read when --path is used")

    monkeypatch.setattr(index_tags.common, "index_documents", fake_index_documents)
    monkeypatch.setattr(index_tags.common, "load_repos", fail_load_repos)

    index_tags.main(["--path", str(git_repo.path)])

    assert calls["documents"]
    assert calls["documents"][0]["metadata"]["repo"] == git_repo.path.name


def test_path_and_repo_together_is_argparse_error(git_repo):
    with pytest.raises(SystemExit):
        index_tags.main(["--path", str(git_repo.path), "--repo", "some-repo"])


def test_path_disambiguates_ids_for_same_basename_different_location(git_repo, tmp_path, monkeypatch):
    """Natural-id collision guard: the same tag ('v1.0.0') in two repos with the same final
    basename ('repo'), indexed via --path -> without the disambiguated key
    the ids would literally collide (same basename + same tag name)."""
    (tmp_path / "elsewhere").mkdir()
    repo2 = GitRepo(tmp_path / "elsewhere" / "repo")
    git_repo.commit("c1")
    git_repo.tag("v1.0.0")
    repo2.commit("c1")
    repo2.tag("v1.0.0")

    calls = []

    def fake_index_documents(documents, desc="Indexing"):
        calls.append(documents)
        return (len(documents), 0, 0)

    monkeypatch.setattr(index_tags.common, "index_documents", fake_index_documents)

    index_tags.main(["--path", str(git_repo.path)])
    index_tags.main(["--path", str(repo2.path)])

    id1 = calls[0][0]["id"]
    id2 = calls[1][0]["id"]
    assert id1 != id2
    assert calls[0][0]["metadata"]["repo"] == "repo"
    assert calls[1][0]["metadata"]["repo"] == "repo"


# --- repos.json missing ----------------------------------------

def test_main_without_repos_json_prints_friendly_error(monkeypatch, capsys):
    def fail_load_repos():
        raise FileNotFoundError()

    monkeypatch.setattr(index_tags.common, "load_repos", fail_load_repos)

    index_tags.main([])

    out = capsys.readouterr().out
    assert "not found" in out
