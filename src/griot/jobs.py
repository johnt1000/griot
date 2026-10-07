"""Child-process jobs launched by a long-lived HOST process — today only
the MCP server (`griot_index_repo`). Extracted out of `mcp_server.py` for
a second host that no longer exists (removed in
2026-08-21): `mcp_server.py` imports the `mcp` SDK and evaluates
GRIOT_MCP_ENABLE_INDEX at module import time, registering tools, and that
host had to pay neither cost. Kept split anyway — the logic is spawning
and validating a subprocess, which is not what a tool-registration module
is for, and index_job_refusal() below is now called from two places. `common.py` was the other candidate location and
was rejected too: it's the data/infra layer, and "spawn a CLI subprocess"
is a distinct responsibility from what already lives there.

Both host processes are long-lived and READ-mostly — the same discipline
common.py's `reuse_active_handle`/`get_embed_model()` rules protect for
the UI applies here by construction: nothing in this module ever imports
fastembed/onnxruntime or opens a Qdrant Edge handle itself. A job always
runs as a subprocess (`python -m griot.cli ...`), so the cost (embedding
model load, Qdrant handle) is paid and released entirely inside a process
that exits — never inside the host.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

from griot import common

DEFAULT_INDEX_SOURCES: list[str] = ["code", "commits", "tags", "branches"]

# Wait time before checking whether the subprocess died immediately (python
# not found, import error, etc.) — short enough to not noticeably delay the
# caller's return, long enough to catch a synchronous start failure (a
# successful fork+exec doesn't guarantee progress — this only covers the
# most obvious case).
_LIVENESS_CHECK_SECONDS = 0.5

# Env vars a job's child process must ALWAYS inherit verbatim from the
# host, regardless of where their value came from — they decide WHERE
# config/data live, so dropping them would point the child at a different
# config dir entirely (silently catastrophic, not just stale).
_NEVER_DROP_ENV_VARS = {"GRIOT_CONFIG_DIR", "GRIOT_DATA_DIR"}


def index_path_allowed(resolved: Path, *, allow_env_roots: bool = True) -> bool:
    """Path allowlist gate for indexing a specific repo. Without it, the
    content retrieved by griot_search is untrusted by definition (indirect
    prompt injection) — a compromised agent (or a forged HTTP request to
    that host) could request indexing $HOME and exfiltrate the
    filesystem to a paid embedding API. `resolved` must already be
    Path.resolve()d by the caller (symlinks followed) — what matters is
    the real destination.

    Allowed if: (a) it's exactly one of the repos in repos.json, or (b)
    allow_env_roots=True (the MCP tool's default) and it's under one of
    the GRIOT_MCP_INDEX_ROOTS prefixes. [decision C] the web
    that caller passed allow_env_roots=False — deliberately MORE restrictive
    than the MCP tool, since a human clicking a UI button doesn't need the
    same "let an operator pre-authorize a whole directory tree" escape
    hatch that exists for agent/automation use. Nothing configured (or
    repos.json unreadable/corrupted — see below) = everything refused
    (fail closed)."""
    try:
        registered = {Path(p).resolve() for p in common.load_repos()}
    except (OSError, ValueError):
        # [risk 5.1] ValueError covers json.JSONDecodeError (a
        # subclass) from a truncated/corrupted repos.json — the ORIGINAL
        # code here only caught FileNotFoundError, so a corrupted file
        # (exactly what a non-atomic write crash used to produce, before
        # repos.py's Step 1 fix) propagated instead of failing closed.
        registered = set()
    if resolved in registered:
        return True
    if not allow_env_roots:
        return False
    for root in os.getenv("GRIOT_MCP_INDEX_ROOTS", "").split(":"):
        if not root:
            continue
        root_resolved = Path(root).resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False


def _repo_name_if_uniquely_registered(resolved: Path) -> str | None:
    """[real bug fix, 2026-08-21] `--path` and `--repo`/repos.json produce
    DIFFERENT stable_id()s for the exact same content: every indexer
    module's `_repo_key_for_path()` suffixes a path hash to
    the id ONLY for `--path` — deliberately, to disambiguate two
    same-basename repos in different locations, but the `--repo`/
    repos.json path was always meant to keep the plain, un-suffixed id
    ("preserving the usual id so as not to break idempotency"). Before
    this fix, start_index_job() ALWAYS passed `--path` — which is correct
    for an MCP-only path allowed via GRIOT_MCP_INDEX_ROOTS (no registered
    name to reference), but WRONG for the common case (a repo registered
    in repos.json, indexed once via plain `griot index all`/`--repo` and
    again via MCP): every chunk got a brand-new id, so
    _split_pending() found no prior match, EVERY chunk was re-embedded
    (0% reuse), and the OLD points were never removed — silently
    duplicating the entire repo's data in the collection. Confirmed
    against real production data: a user's collection had every chunk of
    a reindexed repo duplicated exactly 2x.

    Returns `resolved.name` only when that name resolves BACK to exactly
    this one directory via repos.json — i.e. exactly the condition under
    which `griot index --repo <name>` (index_code.py's `Path(p).name ==
    args.repo` matching) would pick precisely this repo, unambiguously.
    None otherwise (two registered repos sharing a basename, or not
    registered at all) — the caller falls back to `--path`, which is
    always correct even if never optimal for reuse."""
    return common.registered_repo_name(resolved)


def child_env(boot_env: dict, boot_file: dict) -> dict:
    """[risk 2] A job's child process must NOT blindly
    inherit the host's os.environ: common.py resolves .env into
    os.environ ONCE, at import time (load_dotenv(..., override=False)) —
    a long-lived host (potentially open for hours) that
    just had a setting changed through its own Config page would spawn a
    job that silently uses the OLD value (wrong embedding profile, wrong
    spend ceiling), because the host's os.environ was never updated.

    boot_env/boot_file: the pair from ui.service.snapshot_process_env(),
    captured once at host boot — used only to tell apart "this env var's
    value came from the .env FILE at boot" (drop it, let the child re-read
    the file fresh) from "this env var is genuinely exported in the
    shell" (keep it — the host's Config page already flags this case as
    'overridden by shell', and a shell export should keep winning for the
    child too, same as it does for the host). GRIOT_CONFIG_DIR/
    GRIOT_DATA_DIR are never dropped regardless — see
    _NEVER_DROP_ENV_VARS."""
    env = dict(os.environ)
    for var, file_value in boot_file.items():
        if var in _NEVER_DROP_ENV_VARS:
            continue
        if boot_env.get(var) == file_value:
            env.pop(var, None)  # let the child's own load_dotenv() re-read the CURRENT file
    return env


_registry_lock = threading.Lock()
# pid -> {"path": str | None, "sources": list[str], "started_at": float,
#         "proc": Popen | None, "process_start_time": float | None,
#         "progress_path": str, "log_path": str,
#         "finished": None | {"finished_at", "exit_code", "progress"}}
# A job stays here after it ends, as the most recent finished one, until the
# next one ends: an agent that waited on it asks how it ended after the fact.
# `proc` is None for a job reloaded from disk (see _ensure_loaded()): this
# process did not spawn it and cannot wait on it.
_registry: dict[int, dict] = {}

# The registry outlives the process that holds it. A job is a detached
# subprocess that keeps running when the MCP server exits (a client restart,
# a crash); with the registry only in memory, the next server said nothing
# was running, would start a second run beside it (the lock is only held
# while a source embeds), and could never say how the first one ended. So
# every change is written here, and the first read in a new process loads it.
# In the data directory, private: it names the repositories being indexed.
JOBS_FILE_NAME = ".index_jobs.json"
_loaded = False

# What start_index_job names a progress file. A record read from disk is
# data: one whose progress file is anything else is not ours, and is never
# read, let alone removed.
_PROGRESS_PREFIX = "griot-index-progress-"
_PROGRESS_SUFFIX = ".json"

# Where progress files are made: a private directory of the data directory.
# They used to be made in the system temporary directory, on the idea that
# the system reclaims what a dead host leaves there; it did not (macOS keeps
# a file for days, and the test suite, whose data directory is per test but
# whose temp dir is not, left about 1,200). Here they go with the data they
# belong to, and the first read of a new process removes what no record
# names (_sweep_orphan_progress_files()).
PROGRESS_DIR_NAME = "index_progress"

# How old a progress file no record names must be before it is removed: a
# server makes the file before it records the job (the child must be spawned
# and seen alive first), and another server loading in between must not take
# it from under that job.
ORPHAN_PROGRESS_GRACE_SECONDS = 600

# How far a process's start time may drift between two reads of it (float
# rounding), as common._lock_owner_is_alive allows for the lock.
_START_TIME_TOLERANCE_SECONDS = 1.0


def _jobs_path() -> Path:
    return common.DATA_DIR / JOBS_FILE_NAME


def _same_process_alive(pid: int, process_start_time: float | None) -> bool:
    """Whether the process a record names is still running: the pid alone
    is not enough, since the system hands a dead process's pid to the next
    one, so its start time must match the one recorded when the job began.
    A record without one (it could not be read then) is never trusted. A
    zombie has ended: it only waits for a parent to collect its status."""
    if process_start_time is None:
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(process.create_time() - process_start_time) <= _START_TIME_TOLERANCE_SECONDS
    except psutil.Error:
        # Gone, or not ours to look at (another user's process got the pid).
        return False


def _is_a_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _progress_dir() -> Path:
    return common.DATA_DIR / PROGRESS_DIR_NAME


def _is_our_progress_file(path: str) -> bool:
    """Named as start_index_job names one, in the directory it makes them in,
    or in the system temporary directory, where earlier versions made them:
    a job one of those recorded is still followed, and its file removed."""
    candidate = Path(path)
    parent = os.path.realpath(candidate.parent)
    return (candidate.name.startswith(_PROGRESS_PREFIX) and candidate.name.endswith(_PROGRESS_SUFFIX)
            and parent in (os.path.realpath(_progress_dir()), os.path.realpath(tempfile.gettempdir())))


def _record_from_disk(raw) -> dict | None:
    """A registry entry (with its pid) from one record of the jobs file, or
    None when the record is not whole and of the right kinds. The file is
    griot's own, but it is read as data: what it holds decides which
    process is reported as a run and which file is removed."""
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    start_time = raw.get("process_start_time")
    if start_time is not None and not _is_a_number(start_time):
        return None
    sources = raw.get("sources")
    if not _is_a_number(raw.get("started_at")) \
            or not (raw.get("path") is None or isinstance(raw.get("path"), str)) \
            or not isinstance(sources, list) or not all(isinstance(source, str) for source in sources) \
            or not isinstance(raw.get("progress_path"), str) or not _is_our_progress_file(raw["progress_path"]) \
            or not isinstance(raw.get("log_path"), str):
        return None
    finished = raw.get("finished")
    if finished is not None:
        if not isinstance(finished, dict) or not _is_a_number(finished.get("finished_at")):
            return None
        exit_code = finished.get("exit_code")
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            return None
        progress = finished.get("progress")
        if progress is not None:
            progress = common.index_progress_from(progress)
            if progress is None:
                return None
        finished = {"finished_at": finished["finished_at"], "exit_code": exit_code, "progress": progress}
    return {"pid": pid, "path": raw["path"], "sources": list(sources), "started_at": raw["started_at"],
            "proc": None, "process_start_time": start_time, "progress_path": raw["progress_path"],
            "log_path": raw["log_path"], "finished": finished}


def _records_on_disk() -> list[dict]:
    """The whole, well-formed records of the jobs file; none when there is
    no file or it cannot be read as one (a damaged file is no job, never an
    error in the tool that asked)."""
    try:
        data = json.loads(_jobs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    return [record for record in map(_record_from_disk, data["jobs"]) if record is not None]


def _record_to_disk(pid: int, info: dict) -> dict:
    return {"pid": pid, "process_start_time": info.get("process_start_time"), "started_at": info["started_at"],
            "path": info["path"], "sources": info["sources"], "progress_path": info["progress_path"],
            "log_path": info.get("log_path"), "finished": info["finished"]}


def _remove_progress_file(path: str) -> None:
    if not _is_our_progress_file(path):
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _is_progress_scratch_file(name: str) -> bool:
    """Named as common.atomic_scratch_path names the scratch file of one of
    ours, which the run writes before renaming it into place; a child killed
    between the two leaves it. The pid is read from the name and the name
    rebuilt through that function, so a change to how the writer names its
    scratch file cannot leave the sweep matching a name nobody writes."""
    stem, dot, pid = name.removesuffix(".tmp").rpartition(".")
    # isascii: str.isdigit() also takes digits int() cannot read (a superscript two).
    return (bool(dot) and pid.isascii() and pid.isdigit() and stem.startswith(_PROGRESS_PREFIX) and stem.endswith(_PROGRESS_SUFFIX)
            and common.atomic_scratch_path(Path(stem), int(pid)).name == name)


def _sweep_orphan_progress_files() -> None:
    """Removes the progress files no job in the registry names and that are
    old enough (ORPHAN_PROGRESS_GRACE_SECONDS) for no server to be starting
    their job: nothing else would ever remove them. What leaves one: a host
    killed between making the file and recording the job, or a record that
    could not be written; and a scratch file of a child killed mid-write
    (_is_progress_scratch_file()). Only files named as ours, in the
    directory this data directory owns; never fails the caller. Called with _registry_lock
    held, after the registry is loaded."""
    named = {os.path.realpath(info["progress_path"]) for info in _registry.values()}
    try:
        candidates = list(_progress_dir().iterdir())
    except OSError:
        return  # no directory yet: nothing was ever left
    now = time.time()
    for candidate in candidates:
        # A writer's scratch file is removed even beside a file a job names:
        # it lives for one write, so one past the grace period is a child
        # killed mid-write, and nothing else would ever reclaim it.
        if not _is_progress_scratch_file(candidate.name) and (
                not _is_our_progress_file(str(candidate)) or os.path.realpath(candidate) in named):
            continue
        try:
            if now - candidate.stat().st_mtime < ORPHAN_PROGRESS_GRACE_SECONDS:
                continue
            candidate.unlink()
        except OSError:
            pass


def _save() -> None:
    """Writes the registry to the jobs file, atomically (a reader never sees
    half of it). Keeps a running job another process recorded there and this
    one does not know of: two MCP servers share the data directory, and the
    one that loaded first must not erase the other's run by writing after it
    started. Never fails the caller: the job is running whatever happens to
    its record, and losing the record costs only the view after a restart.
    Called with _registry_lock held."""
    records = [_record_to_disk(pid, info) for pid, info in _registry.items()]
    for other in _records_on_disk():
        if other["pid"] not in _registry and other["finished"] is None \
                and _same_process_alive(other["pid"], other["process_start_time"]):
            records.append(_record_to_disk(other["pid"], other))
    try:
        common.secure_mkdir(common.DATA_DIR)
        common.secure_write_text_atomic(_jobs_path(), json.dumps({"jobs": records}))
    except OSError as e:
        common.log_and_print(f"Warning: could not record the indexing job in {_jobs_path()}: {e}",
                             level="warning", echo=False)


def _ensure_loaded() -> None:
    """On the first read in this process, takes back the jobs an earlier
    process recorded: a job still running (the same process, see
    _same_process_alive()) is followed again; one that ended is kept as
    finished only if its run recorded how it ended (common.progress_end()),
    since nothing else can tell this process its exit status; the rest are
    dropped, with their progress files. Called with _registry_lock held."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _jobs_path().exists():
        _sweep_orphan_progress_files()
        return
    for record in _records_on_disk():
        pid = record.pop("pid")
        if record["finished"] is None and not _same_process_alive(pid, record["process_start_time"]):
            progress = common.read_index_progress(record["progress_path"])
            _remove_progress_file(record["progress_path"])
            if progress is None or not progress["ended"]:
                continue  # how it ended is unknown: nothing true to say about it
            record["finished"] = {"finished_at": time.time(), "exit_code": progress["exit_code"], "progress": progress}
        _registry.setdefault(pid, record)
    # Rewritten at once, so that what was dropped (a damaged record, a dead
    # job) is not examined again by every process that starts after this one.
    _save()
    _sweep_orphan_progress_files()


def _keep_only_the_last_finished() -> bool:
    """Only the most recent finished job is worth keeping: the one an agent
    may still ask about. Whether any was dropped. Called with _registry_lock
    held."""
    finished = [pid for pid, info in _registry.items() if info["finished"] is not None]
    stale = sorted(finished, key=lambda pid: _registry[pid]["finished"]["finished_at"])[:-1]
    for pid in stale:
        del _registry[pid]
    return bool(stale)


def running_index_job() -> dict | None:
    """[risk 4] common.index_lock_status()["running"] alone is NOT a
    reliable "is an index job in progress" signal: acquire_lock()/
    release_lock() are scoped to a single index_documents() call (one per
    SOURCE), not to the whole `index all` run — the discovery phase
    (walking files, `git log`) that precedes each source runs with NO lock
    held, often for minutes on a real repo. Asking during that window
    would report "idle" even though a job this process launched is very
    much still alive.

    This registry tracks the jobs this host spawned — proc.poll() both
    answers "is it still alive" and reaps any zombie (a Popen object whose
    process already exited but was never waited on) — and those an earlier
    host recorded on disk and that are still running (see _ensure_loaded()).
    Returns the first still-alive entry (in practice there's at most one,
    since start_index_job() refuses to spawn a second job while one is
    registered) or None."""
    with _registry_lock:
        _ensure_loaded()
        alive = None
        changed = False
        for pid, info in _registry.items():
            if info["finished"] is not None:
                continue
            proc = info["proc"]
            still_running = proc.poll() is None if proc is not None \
                else _same_process_alive(pid, info["process_start_time"])
            if still_running:
                alive = {"pid": pid, "path": info["path"], "sources": info["sources"], "started_at": info["started_at"]}
            else:
                _finish(info)
                changed = True
        if _keep_only_the_last_finished() or changed:
            _save()
        return alive


def _finish(info: dict) -> None:
    """Records how a job that has just been found dead ended: its exit code
    and the last progress its run wrote, read now because its file is removed
    now (nothing else would ever reclaim it). `finished_at` is when the end
    was noticed, which is when this host asked, not when the process exited.
    The exit code is the process's own when this host spawned it, and
    otherwise the one the run recorded at its end (None when it recorded
    none: killed outright). Called with _registry_lock held."""
    progress = common.read_index_progress(info["progress_path"])
    if info["proc"] is not None:
        exit_code = info["proc"].returncode
    else:
        exit_code = progress["exit_code"] if progress is not None else None
    info["finished"] = {"finished_at": time.time(), "exit_code": exit_code, "progress": progress}
    _remove_progress_file(info["progress_path"])


def index_job_report() -> dict:
    """The job this host started, for an agent following it: `running` (its
    pid, path, sources, start time and the progress its run last recorded,
    or None) and `finished` (the most recent job that ended: the same, plus
    its exit code and when its end was noticed, or None). "This host"
    includes the earlier processes on the same data directory whose jobs it
    took back on its first read."""
    running = running_index_job()  # also notices a job that just ended
    with _registry_lock:
        if running is not None:
            running = {**running, "progress": common.read_index_progress(_registry[running["pid"]]["progress_path"])}
        finished = None
        for pid, info in _registry.items():
            if info["finished"] is not None:
                finished = {"pid": pid, "path": info["path"], "sources": info["sources"],
                            "started_at": info["started_at"], **info["finished"]}
    return {"running": running, "finished": finished}


def index_job_refusal(path: str | None, *, allow_env_roots: bool = True) -> str | None:
    """Every reason indexing would be refused, WITHOUT starting anything —
    or None if it would go ahead. All checks are cheap: a registry lookup, a
    lock stat, a path resolve, and one `git rev-parse`.

    Split out of start_index_job() so a caller can find out before spending
    something it cannot take back. griot_index_repo asks a human to confirm a
    run that costs money; asking about a path that was never going to be
    indexed spends that person's attention on a question with one possible
    outcome, which is how confirmation prompts get clicked through. The
    checks stay in ONE place — start_index_job() calls this too, so the two
    paths cannot drift into disagreeing about what is allowed.

    path: a specific repo (validated against index_path_allowed()) — or None,
    meaning "index every repo in repos.json". The None case skips the
    allowlist check entirely: the target already IS the full allowed list,
    there is nothing to validate a path against."""
    # [risk 3/4] Two independent "already running" checks, in order
    # of what a caller is MOST likely to be racing against: a job THIS
    # host already launched (registry) is checked first since it's the
    # scenario the fix in this module specifically targets; the lock
    # pre-check catches a job launched some other way (another griot
    # process, e.g. `griot index` run directly in a terminal). Neither is
    # atomic — the REAL mutual exclusion is acquire_lock()'s O_CREAT|O_EXCL
    # inside the subprocess; both are fast-path checks to avoid spawning a
    # subprocess bound to fail.
    if running_index_job() is not None:
        return "an indexing run is already in progress — wait for it to finish or check griot_index_status."
    if common.index_lock_status()["running"]:
        return "an indexing run is already in progress — wait for it to finish or check griot_index_status."

    if path is None:
        return None
    return repository_path_refusal(path, allow_env_roots=allow_env_roots)


def repository_path_refusal(path: str, *, allow_env_roots: bool = True) -> str | None:
    """Why `path` is not a repository griot may read on an agent's request,
    or None: the allowlist and the "is a git work tree" check, without the
    "an indexing run is in progress" ones. For a caller that reads the
    repository and not the index (griot_golden_set_suggest), which a run
    elsewhere is no reason to refuse."""
    # resolve() BEFORE any check: symlinks followed, the gate and the
    # subprocess see the same real destination.
    repo_path = Path(path).resolve()

    # Allowlist gate first — even before the existence checks, so a
    # refusal doesn't work as a filesystem-enumeration oracle (the
    # same concern behind the collapsed message just below).
    if not index_path_allowed(repo_path, allow_env_roots=allow_env_roots):
        if allow_env_roots:
            return "path outside the allowed repo list — register it with `griot repos add <path>` or include a parent directory in GRIOT_MCP_INDEX_ROOTS (':'-separated list)."
        # [jobs] this caller doesn't honor GRIOT_MCP_INDEX_ROOTS
        # (decision C) — mentioning it here would be actionable-sounding
        # advice that doesn't actually work for this caller.
        return "path outside the allowed repo list — register it first with `griot repos add <path>`."

    # [review — carried over from mcp_server.py] the 3
    # original error messages ("doesn't exist" vs "not a directory" vs
    # "not a git repo") worked as a low-value oracle for enumerating
    # the local filesystem — collapsed into one generic message.
    is_valid_git_repo = False
    if repo_path.is_dir():
        # The exit code alone is not enough: inside a bare repository (which
        # can sit in a project's tracked files, with a config of its own)
        # git answers "false" and still exits 0.
        try:
            check = common.run_git(repo_path, ["rev-parse", "--is-inside-work-tree"], timeout=10, check=False)
            is_valid_git_repo = check.returncode == 0 and check.stdout.strip() == "true"
        except (OSError, subprocess.TimeoutExpired):
            is_valid_git_repo = False
    if not is_valid_git_repo:
        # Shown, not echoed: the path is the caller's own text, and it ends
        # up in a tool result and a terminal.
        return f"'{common.shown(str(path))[:300]}' invalid: must exist, be a directory, and be a git repository."
    return None


def start_index_job(
    path: str | None,
    sources: list[str] | None = None,
    *,
    allow_env_roots: bool = True,
    env: dict | None = None,
    release=None,
) -> dict:
    """Triggers indexing as a detached subprocess — code, commits, tags
    and branches by default. Does NOT wait for completion; returns as
    soon as the process is launched (or as soon as it's clear the launch
    failed). Use index_job_report() (how far the run got, as it records
    it) and common.index_lock_status() to track it afterward.

    path: a specific repo (validated against index_path_allowed()) — or
    None, meaning "index every repo in repos.json" (`griot index all`
    with no --path). The None case skips the allowlist check entirely:
    the target already IS the full allowed list, there's nothing to
    validate a path against.

    env: the child's environment (see child_env()) — None means inherit
    the host's os.environ as-is (fine for the MCP server, whose own
    os.environ. is never stale relative to itself the way a long-lived
    UI's boot snapshot can be). Either way the child also gets
    GRIOT_INDEX_PROGRESS, the file its progress goes to."""
    sources = sources or list(DEFAULT_INDEX_SOURCES)

    refusal = index_job_refusal(path, allow_env_roots=allow_env_roots)
    if refusal is not None:
        return {"started": False, "reason": refusal, "path": None, "pid": None, "sources": None}

    repo_path = Path(path).resolve() if path is not None else None

    # [real finding, carried over from mcp_server.py] a prior read
    # tool/route in THIS host process may already have memoized the
    # active collection's handle (get_client()) — the subprocess needs to
    # open the SAME directory to write; without releasing it here, it
    # dies with "failed to open WAL ... WouldBlock". The next read
    # reopens it on demand. `release` is how a host with other calls in
    # flight lets go (it answers with a reason when it cannot: closing the
    # collection here regardless took it from under a search in progress).
    busy = (release or common.release_client)()
    if busy:
        return {"started": False, "reason": busy, "path": None, "pid": None, "sources": None}

    log_path = common.LOG_DIR / "griot_index.log"
    common.secure_mkdir(log_path.parent)
    fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)  # repairs a legacy file (O_CREAT only applies the mode at creation)
    except OSError:
        os.close(fd)
        raise

    argv = [sys.executable, "-m", "griot.cli", "index", "all"]
    if repo_path is not None:
        # [real bug fix] prefer --repo <name> when it resolves back to
        # exactly this directory (see _repo_name_if_uniquely_registered()'s
        # docstring for why this matters for incremental reuse) — falls
        # back to --path only when the name is ambiguous or unregistered
        # (e.g. an MCP-only path allowed via GRIOT_MCP_INDEX_ROOTS).
        repo_name = _repo_name_if_uniquely_registered(repo_path)
        if repo_name is not None:
            argv += ["--repo", repo_name]
        else:
            argv += ["--path", str(repo_path)]
    argv += ["--sources", ",".join(sources)]

    # Where the run records its progress (common.progress_begin() and what
    # follows): a file of its own per job, so that two servers' jobs never
    # share one, created 0600 by mkstemp because it names the repository. In
    # the data directory (see PROGRESS_DIR_NAME), where what a dead host
    # leaves is found and removed by the next one.
    common.secure_mkdir(_progress_dir())
    progress_fd, progress_path = tempfile.mkstemp(prefix=_PROGRESS_PREFIX, suffix=_PROGRESS_SUFFIX,
                                                  dir=_progress_dir())
    os.close(progress_fd)
    env = {**(env if env is not None else os.environ), common.INDEX_PROGRESS_ENV: progress_path}

    # start_new_session=True: survives even if the caller (an MCP tool
    # call, an HTTP request) is cancelled/disconnects. explicit stdout=/
    # stderr=: without them the subprocess would inherit the host's FDs —
    # for the MCP server, stdout IS the JSON-RPC transport.
    try:
        with os.fdopen(fd, "a") as log_fh:
            proc = subprocess.Popen(argv, stdout=log_fh, stderr=log_fh, start_new_session=True, env=env)
    except BaseException:
        os.unlink(progress_path)
        raise
    time.sleep(_LIVENESS_CHECK_SECONDS)
    if proc.poll() is not None:
        os.unlink(progress_path)
        return {"started": False, "reason": f"process died immediately (exit code {proc.returncode}) — see {log_path}.", "path": None, "pid": None, "sources": None}

    # What tells this process apart from a later one given the same pid,
    # for a host that reloads the record (_same_process_alive()). Read now,
    # while the child cannot have been reaped: it was alive just above.
    try:
        process_start_time = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        process_start_time = None  # followed by this host, never by a later one

    with _registry_lock:
        _ensure_loaded()
        _registry[proc.pid] = {
            "path": str(repo_path) if repo_path is not None else None,
            "sources": sources,
            "started_at": time.time(),
            "proc": proc,
            "process_start_time": process_start_time,
            "progress_path": progress_path,
            "log_path": str(log_path),
            "finished": None,
        }
        _save()

    return {"started": True, "path": str(repo_path) if repo_path is not None else None, "pid": proc.pid, "sources": sources, "reason": None}


def _run_dry_run(argv: list[str], env: dict, timeout: float) -> "subprocess.CompletedProcess":
    """The subprocess of a preview. Its own function so that a test can
    stand in for it without also standing in for the git calls the path
    check makes."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)


def _last_lines(text: str, count: int = 3) -> str:
    """The end of a subprocess's output, as one line, without the progress
    bars: they redraw themselves with carriage returns, and split only on
    line breaks they came along with the message."""
    lines = [segment.strip() for line in text.splitlines() for segment in line.split("\r")]
    kept = [line for line in lines if line and "%|" not in line and "it/s]" not in line]
    return common.printable(" ".join(kept[-count:]))[:400]


def run_index_preview(path: str, sources: list[str] | None = None, *, allow_env_roots: bool = True,
                      env: dict | None = None, timeout: float = 300, release=None) -> dict:
    """What indexing `path` WOULD do, without doing it: the same
    `griot index all --dry-run` a person runs, in a subprocess, with its
    counts read back as data. Nothing is embedded, removed or recorded, and
    no collection is created.

    In a subprocess for the reason start_index_job() uses one: a dry run
    opens the collection and reads every file, which does not belong in a
    long-lived host. Unlike a real run it is waited for: there is nothing to
    follow afterwards, the counts are the answer. The same paths are allowed
    as for a real run, and for the same reason (it reads the repository).

    release: lets go of this process's handle on the collection so that the
    subprocess can open it, and returns a reason when it must not (another
    call is using it). Defaults to common.release_client."""
    result = {"ok": False, "reason": None, "path": None, "profile": common.ACTIVE_PROFILE_NAME,
              "paid": common.ACTIVE_PROFILE["backend"] != "local", "sources": [], "notes": [],
              "to_embed": 0, "up_to_date": 0, "stale": 0, "held_back": 0, "estimated_cost_usd": None}
    refusal = index_job_refusal(path, allow_env_roots=allow_env_roots)
    if refusal is not None:
        return {**result, "reason": refusal}

    repo_path = Path(path).resolve()
    sources = list(dict.fromkeys(sources or DEFAULT_INDEX_SOURCES))  # asked for twice is asked for once
    busy = (release or common.release_client)()  # the subprocess opens the same collection (see start_index_job)
    if busy:
        return {**result, "path": str(repo_path), "reason": busy}

    argv = [sys.executable, "-m", "griot.cli", "index", "all", "--dry-run"]
    repo_name = _repo_name_if_uniquely_registered(repo_path)
    argv += ["--repo", repo_name] if repo_name is not None else ["--path", str(repo_path)]
    argv += ["--sources", ",".join(sources)]

    fd, report_path = tempfile.mkstemp(prefix="griot-dry-run-", suffix=".jsonl")
    os.close(fd)
    try:
        try:
            done = _run_dry_run(argv, {**(env if env is not None else os.environ),
                                       "GRIOT_DRY_RUN_REPORT": report_path, "TQDM_DISABLE": "1"}, timeout)
        except subprocess.TimeoutExpired:
            return {**result, "path": str(repo_path),
                    "reason": f"the dry run did not finish in {int(timeout)} seconds. Run `griot index all --dry-run` "
                              f"in a terminal to see it through."}
        if done.returncode != 0:
            return {**result, "path": str(repo_path),
                    "reason": _last_lines(done.stderr or done.stdout or "")
                              or f"the dry run exited with status {done.returncode}."}
        records = []
        with open(report_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass

    # A source with nothing to look at (no tags, say) reports nothing: zeros.
    by_source = {record["source"]: record for record in records}
    rows = [{"source": source, **{key: by_source.get(source, {}).get(key, 0)
                                  for key in ("to_embed", "up_to_date", "stale", "held_back")}}
            for source in sources]
    chars = sum(record.get("to_embed_chars", 0) for record in records)
    price = common.ACTIVE_PROFILE.get("price_per_1m_tokens")
    held_back = sum(row["held_back"] for row in rows)
    notes = []
    if held_back:
        notes.append(f"{held_back} stale point(s) are more than half of what is indexed for this repository, so a "
                     f"plain run leaves them (another branch checked out? files missing?). If they are really "
                     f"gone, `griot index all --repo <name> --prune` in a terminal removes them; the MCP "
                     f"indexing tool cannot.")
    if repo_name is None:
        notes.append("This directory is not registered under a name of its own, so it is indexed by path: stale "
                     "points are never removed for it. `griot repos add` registers it.")
    return {**result, "ok": True, "path": str(repo_path), "sources": rows, "notes": notes,
            "to_embed": sum(row["to_embed"] for row in rows),
            "up_to_date": sum(row["up_to_date"] for row in rows),
            "stale": sum(row["stale"] for row in rows),
            "held_back": held_back,
            # An estimate from the size of the text; the bill comes from the
            # provider's own token count.
            "estimated_cost_usd": (round(chars / common.CHARS_PER_TOKEN_ESTIMATE / 1_000_000 * price, 4)
                                   if result["paid"] and price is not None else None)}


_quality_check_lock = threading.Lock()


def run_quality_check_job(sample_size: int, *, collection: str | None = None, env: dict | None = None, timeout: float = 600) -> dict:
    """Runs `griot quality-check --json --skip-golden-set` as a
    SYNCHRONOUS, blocking subprocess call — never in-process. This was the
    plan's decision A for a quality-check trigger in a long-lived host (that
    host is gone; the reasoning is why it stays a subprocess):
    `quality_check.run_self_check()` internally calls common.search(),
    which would load the embedding model (640MB-1.1GB on the default
    profile) into whichever process calls it AND, for the active
    collection, memoize common._client (get_client()) — exactly the two
    invariants a long-lived host process must never violate (see
    common.get_index_status()'s reuse_active_handle docstring). A
    subprocess makes memory release the KERNEL's job when it exits,
    instead of hoping ONNX Runtime's destructors run promptly — and it's
    a small, one-time cost (import overhead only; the model load is paid
    either way) for that guarantee.

    Single-flight: refuses a second call while one is already running —
    That host served requests on threads, and two concurrent
    quality-checks would each pay the embedding-model-load cost in its
    own process at the same time, real memory pressure for no reason.

    Contract with the child's stdout (see quality_check.py's --json mode):
    valid JSON on success OR on a self-check that found failures (exit 1
    is a RESULT there, not an execution error — quality_check.main()
    itself distinguishes the two). Empty/non-JSON stdout means something
    broke before a result could even be produced; the real reason is on
    stderr in that case.

    collection: [jobs] forwarded as `--collection <name>` when
    given, letting a caller check a collection other than the active
    profile's (quality_check.py's CLI already supported this; only the
    that host's own trigger never threaded it through). None (default)
    omits the flag entirely — byte-identical to the pre-existing argv, so
    every caller that never selects a collection keeps checking the
    active one exactly as before."""
    acquired = _quality_check_lock.acquire(blocking=False)
    if not acquired:
        return {"ok": False, "reason": "a quality check is already running"}
    try:
        argv = [
            sys.executable, "-m", "griot.cli", "quality-check",
            "--json", "--skip-golden-set", "--sample-size", str(sample_size),
        ]
        if collection:
            argv += ["--collection", collection]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": f"quality check timed out after {timeout}s"}

        stdout = proc.stdout.strip()
        if not stdout:
            return {"ok": False, "reason": proc.stderr.strip() or f"process exited {proc.returncode} with no output"}
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            # [security review finding] quality_check.py only suppresses its
            # OWN print() calls under --json — embed_texts()'s retry warnings
            # (log_and_print(echo=True) on a 429/connection retry, deep in
            # common.search()) still print straight to stdout. The JSON
            # payload is always the LAST line quality_check.py writes in
            # --json mode (see its main()), so retry a parse against just
            # the last line before treating the output as genuinely broken.
            last_line = stdout.rsplit("\n", 1)[-1]
            try:
                result = json.loads(last_line)
            except json.JSONDecodeError:
                return {"ok": False, "reason": proc.stderr.strip() or "quality-check produced invalid output"}

        return {"ok": True, "result": result, "exit_code": proc.returncode}
    finally:
        _quality_check_lock.release()
