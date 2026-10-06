"""`griot stats` counts MCP tool calls and MCP resource reads apart.

Both are recorded in the same log table (a resource read under its URI,
griot://repos), and the report used to sum them under "MCP tools: N calls
across M tools": three reads of griot://stats made a tool count of three.
The two answer different questions (which tools an agent chooses to call,
and whether the resources that duplicate them get read at all), so the text
report, `--json` and the griot_stats MCP tool all carry them as two counts."""

import json

import pytest
from mcp.client.client import Client

from griot import common, logdb, mcp_server, stats

_INDEX_STATUS = {"points_count": 0, "last_indexed_at": None, "running": False, "orphan_lock": False}


def _call(name, ok=True):
    return {"timestamp": "2026-08-21T10:00:00+00:00", "tool": name, "ok": ok}


def _section(text, heading):
    """The heading line and the item lines under it, up to the next blank line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    section = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        section.append(line)
    return section


def test_tool_calls_and_resource_reads_are_counted_apart():
    calls = [_call("griot_search"), _call("griot_search"), _call("griot://stats"),
             _call("griot://repos", ok=False), _call("griot://repos")]

    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=calls)

    assert result["tool_calls"] == {"griot_search": 2}
    assert result["failed_tool_calls"] == {}
    assert result["resource_reads"] == {"griot://repos": 2, "griot://stats": 1}
    assert result["failed_resource_reads"] == {"griot://repos": 1}

    text = stats.format_stats(result, days=30)
    tools = _section(text, "MCP tools:")
    assert tools[0].startswith("MCP tools:   2 calls across 1 tool,")
    assert [line.split() for line in tools[1:]] == [["griot_search", "2"]]
    reads = _section(text, "MCP reads:")
    assert reads[0].startswith("MCP reads:   3 reads across 2 resources,")
    assert [line.split() for line in reads[1:]] == [
        ["griot://repos", "2", "·", "1", "failed"], ["griot://stats", "1"]]


def test_only_tool_calls_shows_no_resource_section():
    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[_call("griot_search")])

    assert result["tool_calls"] == {"griot_search": 1}
    assert result["resource_reads"] == {}
    text = stats.format_stats(result, days=30)
    assert _section(text, "MCP tools:")[0].startswith("MCP tools:   1 call across 1 tool,")
    assert "MCP reads:" not in text


def test_only_resource_reads_shows_no_tool_section():
    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[_call("griot://stats")])

    assert result["tool_calls"] == {}
    assert result["failed_tool_calls"] == {}
    assert result["resource_reads"] == {"griot://stats": 1}
    text = stats.format_stats(result, days=30)
    assert "MCP tools:" not in text
    assert _section(text, "MCP reads:")[0].startswith("MCP reads:   1 read across 1 resource,")
    assert "griot://stats 1" in text


def test_a_call_without_a_name_stays_a_tool_call_as_before():
    """A row with no name was counted as an "unknown" tool before the split;
    sorting it must not take the whole report down."""
    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[{"tool": None, "ok": True}])

    assert result["tool_calls"] == {"unknown": 1}
    assert result["resource_reads"] == {}


def test_neither_shows_no_section():
    text = stats.format_stats(stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[]), days=30)
    assert "MCP tools:" not in text and "MCP reads:" not in text


def test_the_json_report_carries_the_two_counts():
    logdb.write_tool_call(common.LOG_DIR, "griot_search", ok=True, duration_seconds=0.1)
    logdb.write_tool_call(common.LOG_DIR, "griot://repos", ok=True, duration_seconds=0.1)

    report = json.loads(json.dumps(stats.report(30)))

    assert report["tool_calls"] == {"griot_search": 1}
    assert report["resource_reads"] == {"griot://repos": 1}


@pytest.mark.anyio
async def test_griot_stats_over_the_protocol_splits_tools_and_resources():
    """A real read of a resource and a real tool call, through a client: the
    schema the SDK builds from StatsOutput drops an undeclared key, so only
    the protocol proves the agent receives resource_reads."""
    async with Client(mcp_server.mcp) as client:
        await client.read_resource("griot://repos")
        await client.call_tool("griot_config_list", {})
        result = await client.call_tool("griot_stats", {})

    assert result.is_error is False
    out = result.structured_content
    assert out["resource_reads"] == {"griot://repos": 1}
    assert out["failed_resource_reads"] == {}
    assert "griot://repos" not in out["tool_calls"]
    assert out["tool_calls"]["griot_config_list"] == 1
