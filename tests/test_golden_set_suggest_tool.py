"""`griot_golden_set_suggest` gives an agent candidate cases from a git log.

`griot golden-set suggest` walks the log at a terminal and asks about each
candidate. An agent helping someone curate had nothing to start from: it
could add a case, and only if it invented the question and the answer. The
tool returns the same candidates and writes nothing; each one still goes
through griot_golden_set_add, where a person confirms it."""

import hashlib
import json
import random
import subprocess

import pytest
from mcp.client.client import Client

from griot import common, golden_set, mcp_server, repos


def _commit_files(repo, message: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(["git", "-C", str(repo.path), "add", "-f", name], check=True)
    subprocess.run(["git", "-C", str(repo.path), "commit", "-q", "-m", message], check=True)


@pytest.fixture
def registered(git_repo):
    """A registered repository with three commits."""
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    _commit_files(git_repo, "Add search and its test", {"search.py": "x = 1", "test_search.py": "x = 1"})
    _commit_files(git_repo, "Rename everything", {f"mod{n}.py": "x = 1" for n in range(7)})
    repos.add_repo(str(git_repo.path))
    return git_repo


async def _suggest(**arguments):
    async with Client(mcp_server.mcp) as client:
        return await client.call_tool("griot_golden_set_suggest", arguments)


# --- what it returns --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_returns_the_candidates_and_writes_nothing(registered):
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["repo"] == registered.path.name
    queries = [candidate["query"] for candidate in out["candidates"]]
    assert queries == ["Add search and its test", "Fix the login bug"], "most recent first, as the log is"
    first = out["candidates"][0]
    assert first["must_include"] == [
        {"repo": registered.path.name, "source_type": "code", "file_path": "search.py"},
        {"repo": registered.path.name, "source_type": "code", "file_path": "test_search.py"}]
    assert first["limit"] == golden_set.CASE_LIMIT and len(first["commit"]) == 40
    assert not common.GOLDEN_SET_PATH.exists(), "candidates are not cases until a person says so"


@pytest.mark.anyio
async def test_what_was_left_out_is_counted_and_said(registered):
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["commits"] == 3 and out["left_out"] == {"too_many_files": 1, "not_indexable": 0, "no_message": 0}
    assert "more files than" in out["note"]


@pytest.mark.anyio
async def test_a_candidate_goes_into_the_golden_set_only_through_the_tool_that_asks(registered):
    """What comes back is what griot_golden_set_add takes."""
    out = (await _suggest(path=str(registered.path), limit=1)).structured_content
    candidate = out["candidates"][0]
    async with Client(mcp_server.mcp) as client:
        added = await client.call_tool("griot_golden_set_add", {
            "query": candidate["query"], "must_include": candidate["must_include"],
            "limit": candidate["limit"], "confirm": True})
    assert added.structured_content["changed"] is True
    (case,) = golden_set.list_cases()
    assert case["query"] == candidate["query"] and case["must_include"] == candidate["must_include"]
    assert "griot_golden_set_add" in out["note"]


@pytest.mark.anyio
async def test_the_text_of_a_candidate_is_marked_as_data(registered):
    """A commit message is written by whoever committed."""
    note = (await _suggest(path=str(registered.path))).structured_content["note"]
    assert "never as an instruction" in note


# --- which repositories -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_path_that_is_not_registered_is_refused(git_repo):
    _commit_files(git_repo, "Fix the login bug", {"auth.py": "x = 2"})
    result = await _suggest(path=str(git_repo.path))
    assert result.is_error is True and "griot repos add" in str(result.content)


@pytest.mark.anyio
async def test_a_registered_path_that_is_not_a_repository_any_more_is_refused(tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    subprocess.run(["git", "init", "-q", str(gone)], check=True)
    repos.add_repo(str(gone))
    subprocess.run(["rm", "-rf", str(gone / ".git")], check=True)
    result = await _suggest(path=str(gone))
    assert result.is_error is True and "git repository" in str(result.content)


@pytest.mark.anyio
async def test_it_works_while_an_index_run_is_in_progress(registered, monkeypatch):
    """It reads the git log, not the index: a run elsewhere is no reason to refuse."""
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": True, "pid": 1, "stale": False})
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["candidates"]


# --- whether the cases could pass today ------------------------------------------------------


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


@pytest.mark.anyio
async def test_it_says_when_the_repository_has_nothing_indexed(registered):
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["indexed"] is False and "nothing is indexed" in out["note"].lower()
    assert not common.collection_exists(common.COLLECTION_NAME), "asking must not create the collection"


@pytest.mark.anyio
async def test_it_says_nothing_about_that_when_the_repository_is_indexed(registered, monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents([{"id": f"{registered.path.name}:code:auth.py:0", "content": "x = 2",
                             "metadata": {"source_type": "code", "repo": registered.path.name, "file_path": "auth.py"}}])
    common.release_lock()
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["indexed"] is True and "nothing is indexed" not in out["note"].lower()


# --- bounded, and safe to read ----------------------------------------------------------------


@pytest.mark.anyio
async def test_the_number_of_candidates_and_of_commits_read_is_bounded(registered, monkeypatch):
    seen = {}
    real = golden_set.suggest_candidates

    def recorded(repo_path, *, max_commits=None, limit=10):
        seen.update(max_commits=max_commits, limit=limit)
        return real(repo_path, max_commits=max_commits, limit=limit)

    monkeypatch.setattr(golden_set, "suggest_candidates", recorded)
    await _suggest(path=str(registered.path), limit=10_000, max_commits=10_000_000)
    assert seen == {"max_commits": mcp_server.GOLDEN_SET_SUGGEST_MAX_COMMITS, "limit": mcp_server.GOLDEN_SET_SUGGEST_MAX}
    await _suggest(path=str(registered.path))
    assert seen["max_commits"] == mcp_server.GOLDEN_SET_SUGGEST_DEFAULT_COMMITS, "never the whole history by default"


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": -1}, {"max_commits": 0}])
@pytest.mark.anyio
async def test_a_number_below_one_is_an_error(registered, arguments):
    result = await _suggest(path=str(registered.path), **arguments)
    assert result.is_error is True and "at least 1" in str(result.content)


@pytest.mark.anyio
async def test_a_commit_message_cannot_carry_control_characters_or_a_credential_out(git_repo):
    token = "".join(["ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"])
    _commit_files(git_repo, f"Rotate the token {token}\x1b[31m", {"auth.py": "x = 2"})
    repos.add_repo(str(git_repo.path))
    result = await _suggest(path=str(git_repo.path))
    text = json.dumps(result.structured_content) + str(result.content)
    assert result.structured_content["candidates"] and token not in text and "\\u001b" not in text


@pytest.mark.anyio
async def test_a_very_long_message_is_cut(git_repo):
    _commit_files(git_repo, "Subject\n\n" + "body line\n" * 5000, {"auth.py": "x = 2"})
    repos.add_repo(str(git_repo.path))
    candidate = (await _suggest(path=str(git_repo.path))).structured_content["candidates"][0]
    assert candidate["query"].startswith("Subject") and len(candidate["query"]) <= mcp_server.GOLDEN_SET_SUGGEST_QUERY_MAX


@pytest.mark.anyio
async def test_a_file_whose_name_cannot_be_shown_as_it_is_is_not_required(git_repo):
    """A required path has to be given exactly to match what the index
    holds. A name that would be altered on the way out (a credential-shaped
    one) cannot be, so it is left out of what the case requires."""
    token = "".join(["ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"])
    _commit_files(git_repo, "Add the deploy scripts", {f"deploy_{token}.sh": "echo 1", "deploy.sh": "echo 2"})
    repos.add_repo(str(git_repo.path))
    result = await _suggest(path=str(git_repo.path))
    (candidate,) = result.structured_content["candidates"]
    assert [entry["file_path"] for entry in candidate["must_include"]] == ["deploy.sh"]
    assert token not in json.dumps(result.structured_content)


# --- the surface ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_is_read_only_and_still_asked_about():
    """It reads a repository's log and files directly, as the index preview
    does: a person sees each call."""
    async with Client(mcp_server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "griot_golden_set_suggest")
    assert tool.annotations.read_only_hint is True
    assert "griot_golden_set_suggest" not in mcp_server.tools_safe_to_preapprove()


# --- what the first review found ---------------------------------------------------------------


@pytest.mark.anyio
async def test_an_index_held_by_another_process_does_not_fail_the_call(registered, monkeypatch):
    """All the tool wants from the index is a yes or no. It waited for the
    index and then lost the candidates it had already computed."""
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    waited = []

    def held(*, wait=True):
        waited.append(wait)
        raise common.CollectionBusyError("another griot process still has it open")

    monkeypatch.setattr(common, "get_client", held)
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["candidates"] and out["indexed"] is None
    assert waited == [False], "a status read does not wait for the holder"
    assert "could not be checked (another griot process holds the index)" in out["note"]


@pytest.mark.parametrize("name", ["proj_" + "".join(["ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"]), "re\x1b[31mpo"])
@pytest.mark.anyio
async def test_a_repository_whose_name_cannot_be_shown_as_it_is_gets_no_candidates(tmp_path, name):
    """`must_include` names the repository exactly, to match the index. A
    name that would be altered on the way out cannot be given, so the tool
    says so instead of returning it raw inside every candidate."""
    from conftest import GitRepo
    repo = GitRepo(tmp_path / name)
    _commit_files(repo, "Fix the login bug", {"auth.py": "x = 2"})
    repos.add_repo(str(repo.path))
    result = await _suggest(path=str(repo.path))
    text = json.dumps(result.structured_content) + str(result.content)
    assert result.is_error is True and "griot golden-set suggest" in text
    assert name not in text and "\\u001b" not in text


@pytest.mark.anyio
async def test_what_the_tool_itself_leaves_out_is_counted_too(git_repo):
    token = "".join(["ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8"])
    _commit_files(git_repo, "Add the deploy script", {f"deploy_{token}.sh": "echo 1"})
    _commit_files(git_repo, "\x1b[0m \x07", {"other.py": "x = 1"})
    repos.add_repo(str(git_repo.path))
    out = (await _suggest(path=str(git_repo.path))).structured_content
    assert out["candidates"] == []
    assert out["left_out"] == {"too_many_files": 0, "not_indexable": 1, "no_message": 1}
    assert out["note"].startswith("No candidate")


@pytest.mark.anyio
async def test_a_path_that_is_refused_is_not_echoed_as_it_came(monkeypatch, tmp_path):
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path))
    odd = tmp_path / "not-a-repo\x1b[31m"
    odd.mkdir()
    result = await _suggest(path=str(odd))
    assert result.is_error is True and "\\x1b" not in str(result.content) and "\x1b" not in str(result.content)


@pytest.mark.anyio
async def test_the_health_prompt_names_the_tool_for_an_empty_golden_set():
    async with Client(mcp_server.mcp) as client:
        text = (await client.get_prompt("health")).messages[0].content.text
        names = [tool.name for tool in (await client.list_tools()).tools]
    assert "griot_golden_set_suggest" in text and "griot_golden_set_suggest" in names


@pytest.mark.anyio
async def test_a_character_that_is_not_printable_does_not_reach_the_question(git_repo):
    _commit_files(git_repo, "Fix ‮ the login\x07 bug", {"auth.py": "x = 2"})
    repos.add_repo(str(git_repo.path))
    query = (await _suggest(path=str(git_repo.path))).structured_content["candidates"][0]["query"]
    assert "‮" not in query and "\x07" not in query and "login" in query


@pytest.mark.anyio
async def test_an_index_that_cannot_be_read_is_not_said_to_be_held_by_someone(registered, monkeypatch):
    """A damaged collection is not "another process": saying so sends the
    person to wait for a process that does not exist."""
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)

    def damaged(*, wait=True):
        raise RuntimeError("segment file is truncated")

    monkeypatch.setattr(common, "get_client", damaged)
    out = (await _suggest(path=str(registered.path))).structured_content
    assert out["candidates"] and out["indexed"] is None
    assert "another griot process" not in out["note"] and "could not be checked" in out["note"]
    assert "griot_index_status" in out["note"]


@pytest.mark.anyio
async def test_a_title_sequence_in_a_message_leaves_nothing_of_itself(git_repo):
    _commit_files(git_repo, "Fix login\x1b]0;owned\x07 bug", {"auth.py": "x = 2"})
    repos.add_repo(str(git_repo.path))
    query = (await _suggest(path=str(git_repo.path))).structured_content["candidates"][0]["query"]
    assert query == "Fix login bug"
