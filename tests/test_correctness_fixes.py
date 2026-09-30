"""Defects found by running griot against what really happens: an embedding
call that fails, a server that shares its stdout with a protocol, a typo in a
repository name, an empty index, a damaged one, a relative path, two
repositories with the same directory name."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from griot import cli, common, quality_check, repos

INDEXERS = ["index_code", "index_commits", "index_tags", "index_branches", "index_platform"]


# --- a failed embedding of the QUERY says so -------------------------------------


def test_a_search_whose_query_could_not_be_embedded_says_why(monkeypatch):
    """The embedding call returns None for what it could not embed. Passed on
    as the query vector it became a TypeError about vector kinds, which is
    what an agent saw and what was recorded, instead of the cause."""
    def failing(texts, **kw):
        common._note_embedding_failure("HTTP 401 from the embedding API")
        return [None for _ in texts]

    monkeypatch.setattr(common, "embed_texts", failing)
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert "could not be embedded" in str(error.value) and "HTTP 401" in str(error.value)


def test_the_reason_of_an_embedding_failure_is_kept_for_the_caller(monkeypatch):
    common._note_embedding_failure("first")
    common._note_embedding_failure("HTTP 429 after 3 attempts")
    assert common.last_embedding_failure() == "HTTP 429 after 3 attempts"


# --- the MCP server's stdout carries the protocol and nothing else ------------------


def test_once_the_server_starts_notices_go_to_stderr(monkeypatch, capsys):
    from griot import mcp_server
    monkeypatch.setattr(common, "_echo_stream", None)
    common.log_and_print("before")
    mcp_server._keep_stdout_for_the_protocol()
    common.log_and_print("Warning: something an operator should read", level="warning")
    out, err = capsys.readouterr()
    assert out.strip() == "before"
    assert "something an operator should read" in err


def test_a_real_server_writes_only_protocol_messages_to_stdout(tmp_path):
    """Through stdio, with the collection held by this process so that the
    status tool takes the branch that warns. Every line the server writes to
    stdout must be a JSON-RPC message: anything else corrupts the stream the
    client is reading."""
    import qdrant_edge as qe
    env = {**os.environ, "GRIOT_MCP_IDLE_RELEASE_SECONDS": "3600"}
    holder = common.get_client()  # this process holds the active collection
    assert holder is not None
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "griot_index_status", "arguments": {}}},
    ]
    process = subprocess.Popen([sys.executable, "-m", "griot.mcp_server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        answered = False
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        first = []
        while not answered:
            line = process.stdout.readline()
            if not line:
                break
            first.append(line)
            answered = _is_json(line) and json.loads(line).get("id") == 2
        # Closing stdin ends the server normally, so whatever it still held
        # in a buffer is written out: a stray line is not always written
        # before the answer it belongs to.
        rest, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    lines = first + rest.splitlines(keepends=True)
    assert answered, stderr[-2000:]
    not_json = [line for line in lines if line.strip() and not _is_json(line)]
    assert not_json == [], not_json
    assert "held by another process" in stderr, "the warning this test provokes must still be written somewhere"


def _is_json(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


# --- an indexer that cannot start exits with an error -------------------------------


@pytest.mark.parametrize("module", INDEXERS)
def test_a_repository_name_that_matches_nothing_is_an_error(module, tmp_path, capsys):
    path = tmp_path / "proj"
    path.mkdir()
    repos.add_repo(str(path))
    mod = importlib.import_module(f"griot.{module}")
    assert mod.main(["--repo", "typo"]) == 1
    out, err = capsys.readouterr()
    assert "no repo named 'typo'" in err and "no repo named" not in out


@pytest.mark.parametrize("module", INDEXERS)
def test_a_missing_repos_file_is_an_error(module, capsys):
    mod = importlib.import_module(f"griot.{module}")
    assert not common.REPOS_JSON_PATH.exists()
    assert mod.main([]) == 1
    assert "not found" in capsys.readouterr().err


def test_index_all_does_not_announce_success_when_nothing_could_run(tmp_path, capsys):
    path = tmp_path / "proj"
    path.mkdir()
    repos.add_repo(str(path))
    assert cli.main(["index", "all", "--repo", "typo"]) != 0
    assert "all sources completed" not in capsys.readouterr().out


@pytest.mark.parametrize("module", INDEXERS)
def test_a_run_with_nothing_to_index_is_still_a_success(module, tmp_path):
    """Not an error: a repository without tags, without branches, without a
    recognised platform. Only "could not start" is one."""
    path = tmp_path / "proj"
    path.mkdir()
    repos.add_repo(str(path))
    mod = importlib.import_module(f"griot.{module}")
    assert mod.main(["--repo", "proj"]) in (None, 0)


# --- a quality check that measured nothing did not pass ------------------------------


def test_a_quality_check_over_an_empty_collection_fails(capsys):
    common.get_client()  # the collection exists and is empty
    with pytest.raises(SystemExit) as exit_info:
        quality_check.main(["--skip-golden-set"])
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "nothing" in out.lower() and "Quality OK" not in out


def test_the_json_form_says_not_ok_too(capsys):
    common.get_client()
    with pytest.raises(SystemExit):
        quality_check.main(["--skip-golden-set", "--json"])
    assert json.loads(capsys.readouterr().out)["ok"] is False


# --- a damaged collection is not "held by another process" -----------------------------


def _damage_the_collection():
    client = common.get_client()
    assert client is not None
    common.release_client()
    marker = common._collection_path(common.COLLECTION_NAME) / common._EDGE_CONFIG_MARKER
    marker.write_text(marker.read_text()[:20])


def test_opening_a_damaged_collection_reports_the_damage_at_once(monkeypatch):
    monkeypatch.setattr(common, "_LOCK_RETRY_DELAYS", (30, 30, 30))  # a retry would hang the test
    _damage_the_collection()
    with pytest.raises(Exception) as error:
        common.get_client()
    assert not isinstance(error.value, common.CollectionBusyError)
    assert "another griot process" not in str(error.value)


def test_status_says_a_damaged_collection_is_unreadable_not_busy(monkeypatch):
    monkeypatch.setattr(common, "_LOCK_RETRY_DELAYS", (30, 30, 30))
    _damage_the_collection()
    status = common.get_index_status()
    assert status["points_count"] is None
    assert status["points_error"] and status["points_error"].startswith("unreadable")


def test_status_of_a_readable_collection_has_no_error():
    common.get_client()
    status = common.get_index_status()
    assert status["points_count"] == 0 and status["points_error"] is None


# --- `--path .` names the repository ---------------------------------------------------


@pytest.mark.parametrize("module", ["index_code", "index_commits", "index_tags", "index_branches"])
def test_a_relative_path_still_gives_the_repository_its_name(module, git_repo, monkeypatch):
    git_repo.commit("one", filename="a.py")
    git_repo.tag("v1", "first")
    git_repo.branch("feature")
    git_repo.set_remote_head("main")
    monkeypatch.chdir(git_repo.path)
    mod = importlib.import_module(f"griot.{module}")
    seen = {}
    monkeypatch.setattr(common, "index_documents", lambda docs, **kw: seen.setdefault("docs", docs) and (len(docs), 0, 0))
    mod.main(["--path", "."])
    assert seen["docs"], "the fixture gives every source something to index"
    assert {doc["metadata"]["repo"] for doc in seen["docs"]} == {git_repo.path.name}
    assert all(doc["id"].startswith(git_repo.path.name + "-") for doc in seen["docs"])


def test_golden_set_suggest_names_the_repository_for_a_relative_path(git_repo, monkeypatch):
    from griot import golden_set
    git_repo.commit("Add the login flow", filename="login.py")
    monkeypatch.chdir(git_repo.path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    golden_set.cmd_suggest(".")
    cases = golden_set.list_cases()
    assert cases and all(entry["repo"] == git_repo.path.name for case in cases for entry in case["must_include"])


# --- two registered repositories may not share a directory name ---------------------------


def test_a_second_repository_with_the_same_directory_name_is_refused(tmp_path):
    """Both would write `repo: api` and the same ids: each run overwrites the
    other's points and re-embeds them, forever, while reporting success."""
    first = tmp_path / "client-a" / "api"
    second = tmp_path / "client-b" / "api"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    repos.add_repo(str(first))
    with pytest.raises(ValueError) as error:
        repos.add_repo(str(second))
    assert "api" in str(error.value) and str(first.resolve()) in str(error.value)
    assert repos.repo_status()[0]["path"] == str(first.resolve()) and len(repos.repo_status()) == 1


def test_the_check_before_asking_refuses_it_as_well(tmp_path):
    first = tmp_path / "a" / "api"
    second = tmp_path / "b" / "api"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    repos.add_repo(str(first))
    with pytest.raises(ValueError):
        repos.check_add(str(second))


# --- the command a refusal points to indexes under the same ids ----------------------------


@pytest.mark.anyio
async def test_the_index_hint_for_a_registered_repository_uses_its_name(monkeypatch, tmp_path):
    """`--path` keys the ids differently from the registered name: pasted for
    a registered repository it would embed everything again under new ids
    and leave the old points behind."""
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    from griot import mcp_server
    importlib.reload(mcp_server)
    try:
        path = tmp_path / "proj"
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        repos.add_repo(str(path))

        class _Ctx:
            client_capabilities = None

        out = await mcp_server.griot_index_repo(str(path), ctx=_Ctx())
        assert "griot index all --repo=proj" in out["reason"] and "--path" not in out["reason"]
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX")
        importlib.reload(mcp_server)


# --- the same defects, on the other surface and in the real code paths -------------------


@pytest.mark.anyio
async def test_the_mcp_quality_check_over_nothing_is_an_error_and_creates_nothing():
    """The CLI stopped passing an empty collection; the tool an agent is told
    to use returned zeros with no error, which reads as "nothing failed", and
    created the collection on the way."""
    from mcp.client.client import Client
    from griot import mcp_server
    assert not common.collection_exists(common.COLLECTION_NAME)
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {})
    assert result.is_error is True
    assert "not measured" in str(result.content).lower()
    assert not common.collection_exists(common.COLLECTION_NAME), "a check must not create what it checks"


@pytest.mark.anyio
async def test_the_mcp_quality_check_over_an_empty_collection_is_an_error_too():
    from mcp.client.client import Client
    from griot import logdb, mcp_server
    common.get_client()  # the collection exists and holds nothing
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {})
    assert result.is_error is True and "not measured" in str(result.content).lower()
    assert logdb.read_since(common.LOG_DIR, "quality_checks", days=1) == [], "a check that measured nothing leaves no trend point"


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code, self._payload, self.text = status_code, payload or {}, "error body"
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise common.requests.HTTPError(f"{self.status_code} Client Error", response=self)


@pytest.fixture
def openai_profile(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)


def test_the_reason_reaches_the_search_error_through_the_real_embedding_path(openai_profile, monkeypatch):
    monkeypatch.setattr(common.requests, "post", lambda *a, **k: _Response(401))
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert "could not be embedded" in str(error.value) and "401" in str(error.value)


def test_a_reason_from_an_earlier_failure_is_not_given_for_a_later_one(openai_profile, monkeypatch):
    """A server runs for days. Yesterday's rate limit must not be reported as
    the cause of today's failure."""
    common._note_embedding_failure("OLD: 429 rate limit")
    answer = {"data": [], "usage": {"total_tokens": 1}}  # a 200 that carries no vector for the text
    monkeypatch.setattr(common.requests, "post", lambda *a, **k: _Response(200, answer))
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert "OLD" not in str(error.value) and "no vector" in str(error.value)


def test_a_call_that_succeeds_leaves_no_reason_behind(openai_profile, monkeypatch):
    """`last_embedding_failure()` is about the most recent call. Every path
    that fails says why today; this holds the promise for one that forgets."""
    common._note_embedding_failure("OLD: 429 rate limit")
    answer = {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 1}}
    monkeypatch.setattr(common.requests, "post", lambda *a, **k: _Response(200, answer))
    assert common.embed_texts(["anything"]) == [[0.1, 0.2]]
    assert common.last_embedding_failure() is None


def test_the_reason_reaches_the_search_error_on_the_gemini_path_too(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "gemini")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["gemini"])

    def unavailable(*args, **kwargs):
        raise common.GeminiUnavailable("503 from the embedding API after 3 attempts")

    monkeypatch.setattr(common, "_gemini_post_with_retry", unavailable)
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert "503 from the embedding API" in str(error.value)


def test_a_gemini_answer_with_fewer_vectors_than_texts_is_a_failure_with_a_reason(monkeypatch):
    """The answer carries no index, so a short list cannot be lined up with
    the texts. It used to be an IndexError in the search."""
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "gemini")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["gemini"])
    monkeypatch.setattr(common, "_gemini_post_with_retry", lambda *a, **k: {"embeddings": []})
    with pytest.raises(RuntimeError) as error:
        common.search("anything")
    assert "0 vector(s) for 1 text(s)" in str(error.value)


# --- a registry that names no directory is an error, like a name that matches nothing ------


@pytest.mark.parametrize("module", INDEXERS)
def test_when_no_registered_path_is_a_directory_the_run_fails(module, tmp_path, capsys):
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(tmp_path / "gone-a"), str(tmp_path / "gone-b")]))
    mod = importlib.import_module(f"griot.{module}")
    assert mod.main([]) == 1
    assert "is a directory" in capsys.readouterr().err


def test_one_missing_path_among_valid_ones_is_only_a_warning(tmp_path, capsys):
    from griot import index_code
    good = tmp_path / "proj"
    good.mkdir()
    (good / "a.py").write_text("x = 1\n")
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text(json.dumps([str(good), str(tmp_path / "gone")]))
    common.embed_texts = common.embed_texts  # no-op: the dry run below embeds nothing
    assert index_code.main(["--dry-run"]) in (None, 0)
    assert "not a valid directory" in capsys.readouterr().out


# --- what the report says about a collection it cannot count -------------------------------


@pytest.mark.parametrize("error,expected", [
    ("busy", "collection in use"),
    (None, "collection in use"),
    ("unreadable: Failed to deserialize edge_config.json", "could not be read"),
])
def test_stats_tells_a_busy_collection_from_an_unreadable_one(error, expected):
    from griot import stats
    report = stats.compute_stats([], [], {"points_count": None, "points_error": error, "embed_profile": "any"})
    line = next(line for line in stats.format_stats(report, 7).splitlines() if line.startswith("Index:"))
    assert expected in line
    if error and error.startswith("unreadable"):
        assert "edge_config.json" in line and "in use" not in line


@pytest.mark.parametrize("module", INDEXERS)
def test_an_empty_registry_is_an_error_that_says_what_to_do(module, capsys):
    common.REPOS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    common.REPOS_JSON_PATH.write_text("[]")
    mod = importlib.import_module(f"griot.{module}")
    assert mod.main([]) == 1
    err = capsys.readouterr().err
    assert "no repository is registered" in err and "griot repos add" in err
    assert "0 path" not in err


@pytest.mark.parametrize("module", INDEXERS)
def test_the_module_run_directly_exits_with_the_same_status(module, tmp_path):
    """`python -m griot.index_code` called main() and threw its return value
    away, so the failure was visible only through the `griot` command."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "config"), GRIOT_DATA_DIR=str(tmp_path / "data"))
    done = subprocess.run([sys.executable, "-m", f"griot.{module}", "--repo", "no-such-repository"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 1, done.stderr
    assert "Error:" in done.stderr
