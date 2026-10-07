"""A long-running server whose own installation was removed under it.

[real failure, 2026-10-07] Two `griot mcp` servers had been started from a
pipx virtual environment that was later removed when griot was reinstalled
under another name. The processes kept running from what was already in
memory, and every griot_search they served failed with
`OSError: Could not find a suitable TLS CA certificate bundle, invalid path:
<the removed venv>/.../certifi/cacert.pem`: an error about a certificate, when
the cause was that the server's own files were gone and only a restart could
help. These tests remove the installation the way that happened (the paths
the running code came from point at a directory that no longer exists) and
read what the agent receives through a real client."""

import griot
import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from griot import common, golden_set, logdb, mcp_server, repos

CERT_ERROR = ("Could not find a suitable TLS CA certificate bundle, invalid path: "
              "/Users/you/.local/pipx/venvs/old/lib/python3.12/site-packages/certifi/cacert.pem")


@pytest.fixture
def removed_dir(tmp_path):
    """A path that existed once and was deleted, like the removed venv."""
    gone = tmp_path / "removed-venv"
    gone.mkdir()
    gone.rmdir()
    return gone


@pytest.fixture
def package_removed(monkeypatch, removed_dir):
    """griot's package files are gone: the module's __file__ points into the
    removed directory."""
    monkeypatch.setattr(griot, "__file__", str(removed_dir / "lib" / "griot" / "__init__.py"))
    return removed_dir


@pytest.fixture
def search_fails_like_the_removed_certifi(monkeypatch):
    def search(*args, **kwargs):
        raise OSError(CERT_ERROR)
    monkeypatch.setattr(common, "search", search)


def _text(result) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


def _says_the_installation_went_away(text: str) -> None:
    lowered = text.lower()
    assert "removed or replaced" in lowered, text
    assert "Restart the griot MCP server" in text, text


@pytest.mark.anyio
async def test_a_search_after_the_package_files_were_removed_says_so_and_asks_for_a_restart(
        package_removed, search_fails_like_the_removed_certifi):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "anything"})

    assert result.is_error is True
    text = _text(result)
    _says_the_installation_went_away(text)
    # The path that is gone is named, so a person can see which install it was.
    assert str(package_removed) in text, text
    # The original error stays visible: it is what failed, and the
    # explanation must not erase the evidence.
    assert CERT_ERROR in text, text


@pytest.mark.anyio
async def test_the_interpreter_prefix_removed_is_reported_the_same_way(
        monkeypatch, removed_dir, search_fails_like_the_removed_certifi):
    monkeypatch.setattr(mcp_server.sys, "prefix", str(removed_dir))
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "anything"})

    assert result.is_error is True
    _says_the_installation_went_away(_text(result))
    assert str(removed_dir) in _text(result)


@pytest.mark.anyio
async def test_with_the_installation_in_place_the_error_reaches_the_agent_unchanged(
        search_fails_like_the_removed_certifi):
    # Any other OSError (a real certificate problem, a full disk) is not ours
    # to reinterpret: the agent gets it as it was.
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "anything"})

    assert result.is_error is True
    text = _text(result)
    assert CERT_ERROR in text, text
    assert "removed or replaced" not in text.lower(), text


@pytest.mark.anyio
async def test_an_async_tool_failing_after_the_removal_says_so_too(monkeypatch, package_removed):
    # A module imported lazily after the removal fails to import: the other
    # shape this failure takes.
    def remove_case(index):
        raise ModuleNotFoundError("No module named 'griot.something_lazy'")
    monkeypatch.setattr(golden_set, "remove_case", remove_case)

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_golden_set_remove", {"index": 0, "confirm": True})

    assert result.is_error is True
    _says_the_installation_went_away(_text(result))
    [call] = [c for c in logdb.read_tool_calls(common.LOG_DIR, days=1)
              if c["tool"] == "griot_golden_set_remove"]
    assert "removed or replaced" in call["error"], call["error"]


@pytest.mark.anyio
async def test_a_resource_read_after_the_removal_says_so(monkeypatch, package_removed):
    def repo_status():
        raise OSError(CERT_ERROR)
    monkeypatch.setattr(repos, "repo_status", repo_status)

    async with Client(mcp_server.mcp) as client:
        with pytest.raises(MCPError) as raised:
            await client.read_resource("griot://repos")

    text = raised.value.error.message
    _says_the_installation_went_away(text)
    assert "Could not read repos.json" in text, text
    assert CERT_ERROR in text, text


@pytest.mark.anyio
async def test_a_call_that_works_is_left_alone_and_costs_no_check(monkeypatch, package_removed):
    # The check runs only once a call has failed: a call that works pays no
    # stat, and is not turned into an error because files moved under it.
    def no_check():
        raise AssertionError("the installation was checked on a call that worked")
    monkeypatch.setattr(mcp_server, "_installation_removed", no_check)
    monkeypatch.setattr(repos, "repo_status", lambda: [])

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_repos_list", {})

    assert result.is_error is False
    assert result.structured_content["count"] == 0


def test_the_check_says_nothing_while_the_installation_is_in_place():
    assert mcp_server._installation_removed() is None


def test_the_check_names_the_removed_package_directory(package_removed):
    assert mcp_server._installation_removed() == str(package_removed / "lib" / "griot")


def test_the_check_names_the_removed_prefix(monkeypatch, removed_dir):
    monkeypatch.setattr(mcp_server.sys, "prefix", str(removed_dir))
    assert mcp_server._installation_removed() == str(removed_dir)


@pytest.mark.anyio
async def test_the_failed_call_is_recorded_with_the_cause(package_removed, search_fails_like_the_removed_certifi):
    # griot_stats reads these rows: the cause has to be there as well, not
    # only the certificate message.
    async with Client(mcp_server.mcp) as client:
        await client.call_tool("griot_search", {"query": "anything"})

    [call] = [c for c in logdb.read_tool_calls(common.LOG_DIR, days=1) if c["tool"] == "griot_search"]
    assert not call["ok"]
    assert "removed or replaced" in call["error"], call["error"]
