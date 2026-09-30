import multiprocessing as mp
import os
import sqlite3
import time

import pytest

from griot import common, logdb

_CONCURRENT_PROCS = 4
_CALLS_PER_PROC = 25
_CONCURRENT_COST = 0.001


def _spend_worker(config_dir, data_dir):
    """Module-level (picklable) worker for the concurrency test below —
    runs in a FRESH interpreter (spawn), so it shares no in-memory state
    with the parent, exactly like two real griot processes."""
    os.environ["GRIOT_CONFIG_DIR"] = config_dir
    os.environ["GRIOT_DATA_DIR"] = data_dir
    from griot import common as child_common
    for _ in range(_CALLS_PER_PROC):
        child_common.record_spend(_CONCURRENT_COST)


def test_record_spend_accumulates():
    common.record_spend(0.5)
    assert common.get_spend_today() == pytest.approx(0.5)
    common.record_spend(0.5)
    assert common.get_spend_today() == pytest.approx(1.0)


def test_record_spend_ignores_zero():
    """A free call has nothing to add. A NEGATIVE cost is not a free call:
    see tests/test_security_hardening.py."""
    common.record_spend(0.0)
    assert common.get_spend_today() == 0.0
    assert not common.SPEND_STATE_PATH.exists()  # doesn't even create the file


def test_daily_ceiling_blocks_once_reached(monkeypatch):
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1.0)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 999.0)  # does not interfere with this test
    common.check_spend_ceiling()  # still ok, spend = 0
    common.record_spend(1.0)  # hits the ceiling
    with pytest.raises(RuntimeError, match="Local circuit breaker"):
        common.check_spend_ceiling()


def test_velocity_ceiling_blocks_before_daily_ceiling(monkeypatch):
    """A fast burst, well below the total daily ceiling, but abnormal for a
    sequential direct call, must be blocked by the velocity ceiling."""
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1000.0)  # well out of reach
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 0.5)
    common.record_spend(0.4)  # still below both ceilings
    common.check_spend_ceiling()
    common.record_spend(0.2)  # +0.2 = 0.6 within the 5min window -> exceeds velocity
    with pytest.raises(RuntimeError, match="last"):
        common.check_spend_ceiling()


def test_velocity_window_expiry_does_not_block(monkeypatch):
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 1000.0)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 0.5)
    common.record_spend(0.6)  # above the velocity ceiling, but...

    # ...records the SAME spend as if it had happened before the window
    # opened (simulates time passing without a real sleep in the test).
    old = time.time() - common.SPEND_VELOCITY_WINDOW_SECONDS - 1
    with sqlite3.connect(common.LOG_DIR / logdb.DB_FILENAME) as conn:
        conn.execute("UPDATE spend_events SET at = ?", (old,))

    common.check_spend_ceiling()  # should not raise — event outside the window


def test_state_resets_on_new_day():
    common.record_spend(1.0)
    assert common.get_spend_today() == pytest.approx(1.0)

    with sqlite3.connect(common.LOG_DIR / logdb.DB_FILENAME) as conn:
        conn.execute("UPDATE spend_state SET date = '2000-01-01' WHERE id = 1")

    assert common.get_spend_today() == 0.0


def test_a_new_day_overwrites_the_previous_total_rather_than_adding_to_it():
    """The ceiling is per-day: yesterday's total must not carry into
    today's budget (which a plain `spend_usd + cost` upsert would do)."""
    common.record_spend(1.0)
    with sqlite3.connect(common.LOG_DIR / logdb.DB_FILENAME) as conn:
        conn.execute("UPDATE spend_state SET date = '2000-01-01' WHERE id = 1")

    common.record_spend(0.25)

    assert common.get_spend_today() == pytest.approx(0.25)


def test_spend_is_stored_in_the_shared_db_not_a_json_file():
    common.record_spend(0.1)
    assert (common.LOG_DIR / logdb.DB_FILENAME).exists()
    assert not common.SPEND_STATE_PATH.exists()  # legacy file is never recreated


def test_corrupted_state_file_is_treated_as_fresh_start():
    common.SPEND_STATE_PATH.write_text("{not valid json")
    assert common.get_spend_today() == 0.0
    common.check_spend_ceiling()  # should not raise


# --- concurrency (real bug, reproduced before the fix) --------------------
# record_spend() used to be load-JSON -> mutate in Python -> write-whole-
# file. Two griot processes recording spend at the same time (the MCP
# server and a CLI indexing run — a combination this project explicitly
# supports) therefore clobbered each other: with 6 processes, 83% of real
# spend was silently dropped. That is the dangerous direction for a
# circuit breaker — it UNDERESTIMATES spend and keeps letting paid calls
# through. Accumulation must happen inside the storage layer, not in
# Python, so concurrent writers serialize instead of overwriting.


def test_concurrent_processes_never_lose_recorded_spend():
    config_dir = os.environ["GRIOT_CONFIG_DIR"]
    data_dir = os.environ["GRIOT_DATA_DIR"]

    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_spend_worker, args=(config_dir, data_dir)) for _ in range(_CONCURRENT_PROCS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=90)

    assert all(p.exitcode == 0 for p in procs), "a worker crashed while recording spend"
    expected = _CONCURRENT_PROCS * _CALLS_PER_PROC * _CONCURRENT_COST
    assert common.get_spend_today() == pytest.approx(expected, rel=1e-6)
