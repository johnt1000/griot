"""Objects common.py builds on first use and then shares across the threads
of one process: the local embedding model and the log file handler. Sync MCP
tools run in worker threads, so two first uses can arrive at the same moment;
each object must still be built once, and a build that failed must not stand
in for a later one that can succeed."""

import logging
import threading

import pytest

from griot import common

# How long the first build waits for a second one to start alongside it. A
# second build that is not prevented starts at once; this only bounds how long
# a correct run waits before going on.
_OVERLAP_WINDOW = 1.0


def _first_use_in_two_threads(call, entered: threading.Event, other_is_calling: threading.Event):
    """Runs `call` in two threads, the second started while the first is inside
    the build. Returns what each got."""
    results, errors = [None, None], []

    def run(i):
        try:
            if i == 1:
                other_is_calling.set()
            results[i] = call()
        except Exception as e:  # pragma: no cover - surfaced by the assertion below
            errors.append(e)

    first = threading.Thread(target=run, args=(0,))
    first.start()
    assert entered.wait(5), "the first build never started"
    second = threading.Thread(target=run, args=(1,))
    second.start()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors, errors
    return results


def test_two_threads_using_the_model_for_the_first_time_build_it_once(monkeypatch, tmp_path):
    built = []
    entered, second_entered, other_is_calling = threading.Event(), threading.Event(), threading.Event()

    class SlowModel:
        def __init__(self, **kwargs):
            built.append(self)
            if len(built) == 1:
                entered.set()
                # Stay inside the build until the other thread has called in,
                # then give it the chance to start a build of its own.
                other_is_calling.wait(5)
                second_entered.wait(_OVERLAP_WINDOW)
            else:
                second_entered.set()

    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "_text_embedding_class", lambda: SlowModel)

    first, second = _first_use_in_two_threads(common.get_embed_model, entered, other_is_calling)

    assert len(built) == 1
    assert first is second is built[0]
    assert common._embed_model is built[0]


def test_a_failed_build_is_not_kept_and_the_next_call_builds_again(monkeypatch, tmp_path):
    attempts = []

    class FlakyModel:
        def __init__(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise OSError("download interrupted")

    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "_text_embedding_class", lambda: FlakyModel)

    with pytest.raises(OSError, match="download interrupted"):
        common.get_embed_model()
    assert common._embed_model is None

    model = common.get_embed_model()

    assert isinstance(model, FlakyModel) and len(attempts) == 2
    assert common.get_embed_model() is model and len(attempts) == 2


def test_a_thread_waiting_on_a_build_that_fails_builds_it_itself(monkeypatch, tmp_path):
    """A thread that called in during a build that fails is not handed the
    failure nor left with nothing: it finds no model and builds one."""
    attempts = []
    entered, other_is_calling = threading.Event(), threading.Event()

    class FailsFirst:
        def __init__(self, **kwargs):
            attempts.append(self)
            if len(attempts) == 1:
                entered.set()
                other_is_calling.wait(5)
                raise OSError("first build failed")

    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "_text_embedding_class", lambda: FailsFirst)

    outcomes = [None, None]

    def run(i):
        if i == 1:
            other_is_calling.set()
        try:
            outcomes[i] = common.get_embed_model()
        except OSError as e:
            outcomes[i] = e

    first = threading.Thread(target=run, args=(0,))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=run, args=(1,))
    second.start()
    first.join(10)
    second.join(10)

    assert isinstance(outcomes[0], OSError)
    assert isinstance(outcomes[1], FailsFirst)
    assert common._embed_model is outcomes[1] and len(attempts) == 2


def test_two_threads_logging_for_the_first_time_attach_one_file_handler(monkeypatch, tmp_path):
    """Two handlers on the logger would write every later line of the process
    twice to griot.log."""
    made = []
    entered, second_entered, other_is_calling = threading.Event(), threading.Event(), threading.Event()

    class SlowFileHandler(logging.Handler):
        def __init__(self, path):
            super().__init__()
            path.touch()
            made.append(self)
            if len(made) == 1:
                entered.set()
                other_is_calling.wait(5)
                second_entered.wait(_OVERLAP_WINDOW)
            else:
                second_entered.set()

        def emit(self, record):
            pass

    monkeypatch.setattr(common._logger, "handlers", [])
    monkeypatch.setattr(common, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(common.logging, "FileHandler", SlowFileHandler)

    _first_use_in_two_threads(common._ensure_log_handler, entered, other_is_calling)

    assert len(made) == 1
    assert common._logger.handlers == made
