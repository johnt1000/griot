"""Progress of an indexing run, seen through the MCP protocol.

griot_index_status shows the job this server started and how far it got;
griot_index_wait (registered with griot_index_repo) blocks for a bounded time
and sends `notifications/progress` while it does. Everything here goes
through a real in-memory client, on both protocol revisions: what an agent
receives is the schema-validated structured content, which a direct call of
the tool function does not exercise.
"""

import importlib
import json
import time

import anyio

import pytest
from mcp.client.client import Client

from griot import common, jobs, mcp_server

MODES = ["legacy", "2026-07-28"]


class _FakeProc:
    def __init__(self, pid=777, exit_after=None, returncode=0):
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


def _register_job(monkeypatch, tmp_path, proc, sources=("code", "commits")) -> None:
    """A job as start_index_job leaves it, without spawning anything, and its
    progress written the way the child writes it."""
    progress_path = tmp_path / "progress.json"
    progress_path.touch(mode=0o600)
    jobs._registry[proc.pid] = {"path": "/Users/you/repo", "sources": list(sources),
                                "started_at": time.time() - 12, "proc": proc,
                                "progress_path": str(progress_path), "finished": None}
    monkeypatch.setenv(common.INDEX_PROGRESS_ENV, str(progress_path))
    common.progress_begin(list(sources))


@pytest.fixture
def lock_idle(monkeypatch):
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})


@pytest.fixture
def server_with_index(monkeypatch, tmp_path):
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    importlib.reload(mcp_server)
    try:
        yield mcp_server
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX", raising=False)
        importlib.reload(mcp_server)


# --- griot_index_status -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_status_has_no_job_when_nothing_was_started(mode, lock_idle):
    async with Client(mcp_server.mcp, mode=mode) as client:
        result = await client.call_tool("griot_index_status", {})

    assert not result.is_error, result.content
    assert result.structured_content["job"] is None
    assert result.structured_content["running"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_status_shows_the_running_jobs_progress(mode, monkeypatch, tmp_path, lock_idle):
    _register_job(monkeypatch, tmp_path, _FakeProc())
    common.progress_source("code", "done")
    common.progress_source("commits", "reading")

    async with Client(mcp_server.mcp, mode=mode) as client:
        result = await client.call_tool("griot_index_status", {})

    assert not result.is_error, result.content
    job = result.structured_content["job"]
    assert job["pid"] == 777 and job["path"] == "/Users/you/repo" and job["sources"] == ["code", "commits"]
    assert job["elapsed_seconds"] >= 12
    assert job["progress"]["current_source"] == "commits"
    assert [s["state"] for s in job["progress"]["sources"]] == ["done", "reading"]
    assert job["progress"]["seconds_since_update"] >= 0


@pytest.mark.anyio
async def test_status_says_running_while_the_job_reads_the_repository_without_the_lock(monkeypatch, tmp_path, lock_idle):
    """The lock is taken per source, around the embedding only: while the run
    lists files or reads the git log nothing holds it, and `running` used to
    read false for a run that was very much alive."""
    _register_job(monkeypatch, tmp_path, _FakeProc())

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content

    assert status["running"] is True
    assert status["pid"] == 777
    assert status["path"] == "/Users/you/repo"


@pytest.mark.anyio
async def test_status_keeps_the_lock_holder_when_there_is_one(monkeypatch, tmp_path):
    """A run from a terminal holds the lock; what the lock says wins over the
    server's own job, which may be waiting on it."""
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": True, "pid": 55, "path": "/Users/you/other"})
    _register_job(monkeypatch, tmp_path, _FakeProc())

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content

    assert (status["running"], status["pid"], status["path"]) == (True, 55, "/Users/you/other")
    assert status["job"]["pid"] == 777


@pytest.mark.anyio
async def test_status_with_a_job_whose_child_has_not_written_yet(monkeypatch, tmp_path, lock_idle):
    _register_job(monkeypatch, tmp_path, _FakeProc())
    (tmp_path / "progress.json").write_text("")

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {})

    assert not result.is_error, result.content
    assert result.structured_content["job"]["progress"] is None


# --- griot_index_wait ---------------------------------------------------------


@pytest.mark.anyio
async def test_the_wait_tool_is_absent_unless_indexing_is_enabled():
    async with Client(mcp_server.mcp) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "griot_index_wait" not in names


@pytest.mark.anyio
async def test_the_wait_tool_is_read_only(server_with_index):
    async with Client(server_with_index.mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert tools["griot_index_wait"].annotations.read_only_hint is True


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_wait_sends_progress_and_returns_when_the_job_ends(mode, server_with_index, monkeypatch, tmp_path, lock_idle):
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_POLL_SECONDS", 0.01)
    proc = _FakeProc(exit_after=3)
    _register_job(monkeypatch, tmp_path, proc)
    common.progress_source("code", "done")
    common.progress_source("commits", "reading")
    common._progress_chunks(total=100, done=50, indexed=50, skipped=0, failed=0, final=True)

    received = []

    async def on_progress(progress, total, message):
        received.append((progress, total, message))

    async with Client(server_with_index.mcp, mode=mode) as client:
        result = await client.call_tool("griot_index_wait", {"timeout_seconds": 30}, progress_callback=on_progress)

    assert not result.is_error, result.content
    out = result.structured_content
    assert out["running"] is False and out["job"] is None
    assert out["finished"]["pid"] == 777 and out["finished"]["exit_code"] == 0
    # One source done and half of the next, out of two; then the end.
    assert received[0][:2] == (1.5, 2)
    assert "commits" in received[0][2] and "50/100" in received[0][2]
    assert received[-1][:2] == (2, 2)
    # Strictly increasing, as the protocol requires of a progress sequence.
    values = [p for p, _, _ in received]
    assert values == sorted(set(values))


@pytest.mark.anyio
async def test_wait_does_not_report_the_end_of_a_job_that_failed_as_complete(server_with_index, monkeypatch, tmp_path, lock_idle):
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_POLL_SECONDS", 0.01)
    _register_job(monkeypatch, tmp_path, _FakeProc(exit_after=2, returncode=1))
    common.progress_source("code", "failed")

    received = []

    async def on_progress(progress, total, message):
        received.append((progress, total, message))

    async with Client(server_with_index.mcp) as client:
        out = (await client.call_tool("griot_index_wait", {"timeout_seconds": 30},
                                      progress_callback=on_progress)).structured_content

    assert out["finished"]["exit_code"] == 1
    assert all(progress < total for progress, total, _ in received)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", MODES)
async def test_wait_works_for_a_client_that_asked_for_no_progress(mode, server_with_index, monkeypatch, tmp_path, lock_idle):
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_POLL_SECONDS", 0.01)
    _register_job(monkeypatch, tmp_path, _FakeProc(exit_after=2))

    async with Client(server_with_index.mcp, mode=mode) as client:
        result = await client.call_tool("griot_index_wait", {"timeout_seconds": 30})

    assert not result.is_error, result.content
    assert result.structured_content["finished"]["exit_code"] == 0


@pytest.mark.anyio
async def test_wait_returns_the_running_job_when_the_time_is_up(server_with_index, monkeypatch, tmp_path, lock_idle):
    # A poll long enough that no loaded machine takes as long to answer: a
    # wait of 0 that slept even one poll before reading its deadline takes
    # at least that, so the bound is the poll, not a fixed second.
    poll = 10.0
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_POLL_SECONDS", poll)
    _register_job(monkeypatch, tmp_path, _FakeProc())  # never ends

    # fail_after: a wait that ignored its deadline would never return here,
    # and a test that hangs does not fail, it stalls the whole suite.
    with anyio.fail_after(2 * poll):
        async with Client(server_with_index.mcp) as client:
            started = time.monotonic()
            out = (await client.call_tool("griot_index_wait", {"timeout_seconds": 0})).structured_content
            took = time.monotonic() - started

    assert took < poll, "it waited a poll after the time was up"
    assert out["running"] is True and out["job"]["pid"] == 777 and out["finished"] is None


@pytest.mark.anyio
async def test_wait_is_bounded_whatever_the_caller_asks(server_with_index, monkeypatch, tmp_path, lock_idle):
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(server_with_index, "_INDEX_WAIT_MAX_SECONDS", 0.2)
    _register_job(monkeypatch, tmp_path, _FakeProc())

    # fail_after: without the bound the call would never return, and a test
    # that hangs does not fail, it stalls the whole suite.
    with anyio.fail_after(3):
        async with Client(server_with_index.mcp) as client:
            out = (await client.call_tool("griot_index_wait", {"timeout_seconds": 100_000})).structured_content

    assert out["timeout_seconds"] == 0.2
    assert out["running"] is True


@pytest.mark.anyio
async def test_a_negative_wait_is_no_wait(server_with_index, monkeypatch, tmp_path, lock_idle):
    _register_job(monkeypatch, tmp_path, _FakeProc())
    async with Client(server_with_index.mcp) as client:
        out = (await client.call_tool("griot_index_wait", {"timeout_seconds": -5})).structured_content
    assert out["timeout_seconds"] == 0 and out["running"] is True


@pytest.mark.anyio
async def test_wait_with_no_job_returns_at_once(server_with_index, lock_idle):
    async with Client(server_with_index.mcp) as client:
        out = (await client.call_tool("griot_index_wait", {"timeout_seconds": 30})).structured_content
    assert (out["running"], out["job"], out["finished"]) == (False, None, None)
    assert out["waited_seconds"] < 2


@pytest.mark.anyio
async def test_a_run_whose_clock_is_ahead_was_updated_zero_seconds_ago(monkeypatch, tmp_path, lock_idle):
    """The run writes its own time; a record from a moment the server has not
    reached yet (clocks a little apart, a write between two reads) is fresh,
    never a negative age an agent would have to make sense of."""
    _register_job(monkeypatch, tmp_path, _FakeProc())
    record = json.loads((tmp_path / "progress.json").read_text())
    record["updated_at"] = time.time() + 60
    (tmp_path / "progress.json").write_text(json.dumps(record))

    async with Client(mcp_server.mcp) as client:
        status = (await client.call_tool("griot_index_status", {})).structured_content

    assert status["job"]["progress"]["seconds_since_update"] == 0


@pytest.mark.anyio
async def test_a_job_noticed_finished_after_now_finished_zero_seconds_ago(server_with_index, lock_idle):
    jobs._registry[888] = {"path": None, "sources": ["code"], "started_at": time.time() - 30,
                           "proc": _FakeProc(pid=888, exit_after=0), "progress_path": "/nonexistent",
                           "finished": {"finished_at": time.time() + 60, "exit_code": 0, "progress": None}}

    async with Client(server_with_index.mcp) as client:
        out = (await client.call_tool("griot_index_wait", {"timeout_seconds": 0})).structured_content

    assert out["finished"]["pid"] == 888
    assert out["finished"]["finished_seconds_ago"] == 0


def _progress(current: str | None, *entries: tuple[str, str, int | None, int | None]) -> dict:
    return {"current_source": current, "updated_at": None,
            "sources": [{"source": source, "state": state, "chunks_total": total, "chunks_done": done,
                         "indexed": None, "skipped": None, "failed": None}
                        for source, state, total, done in entries]}


def test_progress_never_counts_more_than_the_current_source():
    """A count past its total (a record read mid-write, a recount) is still
    one source at most: progress above the number of sources would tell a
    client the run is further than done."""
    progress, total, _ = mcp_server._progress_numbers(
        ["code", "commits"], _progress("code", ("code", "embedding", 100, 150), ("commits", "pending", None, None)))
    assert (progress, total) == (1.0, 2)


def test_a_finished_current_source_counts_once():
    """The current source done is in the count of done sources already; its
    chunks must not add it a second time."""
    progress, total, message = mcp_server._progress_numbers(
        ["code", "commits"], _progress("code", ("code", "done", 100, 100), ("commits", "pending", None, None)))
    assert (progress, total) == (1.0, 2)
    assert message == "1 of 2 sources done"
