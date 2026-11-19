"""`griot quality-check --from-history`: retrieval measured against the git
history of a registered repository (a commit's message is the question, the
indexed files it touched are the answers), reported as MRR@10 and recall@10
per kind of question and recorded as a series of its own.

Real git repositories and a real embedded collection; only embed_texts is
replaced, by a map from text to vector, so every rank below is known in
advance (cosine of identical vectors is 1, of orthogonal ones 0)."""
import json
import math

import pytest

from griot import common, logdb, quality_check, retrieval_eval, stats


# --- the metric arithmetic, on its own -----------------------------------------------------


def test_reciprocal_rank_is_one_over_the_first_relevant_documents_rank():
    docs = [("repo", "a.py"), ("repo", "b.py"), ("repo", "c.py")]
    assert retrieval_eval.reciprocal_rank(docs, {("repo", "c.py")}) == pytest.approx(1 / 3)
    assert retrieval_eval.reciprocal_rank(docs, {("repo", "b.py"), ("repo", "c.py")}) == pytest.approx(1 / 2)
    assert retrieval_eval.reciprocal_rank(docs, {("repo", "a.py")}) == 1.0
    assert retrieval_eval.reciprocal_rank(docs, {("repo", "z.py")}) == 0.0
    assert retrieval_eval.reciprocal_rank([], {("repo", "a.py")}) == 0.0


def test_recall_is_the_share_of_relevant_documents_found():
    docs = [("repo", "a.py"), ("repo", "b.py")]
    assert retrieval_eval.recall(docs, {("repo", "a.py"), ("repo", "x.py")}) == 0.5
    assert retrieval_eval.recall(docs, {("repo", "a.py"), ("repo", "b.py")}) == 1.0
    assert retrieval_eval.recall(docs, {("repo", "x.py"), ("repo", "y.py"), ("repo", "z.py")}) == 0.0
    assert retrieval_eval.recall(docs, {("repo", "a.py"), ("repo", "x.py"), ("repo", "y.py")}) == pytest.approx(1 / 3)


def test_ranked_documents_gives_a_documents_later_chunks_no_rank_of_their_own():
    payloads = [{"repo": "r", "file_path": "a.py", "chunk_index": 0},
                {"repo": "r", "file_path": "a.py", "chunk_index": 1},
                {"repo": "r", "file_path": "b.py", "chunk_index": 0},
                {"repo": "s", "file_path": "a.py", "chunk_index": 0}]
    assert retrieval_eval.ranked_documents(payloads) == [("r", "a.py"), ("r", "b.py"), ("s", "a.py")]


def test_summary_averages_per_kind_and_overall():
    scored = [{"kind": "subject", "rr": 1.0, "recall": 1.0},
              {"kind": "subject", "rr": 0.0, "recall": 0.0},
              {"kind": "subject_and_body", "rr": 0.5, "recall": 0.5},
              {"kind": "subject_and_body", "rr": 0.25, "recall": 1.0}]
    summary = retrieval_eval.summarize(scored)
    assert summary["pairs"] == 4
    assert summary["mrr"] == pytest.approx(1.75 / 4)
    assert summary["recall"] == pytest.approx(2.5 / 4)
    assert summary["by_kind"] == {
        "subject": {"pairs": 2, "mrr": pytest.approx(0.5), "recall": pytest.approx(0.5)},
        "subject_and_body": {"pairs": 2, "mrr": pytest.approx(0.375), "recall": pytest.approx(0.75)},
    }


def test_a_summary_of_nothing_has_no_rate_rather_than_a_zero_one():
    """Zero would read as "measured and found nothing"; nothing was measured."""
    assert retrieval_eval.summarize([]) == {"pairs": 0, "mrr": None, "recall": None, "by_kind": {}}


def test_the_kind_of_a_question_is_whether_the_message_has_a_body():
    assert retrieval_eval.query_kind({"query": "Fix the lock"}) == "subject"
    assert retrieval_eval.query_kind({"query": "Fix the lock\n\nIt was held twice."}) == "subject_and_body"


# --- the pairs ---------------------------------------------------------------------------


def test_pairs_keep_only_the_files_that_are_indexed(git_repo):
    git_repo.commit("Add the parser", filename="parser.py")
    git_repo.commit("Add the logo", filename="logo.png")  # touched nothing griot indexed: no answer exists
    pairs = retrieval_eval.history_pairs(git_repo.path, "repo", {"parser.py"})
    assert [(p["query"], p["relevant_file_paths"], p["kind"]) for p in pairs] == [
        ("Add the parser", ["parser.py"], "subject")]


def test_pairs_are_evenly_spaced_over_the_history_and_the_same_every_time(git_repo):
    for i in range(10):
        git_repo.commit(f"commit {i}", filename=f"f{i}.py")
    indexed = {f"f{i}.py" for i in range(10)}
    first = retrieval_eval.history_pairs(git_repo.path, "repo", indexed, limit=4)
    again = retrieval_eval.history_pairs(git_repo.path, "repo", indexed, limit=4)
    assert first == again
    # Most recent first, as git log gives them: 9..0, every 10 // 4-th place.
    assert [p["query"] for p in first] == ["commit 9", "commit 7", "commit 4", "commit 2"]
    assert len(retrieval_eval.history_pairs(git_repo.path, "repo", indexed)) == 10
    assert len(retrieval_eval.history_pairs(git_repo.path, "repo", indexed, limit=50)) == 10


def test_spread_picks_evenly_and_never_twice():
    items = list(range(7))
    assert retrieval_eval.spread(items, 3) == [0, 2, 4]
    assert retrieval_eval.spread(items, 7) == items
    assert retrieval_eval.spread(items, 1) == [0]
    assert retrieval_eval.spread(items, None) == items
    picked = retrieval_eval.spread(list(range(100)), 30)
    assert len(picked) == len(set(picked)) == 30


# --- the command, end to end ---------------------------------------------------------------


def _unit(*weights) -> list[float]:
    v = [0.0] * common.EMBED_DIM
    for index, weight in weights:
        v[index] = weight
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


Q_ALPHA = "Add the alpha parser"
Q_BETA = "Teach beta to retry\n\nThe retry waits twice as long each time."
Q_GAMMA = "Rename gamma"


@pytest.fixture
def history_index(git_repo, monkeypatch):
    """A repository of three commits, registered and indexed, where each
    question's answer sits at a known rank:

    - Q_ALPHA (subject only): alpha.py is first. RR 1, recall 1.
    - Q_BETA (subject and body): alpha.py first, beta.py second. RR 1/2, recall 1.
    - Q_GAMMA (subject only): eleven other files come before gamma.py, so it is
      outside the top 10. RR 0, recall 0."""
    git_repo.commit(Q_ALPHA, filename="alpha.py")
    git_repo.commit("Teach beta to retry", body="The retry waits twice as long each time.", filename="beta.py")
    git_repo.commit(Q_GAMMA, filename="gamma.py")
    vectors = {
        Q_ALPHA: _unit((0, 1.0)), "alpha code": _unit((0, 1.0)),
        Q_BETA: _unit((0, 0.9), (1, 0.5)), "beta code": _unit((1, 1.0)),
        Q_GAMMA: _unit((3, 1.0)), "gamma code": _unit((2, 1.0)),
    }
    docs = [{"id": f"repo:code:{name}.py:0", "content": f"{name} code",
             "metadata": {"source_type": "code", "repo": "repo", "file_path": f"{name}.py", "chunk_index": 0}}
            for name in ("alpha", "beta", "gamma")]
    for i in range(11):
        vectors[f"filler {i}"] = _unit((3, 1.0), (10 + i, 0.3))
        docs.append({"id": f"repo:code:filler{i}.py:0", "content": f"filler {i}",
                     "metadata": {"source_type": "code", "repo": "repo", "file_path": f"filler{i}.py",
                                  "chunk_index": 0}})
    # Two points that would move every rank above if they competed: the
    # commit itself (it holds its message word for word), and another
    # repository's file that is Q_BETA's exact neighbour. The search is of
    # this repository's code only, so neither may.
    vectors["the commit that added alpha"] = vectors[Q_ALPHA]
    docs.append({"id": "repo:commit:abc:0", "content": "the commit that added alpha",
                 "metadata": {"source_type": "commit", "repo": "repo", "commit_hash": "abc", "chunk_index": 0}})
    vectors["other beta code"] = vectors[Q_BETA]
    docs.append({"id": "other:code:beta.py:0", "content": "other beta code",
                 "metadata": {"source_type": "code", "repo": "other", "file_path": "beta.py", "chunk_index": 0}})
    embedded = []

    def fake(texts, **kwargs):
        embedded.extend(texts)
        return [vectors[t] for t in texts]
    monkeypatch.setattr(common, "embed_texts", fake)
    common.index_documents(docs)
    common.release_lock()
    _register(git_repo.path)
    embedded.clear()
    return embedded


def _register(path) -> None:
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(path)]))


def _run_json(capsys, *argv):
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector", "--json", *argv])
    return json.loads(capsys.readouterr().out)


def test_from_history_reports_mrr_and_recall_per_kind_of_question(history_index, capsys):
    result = _run_json(capsys)
    (repo,) = result["repositories"]
    assert repo["repo"] == "repo"
    assert repo["pairs"] == 3 and repo["pairs_available"] == 3
    assert repo["mrr"] == pytest.approx((1 + 0.5 + 0) / 3)
    assert repo["recall"] == pytest.approx((1 + 1 + 0) / 3)
    assert repo["by_kind"]["subject"] == {"pairs": 2, "mrr": pytest.approx(0.5), "recall": pytest.approx(0.5)}
    assert repo["by_kind"]["subject_and_body"] == {"pairs": 1, "mrr": pytest.approx(0.5),
                                                   "recall": pytest.approx(1.0)}


def test_from_history_json_shape(history_index, capsys):
    result = _run_json(capsys, "--repo", "repo", "--limit", "2")
    assert set(result) == {"collection", "profile", "mode", "k", "repositories", "not_measured",
                           "estimated_cost_usd", "ok"}
    assert result["collection"] == common.COLLECTION_NAME
    assert result["mode"] == "vector" and result["k"] == 10 and result["ok"] is True
    assert result["not_measured"] == [] and result["estimated_cost_usd"] is None  # a local profile
    (repo,) = result["repositories"]
    assert set(repo) == {"repo", "pairs", "pairs_available", "limit", "questions", "mrr", "recall",
                         "by_kind", "refused", "previous"}
    assert repo["pairs"] == 2 and repo["pairs_available"] == 3 and repo["limit"] == 2
    assert repo["previous"] is None


def test_from_history_runs_neither_the_self_check_nor_the_golden_set(history_index, capsys):
    """Only the commit messages are embedded: the self-check would search
    points' own text, and the series would mix."""
    _run_json(capsys)
    assert sorted(history_index) == sorted([Q_ALPHA, Q_BETA, Q_GAMMA])


def test_from_history_is_recorded_as_a_series_of_its_own(history_index, capsys):
    _run_json(capsys)
    (record,) = logdb.read_recent(common.LOG_DIR, "history_checks", collection=common.COLLECTION_NAME, limit=5)
    assert record["repo"] == "repo" and record["mode"] == "vector" and record["k"] == 10
    assert record["pairs"] == 3 and record["mrr"] == pytest.approx(0.5)
    assert record["by_kind"]["subject_and_body"]["pairs"] == 1
    # Nothing in the self-check and golden-set series: their rates never mix with this one.
    assert logdb.read_since(common.LOG_DIR, "quality_checks", days=1) == []
    assert stats.load_quality_window(30) == []
    assert stats.load_state()["last_quality_check"] is None


def test_a_second_run_shows_the_previous_one_on_the_same_questions(history_index, capsys):
    _run_json(capsys)
    again = _run_json(capsys)
    previous = again["repositories"][0]["previous"]
    assert previous["mrr"] == pytest.approx(0.5) and previous["same_questions"] is True
    assert previous["timestamp"]
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector"])
    out = capsys.readouterr().out
    assert "MRR@10" in out and "recall@10" in out and "previous" in out


def test_a_previous_run_with_another_limit_is_not_compared(history_index, capsys):
    _run_json(capsys, "--limit", "2")
    assert _run_json(capsys)["repositories"][0]["previous"] is None


def test_the_text_report_names_each_kind(history_index, capsys):
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector"])
    out = capsys.readouterr().out
    assert "subject only" in out and "subject and body" in out
    assert "0.500" in out  # overall and per-kind MRR@10 of this history


def test_an_empty_history_says_what_to_do(git_repo, monkeypatch, capsys):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_unit((0, 1.0)) for _ in texts])
    common.index_documents([{"id": "other:code:a.py:0", "content": "a",
                             "metadata": {"source_type": "code", "repo": "repo", "file_path": "a.py",
                                          "chunk_index": 0}}])
    common.release_lock()
    _register(git_repo.path)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector"])
    assert exc.value.code == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any("no commit" in line for line in lines)
    assert "`griot index all --repo repo`" in lines[-1]
    assert logdb.read_recent(common.LOG_DIR, "history_checks", limit=5) == [], "nothing measured, nothing recorded"


def test_a_repository_with_nothing_indexed_says_what_to_do(git_repo, monkeypatch, capsys):
    git_repo.commit("Add the parser", filename="parser.py")
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_unit((0, 1.0)) for _ in texts])
    common.index_documents([{"id": "other:code:a.py:0", "content": "a",
                             "metadata": {"source_type": "code", "repo": "other", "file_path": "a.py",
                                          "chunk_index": 0}}])
    common.release_lock()
    _register(git_repo.path)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--mode", "vector", "--json"])
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["repositories"] == []
    (missing,) = result["not_measured"]
    assert missing["repo"] == "repo"
    assert "nothing is indexed as code" in missing["reason"]
    assert missing["reason"].rstrip().endswith("`griot index code --repo repo`")


def test_a_registered_path_that_is_not_a_repository_here_is_named(history_index, tmp_path, capsys):
    gone = tmp_path / "moved-away"
    common.REPOS_JSON_PATH.write_text(json.dumps([str(gone), *json.loads(common.REPOS_JSON_PATH.read_text())]))
    result = _run_json(capsys)
    assert [r["repo"] for r in result["repositories"]] == ["repo"], "the other repositories are still measured"
    (missing,) = result["not_measured"]
    assert missing["repo"] == "moved-away" and "not a git repository" in missing["reason"]
    assert missing["reason"].endswith("`griot repos list`")


def test_an_unregistered_repository_name_is_refused(history_index, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--repo", "nope", "--json"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nope" in captured.err and "`griot repos list`" in captured.err


@pytest.mark.parametrize("flag", [["--repo", "repo"], ["--limit", "3"], ["--mode", "vector"]])
def test_the_history_flags_need_from_history(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        quality_check.main(flag)
    assert exc.value.code == 2
    assert "--from-history" in capsys.readouterr().err


def test_a_paid_profile_says_the_estimated_cost_before_searching_and_goes_on(history_index, monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "backend": "openai_compatible",
                                                   "price_per_1m_tokens": 0.02})
    result = _run_json(capsys)
    chars = len(Q_ALPHA) + len(Q_BETA) + len(Q_GAMMA)
    expected = chars / common.CHARS_PER_TOKEN_ESTIMATE / 1_000_000 * 0.02
    assert result["estimated_cost_usd"] == pytest.approx(expected)
    assert result["repositories"][0]["pairs"] == 3, "the estimate is said, and the measurement still runs"
    _run_json(capsys)  # the estimate goes to stderr, so --json stdout stays one payload
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector"])
    err = capsys.readouterr().err
    assert "3 commit messages" in err and "estimated" in err


def test_keyword_mode_on_a_paid_profile_costs_nothing(history_index, monkeypatch, capsys):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "backend": "openai_compatible",
                                                   "price_per_1m_tokens": 0.02})
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "keyword", "--json"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["estimated_cost_usd"] is None
    assert "estimated" not in captured.err
    assert history_index == [], "keyword search embeds nothing"


def test_a_question_the_search_refuses_counts_as_not_found_and_is_said(history_index, git_repo, capsys):
    git_repo.commit("the and of", filename="alpha.py", content="changed")  # no word keyword search keeps
    result = _run_json(capsys, "--mode", "keyword")
    (repo,) = result["repositories"]
    assert repo["pairs"] == 4 and repo["refused"] == 1


def test_the_default_mode_is_what_readers_get(history_index, capsys):
    capsys.readouterr()
    quality_check.main(["--from-history", "--json"])
    assert json.loads(capsys.readouterr().out)["mode"] == common.SEARCH_DEFAULT_MODE == "hybrid"


def test_a_mode_that_needs_keyword_vectors_is_refused_before_anything_is_embedded(history_index, monkeypatch,
                                                                                    capsys):
    monkeypatch.setattr(common, "keyword_search_status", lambda collection: False)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--json"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "griot index keywords" in captured.err
    assert captured.err.rstrip().endswith("`griot quality-check --from-history --mode vector`")
    assert history_index == []


def test_from_history_measures_only_the_active_collection(capsys):
    with pytest.raises(SystemExit) as exc:
        quality_check.main(["--from-history", "--collection", "codebase__another"])
    assert exc.value.code == 2
    assert "--profile" in capsys.readouterr().err


def test_only_files_indexed_as_code_are_answers(monkeypatch):
    """The search is of code only: a path that only another kind of point
    carries could never be found by it."""
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_unit((0, 1.0)) for _ in texts])
    common.index_documents([
        {"id": "repo:code:a.py:0", "content": "a",
         "metadata": {"source_type": "code", "repo": "repo", "file_path": "a.py", "chunk_index": 0}},
        {"id": "repo:merge_request:1:0", "content": "b",
         "metadata": {"source_type": "merge_request", "repo": "repo", "file_path": "b.py", "chunk_index": 0}},
        {"id": "other:code:c.py:0", "content": "c",
         "metadata": {"source_type": "code", "repo": "other", "file_path": "c.py", "chunk_index": 0}},
    ])
    assert retrieval_eval.indexed_code_files(common.get_client(), "repo") == {"a.py"}


def test_a_local_profile_says_nothing_about_cost(history_index, capsys):
    capsys.readouterr()
    quality_check.main(["--from-history", "--mode", "vector"])
    assert "Embedding" not in capsys.readouterr().err


def test_the_cost_estimate_is_shared_with_the_index_preview(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {**common.ACTIVE_PROFILE, "price_per_1m_tokens": 0.20})
    assert common.estimated_embedding_cost_usd(3_500_000) == pytest.approx(0.20)
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {k: v for k, v in common.ACTIVE_PROFILE.items()
                                                   if k != "price_per_1m_tokens"})
    assert common.estimated_embedding_cost_usd(3_500_000) is None
