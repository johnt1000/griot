"""Tests for quality_check.py against a real embedded Qdrant Edge (same
pattern as tests/test_indexing.py: real common.index_documents()/
common.get_client(), only embed_texts() is replaced with a deterministic
fake — running the real fastembed per test would be too expensive/slow).
The fake here is deterministic BY CONTENT (hash -> seed), not a fixed
vector: identical text always produces the SAME vector (self-recovery
with score ~1.0, which is what run_self_check() actually tests), different
texts produce different vectors (avoids the trivial false positive of
"anything matches anything" that a fake like [0.1]*dim would give)."""
import hashlib
import json
import random

import pytest
import qdrant_edge as qe

from griot import common, logdb, quality_check, stats


def _content_vector(text: str, dim: int) -> list[float]:
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32)
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _fake_embed(monkeypatch):
    def fake(texts, **kwargs):
        return [_content_vector(t, common.EMBED_DIM) for t in texts]
    monkeypatch.setattr(common, "embed_texts", fake)


def _docs(n: int) -> list[dict]:
    return [
        {
            "id": f"repo:code:file{i}.py:0",
            "content": f"unique content of file number {i} with enough text to differentiate it from the rest",
            "metadata": {"source_type": "code", "repo": "repo", "file_path": f"file{i}.py", "chunk_index": 0},
        }
        for i in range(n)
    ]


# --- run_self_check ---------------------------------------------------------

def test_run_self_check_high_score_for_self_match(monkeypatch):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(10))
    common.release_lock()  # atexit only releases at the end of the process

    result = quality_check.run_self_check(common.COLLECTION_NAME, sample_size=10, min_score=0.90)

    assert result["sampled"] == 10
    assert result["failed"] == 0
    assert result["passed"] == 10
    assert result["failures"] == []
    # same text -> same deterministic vector -> self-match cosine ~1.0
    assert result["avg_score"] > 0.99


def test_run_self_check_reports_failure_for_low_score(monkeypatch):
    """Simulates the real scenario this check exists to catch (wrong
    dimension, model swapped without reindexing, etc: the point itself still
    shows up in the search, but with a score too low to be a genuine
    self-recovery)."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(5))
    common.release_lock()

    class _CorruptedScore:
        def __init__(self, id, score, payload):
            self.id = id
            self.score = score
            self.payload = payload

    real_search = common.search

    def flaky_search(query, limit=5):
        # qe.ScoredPoint doesn't allow assigning .score directly (read-only
        # pyo3 object) — replaces the first result (the self-match, since
        # the query vector is identical to the indexed point's) with an
        # equivalent object with a deliberately low score.
        results = real_search(query, limit=limit)
        if not results:
            return results
        first = results[0]
        corrupted = _CorruptedScore(id=first.id, score=0.0, payload=first.payload)
        return [corrupted, *results[1:]]

    monkeypatch.setattr(common, "search", flaky_search)

    result = quality_check.run_self_check(common.COLLECTION_NAME, sample_size=5, min_score=0.90)

    assert result["failed"] >= 1
    assert result["passed"] < result["sampled"]
    assert any("score too low" in f["reason"] for f in result["failures"])


def test_run_self_check_on_nonexistent_collection_is_empty_not_error():
    """Non-active collection that was never indexed — sample_points has
    nowhere to read from (_open_shard_for_sampling returns None);
    run_self_check must not propagate an exception, just report 'nothing
    sampled'."""
    result = quality_check.run_self_check("codebase__never-indexed", sample_size=10)
    assert result == {"sampled": 0, "passed": 0, "failed": 0, "failures": [], "avg_score": None}


def test_run_self_check_counts_a_blank_sample_as_failed(monkeypatch):
    """[debt 10] A sampled point with no text used to be skipped AND counted
    in `passed`, so a collection whose samples were all blank read as
    healthy. It is a failure with its own reason: a point with nothing to
    retrieve it by is exactly the kind of pipeline breakage this check is
    for. It is not embedded: there is nothing to search with."""
    _fake_embed(monkeypatch)
    docs = _docs(3)
    docs[0]["content"] = "   "
    common.index_documents(docs)
    common.release_lock()
    searched = []
    real_search = common.search
    monkeypatch.setattr(common, "search", lambda q, limit=5: searched.append(q) or real_search(q, limit=limit))

    result = quality_check.run_self_check(common.COLLECTION_NAME, sample_size=3, min_score=0.90)

    assert result["sampled"] == 3
    assert result["failed"] == 1
    assert result["passed"] == 2
    [failure] = result["failures"]
    assert failure["id"] == common.stable_id(docs[0]["id"])
    assert failure["repo"] == "repo"
    assert "no text" in failure["reason"]
    assert len(searched) == 2, "a blank sample has nothing to search with and must not be embedded"


def _all_blank_collection(monkeypatch, n=3):
    _fake_embed(monkeypatch)
    docs = _docs(n)
    for doc in docs:
        doc["content"] = " \n "
    common.index_documents(docs)
    common.release_lock()


def test_a_collection_of_blank_samples_does_not_pass_the_gate(monkeypatch, capsys):
    """[debt 10] The reading that matters: every sample blank must not end
    in "Quality OK." and exit 0."""
    _all_blank_collection(monkeypatch)
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc_info:
        quality_check.main(["--sample-size", "3", "--skip-golden-set"])

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Quality OK." not in out
    assert "0/3 samples self-recovered" in out
    assert "no text" in out


def test_a_collection_of_blank_samples_records_a_zero_pass_rate(monkeypatch, capsys):
    """[debt 10] The trend griot stats shows must read 0, not 100%."""
    _all_blank_collection(monkeypatch)

    with pytest.raises(SystemExit):
        quality_check.main(["--sample-size", "3", "--skip-golden-set", "--json"])

    trend = stats.load_quality_window(30)
    assert [t["pass_rate"] for t in trend] == [0.0]
    capsys.readouterr()
    # And it is shown: a 0.0 rate is falsy, and a reader that tested it for
    # truth would drop the line and read as "never checked".
    stats.main(["--days", "30"])
    assert "Quality:     0% of sampled points self-retrieved" in capsys.readouterr().out


# --- sample_points -----------------------------------------------------------

def test_sample_points_is_deterministic_and_spaced(monkeypatch):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(20))
    common.release_lock()
    client = common.get_client()

    first = quality_check.sample_points(client, 5)
    second = quality_check.sample_points(client, 5)

    assert len(first) == 5
    # same call again (no writes in between) -> the exact same sample,
    # deterministically reproducible (see sample_points docstring)
    assert [p.id for p in first] == [p.id for p in second]
    # spaced out: no point repeated in the sample
    assert len({p.id for p in first}) == 5


def test_sample_points_on_empty_collection_returns_empty_list():
    client = common.get_client()
    assert quality_check.sample_points(client, 10) == []


# --- main() -------------------------------------------------------------------

def test_main_exits_with_error_on_nonexistent_collection(capsys):
    with pytest.raises(SystemExit) as exc_info:
        quality_check.main(["--collection", "codebase__this-does-not-exist"])
    assert exc_info.value.code == 1
    assert "does not exist" in capsys.readouterr().out


def test_main_happy_path_self_check_only(monkeypatch, capsys):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(5))
    common.release_lock()

    quality_check.main(["--sample-size", "5", "--skip-golden-set"])

    out = capsys.readouterr().out
    assert "Quality OK." in out


# --- --json (jobs.run_quality_check_job() calls this as a ------------------
# --- subprocess and parses stdout as JSON — human-readable prints must -----
# --- NEVER mix into stdout in this mode, or the parent's json.loads() ------
# --- would choke on the first non-JSON line) --------------------------------


def test_json_mode_stdout_is_valid_json_only(monkeypatch, capsys):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(5))
    common.release_lock()
    capsys.readouterr()  # drain index_documents()'s own setup prints — not part of what's under test

    quality_check.main(["--sample-size", "5", "--skip-golden-set", "--json"])

    out = capsys.readouterr().out
    payload = json.loads(out)  # raises if anything else got mixed into stdout
    assert payload["ok"] is True
    assert payload["collection"] == common.COLLECTION_NAME
    assert payload["self_check"]["sampled"] == 5
    assert payload["golden_check"] is None


def test_json_mode_exit_code_1_still_has_valid_json_on_stdout(monkeypatch, capsys):
    """[jobs.py contract] exit 1 + valid JSON on stdout = a RESULT (the
    self-check found failures), not an execution error — jobs.py's caller
    must be able to tell these apart from stdout content alone."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))
    common.release_lock()
    capsys.readouterr()  # drain index_documents()'s own setup prints
    # force a failure: min-score higher than any real self-match can reach
    with pytest.raises(SystemExit) as exc_info:
        quality_check.main(["--sample-size", "3", "--skip-golden-set", "--json", "--min-score", "999"])

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["self_check"]["failed"] > 0


def test_json_mode_nonexistent_collection_error_goes_to_stderr_not_stdout(capsys):
    with pytest.raises(SystemExit) as exc_info:
        quality_check.main(["--collection", "codebase__this-does-not-exist", "--json"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err


def test_json_mode_respects_skip_golden_set(monkeypatch, capsys):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(2))
    common.release_lock()
    capsys.readouterr()  # drain index_documents()'s own setup prints

    quality_check.main(["--sample-size", "2", "--skip-golden-set", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["golden_check"] is None


def test_json_mode_still_logs_to_griot_log(monkeypatch, capsys):
    """echo=False on the log_and_print() call must not mean 'don't log at
    all' — the audit trail (griot.log) has to survive --json the same way
    it does for every other command."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(2))
    common.release_lock()

    quality_check.main(["--sample-size", "2", "--skip-golden-set", "--json"])

    assert "quality_check:" in (common.LOG_DIR / "griot.log").read_text()


# --- quality-check history (regression introduced by removing the web UI) ---
# ui/service.py's _persist_last_quality_check() was the ONLY caller of
# logdb.write_quality_check(). Deleting that front end deleted the
# writer with it, leaving griot stats' quality trend reading a table nothing
# fills. The CLI is the surviving surface, so the CLI records it.


def test_quality_check_records_its_result_for_the_trend(monkeypatch, capsys):
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))
    common.release_lock()

    quality_check.main(["--sample-size", "3", "--skip-golden-set"])

    records = logdb.read_since(common.LOG_DIR, "quality_checks", days=1)
    assert len(records) == 1
    assert records[0]["collection"] == common.COLLECTION_NAME
    assert records[0]["self_check"]["sampled"] == 3


def test_recorded_quality_check_reaches_the_stats_trend(monkeypatch, capsys):
    """End-to-end: what the CLI writes must be the shape stats reads. The
    two halves lived in different modules and only the deleted UI ever
    connected them."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))
    common.release_lock()

    quality_check.main(["--sample-size", "3", "--skip-golden-set"])

    trend = stats.load_quality_window(30)
    assert len(trend) == 1 and trend[0]["pass_rate"] is not None


def test_quality_check_caps_the_failures_it_stores(monkeypatch, capsys):
    """Same cap the deleted UI applied: a self-check over a large corpus
    with a high failure rate would otherwise store a multi-MB blob for what
    is meant to be a summary."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(3))
    common.release_lock()
    many = [{"id": str(i), "repo": "r", "reason": "low score"} for i in range(50)]
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda *a, **kw: {"sampled": 50, "passed": 0, "failed": 50,
                                          "failures": many, "avg_score": 0.1})

    # 50 failures means the check FAILED, and main() signals that with
    # SystemExit(1) — the record must still have been written before it.
    with pytest.raises(SystemExit):
        quality_check.main(["--sample-size", "50", "--skip-golden-set"])

    stored = logdb.read_since(common.LOG_DIR, "quality_checks", days=1)[0]
    assert len(stored["self_check"]["failures"]) == quality_check.MAX_STORED_FAILURES
    assert stored["self_check"]["failures_truncated"] is True


def test_quality_check_still_reports_if_recording_fails(monkeypatch, capsys):
    """Recording is observability, not the job — a storage failure must not
    fail the check the user asked for."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(2))
    common.release_lock()
    monkeypatch.setattr(logdb, "write_quality_check",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    quality_check.main(["--sample-size", "2", "--skip-golden-set"])

    # Reached the end and printed its verdict instead of dying on the
    # storage error (main() returns None on a passing check).
    assert "Quality OK." in capsys.readouterr().out


def test_non_json_mode_output_unchanged_by_the_json_flags_existence(monkeypatch, capsys):
    """Regression guard: adding --json must not change a single byte of
    the default (non-json) output path."""
    _fake_embed(monkeypatch)
    common.index_documents(_docs(5))
    common.release_lock()

    quality_check.main(["--sample-size", "5", "--skip-golden-set"])

    out = capsys.readouterr().out
    assert "Quality OK." in out
    assert "{" not in out.split("\n")[0]  # not accidentally JSON-first-line


class _Hit:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


def test_golden_set_counts_a_case_with_no_real_constraint_as_failed(monkeypatch):
    """[review finding] add_case() guards the tool and CLI write paths, but
    cmd_suggest() writes straight through _save(), the file is edited by hand
    as a documented workflow, and files curated before the guard existed are
    still on disk. Read-time is the only point that covers all of them.

    Counted as FAILED rather than skipped, deliberately: the damage a vacuous
    case does is to `griot quality-check`'s exit code — `ok` requires
    golden_check["failed"] == 0, so a case that cannot fail makes the command
    exit 0 having measured nothing, and it is used as a gate. Skipping would
    leave that gate open; failing closes it and names the case to fix."""
    monkeypatch.setattr(quality_check.common, "search",
                        lambda query, limit=5: [_Hit(0.9, {"repo": "alpha", "source_type": "code"})])
    # The search is faked, so the index is too: `alpha` is there.
    monkeypatch.setattr(quality_check.common, "get_client", lambda: None)
    monkeypatch.setattr(quality_check.common, "repository_is_indexed", lambda client, repo: True)

    result = quality_check.run_golden_set([
        {"query": "vacuous", "must_include": [{"bogus": None}]},
        {"query": "real", "must_include": [{"repo": "alpha"}]},
    ])

    assert result["passed"] == 1
    assert result["failed"] == 1
    vacuous = next(c for c in result["cases"] if c["query"] == "vacuous")
    assert vacuous["passed"] is False
    assert "constrain" in vacuous["reason"]
