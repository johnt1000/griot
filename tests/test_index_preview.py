"""`griot_index_preview`: what an index run WOULD do, through MCP.

`griot index --dry-run` counts what would be embedded and removed, at no
cost. An agent could only start a real run (when that tool is enabled at
all), so it could not tell the user what a reindex would cost before anyone
decided. The preview runs the same dry run in a subprocess and returns the
counts as data."""

import hashlib
import json
import os
import random
import subprocess

import pytest
from mcp.client.client import Client

from griot import common, index_code, jobs, logdb, mcp_server, repos


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


@pytest.fixture
def project(git_repo):
    """A registered git repository with two files and two commits."""
    git_repo.commit("first", filename="a.py", content="def a():\n    return 1\n")
    git_repo.commit("second", filename="b.md", content="# notes\n\nsome words\n")
    repos.add_repo(str(git_repo.path))
    return git_repo


def _index_in_process(project, monkeypatch):
    """A real index of the code source, with made-up vectors."""
    with monkeypatch.context() as patched:  # only for the index: the preview runs the real code
        patched.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
        assert index_code.main(["--repo", project.path.name]) in (None, 0)
    common.release_lock()


# --- the record a dry run leaves for whoever asked for one ---------------------------------


def test_a_dry_run_writes_its_counts_where_it_is_told_to(project, tmp_path, monkeypatch, capsys):
    report = tmp_path / "report.jsonl"
    monkeypatch.setenv("GRIOT_DRY_RUN_REPORT", str(report))
    assert index_code.main(["--repo", project.path.name, "--dry-run"]) in (None, 0)
    records = [json.loads(line) for line in report.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["source"] == "code" and records[0]["to_embed"] == 2 and records[0]["up_to_date"] == 0
    assert records[0]["stale"] == 0 and records[0]["to_embed_chars"] > 0
    assert "[dry-run] 2 chunks would need to be (re)embedded, 0 are already up to date." in capsys.readouterr().out


def test_without_being_told_a_dry_run_writes_no_file(project, tmp_path, monkeypatch):
    monkeypatch.delenv("GRIOT_DRY_RUN_REPORT", raising=False)
    monkeypatch.chdir(tmp_path)
    index_code.main(["--repo", project.path.name, "--dry-run"])
    assert not list(tmp_path.glob("*.jsonl"))


# --- the preview ----------------------------------------------------------------------------


def test_a_preview_of_a_repository_never_indexed_counts_everything(project):
    preview = jobs.run_index_preview(str(project.path))
    assert preview["ok"] is True and preview["reason"] is None
    by_source = {row["source"]: row for row in preview["sources"]}
    assert by_source["code"]["to_embed"] == 2 and by_source["commits"]["to_embed"] == 2
    assert preview["to_embed"] == sum(row["to_embed"] for row in preview["sources"]) >= 4
    assert preview["up_to_date"] == 0 and preview["stale"] == 0


def test_after_an_index_the_preview_says_what_changed(project, monkeypatch):
    _index_in_process(project, monkeypatch)
    project.commit("third", filename="c.py", content="def c():\n    return 3\n")
    (project.path / "a.py").unlink()

    preview = jobs.run_index_preview(str(project.path), sources=["code"])

    assert preview["ok"] is True
    code = preview["sources"][0]
    assert code == {"source": "code", "to_embed": 1, "up_to_date": 1, "stale": 1, "held_back": 0}
    assert [row["source"] for row in preview["sources"]] == ["code"], "only what was asked for"


def test_a_preview_changes_nothing(project, monkeypatch):
    _index_in_process(project, monkeypatch)
    before = common.get_client().info().points_count
    runs_before = len(logdb.read_since(common.LOG_DIR, "runs", days=1))
    common.release_client()
    (project.path / "a.py").unlink()

    jobs.run_index_preview(str(project.path))

    assert common.get_client().info().points_count == before, "the stale point is counted, not removed"
    assert len(logdb.read_since(common.LOG_DIR, "runs", days=1)) == runs_before, "and no run is recorded"


def test_a_path_that_may_not_be_indexed_is_refused_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_run_dry_run", lambda *a, **k: pytest.fail("ran a dry run for a refused path"))
    preview = jobs.run_index_preview(str(tmp_path))
    assert preview["ok"] is False and "repos add" in preview["reason"]
    assert preview["sources"] == []


def test_the_platform_source_is_not_previewed_unless_asked_for(project, monkeypatch):
    """It lists pull requests and issues over the network, with a token."""
    seen = {}

    def fake(argv, env, timeout):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(jobs, "_run_dry_run", fake)
    jobs.run_index_preview(str(project.path))
    assert "platform" not in seen["argv"][seen["argv"].index("--sources") + 1]
    assert "--dry-run" in seen["argv"]


def test_a_dry_run_that_fails_is_a_reason_not_a_number(project, monkeypatch):
    monkeypatch.setattr(jobs, "_run_dry_run", lambda argv, env, timeout: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="Error: 'griot index code' failed: boom\n"))
    preview = jobs.run_index_preview(str(project.path))
    assert preview["ok"] is False and "boom" in preview["reason"] and preview["sources"] == []


def test_a_dry_run_that_takes_too_long_is_a_reason_too(project, monkeypatch):
    def slow(argv, env, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(jobs, "_run_dry_run", slow)
    preview = jobs.run_index_preview(str(project.path), timeout=1)
    assert preview["ok"] is False and "did not finish" in preview["reason"]


def _reports(records):
    def fake(argv, env, timeout):
        with open(env["GRIOT_DRY_RUN_REPORT"], "w") as f:
            f.writelines(json.dumps(record) + "\n" for record in records)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return fake


def test_on_a_paid_profile_the_preview_estimates_the_cost(project, monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(jobs, "_run_dry_run", _reports([
        {"source": "code", "to_embed": 1000, "up_to_date": 0, "stale": 0, "to_embed_chars": 3_500_000}]))
    preview = jobs.run_index_preview(str(project.path))
    price = common.EMBED_PROFILES["openai-small"]["price_per_1m_tokens"]
    assert preview["paid"] is True and preview["profile"] == "openai-small"
    assert preview["estimated_cost_usd"] == pytest.approx(price, rel=0.01), "3.5M characters is about 1M tokens"


def test_on_a_local_profile_there_is_no_cost_to_estimate(project, monkeypatch):
    monkeypatch.setattr(jobs, "_run_dry_run", _reports([
        {"source": "code", "to_embed": 10, "up_to_date": 0, "stale": 0, "to_embed_chars": 9000}]))
    preview = jobs.run_index_preview(str(project.path))
    assert preview["paid"] is False and preview["estimated_cost_usd"] is None


# --- through the protocol -------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_tool_returns_the_counts(project):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_preview", {"path": str(project.path), "sources": ["code", "commits"]})
    assert result.is_error is False, result.content
    out = result.structured_content
    assert out["ok"] is True and out["to_embed"] == 4
    assert {row["source"] for row in out["sources"]} == {"code", "commits"}


@pytest.mark.anyio
async def test_the_tool_is_there_without_the_indexing_tool(monkeypatch):
    """The preview is what lets an agent say what turning indexing on, or
    running it, would cost. It must not depend on indexing being enabled."""
    assert os.getenv("GRIOT_MCP_ENABLE_INDEX", "").lower() not in ("1", "true")
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert "griot_index_preview" in tools and "griot_index_repo" not in tools
    assert tools["griot_index_preview"].annotations.read_only_hint is True


@pytest.mark.anyio
async def test_the_tool_refuses_a_path_that_is_not_registered(tmp_path):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_preview", {"path": str(tmp_path)})
    assert result.is_error is False and result.structured_content["ok"] is False


@pytest.mark.anyio
async def test_an_unknown_source_is_refused_by_the_schema(project):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_preview", {"path": str(project.path), "sources": ["everything"]})
    assert result.is_error is True


def test_the_preview_is_read_only_and_still_asked_about():
    """It reads every file of a repository and runs for as long as an index
    does: not something to run unprompted, although it changes nothing."""
    assert "griot_index_preview" not in mcp_server.tools_safe_to_preapprove()


# --- what the first review found ---------------------------------------------------------------


def test_a_source_asked_for_twice_is_counted_once(project):
    preview = jobs.run_index_preview(str(project.path), sources=["code", "code"])
    assert [row["source"] for row in preview["sources"]] == ["code"]
    assert preview["to_embed"] == 2


def test_a_preview_on_a_fresh_install_creates_no_collection(project):
    """A dry run opened the collection to compare hashes, and opening one
    that does not exist creates it. "Changes nothing" has to hold for the
    first preview too: with no collection, everything is to embed."""
    assert not common.collection_exists(common.COLLECTION_NAME)
    preview = jobs.run_index_preview(str(project.path))
    assert preview["ok"] is True and preview["to_embed"] >= 4 and preview["up_to_date"] == 0
    assert not common.collection_exists(common.COLLECTION_NAME)
    assert not (common.QDRANT_PATH / common.COLLECTION_NAME).exists()


def test_stale_points_a_plain_run_would_hold_back_are_reported_apart(git_repo, monkeypatch):
    """More than half of a repository's points gone (and more than 100) is
    held back by a real run. Reported as zero stale, 120 orphans looked like
    a clean index."""
    for i in range(130):
        (git_repo.path / f"f{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    subprocess.run(["git", "-C", str(git_repo.path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "many"], check=True)
    repos.add_repo(str(git_repo.path))
    _index_in_process(git_repo, monkeypatch)
    for i in range(120):
        (git_repo.path / f"f{i}.py").unlink()

    preview = jobs.run_index_preview(str(git_repo.path), sources=["code"])

    code = preview["sources"][0]
    assert code["stale"] == 0 and code["held_back"] == 120
    assert preview["held_back"] == 120
    assert any("--prune" in note for note in preview["notes"])


def test_a_repository_indexed_by_path_is_said_to_be_never_pruned(git_repo, tmp_path, monkeypatch):
    git_repo.commit("first", filename="a.py", content="x = 1\n")
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(git_repo.path.parent))
    preview = jobs.run_index_preview(str(git_repo.path), sources=["code"])
    assert preview["ok"] is True
    assert any("not registered" in note and "never removed" in note for note in preview["notes"])


def test_a_registered_repository_has_no_such_note(project):
    assert jobs.run_index_preview(str(project.path), sources=["code"])["notes"] == []


def test_the_reason_of_a_failure_has_no_progress_bar_in_it(project, monkeypatch):
    noisy = "Reading repo:  50%|#####     | 1/2 [00:00<00:00]\rReading repo: 100%|##########| 2/2 [00:00<00:00]\n" \
            "Another griot process holds the collection 'codebase__x'. Holder: PID 1.\n"
    monkeypatch.setattr(jobs, "_run_dry_run", lambda argv, env, timeout: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr=noisy))
    preview = jobs.run_index_preview(str(project.path))
    assert preview["ok"] is False
    assert "holds the collection" in preview["reason"] and "%|" not in preview["reason"] and "Reading" not in preview["reason"]


def test_the_dry_run_is_asked_not_to_draw_progress_bars(project, monkeypatch):
    seen = {}

    def fake(argv, env, timeout):
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(jobs, "_run_dry_run", fake)
    jobs.run_index_preview(str(project.path))
    assert seen["TQDM_DISABLE"] == "1"


def test_a_report_file_that_cannot_be_written_does_not_break_the_dry_run(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GRIOT_DRY_RUN_REPORT", str(tmp_path / "no-such-dir" / "report.jsonl"))
    assert index_code.main(["--repo", project.path.name, "--dry-run"]) in (None, 0)
    assert "[dry-run] 2 chunks" in capsys.readouterr().out


@pytest.mark.anyio
async def test_the_tool_does_not_close_the_index_under_another_call(project, monkeypatch):
    """The preview has to let go of the collection for its subprocess. With
    another tool call in flight, closing it would pull the index from under
    that call: the preview waits its turn instead."""
    ran = []
    monkeypatch.setattr(jobs, "_run_dry_run", lambda *a, **k: ran.append(1) or subprocess.CompletedProcess(a, 0, "", ""))
    mcp_server._tool_started()  # some other tool call, still running
    try:
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("griot_index_preview", {"path": str(project.path)})
    finally:
        mcp_server._tool_finished()
    out = result.structured_content
    assert out["ok"] is False and "another" in out["reason"].lower() and ran == []


@pytest.mark.anyio
async def test_the_schema_carries_the_held_back_count_and_the_notes():
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_index_preview"]
    assert {"held_back", "notes"} <= set(tool.output_schema["properties"])
