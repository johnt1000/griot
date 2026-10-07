"""The registry of indexing jobs outlives the MCP server that started them.

griot_index_repo starts `griot index all` as a detached subprocess, which
keeps running when the server exits (a client restart, a crash). Before
this, the job's record lived only in the server's memory: the next server
said nothing was running, offered to start a second run, and could never
say how the first one ended. The record is now written to a private file in
the data directory, and reloaded on the first read of a new server: a job
whose process is still alive, and is the same process (its start time,
since a pid is reused), is followed again; an ended one is reported only
from the result its run recorded in its progress file.

Real child processes here (a tiny script standing in for `griot index all`,
which would load an embedding model), because liveness, pid reuse and a
zombie left by a parent that forgot its child are exactly what a fake
cannot show.
"""

import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import psutil
import pytest
from mcp.client.client import Client

from griot import cli, common, jobs, mcp_server

# Stands in for `griot index all`: writes a first progress record, waits for
# the test to say how to end (the content of the go file), records that
# result the way the real run does at its end (unless told "silent"), and
# exits with it.
_CHILD = r"""
import json, os, sys, time, pathlib
progress = pathlib.Path(os.environ["GRIOT_INDEX_PROGRESS"])
progress.write_text(json.dumps({"current_source": "code", "updated_at": time.time(),
                                "sources": [{"source": "code", "state": "reading"}]}))
go = pathlib.Path(sys.argv[1])
while not go.exists() or not go.read_text():
    time.sleep(0.02)
word = go.read_text()
code = 0 if word == "silent" else int(word)
if word != "silent":
    progress.write_text(json.dumps({"current_source": "code", "updated_at": time.time(), "ended": True, "exit_code": code,
                                    "sources": [{"source": "code", "state": "done" if code == 0 else "failed"}]}))
sys.exit(code)
"""


class _Children:
    """Starts jobs through start_index_job with the tiny child, and makes
    sure none outlives the test."""

    def __init__(self, monkeypatch, tmp_path):
        self.tmp_path = tmp_path
        self.script = tmp_path / "child.py"
        self.script.write_text(_CHILD)
        self.procs = []
        self.go_files = {}
        real_popen = subprocess.Popen
        children = self

        def popen(cmd, **kwargs):
            if "griot.cli" not in cmd:
                return real_popen(cmd, **kwargs)
            go = children.tmp_path / f"go-{len(children.procs)}"
            proc = real_popen([sys.executable, str(children.script), str(go)], **kwargs)
            children.procs.append(proc)
            children.go_files[proc.pid] = go
            return proc

        monkeypatch.setattr(jobs.subprocess, "Popen", popen)
        monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    def start(self, sources=("code",)) -> int:
        result = jobs.start_index_job(None, list(sources), release=lambda: None)
        assert result["started"], result
        return result["pid"]

    def end(self, pid, word="0") -> None:
        self.go_files[pid].write_text(word)

    def wait_gone(self, pid, *, reap=True) -> None:
        """Waits for the child to exit. reap=False leaves it a zombie, as it
        is when the server that spawned it is still this process but has
        forgotten it (the simulated restart)."""
        proc = next(p for p in self.procs if p.pid == pid)
        if reap:
            proc.wait(timeout=10)
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    return
            except psutil.NoSuchProcess:
                return
            time.sleep(0.02)
        raise AssertionError(f"child {pid} did not exit")

    def cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def children(monkeypatch, tmp_path):
    # Progress files are made in the test's own data directory: nothing of
    # this test's is left in the system's temporary directory.
    kids = _Children(monkeypatch, tmp_path)
    try:
        yield kids
    finally:
        kids.cleanup()


def _restart(monkeypatch):
    """A new server process as far as this module is concerned: nothing in
    memory, nothing loaded yet."""
    monkeypatch.setattr(jobs, "_registry", {})
    monkeypatch.setattr(jobs, "_loaded", False)


def _jobs_file() -> Path:
    return common.DATA_DIR / ".index_jobs.json"


def _records() -> list[dict]:
    return json.loads(_jobs_file().read_text())["jobs"]


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


# --- what is written ----------------------------------------------------------


def test_a_started_job_is_written_to_a_private_file(children):
    pid = children.start(["code", "commits"])

    assert stat.S_IMODE(_jobs_file().stat().st_mode) == 0o600
    [record] = _records()
    assert record["pid"] == pid
    assert abs(record["process_start_time"] - psutil.Process(pid).create_time()) < 0.01
    assert record["sources"] == ["code", "commits"]
    assert record["path"] is None
    assert record["progress_path"] == jobs._registry[pid]["progress_path"]
    assert record["log_path"] == str(common.LOG_DIR / "griot_index.log")
    assert record["finished"] is None


# --- a restart while the job runs ---------------------------------------------


def test_a_running_job_survives_a_restart(children, monkeypatch):
    pid = children.start(["code", "commits"])
    started_at = jobs._registry[pid]["started_at"]
    _wait_until(lambda: common.read_index_progress(jobs._registry[pid]["progress_path"]) is not None)
    _restart(monkeypatch)

    running = jobs.index_job_report()["running"]

    assert running["pid"] == pid
    # Taken back, not added a second time beside the record it came from.
    assert [record["pid"] for record in _records()] == [pid]
    assert running["sources"] == ["code", "commits"]
    assert running["started_at"] == started_at
    assert running["progress"]["current_source"] == "code"


def test_the_one_job_rule_holds_after_a_restart(children, monkeypatch):
    children.start()
    _restart(monkeypatch)

    assert jobs.index_job_refusal(None) is not None
    assert jobs.start_index_job(None, ["code"], release=lambda: None)["started"] is False


def test_after_a_restart_the_end_is_read_from_what_the_run_recorded(children, monkeypatch):
    pid = children.start()
    _restart(monkeypatch)
    assert jobs.index_job_report()["running"]["pid"] == pid
    progress_path = jobs._registry[pid]["progress_path"]

    children.end(pid, "3")
    children.wait_gone(pid, reap=False)  # this server is not its parent any more: nobody reaps it
    report = jobs.index_job_report()

    assert report["running"] is None
    assert report["finished"]["pid"] == pid
    assert report["finished"]["exit_code"] == 3
    assert report["finished"]["progress"]["sources"][0]["state"] == "failed"
    assert not Path(progress_path).exists()
    # And the next server still knows how it ended.
    _restart(monkeypatch)
    assert jobs.index_job_report()["finished"]["exit_code"] == 3


def test_a_job_that_ended_while_no_server_was_up_reports_its_recorded_result(children, monkeypatch):
    pid = children.start()
    progress_path = jobs._registry[pid]["progress_path"]
    children.end(pid, "0")
    children.wait_gone(pid)
    _restart(monkeypatch)

    report = jobs.index_job_report()

    assert report["running"] is None
    assert report["finished"]["pid"] == pid and report["finished"]["exit_code"] == 0
    assert not Path(progress_path).exists()


def test_a_job_that_died_without_a_result_is_dropped(children, monkeypatch):
    pid = children.start()
    progress_path = jobs._registry[pid]["progress_path"]
    os.kill(pid, signal.SIGKILL)
    children.wait_gone(pid)
    _restart(monkeypatch)

    report = jobs.index_job_report()

    assert report == {"running": None, "finished": None}
    assert not Path(progress_path).exists()
    assert _records() == []


def test_a_finished_job_noticed_before_the_restart_is_still_reported(children, monkeypatch):
    pid = children.start()
    children.end(pid, "0")
    children.wait_gone(pid)
    before = jobs.index_job_report()["finished"]
    assert before["exit_code"] == 0
    _restart(monkeypatch)

    assert jobs.index_job_report()["finished"] == before


def test_a_job_started_after_a_restart_replaces_the_finished_one(children, monkeypatch):
    first = children.start()
    children.end(first, "0")
    children.wait_gone(first)
    _restart(monkeypatch)
    second = children.start()

    assert [record["pid"] for record in _records() if record["finished"] is None] == [second]
    children.end(second, "0")
    children.wait_gone(second)
    assert jobs.index_job_report()["finished"]["pid"] == second
    assert [record["pid"] for record in _records()] == [second]


# --- what is not trusted ------------------------------------------------------


def _write_jobs_file(records) -> None:
    common.secure_mkdir(common.DATA_DIR)
    _jobs_file().write_text(json.dumps({"jobs": records}))


def _record(owner, start_time, progress, **overrides):
    return {"pid": owner, "process_start_time": start_time, "started_at": time.time() - 5, "path": None,
            "sources": ["code"], "progress_path": str(progress), "log_path": "/Users/you/griot_index.log",
            "finished": None, **overrides}


def _temp_progress_file(content=None) -> Path:
    progress_dir = common.DATA_DIR / jobs.PROGRESS_DIR_NAME
    common.secure_mkdir(progress_dir)
    fd, name = tempfile.mkstemp(prefix="griot-index-progress-", suffix=".json", dir=progress_dir)
    os.close(fd)
    if content is not None:
        Path(name).write_text(json.dumps(content))
    return Path(name)


@pytest.mark.parametrize("shift", [100.0, -100.0])
def test_a_reused_pid_is_not_mistaken_for_the_job(shift):
    """This very process is alive, but it is not the process the record
    names: its start time differs."""
    me = psutil.Process(os.getpid())
    progress = _temp_progress_file()
    try:
        _write_jobs_file([_record(me.pid, me.create_time() + shift, progress)])

        assert jobs.index_job_report() == {"running": None, "finished": None}
        assert jobs.index_job_refusal(None) is None
        assert _records() == []
    finally:
        progress.unlink(missing_ok=True)


def test_the_same_process_within_the_tolerance_is_the_job():
    me = psutil.Process(os.getpid())
    progress = _temp_progress_file()
    try:
        _write_jobs_file([_record(me.pid, me.create_time() + 0.5, progress)])

        assert jobs.index_job_report()["running"]["pid"] == me.pid
    finally:
        progress.unlink(missing_ok=True)


def test_a_record_without_a_start_time_is_never_followed_again():
    """No start time (it could not be read when the job started): nothing
    tells the job from a stranger that got its pid, so it is not followed."""
    me = psutil.Process(os.getpid())
    progress = _temp_progress_file()
    try:
        _write_jobs_file([_record(me.pid, None, progress)])

        assert jobs.index_job_report()["running"] is None
    finally:
        progress.unlink(missing_ok=True)


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"jobs": 5}', '{"jobs": [5]}'])
def test_a_damaged_jobs_file_is_no_job(content):
    common.secure_mkdir(common.DATA_DIR)
    _jobs_file().write_text(content)

    assert jobs.index_job_report() == {"running": None, "finished": None}
    assert jobs.index_job_refusal(None) is None


_FINISHED = {"finished_at": 1.0, "exit_code": 0, "progress": None}


@pytest.mark.parametrize("state", ["running", "finished"])
@pytest.mark.parametrize("field, value", [
    ("pid", "12"), ("pid", True), ("pid", 0), ("pid", -3),
    ("process_start_time", "yesterday"), ("started_at", None), ("started_at", True),
    ("path", 5), ("sources", "code"), ("sources", [1]), ("progress_path", None), ("log_path", 3),
    ("finished", "yes"),
    ("finished", {**_FINISHED, "exit_code": "0"}), ("finished", {**_FINISHED, "exit_code": False}),
    ("finished", {**_FINISHED, "finished_at": "now"}), ("finished", {**_FINISHED, "finished_at": True}),
    ("finished", {**_FINISHED, "progress": {"sources": "code"}})])
def test_a_record_with_a_field_of_the_wrong_kind_is_dropped(state, field, value):
    """Each record is whole as written, then one field is spoilt. Both a
    running record (this very process, alive) and a finished one (shown
    without any liveness check) would be reported if the field passed."""
    me = psutil.Process(os.getpid())
    progress = _temp_progress_file()
    try:
        whole = _record(me.pid, me.create_time(), progress, **({"finished": _FINISHED} if state == "finished" else {}))
        _write_jobs_file([whole])
        assert jobs.index_job_report()[state]["pid"] == me.pid  # the record as written is taken
        _restart_in_place()

        _write_jobs_file([{**whole, field: value}])

        assert jobs.index_job_report() == {"running": None, "finished": None}
    finally:
        progress.unlink(missing_ok=True)


def _restart_in_place():
    jobs._registry.clear()
    jobs._loaded = False


def test_only_the_last_finished_job_is_taken_back():
    newer = _record(11, None, _temp_progress_file(), finished={**_FINISHED, "finished_at": 200.0})
    older = _record(12, None, _temp_progress_file(), finished={**_FINISHED, "finished_at": 100.0})
    try:
        _write_jobs_file([newer, older])

        assert jobs.index_job_report()["finished"]["pid"] == 11
        assert [record["pid"] for record in _records()] == [11]
    finally:
        for record in (newer, older):
            Path(record["progress_path"]).unlink(missing_ok=True)


def test_a_save_does_not_keep_another_servers_finished_job(children, monkeypatch):
    """Only another server's RUNNING job is kept when this one writes: a
    finished one is not followed by anyone, and only the last is kept."""
    # A process that is alive and the same, so that only its being finished
    # keeps it out.
    me = psutil.Process(os.getpid())
    other_finished = _record(me.pid, me.create_time(), _temp_progress_file(), finished=_FINISHED)
    try:
        monkeypatch.setattr(jobs, "_loaded", True)
        _write_jobs_file([other_finished])

        mine = children.start()

        assert [record["pid"] for record in _records()] == [mine]
    finally:
        Path(other_finished["progress_path"]).unlink(missing_ok=True)


@pytest.mark.parametrize("where", ["elsewhere", "wrong_name"])
def test_a_progress_path_that_is_not_one_of_ours_is_never_removed(tmp_path, where):
    """The file is only written by griot, but it is read as data: a record
    pointing at any other file must not make the server delete that file."""
    if where == "elsewhere":
        victim = tmp_path / "griot-index-progress-x.json"
    else:
        fd, name = tempfile.mkstemp(prefix="precious-", suffix=".json")
        os.close(fd)
        victim = Path(name)
    # A whole record of an ended run: were the file taken for ours, the job
    # would be reported as finished from it, then the file removed.
    victim.write_text(json.dumps({"sources": [], "ended": True, "exit_code": 0}))
    try:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _write_jobs_file([_record(dead.pid, time.time() - 60, victim)])

        assert jobs.index_job_report() == {"running": None, "finished": None}
        assert victim.exists()
        # And the removal checks on its own, whatever reached it.
        jobs._remove_progress_file(str(victim))
        assert victim.exists()
    finally:
        victim.unlink(missing_ok=True)


def _aged(path: Path, seconds: float) -> Path:
    when = time.time() - seconds
    os.utime(path, (when, when))
    return path


def test_progress_files_nobody_can_reclaim_are_removed_when_a_server_loads():
    """A progress file no record names (the record could not be written, or
    the host died between making the file and recording the job) is never
    removed by anything else. The first read of a new server removes it,
    once it is old enough that no server can still be starting its job."""
    me = psutil.Process(os.getpid())
    progress_dir = common.DATA_DIR / jobs.PROGRESS_DIR_NAME
    orphan = _aged(_temp_progress_file(), jobs.ORPHAN_PROGRESS_GRACE_SECONDS + 60)
    just_made = _temp_progress_file()  # a job another server is starting right now
    followed = _aged(_temp_progress_file(), jobs.ORPHAN_PROGRESS_GRACE_SECONDS + 60)
    foreign = progress_dir / "notes.txt"
    foreign.write_text("not ours")
    _aged(foreign, jobs.ORPHAN_PROGRESS_GRACE_SECONDS + 60)
    _write_jobs_file([_record(me.pid, me.create_time(), followed)])

    assert jobs.index_job_report()["running"]["pid"] == me.pid

    assert not orphan.exists()
    assert just_made.exists() and followed.exists() and foreign.exists()


def test_progress_files_are_swept_even_without_a_jobs_file():
    orphan = _aged(_temp_progress_file(), jobs.ORPHAN_PROGRESS_GRACE_SECONDS + 60)
    assert not _jobs_file().exists()

    assert jobs.index_job_report() == {"running": None, "finished": None}

    assert not orphan.exists()


def test_a_record_from_before_the_move_still_has_its_temp_dir_file_removed():
    """Earlier versions made the progress file in the system temporary
    directory: a job recorded by one of them is still followed, and its file
    removed once it is found ended."""
    fd, name = tempfile.mkstemp(prefix="griot-index-progress-", suffix=".json")
    os.close(fd)
    legacy = Path(name)
    legacy.write_text(json.dumps({"sources": [{"source": "code", "state": "done"}], "ended": True, "exit_code": 0}))
    try:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _write_jobs_file([_record(dead.pid, time.time() - 60, legacy)])

        assert jobs.index_job_report()["finished"]["exit_code"] == 0
        assert not legacy.exists()
    finally:
        legacy.unlink(missing_ok=True)


def test_a_server_that_saves_keeps_another_servers_running_job(children, monkeypatch):
    """Two servers share the data directory. One that loaded before the
    other started a job must not erase that job's record when it writes."""
    other = children.start()
    other_record = _records()[0]
    _restart(monkeypatch)
    monkeypatch.setattr(jobs, "_loaded", True)  # this server loaded before the other started
    monkeypatch.setattr(jobs, "index_job_refusal", lambda path, **kw: None)

    mine = children.start()

    pids = {record["pid"] for record in _records()}
    assert pids == {other, mine}
    assert other_record in _records()


# --- the run records how it ended --------------------------------------------


@pytest.fixture
def progress_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "progress.json"
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, str(path))
    return path


def test_the_run_records_its_exit_code_at_the_end(progress_file, monkeypatch):
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: 0 if source == "code" else 3)
    assert cli.main(["index", "all", "--sources", "code,commits"]) == 3

    assert common.read_index_progress(progress_file)["exit_code"] == 3


def test_a_successful_run_records_zero(progress_file, monkeypatch):
    monkeypatch.setattr(cli, "_run_index_source", lambda source, rest: 0)
    assert cli.main(["index", "all", "--sources", "code"]) == 0

    progress = common.read_index_progress(progress_file)
    assert progress["exit_code"] == 0 and progress["ended"] is True


def test_a_run_stopped_by_a_busy_collection_records_what_it_exits_with(progress_file, monkeypatch):
    def raise_busy(source, rest):
        raise common.CollectionBusyError("griot_test", Path("/Users/you/qdrant"), "held")

    monkeypatch.setattr(cli, "_run_index_source", raise_busy)
    monkeypatch.setattr(common, "find_collection_holders", lambda path: [])
    assert cli.main(["index", "all", "--sources", "code"]) == 1

    assert common.read_index_progress(progress_file)["exit_code"] == 1


def test_a_run_interrupted_records_the_signal_python_exits_with(progress_file, monkeypatch):
    def interrupted(source, rest):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_index_source", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["index", "all", "--sources", "code"])

    assert common.read_index_progress(progress_file)["exit_code"] == -signal.SIGINT


def test_a_run_that_crashed_records_one(progress_file, monkeypatch):
    monkeypatch.setattr(common, "progress_source", lambda source, state: (_ for _ in ()).throw(ValueError("bug")))
    common.progress_begin(["code"])
    with pytest.raises(ValueError):
        cli.main(["index", "all", "--sources", "code"])

    assert common.read_index_progress(progress_file)["exit_code"] == 1


@pytest.mark.parametrize("code, recorded", [(4, 4), (None, 0), ("a message", 1)])
def test_a_run_that_exited_records_the_status_python_exits_with(progress_file, monkeypatch, code, recorded):
    """SystemExit escaping the run (the loop only catches it around a
    source): its code, as the interpreter turns it into an exit status."""
    def exits(source, state):
        raise SystemExit(code)

    monkeypatch.setattr(common, "progress_source", exits)
    common.progress_begin(["code"])
    with pytest.raises(SystemExit):
        cli.main(["index", "all", "--sources", "code"])

    assert common.read_index_progress(progress_file)["exit_code"] == recorded


def test_a_run_still_going_has_not_ended(progress_file):
    common.progress_begin(["code"])

    progress = common.read_index_progress(progress_file)
    assert progress["ended"] is False and progress["exit_code"] is None


@pytest.mark.parametrize("record, ended, exit_code", [
    ({"ended": True, "exit_code": 0}, True, 0), ({"ended": True, "exit_code": -9}, True, -9),
    ({"ended": True, "exit_code": True}, True, None), ({"ended": True, "exit_code": "0"}, True, None),
    ({"ended": True, "exit_code": None}, True, None), ({"exit_code": 0}, False, None),
    ({"ended": "yes", "exit_code": 0}, False, None), ({"ended": 1, "exit_code": 0}, False, None)])
def test_an_end_that_is_not_one_reads_as_unknown(tmp_path, record, ended, exit_code):
    """Only a run that says it ended has an exit code: one without the
    marker is still going, whatever else its file holds."""
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"sources": [], **record}))

    progress = common.read_index_progress(path)
    assert (progress["ended"], progress["exit_code"]) == (ended, exit_code)


def test_reading_a_record_again_reads_the_same():
    """A finished job's progress is kept in the jobs file and read back
    after a restart: parsing what was parsed must change nothing."""
    data = {"current_source": "code", "updated_at": 5.0, "ended": True, "exit_code": 2,
            "sources": [{"source": "code", "state": "failed", "chunks_total": 4}]}
    once = common.index_progress_from(data)
    assert common.index_progress_from(once) == once
    running = common.index_progress_from({"sources": []})
    assert common.index_progress_from(running) == running


def test_another_command_records_nothing(progress_file, monkeypatch):
    """Only an indexing run has a record to end; with none begun, nothing
    is written, even with the variable set."""
    monkeypatch.setattr(common, "_progress", None)
    cli.main(["repos", "list"])

    assert not progress_file.exists()


# --- through the protocol, after a restart ------------------------------------


@pytest.fixture
def server_with_index(monkeypatch):
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    importlib.reload(mcp_server)
    try:
        yield
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX", raising=False)
        importlib.reload(mcp_server)


@pytest.mark.anyio
async def test_status_and_wait_follow_a_job_across_a_server_restart(children, monkeypatch, server_with_index):
    pid = children.start(["code"])
    # The restart: a new server module, a registry that starts empty.
    _restart(monkeypatch)
    importlib.reload(mcp_server)

    async with Client(mcp_server.mcp) as client:
        status = await client.call_tool("griot_index_status", {})
        assert not status.is_error, status.content
        assert status.structured_content["job"]["pid"] == pid
        assert status.structured_content["running"] is True

        children.end(pid, "0")
        waited = await client.call_tool("griot_index_wait", {"timeout_seconds": 20})

    assert not waited.is_error, waited.content
    content = waited.structured_content
    assert content["running"] is False
    assert content["finished"]["pid"] == pid
    assert content["finished"]["exit_code"] == 0
