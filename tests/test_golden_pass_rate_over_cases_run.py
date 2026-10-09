"""The golden-set pass rate counts only the cases that ran.

A keyword or hybrid case on a collection without keyword vectors is skipped:
nothing about search got worse. With `total` counting it, a run of 8 passing
cases and 2 skipped read "8 of 10 passed", a dip in the trend for a reason
that is not quality. `total` is now the cases that ran (passed + failed), and
skipped cases are reported apart: "8 of 8 passed, 2 skipped".

Records written before this change keep their stored `total`; one written
between case modes and this change counted skipped cases in it, so the
reader derives what ran from passed + failed, which every stored record has."""

import json

import pytest
from mcp.client.client import Client

from griot import common, logdb, mcp_server, quality_check, stats
from test_golden_set_modes import KEYWORD_CASE, _write
from test_keyword_search import fake_embeddings, legacy_index  # noqa: F401
from test_stats_state import SELF

VECTOR_CASE = {"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha"}]}
MISSING_CASE = {"query": "acquire_lock", "limit": 5, "must_include": [{"repo": "alpha", "file_path": "nowhere.py"}]}
STATUS = {"points_count": 5, "embed_profile": "any", "spend_ceiling_exceeded": False, "last_indexed": None}


def test_total_is_the_cases_that_ran_and_skipped_ones_are_apart(legacy_index):
    result = quality_check.run_golden_set([VECTOR_CASE, KEYWORD_CASE, {**KEYWORD_CASE, "mode": "hybrid"}])
    assert (result["total"], result["passed"], result["failed"], result["skipped"]) == (1, 1, 0, 2)
    assert len(result["cases"]) == 3, "the skipped cases are still listed, each with its reason"


def test_a_failed_case_counts_in_total_and_a_skipped_one_does_not(legacy_index):
    result = quality_check.run_golden_set([VECTOR_CASE, MISSING_CASE, KEYWORD_CASE])
    assert (result["total"], result["passed"], result["failed"], result["skipped"]) == (2, 1, 1, 1)


def test_the_summary_reads_passed_of_what_ran_and_skipped_apart(legacy_index):
    result = quality_check.run_golden_set([VECTOR_CASE, KEYWORD_CASE])
    assert quality_check.golden_summary(result) == \
        "1/1 golden set questions passed (searched: 1 vector; 1 skipped)"


def test_the_trend_record_and_stats_read_what_ran(legacy_index):
    _write([VECTOR_CASE, KEYWORD_CASE, {**KEYWORD_CASE, "mode": "hybrid"}])
    common.release_client()
    quality_check.main(["--sample-size", "2"])
    stored = logdb.read_latest(common.LOG_DIR, "quality_checks")["golden_check"]
    assert (stored["total"], stored["passed"], stored["skipped"]) == (1, 1, 2)
    state = stats.load_state()
    assert (state["last_golden_check"]["passed"], state["last_golden_check"]["total"],
            state["last_golden_check"]["skipped"]) == (1, 1, 2)
    result = stats.compute_stats([], [], STATUS, state=state)
    assert (result["golden_set"]["last_passed"], result["golden_set"]["last_total"],
            result["golden_set"]["last_skipped"]) == (1, 1, 2)
    assert "last run: 1 of 1 passed, 2 skipped" in stats.format_stats(result, 7)


def test_a_record_whose_total_counted_skipped_cases_is_read_as_what_ran():
    """Written between case modes and this change: total = passed + failed + skipped."""
    quality_check.record_for_trend(common.COLLECTION_NAME, SELF,
                                    {"total": 10, "passed": 7, "failed": 1, "skipped": 2, "cases": []})
    last = stats.load_state()["last_golden_check"]
    assert (last["passed"], last["total"], last["skipped"]) == (7, 8, 2)


def test_a_record_from_before_case_modes_keeps_its_total():
    """No `skipped` then, and nothing could be skipped: total was what ran."""
    quality_check.record_for_trend(common.COLLECTION_NAME, SELF, {"total": 6, "passed": 4, "failed": 2, "cases": []})
    last = stats.load_state()["last_golden_check"]
    assert (last["passed"], last["total"], last["skipped"]) == (4, 6, 0)


@pytest.mark.parametrize("counts", [{"passed": 3}, {"passed": None, "failed": 0}, {"passed": 3, "failed": None}])
def test_a_record_without_both_counts_keeps_its_total(counts):
    """Not one griot ever wrote, but logs.db is a file: a record missing a
    count is read for what it says, never as zero cases run or a crash."""
    quality_check.record_for_trend(common.COLLECTION_NAME, SELF, {"total": 3, **counts, "cases": []})
    assert stats.load_state()["last_golden_check"]["total"] == 3


def test_the_gate_still_closes_on_a_failure_beside_skipped_cases(legacy_index, capsys):
    _write([MISSING_CASE, KEYWORD_CASE])
    common.release_client()
    with pytest.raises(SystemExit) as exit_:
        quality_check.main(["--json", "--sample-size", "2"])
    assert exit_.value.code == 1
    golden = json.loads(capsys.readouterr().out)["golden_check"]
    assert (golden["total"], golden["failed"], golden["skipped"]) == (1, 1, 1)


def test_the_gate_stays_open_when_every_case_was_skipped(legacy_index, capsys):
    _write([KEYWORD_CASE])
    common.release_client()
    quality_check.main(["--sample-size", "2"])  # exit 0: nothing failed
    out = capsys.readouterr().out
    assert "0/0 golden set questions passed" in out and "1 golden set case(s) were not run" in out


@pytest.mark.anyio
async def test_the_mcp_tools_count_what_ran(legacy_index):
    _write([VECTOR_CASE, KEYWORD_CASE, {**KEYWORD_CASE, "mode": "hybrid"}])
    common.release_client()
    async with Client(mcp_server.mcp) as client:
        checked = await client.call_tool("griot_quality_check", {"sample_size": 2})
        assert checked.is_error is False, checked.content
        golden = checked.structured_content["golden_check"]
        assert (golden["total"], golden["passed"], golden["failed"], golden["skipped"]) == (1, 1, 0, 2)
        stated = await client.call_tool("griot_stats", {})
    assert stated.is_error is False, stated.content
    state = stated.structured_content["golden_set"]
    assert (state["last_passed"], state["last_total"], state["last_skipped"]) == (1, 1, 2)
