"""Points whose source is gone are removed when a repository is indexed again.

Indexing only ever added or replaced. A deleted or renamed file, a file that
shrank, a file that became ignored, a deleted branch: their points stayed for
good and search kept returning text that no longer exists. Removing points is
the one destructive thing indexing does, so it is fenced in: only after a run
that read everything and wrote everything, only for a registered repository
whose name is unambiguous, never for an empty listing, and never more than
half of a repository's points without being told to."""

import subprocess

import pytest
import qdrant_edge as qe

from griot import common, index_code, index_commits, repos


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [[0.1] * common.EMBED_DIM for _ in texts])


def _register(tmp_path, name="proj", parent="a"):
    """Written straight into repos.json: add_repo() refuses a second
    repository with the same directory name, but a file edited by hand or
    written by an older version can still hold one, and that is what the
    ambiguity tests need."""
    import json
    path = tmp_path / parent / name
    path.mkdir(parents=True)
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    existing = json.loads(common.REPOS_JSON_PATH.read_text()) if common.REPOS_JSON_PATH.exists() else []
    common.REPOS_JSON_PATH.write_text(json.dumps(existing + [str(path.resolve())]))
    return path


def _docs(repo, names, source_type="code"):
    return [{"id": f"{repo}:{source_type}:{n}:0", "content": f"content of {n}",
             "metadata": {"source_type": source_type, "repo": repo, "file_path": n, "chunk_index": 0}} for n in names]


def _stored(repo=None, source_type=None):
    client = common.get_client()
    out, offset = [], None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(limit=100, offset=offset, with_payload=True, with_vector=False))
        out += [p.payload for p in points]
        if offset is None:
            break
    return sorted(p.get("file_path") or p.get("commit_hash") for p in out
                  if (repo is None or p["repo"] == repo) and (source_type is None or p["source_type"] == source_type))


def _prune(documents, path, **kw):
    kw.setdefault("source_type", "code")
    return common.prune_orphans(documents, repo_paths=[path], **kw)


# --- what gets removed -----------------------------------------------------------


def test_points_the_run_no_longer_produces_are_removed(tmp_path):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    now = _docs("proj", ["a.py"])
    common.index_documents(now)
    assert _prune(now, path) == 2
    assert _stored("proj") == ["a.py"]


def test_other_repositories_and_other_sources_are_left_alone(tmp_path):
    path = _register(tmp_path)
    _register(tmp_path, name="other", parent="b")
    common.index_documents(_docs("proj", ["a.py", "gone.py"]) + _docs("other", ["x.py"])
                           + _docs("proj", ["abc123"], source_type="commit"))
    now = _docs("proj", ["a.py"])
    assert _prune(now, path) == 1
    assert _stored("proj", "code") == ["a.py"]
    assert _stored("other") == ["x.py"]
    assert len(_stored("proj", "commit")) == 1


def test_more_orphans_than_one_page_are_all_found(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_PRUNE_PAGE", 2)
    path = _register(tmp_path)
    names = [f"f{i}.py" for i in range(9)]
    common.index_documents(_docs("proj", names))
    now = _docs("proj", names[:5])
    assert _prune(now, path) == 4
    assert _stored("proj") == sorted(names[:5])


def test_a_dry_run_counts_and_removes_nothing(tmp_path, capsys):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path, dry_run=True) == 1
    assert _stored("proj") == ["a.py", "b.py", "c.py"]
    assert "[dry-run] 1 stale point" in capsys.readouterr().out


def test_nothing_stale_is_silent(tmp_path, capsys):
    path = _register(tmp_path)
    docs = _docs("proj", ["a.py"])
    common.index_documents(docs)
    capsys.readouterr()
    assert _prune(docs, path) == 0
    assert capsys.readouterr().out == ""


# --- the fences -------------------------------------------------------------------


def test_a_path_run_never_removes(tmp_path):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path, used_path=True) == 0
    assert len(_stored("proj")) == 3


def test_an_unregistered_repository_is_never_pruned(tmp_path):
    path = tmp_path / "proj"
    path.mkdir()
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path) == 0
    assert len(_stored("proj")) == 3


def test_a_name_shared_by_two_registered_repositories_is_never_pruned(tmp_path):
    """Both write `repo: proj` into their points, so one repository's run
    cannot tell its own points from the other's."""
    path = _register(tmp_path, parent="a")
    _register(tmp_path, parent="b")
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path) == 0
    assert len(_stored("proj")) == 3


def test_an_empty_listing_removes_nothing(tmp_path):
    """Zero documents is what a failed read looks like (git could not list,
    the directory is gone), far more often than a repository emptied on
    purpose."""
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py"]))
    assert _prune([], path) == 0
    assert len(_stored("proj")) == 2


def test_a_run_with_failures_removes_nothing(tmp_path, capsys):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path, failed=1) == 0
    assert len(_stored("proj")) == 3
    assert "not removed" in capsys.readouterr().out


def test_a_repository_that_could_not_be_read_completely_is_skipped(tmp_path):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py", "b.py"]), path, incomplete={"proj"}) == 0
    assert len(_stored("proj")) == 3


def test_more_than_half_of_a_repository_is_not_removed_without_being_told(tmp_path, monkeypatch, capsys):
    """Most of a repository going stale at once usually means the wrong
    branch is checked out, or the listing is wrong, not that the code left."""
    monkeypatch.setattr(common, "_PRUNE_GUARD_MIN_POINTS", 2)
    path = _register(tmp_path)
    names = [f"f{i}.py" for i in range(10)]
    common.index_documents(_docs("proj", names))
    now = _docs("proj", names[:4])
    assert _prune(now, path) == 0
    assert len(_stored("proj")) == 10
    out = capsys.readouterr().out
    assert "6 of 10" in out and "--prune" in out
    assert _prune(now, path, force=True) == 6
    assert _stored("proj") == sorted(names[:4])


def test_exactly_half_is_still_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_PRUNE_GUARD_MIN_POINTS", 2)
    path = _register(tmp_path)
    names = [f"f{i}.py" for i in range(10)]
    common.index_documents(_docs("proj", names))
    assert _prune(_docs("proj", names[:5]), path) == 5


def test_a_small_repository_is_not_held_back_by_the_proportion(tmp_path):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    assert _prune(_docs("proj", ["a.py"]), path) == 2


def test_the_index_lock_is_released_afterwards(tmp_path):
    path = _register(tmp_path)
    common.index_documents(_docs("proj", ["a.py", "b.py", "c.py"]))
    _prune(_docs("proj", ["a.py", "b.py"]), path)
    assert common.index_lock_status()["running"] is False


# --- through the real indexers ------------------------------------------------------


def _write(path, name, text="x = 1\n"):
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)


def test_index_code_removes_the_points_of_a_deleted_and_a_shrunk_file(tmp_path, capsys):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    _write(path, "big.py", "y = 2\n" * 700)  # several chunks
    index_code.main(["--repo", "proj"])
    before = _stored("proj")
    assert before.count("big.py") > 1 and "gone.py" in before

    (path / "gone.py").unlink()
    _write(path, "big.py", "y = 2\n")
    index_code.main(["--repo", "proj"])
    assert _stored("proj") == ["big.py", "keep.py"]
    assert "stale point" in capsys.readouterr().out


def test_index_code_dry_run_says_how_many_would_go(tmp_path, capsys):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    capsys.readouterr()
    index_code.main(["--repo", "proj", "--dry-run"])
    assert "[dry-run] 1 stale point" in capsys.readouterr().out
    assert _stored("proj") == ["gone.py", "keep.py"]


def test_index_code_with_path_does_not_remove(tmp_path):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    index_code.main(["--path", str(path)])
    assert "gone.py" in _stored("proj")


def test_a_file_that_could_not_be_read_keeps_every_point_of_its_repository(tmp_path, monkeypatch):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    _write(path, "flaky.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    real = index_code.read_source
    monkeypatch.setattr(index_code, "read_source",
                        lambda p, root: None if p.name == "flaky.py" else real(p, root))
    index_code.main(["--repo", "proj"])
    assert _stored("proj") == ["flaky.py", "gone.py", "keep.py"]


def test_the_run_record_says_how_many_were_removed(tmp_path):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    index_code.main(["--repo", "proj"])
    from griot import logdb
    runs = logdb.read_since(common.LOG_DIR, "runs", days=1)
    assert [r.get("pruned") for r in runs] == [0, 1]


def _all_ids(name, source):
    """Every point id stored for a repository name and source, read straight
    from the store. Deliberately NOT through the ownership check under test:
    a point that stops being recognised would vanish from that view without
    having been removed, and a "was it removed" assertion would pass on it."""
    scope = qe.Filter(must=[qe.FieldCondition(key="repo", match=qe.MatchValue(value=name)),
                            qe.FieldCondition(key="source_type", match=qe.MatchValue(value=source))])
    client, ids, offset = common.get_client(), set(), None
    while True:
        points, offset = client.scroll(qe.ScrollRequest(limit=200, offset=offset, filter=scope,
                                                        with_payload=False, with_vector=False))
        ids |= {str(point.id) for point in points}
        if offset is None:
            return ids


def _commit_point_ids(name):
    return _all_ids(name, "commit")


def test_index_commits_removes_points_left_by_older_id_shapes(git_repo):
    """A commit has been indexed as one point, as one point with a `:0`
    suffix, and as chunks. The old shapes share no id with the current one
    and would otherwise stay forever, duplicating the commit in results."""
    short = git_repo.commit("one", filename="a.py")
    long = git_repo.commit("two", body="b" * 4000, filename="b.py")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    suffixed = f"{name}:commit:{short}:0"   # early shape of a single-point commit
    unchunked = f"{name}:commit:{long}"     # a long commit from before chunking
    common.index_documents([
        {"id": suffixed, "content": "old", "metadata": {"source_type": "commit", "repo": name, "commit_hash": short, "chunk_index": 0}},
        {"id": unchunked, "content": "old whole body", "metadata": {"source_type": "commit", "repo": name, "commit_hash": long}},
    ])
    index_commits.main(["--repo", name])
    ids = _commit_point_ids(name)
    assert common.stable_id(suffixed) not in ids and common.stable_id(unchunked) not in ids
    assert common.stable_id(f"{name}:commit:{short}") in ids and common.stable_id(f"{name}:commit:{long}:0") in ids


@pytest.mark.parametrize("module", ["index_code", "index_commits", "index_tags", "index_branches", "index_platform"])
def test_every_source_accepts_the_prune_flag(module, capsys):
    """`griot index all --prune` forwards the flag to every source."""
    import importlib
    mod = importlib.import_module(f"griot.{module}")
    with pytest.raises(SystemExit) as exit_info:
        mod.main(["--prune", "--help"])
    assert exit_info.value.code == 0
    assert "--prune" in capsys.readouterr().out


# --- a point is only stale if it was written under THIS repository's key ---------------
# `payload.repo` is the directory name, and a `--path` run of another directory with
# the same name writes it too, under a different id key. The name alone would let one
# repository's run delete the other's points.


def _path_docs(name, key, files):
    return [{"id": f"{key}:code:{n}:0", "content": f"content of {n}",
             "metadata": {"source_type": "code", "repo": name, "file_path": n, "chunk_index": 0}} for n in files]


def test_points_another_directory_of_the_same_name_wrote_are_never_removed(tmp_path, capsys):
    path = _register(tmp_path)
    common.index_documents(_path_docs("proj", "proj-1a2b3c4d", ["x.py", "y.py", "z.py"]))  # a --path run elsewhere
    now = _docs("proj", ["a.py"])
    common.index_documents(now + _docs("proj", ["gone.py"]))
    assert _prune(now, path) == 1
    assert _stored("proj") == ["a.py", "x.py", "y.py", "z.py"]
    assert "3 point(s)" in capsys.readouterr().out


def test_through_the_indexers_a_same_named_path_run_survives_the_registered_run(tmp_path):
    registered = _register(tmp_path, parent="a")
    other = tmp_path / "b" / "proj"
    other.mkdir(parents=True)
    _write(other, "x.py")
    _write(other, "y.py")
    _write(registered, "a.py")
    index_code.main(["--path", str(other)])
    index_code.main(["--repo", "proj"])
    assert _stored("proj") == ["a.py", "x.py", "y.py"]


def test_a_point_missing_the_fields_its_id_is_built_from_is_left_alone(tmp_path):
    path = _register(tmp_path)
    common.index_documents([{"id": "proj:code:weird", "content": "c", "metadata": {"source_type": "code", "repo": "proj"}}])
    now = _docs("proj", ["a.py"])
    common.index_documents(now)
    assert _prune(now, path) == 0
    assert common.get_client().count(qe.CountRequest()) == 2


@pytest.mark.parametrize("source", ["code", "commit", "tag", "branch"])
def test_the_id_rebuilt_from_a_point_matches_the_id_the_indexer_wrote(source, git_repo):
    """prune_orphans rebuilds ids from the payload. If an indexer changes its
    id format and this does not follow, nothing would ever be stale (safe),
    but silently: so the two are held together here."""
    from griot import index_branches, index_tags
    git_repo.commit("one", body="b" * 4000, filename="a.py", content="x = 1\n" * 600)
    git_repo.commit("two", filename="b.py")
    git_repo.tag("v1", "release")
    git_repo.branch("feature")
    git_repo.set_remote_head("main")
    build = {"code": index_code.process_repository, "commit": index_commits.build_documents,
             "tag": index_tags.build_documents, "branch": index_branches.build_documents}[source]
    documents = build(git_repo.path)
    assert documents
    key = git_repo.path.name
    for doc in documents:
        assert doc["id"] in common._ids_a_point_could_have(source, key, doc["metadata"]), doc["id"]


# --- wiring of every indexer -----------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo.path), *args], check=True, capture_output=True)


def test_index_tags_removes_the_point_of_a_deleted_tag(git_repo):
    from griot import index_tags
    git_repo.commit("one")
    git_repo.tag("v1", "first")
    git_repo.tag("v2", "second")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_tags.main(["--repo", name])
    _git(git_repo, "tag", "-d", "v1")
    index_tags.main(["--repo", name])
    client = common.get_client()
    points, _ = client.scroll(qe.ScrollRequest(limit=50, with_payload=True, with_vector=False))
    assert sorted(p.payload["tag_name"] for p in points if p.payload["source_type"] == "tag") == ["v2"]


def test_index_branches_removes_the_point_of_a_deleted_branch(git_repo):
    from griot import index_branches
    git_repo.commit("one")
    git_repo.branch("feature-a")
    git_repo.branch("feature-b")
    git_repo.set_remote_head("main")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    index_branches.main(["--repo", name])
    _git(git_repo, "update-ref", "-d", "refs/remotes/origin/feature-a")
    index_branches.main(["--repo", name])
    client = common.get_client()
    points, _ = client.scroll(qe.ScrollRequest(limit=50, with_payload=True, with_vector=False))
    assert sorted(p.payload["branch_name"] for p in points if p.payload["source_type"] == "branch") == ["origin/feature-b"]


STALE = {
    "code": (lambda n: f"{n}:code:gone.py:0", {"file_path": "gone.py", "chunk_index": 0}),
    "commit": (lambda n: f"{n}:commit:{'0' * 40}", {"commit_hash": "0" * 40}),
    "tag": (lambda n: f"{n}:tag:gone", {"tag_name": "gone"}),
    "branch": (lambda n: f"{n}:branch:origin/gone", {"branch_name": "origin/gone"}),
}
INDEXERS = [("index_code", "code"), ("index_commits", "commit"), ("index_tags", "tag"), ("index_branches", "branch")]


def _repo_with_a_stale_point(git_repo, source):
    """A registered repository with every source populated, plus one point
    of `source` whose origin no longer exists. Returns (name, its point id)."""
    git_repo.commit("one", filename="a.py")
    git_repo.tag("v1", "first")
    git_repo.branch("feature-a")
    git_repo.set_remote_head("main")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    natural, fields = STALE[source][0](name), STALE[source][1]
    common.index_documents([{"id": natural, "content": "stale",
                             "metadata": {"source_type": source, "repo": name, **fields}}])
    return name, common.stable_id(natural)


def _own_ids(name, source):
    return _all_ids(name, source)


@pytest.mark.parametrize("module,source", INDEXERS)
def test_every_indexer_removes_a_stale_point_on_a_registered_run_only(module, source, git_repo):
    import importlib
    mod = importlib.import_module(f"griot.{module}")
    name, stale_id = _repo_with_a_stale_point(git_repo, source)
    mod.main(["--path", str(git_repo.path)])
    assert stale_id in _own_ids(name, source), "a --path run removes nothing"
    mod.main(["--repo", name])
    assert stale_id not in _own_ids(name, source), "the registered run removes it"


@pytest.mark.parametrize("module,source", INDEXERS)
def test_no_indexer_removes_anything_when_documents_failed(module, source, git_repo, monkeypatch):
    import importlib
    mod = importlib.import_module(f"griot.{module}")
    name, stale_id = _repo_with_a_stale_point(git_repo, source)
    real = common.index_documents
    monkeypatch.setattr(common, "index_documents", lambda docs, **kw: (real(docs, **kw)[0], 0, 1))
    mod.main(["--repo", name])
    assert stale_id in _own_ids(name, source)


def test_the_prune_flag_reaches_the_guard_through_an_indexer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "_PRUNE_GUARD_MIN_POINTS", 1)
    path = _register(tmp_path)
    for i in range(6):
        _write(path, f"f{i}.py")
    index_code.main(["--repo", "proj"])
    for i in range(1, 6):
        (path / f"f{i}.py").unlink()
    index_code.main(["--repo", "proj"])
    assert len(_stored("proj")) == 6 and "--prune" in capsys.readouterr().out
    index_code.main(["--repo", "proj", "--prune"])
    assert _stored("proj") == ["f0.py"]


def test_a_file_whose_read_raises_keeps_every_point_of_its_repository(tmp_path, monkeypatch):
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    _write(path, "broken.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    real = index_code.read_source

    def flaky(p, root):
        if p.name == "broken.py":
            raise OSError("disk hiccup")
        return real(p, root)

    monkeypatch.setattr(index_code, "read_source", flaky)
    index_code.main(["--repo", "proj"])
    assert _stored("proj") == ["broken.py", "gone.py", "keep.py"]


# --- what happens around it --------------------------------------------------------------


def test_a_taken_lock_skips_the_removal_and_the_run_is_still_recorded(tmp_path, monkeypatch, capsys):
    """The documents were written. A removal that cannot take the lock must
    not turn a run that indexed into one recorded as dead."""
    path = _register(tmp_path)
    _write(path, "keep.py")
    _write(path, "gone.py")
    index_code.main(["--repo", "proj"])
    (path / "gone.py").unlink()
    _write(path, "new.py")
    real, calls = common.acquire_lock, []

    def second_call_fails():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("A griot process is already running")
        return real()

    monkeypatch.setattr(common, "acquire_lock", second_call_fails)
    index_code.main(["--repo", "proj"])
    assert _stored("proj") == ["gone.py", "keep.py", "new.py"]
    assert "not removed" in capsys.readouterr().out
    from griot import logdb
    last = logdb.read_since(common.LOG_DIR, "runs", days=1)[-1]
    assert last["indexed"] == 1 and last["pruned"] == 0


def test_an_ambiguous_name_says_why_nothing_was_removed(tmp_path, capsys):
    path = _register(tmp_path, parent="a")
    _register(tmp_path, parent="b")
    docs = _docs("proj", ["a.py"])
    common.index_documents(docs)
    capsys.readouterr()
    assert _prune(docs, path) == 0
    assert "more than one registered repository is named 'proj'" in capsys.readouterr().out


def test_a_repository_registered_through_a_symlink_is_pruned_under_the_name_it_is_indexed_as(tmp_path):
    import json
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(link)]))
    common.index_documents(_docs("link", ["a.py", "gone.py"]))
    now = _docs("link", ["a.py"])
    assert common.prune_orphans(now, source_type="code", repo_paths=[str(link)]) == 1
    assert _stored("link") == ["a.py"]


def test_the_guard_message_in_a_dry_run_does_not_claim_a_removal_was_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "_PRUNE_GUARD_MIN_POINTS", 2)
    path = _register(tmp_path)
    names = [f"f{i}.py" for i in range(10)]
    common.index_documents(_docs("proj", names))
    capsys.readouterr()
    assert _prune(_docs("proj", names[:4]), path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "would not be removed" in out and "nothing was removed" not in out


def test_a_directory_registered_twice_is_indexed_but_never_pruned(tmp_path, capsys):
    """Through a symlink and by its real path: two names for one directory.
    Each name writes its own points, and neither run may act on the other's."""
    import json
    real = tmp_path / "a" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(link), str(real)]))
    _write(real, "a.py")
    _write(real, "gone.py")
    index_code.main([])
    (real / "gone.py").unlink()
    capsys.readouterr()
    index_code.main(["--repo", "proj"])
    out = capsys.readouterr().out
    assert "registered more than once" in out
    assert _stored("proj") == ["a.py", "gone.py"] and _stored("link") == ["a.py", "gone.py"]
    from griot import logdb
    assert logdb.read_since(common.LOG_DIR, "runs", days=1)[-1]["pruned"] == 0


def test_an_early_suffixed_commit_point_without_a_chunk_index_is_still_recognised(git_repo):
    sha = git_repo.commit("one", filename="a.py")
    repos.add_repo(str(git_repo.path))
    name = git_repo.path.name
    suffixed = f"{name}:commit:{sha}:0"
    common.index_documents([{"id": suffixed, "content": "old",
                             "metadata": {"source_type": "commit", "repo": name, "commit_hash": sha}}])
    index_commits.main(["--repo", name])
    assert common.stable_id(suffixed) not in _commit_point_ids(name)
