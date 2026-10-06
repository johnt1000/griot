"""Which tools may run without asking, decided by what each one DOES.

tools_safe_to_preapprove() reads the server's own markers: the read-only
hint, the human-only marker and a named list of exceptions. The test that held
it to a real client's view (test_preapprove_public_list.py) filtered that view
with the same three readings, so a misreading both shared (the exception list
above all, which it imported from the server) passed either way.

The expected set here comes from behaviour alone, through a real in-memory MCP
client, without reading any annotation, marker or list of the server's: every
tool is called in a throwaway world with arguments that do what it is for, and
is safe to pre-approve when that call

- changes no file (griot's own record of the call is not a change: every call
  is recorded, the safe ones included, and the offer says so);
- asks no person;
- costs no more than a search: at most one text embedded;
- reads part of the index, not all of it;
- reads at most where a repository's refs point: no history, no files, and no
  program started but git.

The costly part is stood in for: the embedding is a hash, so the test loads no
model and spends nothing."""

import hashlib
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from mcp.types import ToolAnnotations
from mcp_types import ElicitResult

from griot import common, logdb, mcp_server, repos

# Points in the throwaway index: more than any call that reads "some" of it
# asks for, so a call that read this many read all of it.
INDEX_POINTS = 120

# What git is asked that reads only where refs point, not what they hold.
# Anything else (log, show, diff, cat-file, ls-files...) reads history or
# files, and costs more the larger the repository.
_GIT_REF_READS = frozenset({"rev-parse", "for-each-ref", "symbolic-ref", "show-ref"})

# The rows griot writes for every call, the safe ones included: the call log
# and, for a search, the query. Every other table is state.
_PER_CALL_RECORD = frozenset({"tool_calls", "queries"})

# Bookkeeping of the log itself, not state a call changes: SQLite's counter
# of the ids each table handed out (the per-call tables' move with them), and
# the marker of the one-time import of older log files, written by whichever
# read comes first on an install that has never had it.
_LOG_BOOKKEEPING = frozenset({"sqlite_sequence", "_jsonl_migrated"})

# Programs started while a call runs. An audit hook cannot be removed, so it
# is installed once and records only while a call is being observed.
_spawns: list[list[str]] | None = None


def _record_spawn(event, args):
    if _spawns is None:
        return
    if event == "subprocess.Popen":
        argv = args[1] if isinstance(args[1], (list, tuple)) else [args[1]]
        _spawns.append([str(word) for word in argv])
    elif event == "os.system":
        _spawns.append(["sh", "-c", str(args[0])])


sys.addaudithook(_record_spawn)


def _git_subcommand(argv: list[str]) -> str | None:
    """The subcommand of a git command line, past git's own options."""
    takes_a_value = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}
    rest = argv[1:]
    while rest:
        word = rest.pop(0)
        if word in takes_a_value:
            rest = rest[1:]
        elif not word.startswith("-"):
            return word
    return None


def _reads_more_than_refs(argv: list[str]) -> bool:
    program = argv[0].rsplit("/", 1)[-1] if argv else ""
    return program != "git" or _git_subcommand(argv) not in _GIT_REF_READS


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


class _CountingIndex:
    """The index, counting the points handed out of it."""

    def __init__(self, real):
        self.real, self.read = real, 0

    def scroll(self, request):
        points, offset = self.real.scroll(request)
        self.read += len(points)
        return points, offset

    def query(self, request):
        points = self.real.query(request)
        self.read += len(points)
        return points

    def retrieve(self, *args, **kwargs):
        points = self.real.retrieve(*args, **kwargs)
        self.read += len(points)
        return points

    def __getattr__(self, name):
        return getattr(self.real, name)


def _state(root) -> dict:
    """Every file under `root` by content, and the log database by table,
    without the per-call record."""
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob("*"))
             if p.is_file() and not p.is_symlink() and not p.name.startswith(logdb.DB_FILENAME)}
    tables = {}
    db = common.LOG_DIR / logdb.DB_FILENAME
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for name in names:
                if name not in _PER_CALL_RECORD | _LOG_BOOKKEEPING:
                    tables[name] = sorted(map(repr, conn.execute(f'SELECT * FROM "{name}"')))
        finally:
            conn.close()
    return {"files": files, "tables": tables}


def _changes(before: dict, after: dict) -> list[str]:
    changed = [path for path in before["files"].keys() | after["files"].keys()
               if before["files"].get(path) != after["files"].get(path)]
    changed += [f"{logdb.DB_FILENAME}:{name}" for name in before["tables"].keys() | after["tables"].keys()
                if before["tables"].get(name) != after["tables"].get(name)]
    return sorted(changed)


class _World:
    """A registered repository, an index of INDEX_POINTS points and one
    curated case, all under the test's own directory, with the embedding and
    the index access counted."""

    def __init__(self, root, repository, monkeypatch):
        self.root, self.repository = root, repository
        repository.commit("Fix the login bug", filename="auth.py", content="def login():\n    return 2\n")
        repository.commit("Add search", filename="search.py", content="def search():\n    return 1\n")
        repos.add_repo(str(repository.path))
        self.unregistered = root / "unregistered"
        self.unregistered.mkdir()
        subprocess.run(["git", "init", "-q", str(self.unregistered)], check=True)
        home = root / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.chdir(home)

        self.embedded: list[str] = []

        def embed(texts, **kwargs):
            self.embedded.extend(texts)
            return [_vector(t, common.EMBED_DIM) for t in texts]

        monkeypatch.setattr(common, "embed_texts", embed)
        name = repository.path.name
        common.index_documents([
            {"id": f"{name}:code:f{n}.py:0", "content": f"function number {n} retries the request",
             "metadata": {"source_type": "code", "repo": name, "file_path": f"f{n}.py", "chunk_index": 0}}
            for n in range(INDEX_POINTS)])
        common.release_lock()
        # An install that has been used has its log: without it, the first
        # call to write a row would show as having created every table.
        logdb._connect(common.LOG_DIR).close()
        common.GOLDEN_SET_PATH.write_text(json.dumps([
            {"query": "function number 1 retries the request", "must_include": [{"repo": name}], "limit": 5}]))

        self.indexes: dict[int, _CountingIndex] = {}
        real_get_client = common.get_client

        def counting_get_client(*args, **kwargs):
            real = real_get_client(*args, **kwargs)
            return self.indexes.setdefault(id(real), _CountingIndex(real))

        monkeypatch.setattr(common, "get_client", counting_get_client)

    def arguments(self, tool) -> dict:
        """Arguments that make the tool do what it is for. Only a tool with a
        required argument needs an entry: a new one fails here, by name,
        until it is given one."""
        name = self.repository.path.name
        given = {
            "griot_search": {"query": "retries the request"},
            "griot_golden_set_suggest": {"path": str(self.repository.path)},
            "griot_index_preview": {"path": str(self.repository.path)},
            "griot_repos_add": {"path": str(self.unregistered)},
            "griot_repos_remove": {"path": str(self.repository.path)},
            "griot_profiles_delete": {"profile": "openai-small"},
            "griot_golden_set_add": {"query": "where is login", "must_include": [{"repo": name}]},
            "griot_golden_set_remove": {"index": 0},
            "griot_assist_install": {"harness": "claude-code", "scope": "global"},
        }
        if tool.name in given:
            return given[tool.name]
        required = (tool.input_schema or {}).get("required") or []
        assert not required, f"{tool.name} needs arguments that do what it is for: add them to _World.arguments"
        return {}

    async def observe(self, client, asked: list, tool) -> dict:
        global _spawns
        before = _state(self.root)
        asked.clear()
        self.embedded.clear()
        for index in self.indexes.values():
            index.read = 0
        _spawns = []
        try:
            result = await client.call_tool(tool.name, self.arguments(tool))
            spawned = _spawns
        finally:
            _spawns = None
        # A call that failed did nothing, and would pass every check below
        # for that reason alone.
        assert result.is_error is False, (tool.name, result.content)
        return {
            "changed": _changes(before, _state(self.root)),
            "asked": len(asked),
            "embedded": len(self.embedded),
            "points_read": sum(index.read for index in self.indexes.values()),
            "beyond_refs": [argv for argv in spawned if _reads_more_than_refs(argv)],
        }


def _safe(seen: dict) -> bool:
    return (not seen["changed"] and not seen["asked"] and seen["embedded"] <= 1
            and seen["points_read"] < INDEX_POINTS and not seen["beyond_refs"])


@pytest.fixture
def world(tmp_path, git_repo, monkeypatch):
    return _World(tmp_path, git_repo, monkeypatch)


async def _observe(world, only: set[str] | None = None) -> dict[str, dict]:
    asked = []

    async def decline(ctx, params):
        # Declined, so that a tool that asks changes nothing either way.
        asked.append(params)
        return ElicitResult(action="decline")

    async with Client(mcp_server.mcp, elicitation_callback=decline) as client:
        tools = (await client.list_tools()).tools
        return {tool.name: await world.observe(client, asked, tool)
                for tool in tools if only is None or tool.name in only}


def test_the_tools_offered_are_the_ones_that_behave_safely(world):
    seen = anyio.run(_observe, world)
    behaves_safely = sorted(name for name, observed in seen.items() if _safe(observed))
    offered = mcp_server.tools_safe_to_preapprove()
    assert offered == behaves_safely, {name: seen[name] for name in set(offered) ^ set(behaves_safely)}
    # Not vacuous: a search is offered, and some tool is not.
    assert "griot_search" in offered and len(offered) < len(seen)


# --- the oracle tells them apart: a tool marked read-only that does one unsafe thing --------


def _writes_a_file() -> str:
    """Says it only reads, and writes a file."""
    (common.CONFIG_DIR / "written.txt").write_text("x")
    return "x"


def _embeds_two_texts() -> str:
    """Says it only reads, and embeds two texts."""
    common.embed_texts(["one", "two"])
    return "x"


def _reads_the_whole_index() -> str:
    """Says it only reads, and reads every point."""
    import qdrant_edge as qe
    client, offset = common.get_client(), None
    while True:
        _, offset = client.scroll(qe.ScrollRequest(limit=7, offset=offset, with_payload=True))
        if offset is None:
            return "x"


def _reads_the_history() -> str:
    """Says it only reads, and reads a repository's history."""
    subprocess.run(["git", "-C", repos._load()[0], "log", "--oneline"], capture_output=True, check=True)
    return "x"


def _starts_another_program() -> str:
    """Says it only reads, and starts a program."""
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    return "x"


def _reads_where_a_ref_points() -> str:
    """Says it only reads, and asks git only where HEAD points."""
    subprocess.run(["git", "-C", repos._load()[0], "rev-parse", "HEAD"], capture_output=True, check=True)
    return "x"


def _only_reads() -> str:
    """Says it only reads, and only reads."""
    return str(common.GOLDEN_SET_PATH.exists())


@pytest.mark.parametrize("behaviour,safe", [
    (_writes_a_file, False), (_embeds_two_texts, False), (_reads_the_whole_index, False),
    (_reads_the_history, False), (_starts_another_program, False),
    (_reads_where_a_ref_points, True), (_only_reads, True),
])
def test_a_tool_is_judged_by_what_it_does_not_by_its_marker(world, behaviour, safe):
    """The server trusts the marker, so it offers every one of these. Their
    behaviour says otherwise for the unsafe ones: the disagreement the test
    above reports for a real tool marked like them."""
    name = "griot_fake" + behaviour.__name__
    mcp_server.mcp.add_tool(behaviour, name=name, annotations=ToolAnnotations(readOnlyHint=True))
    try:
        assert name in mcp_server.tools_safe_to_preapprove()
        seen = anyio.run(_observe, world, {name})
        assert _safe(seen[name]) is safe, seen[name]
    finally:
        mcp_server.mcp.remove_tool(name)
