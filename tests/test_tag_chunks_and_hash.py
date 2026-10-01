"""The tags source: a long tag message is cut into chunks, and an annotated
tag carries the hash of the commit it marks.

A release tag often holds the whole release note. It was stored as one
document whatever its length: an embedding model reads only so far, so the
rest of the note could not be found, and an API that refuses an oversized
input failed that tag on every run. And for an annotated tag the hash stored
as `commit_hash` was the hash of the TAG OBJECT, which names no commit:
`git show <hash>` of it shows the tag, and it matches no commit of the
commits source."""

import subprocess

import pytest

from griot import common, index_tags


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo.path), *args], check=True, capture_output=True, text=True).stdout.strip()


# --- the hash ---------------------------------------------------------------------------------


def test_an_annotated_tag_carries_the_commit_it_marks(git_repo):
    commit = git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0")
    assert _git(git_repo, "rev-parse", "v1.0.0") != commit, "the tag object has a hash of its own"
    (doc,) = index_tags.build_documents(git_repo.path)
    assert doc["metadata"]["commit_hash"] == commit


def test_a_lightweight_tag_still_carries_its_commit(git_repo):
    commit = git_repo.commit("first commit")
    git_repo.tag("v0.0.1")
    (doc,) = index_tags.build_documents(git_repo.path)
    assert doc["metadata"]["commit_hash"] == commit


def test_a_tag_of_a_tag_carries_the_commit_at_the_end(git_repo):
    commit = git_repo.commit("first commit")
    git_repo.tag("inner", message="inner")
    _git(git_repo, "tag", "-a", "outer", "-m", "outer", "inner")
    docs = {d["metadata"]["tag_name"]: d for d in index_tags.build_documents(git_repo.path)}
    assert docs["outer"]["metadata"]["commit_hash"] == commit


# --- the length -------------------------------------------------------------------------------


def _long_note() -> str:
    return "Release 2.0\n\n" + "\n".join(f"- change number {n} of the release, described at some length" for n in range(200))


def test_a_long_tag_message_is_cut_into_chunks(git_repo):
    git_repo.commit("first commit")
    git_repo.tag("v2.0.0", message=_long_note())
    docs = index_tags.build_documents(git_repo.path)
    assert len(docs) > 1
    assert all(len(d["content"]) <= 1500 for d in docs)
    assert "change number 199" in docs[-1]["content"], "the end of the note is in the index too"
    assert [d["metadata"]["chunk_index"] for d in docs] == list(range(len(docs)))
    assert {d["metadata"]["tag_name"] for d in docs} == {"v2.0.0"}
    assert len({d["id"] for d in docs}) == len(docs)


def test_a_short_tag_keeps_the_id_it_always_had(git_repo):
    """Changing it would embed every tag again and leave the old points behind."""
    git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0")
    (doc,) = index_tags.build_documents(git_repo.path)
    assert doc["id"] == f"{git_repo.path.name}:tag:v1.0.0" and doc["metadata"]["chunk_index"] == 0


# --- what is already indexed ------------------------------------------------------------------


@pytest.fixture
def embedded(monkeypatch):
    """A stand-in embedding that records what it was asked to embed."""
    sent = []

    def fake(texts, **kw):
        sent.extend(texts)
        return [[0.1] * common.EMBED_DIM for _ in texts]

    monkeypatch.setattr(common, "embed_texts", fake)
    return sent


def _stored(doc_id: str) -> dict | None:
    found = common.get_client().retrieve(point_ids=[common.stable_id(doc_id)], with_payload=True, with_vector=False)
    return found[0].payload if found else None


def _tag_points(repo_name: str) -> list[dict]:
    import qdrant_edge as qe
    scope = qe.Filter(must=[qe.FieldCondition(key="repo", match=qe.MatchValue(value=repo_name)),
                            qe.FieldCondition(key="source_type", match=qe.MatchValue(value="tag"))])
    points, _ = common.get_client().scroll(qe.ScrollRequest(limit=1000, filter=scope, with_payload=True, with_vector=False))
    return [p.payload for p in points]


def test_a_tag_indexed_with_the_wrong_hash_is_corrected_without_embedding_it_again(git_repo, embedded):
    """The text of the tag did not change, so nothing is embedded: the hash
    stored beside it is brought up to date all the same."""
    from griot import repos
    commit = git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    # As a version before this fix left it: the tag object's hash, no chunk number.
    old = dict(index_tags.build_documents(git_repo.path)[0])
    old["metadata"] = {**old["metadata"], "commit_hash": _git(git_repo, "rev-parse", "v1.0.0")}
    del old["metadata"]["chunk_index"]
    common.index_documents([old])
    common.release_lock()
    assert _stored(f"{name}:tag:v1.0.0")["commit_hash"] != commit
    embedded.clear()

    index_tags.main(["--repo", name])

    stored = _stored(f"{name}:tag:v1.0.0")
    assert stored["commit_hash"] == commit and stored["chunk_index"] == 0
    assert embedded == [], "the text is the same: nothing to pay for"
    assert stored["content"] == "Release 1.0.0"


def test_a_tag_whose_note_got_shorter_leaves_no_chunk_behind(git_repo, embedded):
    from griot import repos
    git_repo.commit("first commit")
    git_repo.tag("v2.0.0", message=_long_note())
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_tags.main(["--repo", name])
    assert len(_tag_points(name)) > 1
    _git(git_repo, "tag", "-f", "-a", "v2.0.0", "-m", "Release 2.0")
    index_tags.main(["--repo", name, "--prune"])
    points = _tag_points(name)
    assert [p["content"] for p in points] == ["Release 2.0"]


def test_a_deleted_tag_takes_all_its_chunks_with_it(git_repo, embedded):
    from griot import repos
    git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0")
    git_repo.tag("v2.0.0", message=_long_note())
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_tags.main(["--repo", name])
    _git(git_repo, "tag", "-d", "v2.0.0")
    index_tags.main(["--repo", name, "--prune"])
    assert {p["tag_name"] for p in _tag_points(name)} == {"v1.0.0"}


# --- the text of a tag ------------------------------------------------------------------------


def test_the_subject_of_a_tag_with_a_body_is_there_once(git_repo):
    """`contents` begins with the subject, and it was added after the subject."""
    git_repo.commit("first commit")
    git_repo.tag("v1.0.0", message="Release 1.0.0\n\nWhat changed in this release.")
    (doc,) = index_tags.build_documents(git_repo.path)
    assert doc["content"] == "Release 1.0.0\n\nWhat changed in this release."


def test_every_chunk_of_a_long_tag_is_recognised_as_that_tag_s(git_repo):
    """The removal of stale points rebuilds ids from what is stored: a
    format it does not know would never be removed, in silence."""
    git_repo.commit("first commit")
    git_repo.tag("v2.0.0", message=_long_note())
    docs = index_tags.build_documents(git_repo.path)
    assert len(docs) > 1
    for doc in docs:
        assert doc["id"] in common._ids_a_point_could_have("tag", git_repo.path.name, doc["metadata"]), doc["id"]


def test_the_chunks_of_one_tag_are_one_document(git_repo):
    git_repo.commit("first commit")
    git_repo.tag("v2.0.0", message=_long_note())
    docs = index_tags.build_documents(git_repo.path)
    assert len({common.document_key(doc["metadata"]) for doc in docs}) == 1


# --- details stored beside the text, for every source -----------------------------------------


def _doc(state: str) -> dict:
    return {"id": "repo:merge_request:7", "content": "Add retries to the client",
            "metadata": {"source_type": "merge_request", "repo": "repo", "mr_iid": 7, "state": state}}


def test_a_detail_that_changed_while_the_text_stayed_is_written_without_embedding(embedded, capsys):
    """What decides whether to embed is the text. A pull request that was
    merged without a word of it changing kept saying "opened" for ever."""
    common.index_documents([_doc("opened")])
    embedded.clear()
    indexed, skipped, failed = common.index_documents([_doc("merged")])
    assert (indexed, skipped, failed) == (0, 1, 0) and embedded == []
    assert _stored("repo:merge_request:7")["state"] == "merged"
    assert "Updated the stored details of 1 unchanged point" in capsys.readouterr().out


def test_nothing_is_written_when_nothing_changed(embedded, capsys, monkeypatch):
    common.index_documents([_doc("opened")])
    capsys.readouterr()
    written = []
    monkeypatch.setattr(common, "_write_stale_details", lambda batch, client: written.append(
        [d for d in batch if d.get("_details_changed")]) or 0)
    common.index_documents([_doc("opened")])
    assert written == [[]] and "Updated the stored details" not in capsys.readouterr().out


def test_what_is_stored_and_not_among_the_details_of_a_document_is_kept(embedded):
    common.index_documents([_doc("opened")])
    before = _stored("repo:merge_request:7")
    common.index_documents([_doc("merged")])
    after = _stored("repo:merge_request:7")
    assert after["content"] == before["content"] and after["content_hash"] == before["content_hash"]


def test_a_dry_run_does_not_write_the_details(embedded):
    common.index_documents([_doc("opened")])
    common.release_lock()
    assert common.count_pending([_doc("merged")]) == (0, 1)
    assert _stored("repo:merge_request:7")["state"] == "opened"


def test_a_failure_to_write_the_details_does_not_fail_the_run(embedded, monkeypatch):
    common.index_documents([_doc("opened")])

    def broken(batch, client):
        raise OSError("disk full")

    monkeypatch.setattr(common, "_write_stale_details", broken)
    assert common.index_documents([_doc("merged")]) == (0, 1, 0)


def test_only_the_points_whose_details_changed_are_written(embedded):
    one = {"id": "repo:merge_request:1", "content": "first", "metadata": {"source_type": "merge_request", "repo": "repo", "mr_iid": 1, "state": "opened"}}
    two = {"id": "repo:merge_request:2", "content": "second", "metadata": {"source_type": "merge_request", "repo": "repo", "mr_iid": 2, "state": "opened"}}
    common.index_documents([one, two])
    client = common.get_client()
    batch = [dict(one), {**two, "metadata": {**two["metadata"], "state": "merged"}}]
    assert common._split_pending(batch, client) == []
    assert common._write_stale_details(batch, client) == 1
    assert [doc["_details_changed"] for doc in batch] == [False, True]


def test_a_document_that_is_about_to_be_embedded_is_not_a_detail_update(embedded, capsys):
    """New, or with a text that changed: it is written whole, once."""
    common.index_documents([_doc("opened")])
    assert "Updated the stored details" not in capsys.readouterr().out
    changed = {**_doc("merged"), "content": "Add retries to the client, with backoff"}
    assert common.index_documents([changed]) == (1, 0, 0)
    assert "Updated the stored details" not in capsys.readouterr().out
    assert _stored("repo:merge_request:7")["state"] == "merged"


def test_a_git_that_peels_one_level_only_is_asked_for_the_commit(git_repo, monkeypatch):
    """Newer versions of git follow a tag of a tag to its commit in
    `for-each-ref`; older ones stop at the inner tag."""
    commit = git_repo.commit("first commit")
    git_repo.tag("inner", message="inner")
    _git(git_repo, "tag", "-a", "outer", "-m", "outer", "inner")
    inner_object = _git(git_repo, "rev-parse", "inner")
    outer_object = _git(git_repo, "rev-parse", "outer")
    real = common.run_git

    def older_git(repo_path, args, **kw):
        if args[0] != "for-each-ref":
            return real(repo_path, args, **kw)
        record = index_tags.FIELD_SEP.join(["outer", outer_object, "tag", inner_object, "tag",
                                            "2026-01-01T00:00:00+00:00", "outer", ""]) + index_tags.RECORD_SEP
        return type("Done", (), {"stdout": record, "returncode": 0})()

    monkeypatch.setattr(common, "run_git", older_git)
    (tag,) = index_tags.list_tags(git_repo.path)
    assert tag["commit_hash"] == commit


def test_a_tag_that_leads_to_no_commit_keeps_the_hash_of_what_it_points_at(git_repo):
    git_repo.commit("first commit")
    blob = _git(git_repo, "rev-parse", "HEAD:file.txt")
    _git(git_repo, "tag", "-a", "a-file", "-m", "a tag of a file", blob)
    (tag,) = index_tags.list_tags(git_repo.path)
    assert tag["commit_hash"] == blob


def test_the_signature_of_a_signed_tag_is_not_part_of_its_text(git_repo):
    git_repo.commit("first commit")
    signature = "-----BEGIN PGP SIGNATURE-----\n\niQEzBAABCAAdFiEE\n=abcd\n-----END PGP SIGNATURE-----"
    git_repo.tag("v1.0.0", message=f"Release 1.0.0\n\nWhat changed.\n{signature}")
    (doc,) = index_tags.build_documents(git_repo.path)
    assert doc["content"] == "Release 1.0.0\n\nWhat changed."


def test_one_point_that_is_gone_does_not_stop_the_others_from_being_updated(embedded):
    import qdrant_edge as qe
    docs = [{"id": f"repo:merge_request:{n}", "content": f"text {n}",
             "metadata": {"source_type": "merge_request", "repo": "repo", "mr_iid": n, "state": "opened"}} for n in (1, 2, 3)]
    common.index_documents(docs)
    client = common.get_client()
    batch = [{**doc, "metadata": {**doc["metadata"], "state": "merged"}} for doc in docs]
    assert common._split_pending(batch, client) == []
    # Gone between the read and the write (another run removed it).
    client.update(qe.UpdateOperation.delete_points([common.stable_id("repo:merge_request:2")]))
    assert common._write_stale_details(batch, client) == 2
    assert _stored("repo:merge_request:1")["state"] == "merged" and _stored("repo:merge_request:3")["state"] == "merged"
    assert _stored("repo:merge_request:2") is None, "and it is not brought back as a point without a vector"
