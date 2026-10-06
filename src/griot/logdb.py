"""SQLite-backed storage for griot's run/query history (logs/logs.db) —
replaces logs/runs.jsonl and logs/queries.jsonl, both of which stats.py and
common.get_index_status() had to read and re-parse in full on every call,
with no rotation, so the cost grew unboundedly with history (user-raised
concern, 2026-08-21). timestamp is an indexed column, so day-window reads
(stats.py) and get_index_status()'s "most recent run for
this collection" lookup are real indexed queries instead of linear scans.

Deliberately does NOT import griot.common (which would be the natural
place to read LOG_DIR from) — common.py is this module's own caller
(log_run_summary()/log_query()/get_index_status() all call in here), and
common.py importing logdb.py while logdb.py imported common.py back would
be circular. Every function here takes log_dir explicitly instead.

Existing runs.jsonl/queries.jsonl history is imported once, on first use
per JSONL file (tracked in _jsonl_migrated, never re-imported), and left
on disk afterward untouched — nothing griot writes is ever deleted as a
side effect of a storage migration.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_FILENAME = "logs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    collection TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp);
CREATE INDEX IF NOT EXISTS idx_runs_collection ON runs(collection);

CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queries_timestamp ON queries(timestamp);

CREATE TABLE IF NOT EXISTS quality_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    collection TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_checks_timestamp ON quality_checks(timestamp);
CREATE INDEX IF NOT EXISTS idx_quality_checks_collection ON quality_checks(collection);

-- [real bug, reproduced with concurrent processes] The spend circuit
-- breaker's state. Single row (id=1): today's date plus the running
-- total. It lives here, rather than in the .spend_state.json it replaces,
-- so the accumulation happens INSIDE one SQL statement instead of
-- load-JSON -> add in Python -> rewrite the whole file: that read-modify-
-- write pattern silently dropped most concurrent spend, making the
-- breaker underestimate and keep letting paid calls through.
CREATE TABLE IF NOT EXISTS spend_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    date TEXT NOT NULL,
    spend_usd REAL NOT NULL
);

-- Individual spend events, for the short velocity window the breaker uses
-- to catch a burst well before the daily ceiling is reached. Pruned on
-- every write, so this never accumulates beyond the window.
CREATE TABLE IF NOT EXISTS spend_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_events_at ON spend_events(at);

-- [user-requested] One row per MCP tool invocation. `tool` gets its own
-- indexed column rather than living inside a JSON blob because the whole
-- question this table answers is "how often is each tool called" — a
-- GROUP BY, not a scan. ok/error are kept so a tool that is called
-- constantly but always fails can't hide behind a healthy-looking count.
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    tool TEXT NOT NULL,
    ok INTEGER NOT NULL,
    duration_seconds REAL,
    error TEXT,
    project TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool);

-- Tracks which legacy on-disk file has already been imported, so history
-- is never double-counted. Despite the name (kept as-is: renaming it
-- would make an already-migrated database re-import its JSONL history and
-- duplicate it), it tracks legacy files generally, not only JSONL ones.
CREATE TABLE IF NOT EXISTS _jsonl_migrated (
    jsonl_file TEXT PRIMARY KEY,
    migrated_at TEXT NOT NULL,
    records_imported INTEGER NOT NULL
);
"""

# Tables whose records carry a `collection` field worth indexing on its
# own (see _insert()) — `queries` deliberately does not.
_TABLES_WITH_COLLECTION = ("runs", "quality_checks")

_TABLES = ("runs", "queries", "quality_checks")

# (legacy filename, target table) — the exact two files common.py used to
# write via secure_append_line() before this module existed.
_JSONL_SOURCES = (
    ("runs.jsonl", "runs"),
    ("queries.jsonl", "queries"),
)


def _read_jsonl(path: Path) -> list[dict]:
    """Same permissive parsing runs.jsonl/queries.jsonl always got (this was
    stats.py's _read_jsonl()) — used only by the one-time legacy import
    below, never on the live read/write path anymore."""
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a single corrupted line doesn't take down the rest of the file's read
    return records


def _insert(conn: sqlite3.Connection, table: str, record: dict) -> None:
    timestamp = record.get("timestamp") or datetime.now(timezone.utc).isoformat()
    data = json.dumps(record)
    # table is always one of _TABLES (every caller either hardcodes it or
    # validates against the allowlist first) — never user-controlled.
    if table in _TABLES_WITH_COLLECTION:
        conn.execute(
            f"INSERT INTO {table} (timestamp, collection, data) VALUES (?, ?, ?)",
            (timestamp, record.get("collection"), data),
        )
    else:
        conn.execute(f"INSERT INTO {table} (timestamp, data) VALUES (?, ?)", (timestamp, data))


def _ensure_migrated(conn: sqlite3.Connection, log_dir: Path) -> None:
    for jsonl_name, table in _JSONL_SOURCES:
        already = conn.execute(
            "SELECT 1 FROM _jsonl_migrated WHERE jsonl_file = ?", (jsonl_name,)
        ).fetchone()
        if already:
            continue
        records = _read_jsonl(log_dir / jsonl_name)
        try:
            with conn:
                for record in records:
                    _insert(conn, table, record)
                conn.execute(
                    "INSERT INTO _jsonl_migrated (jsonl_file, migrated_at, records_imported) VALUES (?, ?, ?)",
                    (jsonl_name, datetime.now(timezone.utc).isoformat(), len(records)),
                )
        except sqlite3.IntegrityError:
            # Another process (or an earlier connection in this same
            # process) already migrated this file between the SELECT above
            # and this transaction — `with conn:` rolled the whole
            # transaction back on this exception, so no partial/duplicate
            # import happened here. Nothing left to do.
            pass


def _has_project_column(conn: sqlite3.Connection) -> bool:
    return any(row["name"] == "project" for row in conn.execute("PRAGMA table_info(tool_calls)"))


def _ensure_tool_calls_project_column(conn: sqlite3.Connection) -> bool:
    """CREATE TABLE IF NOT EXISTS leaves an existing tool_calls table as it was, so a
    logs.db created before the `project` column gets it added in place, history kept.
    True when the column is there afterwards. False when the database cannot be
    written (a read-only file system): it could be READ before this column existed
    and still can, so that is not an error."""
    if _has_project_column(conn):
        return True
    try:
        with conn:
            conn.execute("ALTER TABLE tool_calls ADD COLUMN project TEXT")
    except sqlite3.OperationalError as e:
        message = str(e).lower()
        if "duplicate column" in message:  # another griot process added it first
            return True
        if "readonly" in message or "read-only" in message:
            return False
        raise
    return True


def private_mkdir(path: Path) -> None:
    """Makes `path`, and whatever is missing on the way to it, for its owner
    alone (0700). A directory that was already there on the way keeps its
    mode: it is not this code's to close.

    `mkdir(parents=True)` gives the directories it makes on the way the
    permissions of the day, so `<data>/griot`, made on the way to `logs/` by
    whichever command ran first, was readable by every user of the machine
    while everything inside it was closed. What protects a file is every
    directory on the way to it."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue  # another process made it meanwhile: its own to close
        os.chmod(directory, 0o700)  # mkdir's mode goes through the umask
    if not path.is_dir():
        # A file in the way. mkdir(exist_ok=True) refused it; changing its
        # mode instead would make it executable and fail somewhere else.
        raise NotADirectoryError(f"{path} exists and is not a directory")
    os.chmod(path, 0o700)


def _nothing_logged_yet(log_dir: Path) -> bool:
    """No database and no legacy file to migrate into one: nothing was ever
    written. A READ then has nothing to open, and opening would create the
    directory and the database on the way: looking at a status must not
    leave files behind (`griot doctor`, day one)."""
    return not (log_dir / DB_FILENAME).exists() and not any((log_dir / name).exists() for name, _ in _JSONL_SOURCES)


def _connect(log_dir: Path) -> sqlite3.Connection:
    # [real bug, found via a subprocess integration test] read paths
    # (get_index_status(), stats.load_window()) never call secure_mkdir()
    # themselves — the old runs.jsonl/queries.jsonl code guarded this with
    # `if path.is_file(): ...`, silently doing nothing on a fresh install.
    # sqlite3.connect() has no such tolerance: it raises if the parent
    # directory doesn't exist yet, so this must ensure it here instead.
    private_mkdir(log_dir)
    db_path = log_dir / DB_FILENAME
    # [review finding] sqlite3.connect() creates the physical file itself,
    # under the process umask, before any table exists — chmod()ing
    # afterward (below) repairs a legacy file, but pre-creating it here at
    # 0600 (a no-op if it already exists) closes the brief window where a
    # brand-new file would otherwise sit at umask-default permissions.
    os.close(os.open(db_path, os.O_CREAT, 0o600))
    # [review finding] sqlite3's default 5s busy_timeout can surface as
    # 'database is locked' (OperationalError) under real contention
    # between two griot processes (MCP + CLI + UI can all touch this file)
    # — a longer timeout lets SQLite's own internal retry absorb realistic
    # contention instead of raising.
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Same 0600 contract every other griot data file has (common.py's
    # secure_* helpers) — repairs a legacy/world-readable file on every
    # connect, not just at creation.
    os.chmod(db_path, 0o600)
    conn.executescript(_SCHEMA)
    _ensure_tool_calls_project_column(conn)
    _ensure_migrated(conn, log_dir)
    return conn


def write_run(log_dir: Path, record: dict) -> None:
    """_connect() creates log_dir itself if missing (see its docstring) —
    common.log_run_summary() still calls secure_mkdir() first too, but
    that's belt-and-suspenders, not a requirement of this function."""
    conn = _connect(log_dir)
    try:
        with conn:
            _insert(conn, "runs", record)
    finally:
        conn.close()


def write_query(log_dir: Path, record: dict) -> None:
    conn = _connect(log_dir)
    try:
        with conn:
            _insert(conn, "queries", record)
    finally:
        conn.close()


def write_quality_check(log_dir: Path, record: dict) -> None:
    """[user-requested, 2026-08-21] Replaces last_quality_check.json, which
    only ever held the single most recent result — every run overwrote the
    previous one, so quality history was discarded. Keeping every check is
    what makes a pass-rate trend (a real regression signal for the index)
    possible at all."""
    conn = _connect(log_dir)
    try:
        with conn:
            _insert(conn, "quality_checks", record)
    finally:
        conn.close()


def migrate_legacy_json_file(log_dir: Path, legacy_path: Path, table: str) -> None:
    """One-time import of a legacy single-JSON-object file (as opposed to
    the JSONL ones _ensure_migrated() handles automatically) into `table`,
    tracked by the same marker table so it never re-imports.

    Kept as an explicit call rather than folded into _connect() because the
    file lives outside log_dir (last_quality_check.json sits in DATA_DIR,
    not DATA_DIR/logs) and this module deliberately knows nothing about
    griot's directory layout — the caller passes the path.

    Never deletes the legacy file: same discipline the JSONL migration
    follows (griot does not delete user data as a side effect of a storage
    change). A missing or corrupted file imports nothing and is still
    marked done, so it is not retried on every single call."""
    if table not in _TABLES:
        raise ValueError(f"unknown table {table!r}")
    if not legacy_path.exists() and _nothing_logged_yet(log_dir):
        return  # nothing to carry over, and a READ asked for this: creating the log here would be its only effect
    conn = _connect(log_dir)
    try:
        marker = legacy_path.name
        if conn.execute("SELECT 1 FROM _jsonl_migrated WHERE jsonl_file = ?", (marker,)).fetchone():
            return
        records = []
        if legacy_path.exists():
            try:
                records = [json.loads(legacy_path.read_text())]
            except (OSError, ValueError):
                records = []  # corrupted/unreadable — same "treat as absent" tolerance the JSONL import has
        try:
            with conn:
                for record in records:
                    _insert(conn, table, record)
                conn.execute(
                    "INSERT INTO _jsonl_migrated (jsonl_file, migrated_at, records_imported) VALUES (?, ?, ?)",
                    (marker, datetime.now(timezone.utc).isoformat(), len(records)),
                )
        except sqlite3.IntegrityError:
            pass  # another process won the race; its transaction imported the same data — see _ensure_migrated()
    finally:
        conn.close()


def read_since(log_dir: Path, table: str, days: int, now: datetime | None = None) -> list[dict]:
    """Indexed SQL narrowing by timestamp — the actual reason this module
    exists instead of runs.jsonl/queries.jsonl. Callers (stats.py's
    load_window()) still run their own _filter_by_days() over the result
    for the authoritative, already-tested validation/robustness logic;
    this only avoids parsing the FULL unbounded history on every call."""
    if table not in _TABLES:
        raise ValueError(f"unknown table {table!r}")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    if _nothing_logged_yet(log_dir):
        return []
    conn = _connect(log_dir)
    try:
        # table is validated against a fixed allowlist above, never
        # user-controlled — safe to interpolate into the query string.
        rows = conn.execute(
            f"SELECT data FROM {table} WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(row["data"]) for row in rows]


# table -> the SQL that reads which collection a row is about. `queries` has
# no column for it (see _TABLES_WITH_COLLECTION): its records carry the field
# in their JSON since log_query() began writing it, and a record from before
# then gives NULL, so it matches no collection rather than a guessed one.
_RECENT_TABLES = {"runs": "collection", "queries": "json_extract(data, '$.collection')",
                  "quality_checks": "collection"}


def read_recent(log_dir: Path, table: str, *, collection: str | None = None, limit: int = 1) -> list[dict]:
    """The `limit` most recently written records of `table`, newest first,
    optionally only those of one collection. Not bounded by a time window:
    it answers "when was the last one", which a report over the last N days
    cannot. By insertion order, like most_recent_run_for_collection()."""
    if table not in _RECENT_TABLES:
        raise ValueError(f"unknown table {table!r}")
    where, args = (f"WHERE {_RECENT_TABLES[table]} = ? ", (collection,)) if collection is not None else ("", ())
    if _nothing_logged_yet(log_dir):
        return []
    conn = _connect(log_dir)
    try:
        rows = conn.execute(f"SELECT data FROM {table} {where}ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
    finally:
        conn.close()
    return [json.loads(row["data"]) for row in rows]


def read_latest(log_dir: Path, table: str, *, collection: str | None = None) -> dict | None:
    """The most recent record of `table` (see read_recent), or None."""
    return next(iter(read_recent(log_dir, table, collection=collection, limit=1)), None)


def most_recent_run_for_collection(log_dir: Path, collection: str) -> dict | None:
    """get_index_status()'s read — ordered by id (insertion order), not
    timestamp, matching the original runs.jsonl invariant this replaces
    ("append-only, so the last matching line is always the most recent, no
    need to sort by timestamp")."""
    if _nothing_logged_yet(log_dir):
        return None
    conn = _connect(log_dir)
    try:
        row = conn.execute(
            "SELECT data FROM runs WHERE collection = ? ORDER BY id DESC LIMIT 1",
            (collection,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["data"]) if row else None


def write_spend(log_dir: Path, today: str, cost_usd: float, now: float, window_seconds: int) -> None:
    """Atomically adds `cost_usd` to today's running total and records the
    individual event for the velocity window.

    The accumulation is a single UPSERT — `spend_usd + excluded.spend_usd`
    is computed by SQLite itself, never read into Python and written back.
    That is the whole point: the JSON file this replaces was
    load -> add -> rewrite-whole-file, so two griot processes recording
    spend at the same time overwrote each other and most of the spend
    vanished (measured: 83% lost across 6 processes). A circuit breaker
    that loses spend fails OPEN — it keeps authorizing paid calls — so
    this is the direction that actually costs money.

    A date change resets the total instead of adding to it (the breaker is
    a DAILY ceiling), still inside the same single statement."""
    conn = _connect(log_dir)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO spend_state (id, date, spend_usd) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    spend_usd = CASE WHEN spend_state.date = excluded.date
                                     THEN spend_state.spend_usd + excluded.spend_usd
                                     ELSE excluded.spend_usd END,
                    date = excluded.date
                """,
                (today, cost_usd),
            )
            conn.execute("INSERT INTO spend_events (at, cost_usd) VALUES (?, ?)", (now, cost_usd))
            conn.execute("DELETE FROM spend_events WHERE at < ?", (now - window_seconds,))
    finally:
        conn.close()


def read_spend_today(log_dir: Path, today: str) -> float:
    """Today's accumulated spend, or 0.0 when nothing was recorded today —
    a stored total from a PREVIOUS day reads as 0.0 rather than leaking
    into today's ceiling (same daily-reset semantics the JSON file had)."""
    if _nothing_logged_yet(log_dir):
        return 0.0
    conn = _connect(log_dir)
    try:
        row = conn.execute("SELECT spend_usd FROM spend_state WHERE id = 1 AND date = ?", (today,)).fetchone()
    finally:
        conn.close()
    return float(row["spend_usd"]) if row else 0.0


def read_spend_velocity(log_dir: Path, since: float) -> float:
    """Total spend recorded at or after `since` — the breaker's burst
    check, summed by SQLite over the indexed `at` column."""
    if _nothing_logged_yet(log_dir):
        return 0.0
    conn = _connect(log_dir)
    try:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend_events WHERE at >= ?", (since,)).fetchone()
    finally:
        conn.close()
    return float(row["total"])


def migrate_legacy_spend_file(log_dir: Path, legacy_path: Path, today: str) -> None:
    """One-time import of an existing install's .spend_state.json, so the
    day's already-accumulated spend is not forgotten mid-day by the switch
    to SQLite (forgetting it would reset the ceiling to zero and authorize
    a second full day of spend).

    Only imports when the stored date IS today — an older file has nothing
    relevant left to carry over. Same marker/idempotency and
    never-delete-the-original discipline as the other migrations here."""
    conn = _connect(log_dir)
    try:
        marker = legacy_path.name
        if conn.execute("SELECT 1 FROM _jsonl_migrated WHERE jsonl_file = ?", (marker,)).fetchone():
            return
        state = {}
        if legacy_path.exists():
            try:
                state = json.loads(legacy_path.read_text())
            except (OSError, ValueError):
                state = {}
        imported = 0
        try:
            with conn:
                if state.get("date") == today and state.get("spend_usd"):
                    # `date = excluded.date` is deliberate, not redundant
                    # [review finding]: the conflict branch is unreachable
                    # today (this migration always runs before any normal
                    # write, so the row is absent), but WITHOUT stamping
                    # the date, a migrated total landing on a row left
                    # from an older day would be hidden behind that stale
                    # date and silently reset away by the next write.
                    # Money must not depend on an undocumented call-order
                    # assumption holding forever.
                    conn.execute(
                        "INSERT INTO spend_state (id, date, spend_usd) VALUES (1, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "spend_usd = spend_state.spend_usd + excluded.spend_usd, date = excluded.date",
                        (today, float(state["spend_usd"])),
                    )
                    for event in state.get("recent_events") or []:
                        try:
                            at, cost = float(event[0]), float(event[1])
                        except (TypeError, ValueError, IndexError):
                            continue  # a malformed event must not abort the whole import
                        conn.execute("INSERT INTO spend_events (at, cost_usd) VALUES (?, ?)", (at, cost))
                    imported = 1
                conn.execute(
                    "INSERT INTO _jsonl_migrated (jsonl_file, migrated_at, records_imported) VALUES (?, ?, ?)",
                    (marker, datetime.now(timezone.utc).isoformat(), imported),
                )
        except sqlite3.IntegrityError:
            pass  # another process won the race — see _ensure_migrated()
    finally:
        conn.close()


def write_tool_call(log_dir: Path, tool: str, *, ok: bool, duration_seconds: float | None = None,
                    error: str | None = None, timestamp: str | None = None, project: str | None = None) -> None:
    """Records one MCP tool invocation. Deliberately narrow: which tool,
    whether it worked, how long, and why not — never the arguments or the
    result. Arguments can carry the user's own question text and results
    carry indexed source content; neither belongs in a usage counter, and
    griot_search already records what a search needs via log_query()."""
    conn = _connect(log_dir)
    try:
        with conn:
            conn.execute(
                "INSERT INTO tool_calls (timestamp, tool, ok, duration_seconds, error, project) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (timestamp or datetime.now(timezone.utc).isoformat(), tool, 1 if ok else 0,
                 duration_seconds, error, project),
            )
    finally:
        conn.close()


# The tables pruned by age: the two that grow with use, one row per search or
# question and one per MCP tool call. Not `runs`: the freshness report and
# doctor read the last run of every source and repository, and a repository
# indexed once a year would lose its only run. Not `quality_checks`: it is
# the pass-rate trend, a few rows a week at most. Not `spend_events`: it
# prunes itself on every write (write_spend).
_PRUNED_BY_AGE = ("queries", "tool_calls")

# Which rows a prune takes, written once: count_older_than() states this
# number to a person before they shorten the window, and a count that drifted
# from the delete would be a promise the next search breaks. {table} is one
# of _PRUNED_BY_AGE, never user-controlled; the one parameter is the cutoff.
_OLDER_THAN = "WHERE timestamp < ? AND id < (SELECT MAX(id) FROM {table})"


def _cutoff(days: int, now: datetime | None) -> str:
    if days < 1:
        raise ValueError(f"a retention under one day would delete what was just written (got {days})")
    return ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()


def count_older_than(log_dir: Path, days: int, now: datetime | None = None) -> dict[str, int]:
    """How many rows of each _PRUNED_BY_AGE table prune_older_than() would
    delete with the same `days` and `now`, without deleting them.

    Read-only, opened so: it answers a question asked BEFORE anything is
    decided (`griot config set log-retention-days`), so it must not create
    the database, migrate legacy files into it or touch it in any way. A
    database that predates a table counts nothing for it."""
    cutoff = _cutoff(days, now)
    counted = {table: 0 for table in _PRUNED_BY_AGE}
    db_path = log_dir / DB_FILENAME
    if not db_path.is_file():
        return counted
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30.0)
    try:
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in _PRUNED_BY_AGE:  # a fixed allowlist, never user-controlled
            if table in present:
                counted[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} {_OLDER_THAN.format(table=table)}", (cutoff,)
                ).fetchone()[0]
    finally:
        conn.close()
    return counted


def prune_older_than(log_dir: Path, days: int, now: datetime | None = None) -> dict[str, int]:
    """Deletes the rows of _PRUNED_BY_AGE written more than `days` days
    before `now`, and returns how many each table lost.

    The newest row of each table stays, however old: it is what "last
    search N days ago" is read from (stats.load_state()), and deleting it
    would turn that into "never searched", a different fact.

    One short transaction over the indexed `timestamp` column. Other griot
    processes write to the same file meanwhile; they wait on SQLite's lock
    (the busy timeout in _connect()) and nothing they write is older than
    the cutoff."""
    cutoff = _cutoff(days, now)
    if _nothing_logged_yet(log_dir):
        return {table: 0 for table in _PRUNED_BY_AGE}
    conn = _connect(log_dir)
    try:
        # Zeroes what is deleted: a question deleted from the table would
        # otherwise stay readable in the file's free pages, which defeats
        # deleting it. Per connection, and this one only deletes.
        conn.execute("PRAGMA secure_delete = ON")
        removed = {}
        with conn:
            for table in _PRUNED_BY_AGE:  # a fixed allowlist, never user-controlled
                removed[table] = conn.execute(
                    f"DELETE FROM {table} {_OLDER_THAN.format(table=table)}", (cutoff,),
                ).rowcount
    finally:
        conn.close()
    return removed


def _tool_call_rows(conn: sqlite3.Connection, cutoff: datetime) -> list[sqlite3.Row]:
    project = "project" if _has_project_column(conn) else "NULL AS project"  # a database we could not migrate
    return conn.execute(
        f"SELECT timestamp, tool, ok, duration_seconds, error, {project} FROM tool_calls "
        "WHERE timestamp >= ? ORDER BY timestamp",
        (cutoff.isoformat(),),
    ).fetchall()


def read_tool_calls(log_dir: Path, days: int, now: datetime | None = None) -> list[dict]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    if _nothing_logged_yet(log_dir):
        return []
    conn = _connect(log_dir)
    try:
        rows = _tool_call_rows(conn, cutoff)
    finally:
        conn.close()
    return [
        {"timestamp": r["timestamp"], "tool": r["tool"], "ok": bool(r["ok"]),
         "duration_seconds": r["duration_seconds"], "error": r["error"], "project": r["project"]}
        for r in rows
    ]
