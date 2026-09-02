import pytest

from griot import common, retrieval_eval


def test_build_qrels_from_git_returns_correct_pairs(git_repo):
    sha1 = git_repo.commit("Fix bug X", body="Bug detail.", filename="a.py")
    sha2 = git_repo.commit("Add feature Y", filename="b.py")

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")

    assert len(qrels) == 2
    by_hash = {q["commit_hash"]: q for q in qrels}

    assert by_hash[sha1]["query"] == "Fix bug X\n\nBug detail."
    assert by_hash[sha1]["repo"] == "myrepo"
    assert by_hash[sha1]["relevant_file_paths"] == ["a.py"]

    assert by_hash[sha2]["query"] == "Add feature Y"
    assert by_hash[sha2]["relevant_file_paths"] == ["b.py"]


def test_build_qrels_from_git_multiple_files_per_commit(git_repo, tmp_path):
    import subprocess

    (git_repo.path / "c.py").write_text("c")
    (git_repo.path / "d.py").write_text("d")
    subprocess.run(["git", "-C", str(git_repo.path), "add", "c.py", "d.py"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "Touch two files"], check=True)

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")

    assert len(qrels) == 1
    assert set(qrels[0]["relevant_file_paths"]) == {"c.py", "d.py"}


def test_build_qrels_from_git_skips_commits_without_files(git_repo):
    """A merge commit with no diff (or any commit that touches no file) does
    not become a query/document pair — there's no relevant_file_paths to
    measure anything against."""
    git_repo.commit("normal commit", filename="a.py")
    git_repo.branch("feature")

    import subprocess

    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "feature"], check=True)
    git_repo.commit("commit on feature", filename="b.py")

    subprocess.run(["git", "-C", str(git_repo.path), "checkout", "-q", "main"], check=True)
    # merge --no-ff without conflict: combines both files, but --name-only on
    # the merge commit itself (without -m/--first-parent) lists no files.
    subprocess.run(
        ["git", "-C", str(git_repo.path), "merge", "-q", "--no-ff", "feature", "-m", "Merge feature"],
        check=True,
    )

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")

    subjects = {q["query"] for q in qrels}
    assert "normal commit" in subjects
    assert "commit on feature" in subjects
    assert "Merge feature" not in subjects
    assert len(qrels) == 2


def test_build_qrels_from_git_empty_repo(git_repo):
    assert retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo") == []


def test_build_qrels_from_git_discards_qrel_when_only_file_was_deleted(git_repo):
    """git log --all covers the whole history, but index_code.py
    only indexes the working tree's current snapshot — a commit whose only
    touched file no longer exists can never match in the index, so it must
    not become a qrel (otherwise it would artificially underestimate
    Recall/MRR)."""
    import subprocess

    git_repo.commit("Add permanent file", filename="keep.py")
    git_repo.commit("Add temporary file", filename="temp.py")
    subprocess.run(["git", "-C", str(git_repo.path), "rm", "-q", "temp.py"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "Remove temporary file"], check=True)

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")
    by_query = {q["query"]: q for q in qrels}

    assert "Add permanent file" in by_query
    assert by_query["Add permanent file"]["relevant_file_paths"] == ["keep.py"]
    # temp.py no longer exists in the working tree -> neither of the two
    # commits that only touched it (creation and removal) can produce a qrel.
    assert "Add temporary file" not in by_query
    assert "Remove temporary file" not in by_query


def test_build_qrels_from_git_keeps_only_existing_files_from_multi_file_commit(git_repo):
    """When a commit touches several files and only some still exist, the
    qrel survives but relevant_file_paths only lists the ones still in the
    working tree — it doesn't discard the whole qrel needlessly."""
    import subprocess

    (git_repo.path / "keep.py").write_text("k")
    (git_repo.path / "gone.py").write_text("g")
    subprocess.run(["git", "-C", str(git_repo.path), "add", "keep.py", "gone.py"], check=True)
    subprocess.run(
        ["git", "-C", str(git_repo.path), "commit", "-q", "-m", "Touch two files, one will be removed later"],
        check=True,
    )
    subprocess.run(["git", "-C", str(git_repo.path), "rm", "-q", "gone.py"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "Remove gone.py"], check=True)

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")
    by_query = {q["query"]: q for q in qrels}

    assert by_query["Touch two files, one will be removed later"]["relevant_file_paths"] == ["keep.py"]


def test_build_qrels_from_git_holdout_recent_excludes_most_recent_commits(git_repo):
    git_repo.commit("commit 1", filename="a.py")
    git_repo.commit("commit 2", filename="b.py")
    git_repo.commit("commit 3", filename="c.py")
    git_repo.commit("commit 4", filename="d.py")

    all_qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo")
    assert len(all_qrels) == 4

    held = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo", holdout_recent=2)
    assert len(held) == 2
    subjects = {q["query"] for q in held}
    # git log --all is most-recent-first; holdout_recent=2 excludes commits 4 and 3.
    assert subjects == {"commit 1", "commit 2"}


def test_build_qrels_from_git_max_commits_limits_history(git_repo):
    git_repo.commit("commit 1", filename="a.py")
    git_repo.commit("commit 2", filename="b.py")
    git_repo.commit("commit 3", filename="c.py")

    qrels = retrieval_eval.build_qrels_from_git(git_repo.path, "myrepo", max_commits=2)

    assert len(qrels) == 2
    subjects = {q["query"] for q in qrels}
    assert subjects == {"commit 3", "commit 2"}


def _fake_embed_from_map(monkeypatch, vector_map: dict[str, list[float]]):
    """Replaces common.embed_texts with a deterministic text->vector map —
    both index_documents() (embeds the chunks' `content`) and common.search()
    (embeds the query) go through this same function, so a vector identical
    to a doc's guarantees top-1 without depending on real embeddings or on
    geometry (cosine of identical vectors = 1.0, the maximum possible)."""
    def fake(texts, **kwargs):
        return [vector_map[t] for t in texts]
    monkeypatch.setattr(common, "embed_texts", fake)


def _basis_vector(index: int) -> list[float]:
    v = [0.0] * common.EMBED_DIM
    v[index] = 1.0
    return v


def test_evaluate_qrels_recall_and_mrr_against_synthetic_collection(monkeypatch):
    """Embedded Qdrant collection (tmp_path, via conftest's autouse
    isolation) with 3 synthetic points and orthogonal basis vectors (cosine=0
    between them, cosine=1 when the query uses the vector identical to the
    doc's — no rank ambiguity). qrels:
      - q_alpha and q_beta: guaranteed top-1 hit (vector identical to the
        right doc's, same repo).
      - q_missing: the query's vector is identical to doc `repoB/missing.py`'s
        (hits top-1 in the "raw" vector search), but the qrel asks for
        repo="repoA" — the result's payload has repo="repoB", so
        evaluate_qrels must REJECT this result as not relevant and find no
        other document that satisfies (repo, file_path) — this tests that
        the repo filter isn't ignored just because the file_path matched."""
    vec_alpha = _basis_vector(0)
    vec_beta = _basis_vector(1)
    vec_missing = _basis_vector(2)

    vector_map = {
        "content alpha": vec_alpha,
        "content beta": vec_beta,
        "content missing": vec_missing,
        "where is alpha": vec_alpha,
        "where is beta": vec_beta,
        "where is missing": vec_missing,
    }
    _fake_embed_from_map(monkeypatch, vector_map)

    docs = [
        {
            "id": "repoA:code:alpha.py:0",
            "content": "content alpha",
            "metadata": {"source_type": "code", "repo": "repoA", "file_path": "alpha.py", "chunk_index": 0},
        },
        {
            "id": "repoA:code:beta.py:0",
            "content": "content beta",
            "metadata": {"source_type": "code", "repo": "repoA", "file_path": "beta.py", "chunk_index": 0},
        },
        # DIFFERENT repo (repoB) — even though the vector matches q_missing
        # perfectly, the qrel below asks for repoA, so it can't count as a hit.
        {
            "id": "repoB:code:missing.py:0",
            "content": "content missing",
            "metadata": {"source_type": "code", "repo": "repoB", "file_path": "missing.py", "chunk_index": 0},
        },
    ]
    indexed, skipped, failed = common.index_documents(docs)
    assert (indexed, skipped, failed) == (3, 0, 0)

    qrels = [
        {"query": "where is alpha", "repo": "repoA", "relevant_file_paths": ["alpha.py"]},
        {"query": "where is beta", "repo": "repoA", "relevant_file_paths": ["beta.py"]},
        {"query": "where is missing", "repo": "repoA", "relevant_file_paths": ["missing.py"]},
    ]

    report = retrieval_eval.evaluate_qrels(qrels, k_values=[1, 3])

    assert report["n_queries"] == 3
    # 2 of 3 queries hit top-1 (independent of k, since the 3rd never hits).
    assert report["recall_at_k"] == {1: 2 / 3, 3: 2 / 3}
    # MRR = average of (1/1, 1/1, 0) = 2/3.
    assert report["mrr"] == pytest.approx(2 / 3)
    assert report["queries_without_match"] == ["where is missing"]


def test_evaluate_qrels_all_hits_gives_perfect_score(monkeypatch):
    vec_a = _basis_vector(0)
    vec_b = _basis_vector(1)
    vector_map = {
        "content a": vec_a,
        "content b": vec_b,
        "query a": vec_a,
        "query b": vec_b,
    }
    _fake_embed_from_map(monkeypatch, vector_map)

    docs = [
        {"id": "r:code:a.py:0", "content": "content a", "metadata": {"source_type": "code", "repo": "r", "file_path": "a.py", "chunk_index": 0}},
        {"id": "r:code:b.py:0", "content": "content b", "metadata": {"source_type": "code", "repo": "r", "file_path": "b.py", "chunk_index": 0}},
    ]
    common.index_documents(docs)

    qrels = [
        {"query": "query a", "repo": "r", "relevant_file_paths": ["a.py"]},
        {"query": "query b", "repo": "r", "relevant_file_paths": ["b.py"]},
    ]
    report = retrieval_eval.evaluate_qrels(qrels, k_values=[5])

    assert report["recall_at_k"] == {5: 1.0}
    assert report["mrr"] == 1.0
    assert report["queries_without_match"] == []


def test_evaluate_qrels_empty_qrels_returns_zeroed_report(monkeypatch):
    report = retrieval_eval.evaluate_qrels([], k_values=[5, 20])
    assert report == {
        "n_queries": 0,
        "recall_at_k": {5: 0.0, 20: 0.0},
        "mrr": 0.0,
        "queries_without_match": [],
    }


def test_print_report_does_not_raise(capsys):
    report = {
        "n_queries": 3,
        "recall_at_k": {5: 0.6667, 20: 0.6667},
        "mrr": 0.6667,
        "queries_without_match": ["query without match\n\nbody too long"],
    }
    retrieval_eval.print_report(report)
    out = capsys.readouterr().out
    assert "3 queries evaluated" in out
    assert "Recall@5" in out
    assert "Recall@20" in out
    assert "MRR" in out
    assert "query without match" in out
    assert "body too long" not in out  # only the first line is printed
