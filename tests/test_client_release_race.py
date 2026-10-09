"""The handle of the active collection is opened once, and never closed
under a call that is using it or while one is opening it.

Three ways it went wrong, all in a server with more than one call at a time
(sync tools run in worker threads):

- `get_client()` itself closed and reopened the handle once the idle window
  had passed. It cannot know who is using the handle: a second call arriving
  during a long first one (a quality check, an audit) closed the collection
  under it.
- Nothing serialised opening: two calls that both found the handle closed
  both opened it, and the one that lost was never closed, so the server held
  the collection for good.
- A release that came while another call was still opening found nothing to
  close; the open then finished, and the subprocess the release was for
  (an index run, a preview) met a collection that was held.

Releasing on idleness is the reaper's job (mcp_server), which counts the
calls in flight."""

import threading
import time

import pytest

from griot import common, jobs, mcp_server


class _Handle:
    """Stands in for an open collection."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.fixture
def multi(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    monkeypatch.setattr(common, "IDLE_RELEASE_SECONDS", 30.0)


@pytest.fixture
def slow_open(monkeypatch):
    """The collection exists and takes a moment to open. `handles` are the
    opens that happened; `started` is set when the first one begins and
    `finished` when one ends. An open lasts `seconds`, unless the test sets
    `end` to finish it early: a test can make the open long enough that
    nothing correct outlasts it on a loaded machine, without every run
    sitting through it."""
    common.get_client()
    common.release_client()
    opened = type("Opened", (), {})()
    opened.handles, opened.started, opened.finished, opened.end = [], threading.Event(), threading.Event(), threading.Event()
    opened.seconds = 0.3

    def load(path, *, retry):
        handle = _Handle()
        opened.handles.append(handle)
        opened.started.set()
        opened.end.wait(opened.seconds)
        opened.finished.set()
        return handle

    monkeypatch.setattr(common, "_load_shard", load)
    monkeypatch.setattr(common, "_secure_collection_dir", lambda collection: None)
    monkeypatch.setattr(common, "ensure_collection", lambda client: None)
    return opened


def _in_threads(*functions):
    results, errors = [None] * len(functions), []

    def run(i, fn):
        try:
            results[i] = fn()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(functions)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, errors
    return results


# --- a call does not close the handle another call is using ------------------------------------


def test_a_call_that_comes_after_the_idle_window_does_not_close_the_handle(multi, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(common.time, "time", lambda: clock["now"])
    first = common.get_client()
    clock["now"] += 31.0  # a long call is still using `first`
    second = common.get_client()
    assert second is first
    assert first.info().points_count == 0, "and it still answers: it was not closed under whoever holds it"


def test_the_reaper_is_still_what_lets_go_of_an_idle_handle(multi):
    common.get_client()
    mcp_server._release_if_idle(now=common._client_last_used_at + 31)
    assert common._client is None


# --- opening is done once ---------------------------------------------------------------------


def test_two_calls_that_find_it_closed_open_it_once(multi, slow_open):
    first, second = _in_threads(common.get_client, common.get_client)
    assert len(slow_open.handles) == 1, "the second waits for the first instead of opening again"
    assert first is second is slow_open.handles[0]


def test_a_status_read_does_not_wait_behind_a_slow_open(multi, slow_open, monkeypatch):
    """`wait=False` is for callers to whom "busy" is an answer."""
    monkeypatch.setattr(common, "_STATUS_READ_PATIENCE", 0.05)
    # The defect is sitting through the open, so the bound is the open itself:
    # one long enough that a status read which waits for it returns only after
    # it has finished, while one that does not wait has the whole of it to
    # come back in, however loaded the machine. The test then ends it early.
    slow_open.seconds = 10.0
    opener = threading.Thread(target=common.get_client)
    opener.start()
    try:
        assert slow_open.started.wait(5)
        with pytest.raises(common.CollectionBusyError) as busy:
            common.get_client(wait=False)
        assert not slow_open.finished.is_set(), "the status read sat through the open"
        assert busy.value.in_this_process is True, "it is a call of this process, not another process"
    finally:
        slow_open.end.set()
        opener.join(timeout=10)
    assert len(slow_open.handles) == 1


def test_a_call_that_needs_the_index_waits_for_the_open_and_gets_the_handle(multi, slow_open):
    opener = threading.Thread(target=common.get_client)
    opener.start()
    assert slow_open.started.wait(5)
    handle = common.get_client()
    opener.join(timeout=10)
    assert handle is slow_open.handles[0] and len(slow_open.handles) == 1


# --- a release and an open do not cross -------------------------------------------------------


def test_a_release_that_comes_during_an_open_closes_what_was_opened(multi, slow_open):
    """The subprocess that the release makes room for must find the
    collection free: not held by an open that finished a moment later."""
    def release_a_moment_later():
        # Once the open has begun, not after a fixed pause: on a loaded
        # machine a pause can end before the open starts, and a release that
        # comes first has nothing to close, which is not this case.
        assert slow_open.started.wait(5)
        common.release_client()

    _in_threads(common.get_client, release_a_moment_later)
    assert common._client is None and [handle.closed for handle in slow_open.handles] == [1]


# --- the two tools that start a subprocess ask before they let go ------------------------------


def test_letting_go_for_a_subprocess_is_refused_while_another_call_uses_the_index(multi, monkeypatch):
    monkeypatch.setattr(mcp_server, "_WAIT_FOR_OTHER_CALLS_SECONDS", 0.2)
    common.get_client()
    mcp_server._tool_started()   # this call
    mcp_server._tool_started()   # another one, in flight
    try:
        reason = mcp_server._release_for_a_subprocess()
        assert reason and "another griot tool call" in reason and common._client is not None
    finally:
        mcp_server._tool_finished()
        mcp_server._tool_finished()


def test_letting_go_for_a_subprocess_closes_it_when_this_is_the_only_call(multi):
    common.get_client()
    mcp_server._tool_started()
    try:
        assert mcp_server._release_for_a_subprocess() is None and common._client is None
    finally:
        mcp_server._tool_finished()


def test_an_index_run_is_not_started_when_the_index_cannot_be_let_go(git_repo, monkeypatch):
    """It closed the collection whatever else was running: a search in
    flight lost it mid-call."""
    from griot import repos
    git_repo.commit("first", filename="a.py")
    repos.add_repo(str(git_repo.path))
    out = jobs.start_index_job(str(git_repo.path), release=lambda: "another griot tool call is using the index right now.")
    assert out["started"] is False and "another griot tool call" in out["reason"] and out["pid"] is None
    assert jobs.running_index_job() is None, "nothing was started"


def test_letting_go_for_a_subprocess_waits_for_a_call_that_is_about_to_finish(multi, monkeypatch):
    monkeypatch.setattr(mcp_server, "_WAIT_FOR_OTHER_CALLS_SECONDS", 5.0)
    common.get_client()
    mcp_server._tool_started()   # this call
    mcp_server._tool_started()   # another one, finishing soon

    def finishes_soon():
        time.sleep(0.2)
        mcp_server._tool_finished()

    threading.Thread(target=finishes_soon).start()
    try:
        assert mcp_server._release_for_a_subprocess() is None and common._client is None
    finally:
        mcp_server._tool_finished()


def test_two_calls_that_each_need_the_index_to_themselves_do_not_block_each_other_out(multi, monkeypatch):
    """Each counted the other as "another call in flight": both waited the
    whole time, and then one of them refused. The one that came first goes
    as soon as the ordinary call ends; the second waits for it."""
    monkeypatch.setattr(mcp_server, "_WAIT_FOR_OTHER_CALLS_SECONDS", 1.5)
    common.get_client()
    mcp_server._tool_started()   # an ordinary call (a search), over in a moment
    took, answers = {}, {}

    def wants_it_alone(name):
        mcp_server._tool_started()
        began = time.monotonic()
        answers[name] = mcp_server._release_for_a_subprocess()
        took[name] = time.monotonic() - began
        # It stays in flight while its subprocess runs: the test ends it below.

    first = threading.Thread(target=wants_it_alone, args=("first",))
    second = threading.Thread(target=wants_it_alone, args=("second",))
    first.start()
    time.sleep(0.1)
    second.start()
    time.sleep(0.2)
    mcp_server._tool_finished()  # the search ends
    first.join(timeout=10)
    second.join(timeout=10)
    try:
        # Sitting through the wait takes at least the whole of it (the deadline
        # is what ends it), while going at once takes about the 0.3 s until the
        # search ends: the bound is the wait itself, not a fixed second, so a
        # loaded machine has the most room before it fails a correct release.
        assert answers["first"] is None, "the first did not get the index"
        assert took["first"] < mcp_server._WAIT_FOR_OTHER_CALLS_SECONDS, "it sat through the whole wait"
        assert answers["second"] and "another griot tool call" in answers["second"], "the first is still running"
    finally:
        mcp_server._tool_finished()
        mcp_server._tool_finished()
    assert mcp_server._alone_queue == [], "nobody is left in the queue"


def test_a_call_that_gave_up_waiting_does_not_hold_the_queue(multi, monkeypatch):
    monkeypatch.setattr(mcp_server, "_WAIT_FOR_OTHER_CALLS_SECONDS", 0.2)
    common.get_client()
    mcp_server._tool_started()   # this call
    mcp_server._tool_started()   # another one, staying
    try:
        assert mcp_server._release_for_a_subprocess()
        assert mcp_server._alone_queue == []
    finally:
        mcp_server._tool_finished()
    try:
        assert mcp_server._release_for_a_subprocess() is None, "the next one is not stuck behind the one that left"
    finally:
        mcp_server._tool_finished()
