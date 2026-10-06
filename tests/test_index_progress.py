"""Progress of an indexing run, as the run itself records it.

`griot_index_repo` starts `griot index all` in a subprocess (the server's
stdout is the protocol, so the run cannot print into it) and returns at
once. The run records where it is to a small file the job's host named in
GRIOT_INDEX_PROGRESS; the host reads it back for griot_index_status and
griot_index_wait. These tests cover the writer (common.index_documents and
the `index all` loop in cli.py), the reader, and the job registry that
carries the file (jobs.py). The MCP side is in tests/test_mcp_index_progress.py.
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from griot import cli, common, jobs


def _docs(n: int) -> list[dict]:
    return [
        {"id": f"repo:code:file{i}.py:0", "content": f"content {i}",
         "metadata": {"source_type": "code", "repo": "repo", "file_path": f"file{i}.py", "chunk_index": 0}}
        for i in range(n)
    ]


def _fake_embed(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [[0.1] * common.EMBED_DIM for _ in texts])


@pytest.fixture
def progress_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "progress.json"
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, str(path))
    return path


def _sources(path: Path) -> dict:
    progress = common.read_index_progress(path)
    assert progress is not None, path.read_text() if path.exists() else "no file"
    return {entry["source"]: entry for entry in progress["sources"]}


# --- the writer ---------------------------------------------------------------


def test_begin_lists_every_source_as_pending(progress_file):
    common.progress_begin(["code", "commits"])

    progress = common.read_index_progress(progress_file)
    assert progress["current_source"] is None
    assert [entry["source"] for entry in progress["sources"]] == ["code", "commits"]
    assert all(entry["state"] == "pending" for entry in progress["sources"])
    assert all(entry["chunks_total"] is None and entry["chunks_done"] is None for entry in progress["sources"])


def test_the_file_is_private(progress_file):
    common.progress_begin(["code"])
    assert stat.S_IMODE(os.stat(progress_file).st_mode) == 0o600


def test_index_documents_records_chunks_of_the_current_source(progress_file, monkeypatch):
    _fake_embed(monkeypatch)
    common.progress_begin(["code", "commits"])
    common.progress_source("code", "reading")

    common.index_documents(_docs(5))

    entry = _sources(progress_file)["code"]
    assert entry["state"] == "embedding"
    assert (entry["chunks_total"], entry["chunks_done"]) == (5, 5)
    assert (entry["indexed"], entry["skipped"], entry["failed"]) == (5, 0, 0)
    assert common.read_index_progress(progress_file)["current_source"] == "code"
    assert _sources(progress_file)["commits"]["state"] == "pending"


def test_index_documents_counts_skipped_chunks_on_a_rerun(progress_file, monkeypatch):
    _fake_embed(monkeypatch)
    common.progress_begin(["code"])
    common.progress_source("code", "reading")
    common.index_documents(_docs(3))
    common.index_documents(_docs(3))

    entry = _sources(progress_file)["code"]
    assert (entry["chunks_done"], entry["indexed"], entry["skipped"]) == (3, 0, 3)


def test_progress_inside_a_run_is_throttled_but_the_last_batch_is_always_written(progress_file, monkeypatch):
    """A rerun over an unchanged repository skips batch after batch in
    milliseconds; a write per batch would be most of the work. Within the
    interval only the end of the run is written, and it is never lost."""
    _fake_embed(monkeypatch)
    monkeypatch.setattr(common, "INDEX_BATCH_SIZE", 1)
    monkeypatch.setattr(common, "_progress_clock", lambda: 100.0)  # time stands still: every batch is inside the interval
    writes = []
    real_write = common.secure_write_text_atomic
    monkeypatch.setattr(common, "secure_write_text_atomic",
                        lambda path, text: (writes.append(json.loads(text)), real_write(path, text)))
    common.progress_begin(["code"])
    common.progress_source("code", "reading")
    writes.clear()

    common.index_documents(_docs(6))

    # The first batch (the source's total becomes known) and the last one.
    assert len(writes) == 2
    assert writes[-1]["sources"][0]["chunks_done"] == 6


def test_progress_is_written_again_once_the_interval_has_passed(progress_file, monkeypatch):
    _fake_embed(monkeypatch)
    monkeypatch.setattr(common, "INDEX_BATCH_SIZE", 1)
    clock = iter(range(0, 10_000, 10))  # every reading is 10 s after the last
    monkeypatch.setattr(common, "_progress_clock", lambda: float(next(clock)))
    writes = []
    real_write = common.secure_write_text_atomic
    monkeypatch.setattr(common, "secure_write_text_atomic",
                        lambda path, text: (writes.append(json.loads(text)), real_write(path, text)))
    common.progress_begin(["code"])
    common.progress_source("code", "reading")
    writes.clear()

    common.index_documents(_docs(4))

    assert [w["sources"][0]["chunks_done"] for w in writes] == [1, 2, 3, 4]


def test_nothing_is_written_without_the_variable(tmp_path, monkeypatch):
    """A run started from a terminal has no host to read progress: no file."""
    _fake_embed(monkeypatch)
    monkeypatch.delenv(common.INDEX_PROGRESS_ENV, raising=False)
    writes = []
    monkeypatch.setattr(common, "secure_write_text_atomic", lambda path, text: writes.append(path))
    common.progress_begin(["code"])
    common.progress_source("code", "reading")
    common.index_documents(_docs(2))
    assert writes == []


def test_a_progress_file_that_cannot_be_written_never_stops_the_run(tmp_path, monkeypatch):
    _fake_embed(monkeypatch)
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, str(tmp_path / "missing-dir" / "progress.json"))
    common.progress_begin(["code"])
    common.progress_source("code", "reading")
    assert common.index_documents(_docs(2)) == (2, 0, 0)


def test_index_all_marks_each_source_as_it_goes(progress_file, monkeypatch):
    seen = []

    def fake_run(source, rest):
        seen.append((source, _sources(progress_file)[source]["state"]))
        return 0

    monkeypatch.setattr(cli, "_run_index_source", fake_run)
    assert cli.main(["index", "all", "--sources", "code,commits"]) == 0

    assert seen == [("code", "reading"), ("commits", "reading")]
    assert [entry["state"] for entry in common.read_index_progress(progress_file)["sources"]] == ["done", "done"]


def test_index_all_marks_the_source_that_failed_and_leaves_the_rest_pending(progress_file, monkeypatch):
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: 0 if source == "code" else 3)
    assert cli.main(["index", "all", "--sources", "code,commits,tags"]) == 3

    states = {source: entry["state"] for source, entry in _sources(progress_file).items()}
    assert states == {"code": "done", "commits": "failed", "tags": "pending"}


def test_index_all_marks_a_source_that_raised_as_failed(progress_file, monkeypatch):
    def boom(source, rest):
        raise RuntimeError("the embedding API is down")

    monkeypatch.setattr(cli, "_run_index_source", boom)
    assert cli.main(["index", "all", "--sources", "code"]) == 1
    assert _sources(progress_file)["code"]["state"] == "failed"


def test_index_all_marks_a_source_that_exited_as_failed(progress_file, monkeypatch):
    def exits(source, rest):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_run_index_source", exits)
    assert cli.main(["index", "all", "--sources", "code"]) == 2
    assert _sources(progress_file)["code"]["state"] == "failed"


# --- the reader ---------------------------------------------------------------


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", '{"sources": "code"}', '{"sources": 5}',
                                     '{"sources": [{"source": 1, "state": "done"}]}',
                                     '{"sources": [{"source": "code"}]}',
                                     '{"sources": [{"source": "code", "state": 3}]}',
                                     '{"sources": [{"source": "code", "state": "sleeping"}]}',
                                     '{"sources": [], "current_source": 5}'])
def test_a_missing_or_malformed_file_reads_as_no_progress(tmp_path, content):
    """Read while the child may be anywhere (not started writing, killed):
    anything that is not a whole record of ours is no progress, never an
    exception in the tool that asked."""
    path = tmp_path / "progress.json"
    if content is not None:
        path.write_text(content)
    assert common.read_index_progress(path) is None


def test_counts_that_are_not_counts_read_as_unknown(tmp_path):
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"current_source": "code", "updated_at": 5.0, "sources": [
        {"source": "code", "state": "embedding", "chunks_total": "many", "chunks_done": True,
         "indexed": 3, "skipped": None, "failed": -1}]}))
    entry = common.read_index_progress(path)["sources"][0]
    assert (entry["chunks_total"], entry["chunks_done"], entry["indexed"], entry["skipped"], entry["failed"]) == \
        (None, None, 3, None, None)


# --- the job registry ---------------------------------------------------------


class _FakeProc:
    def __init__(self, pid=4242, exit_after=None, returncode=0):
        self.pid = pid
        self._polls = 0
        self._exit_after = exit_after
        self._returncode = returncode
        self.returncode = None

    def poll(self):
        self._polls += 1
        if self._exit_after is not None and self._polls > self._exit_after:
            self.returncode = self._returncode
        return self.returncode


def _start(monkeypatch, tmp_path, proc, env=None):
    from conftest import GitRepo
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path))
    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        if cmd[0] == "git":
            return real_popen(cmd, **kwargs)
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    result = jobs.start_index_job(str(repo.path), ["code", "commits"], release=lambda: None, env=env)
    return result, captured


def test_the_child_is_told_where_to_write_its_progress(monkeypatch, tmp_path):
    result, kwargs = _start(monkeypatch, tmp_path, _FakeProc())

    assert result["started"] is True
    progress_path = Path(kwargs["env"][common.INDEX_PROGRESS_ENV])
    assert progress_path.exists()
    assert stat.S_IMODE(os.stat(progress_path).st_mode) == 0o600
    # The rest of the environment is what it would have been: the host's.
    assert kwargs["env"]["GRIOT_DATA_DIR"] == os.environ["GRIOT_DATA_DIR"]


def test_an_environment_given_by_the_caller_is_kept(monkeypatch, tmp_path):
    _, kwargs = _start(monkeypatch, tmp_path, _FakeProc(), env={"ONLY": "this"})
    assert set(kwargs["env"]) == {"ONLY", common.INDEX_PROGRESS_ENV}


def test_a_child_that_died_at_once_leaves_no_progress_file(monkeypatch, tmp_path):
    proc = _FakeProc(exit_after=0, returncode=1)
    result, kwargs = _start(monkeypatch, tmp_path, proc)
    assert result["started"] is False
    assert not Path(kwargs["env"][common.INDEX_PROGRESS_ENV]).exists()


def test_the_report_carries_the_running_jobs_progress(monkeypatch, tmp_path):
    _, kwargs = _start(monkeypatch, tmp_path, _FakeProc())
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, kwargs["env"][common.INDEX_PROGRESS_ENV])
    common.progress_begin(["code", "commits"])
    common.progress_source("code", "done")

    report = jobs.index_job_report()

    assert report["finished"] is None
    running = report["running"]
    assert running["pid"] == 4242 and running["sources"] == ["code", "commits"]
    assert running["progress"]["sources"][0]["state"] == "done"


def test_a_job_that_ended_is_reported_once_as_finished_with_its_last_progress(monkeypatch, tmp_path):
    proc = _FakeProc(exit_after=1, returncode=0)  # alive at the liveness check, gone after
    _, kwargs = _start(monkeypatch, tmp_path, proc)
    progress_path = Path(kwargs["env"][common.INDEX_PROGRESS_ENV])
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, str(progress_path))
    common.progress_begin(["code", "commits"])
    common.progress_source("code", "done")
    common.progress_source("commits", "done")

    report = jobs.index_job_report()

    assert report["running"] is None
    finished = report["finished"]
    assert finished["pid"] == 4242 and finished["exit_code"] == 0
    assert [entry["state"] for entry in finished["progress"]["sources"]] == ["done", "done"]
    assert finished["finished_at"] >= finished["started_at"]
    # Its file is gone: the snapshot is what remains.
    assert not progress_path.exists()
    assert jobs.index_job_report()["finished"] == finished


def test_a_finished_job_frees_the_way_for_the_next(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path, _FakeProc(pid=1, exit_after=1))
    assert jobs.running_index_job() is None
    assert jobs.index_job_refusal(None) is None


def test_only_the_most_recent_finished_job_is_kept(monkeypatch, tmp_path):
    _start(monkeypatch, tmp_path / "a", _FakeProc(pid=1, exit_after=1, returncode=1))
    jobs.index_job_report()
    _start(monkeypatch, tmp_path / "b", _FakeProc(pid=2, exit_after=1, returncode=0))

    report = jobs.index_job_report()

    assert report["finished"]["pid"] == 2
    assert list(jobs._registry) == [2]
