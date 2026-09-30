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
import threading
import time
from pathlib import Path

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
# pid -> {"path": str | None, "sources": list[str], "started_at": float, "proc": Popen}
_registry: dict[int, dict] = {}

def running_index_job() -> dict | None:
    """[risk 4] common.index_lock_status()["running"] alone is NOT a
    reliable "is an index job in progress" signal: acquire_lock()/
    release_lock() are scoped to a single index_documents() call (one per
    SOURCE), not to the whole `index all` run — the discovery phase
    (walking files, `git log`) that precedes each source runs with NO lock
    held, often for minutes on a real repo. Asking during that window
    would report "idle" even though a job this process launched is very
    much still alive.

    This in-process registry tracks jobs BY THE PROCESS THIS HOST ITSELF
    SPAWNED — proc.poll() both answers "is it still alive" and reaps any
    zombie (a Popen object whose process already exited but was never
    waited on). Returns the first still-alive entry (in practice there's
    at most one, since start_index_job() refuses to spawn a second job
    while one is registered) or None."""
    with _registry_lock:
        dead_pids = []
        alive = None
        for pid, info in _registry.items():
            if info["proc"].poll() is None:
                alive = {"pid": pid, "path": info["path"], "sources": info["sources"], "started_at": info["started_at"]}
            else:
                dead_pids.append(pid)
        for pid in dead_pids:
            del _registry[pid]
        return alive


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
        return f"'{path}' invalid: must exist, be a directory, and be a git repository."
    return None


def start_index_job(
    path: str | None,
    sources: list[str] | None = None,
    *,
    allow_env_roots: bool = True,
    env: dict | None = None,
) -> dict:
    """Triggers indexing as a detached subprocess — code, commits, tags
    and branches by default. Does NOT wait for completion; returns as
    soon as the process is launched (or as soon as it's clear the launch
    failed). Use running_index_job()/common.index_lock_status() to track
    progress afterward.

    path: a specific repo (validated against index_path_allowed()) — or
    None, meaning "index every repo in repos.json" (`griot index all`
    with no --path). The None case skips the allowlist check entirely:
    the target already IS the full allowed list, there's nothing to
    validate a path against.

    env: the child's environment (see child_env()) — None means inherit
    the host's os.environ as-is (fine for the MCP server, whose own
    os.environ. is never stale relative to itself the way a long-lived
    UI's boot snapshot can be)."""
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
    # reopens it on demand.
    common.release_client()

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

    # start_new_session=True: survives even if the caller (an MCP tool
    # call, an HTTP request) is cancelled/disconnects. explicit stdout=/
    # stderr=: without them the subprocess would inherit the host's FDs —
    # for the MCP server, stdout IS the JSON-RPC transport.
    with os.fdopen(fd, "a") as log_fh:
        proc = subprocess.Popen(argv, stdout=log_fh, stderr=log_fh, start_new_session=True, env=env)
    time.sleep(_LIVENESS_CHECK_SECONDS)
    if proc.poll() is not None:
        return {"started": False, "reason": f"process died immediately (exit code {proc.returncode}) — see {log_path}.", "path": None, "pid": None, "sources": None}

    with _registry_lock:
        _registry[proc.pid] = {
            "path": str(repo_path) if repo_path is not None else None,
            "sources": sources,
            "started_at": time.time(),
            "proc": proc,
        }

    return {"started": True, "path": str(repo_path) if repo_path is not None else None, "pid": proc.pid, "sources": sources, "reason": None}


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
