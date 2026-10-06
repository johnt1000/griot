"""Every section heading of `griot stats` starts its value at the same column
(debt 36).

The report is a two-column layout: a heading ("Index:", "API spend:") on the
left, and its value plus every continuation line under it starting at
column 13 (stats._INDENT). "MCP resources: " was two columns wider than
that, so its value started two columns right of the item lines listed under
it and of every other heading's value. The test reads the headings off the
rendered report instead of a list, so a heading added later is held to the
same column without anyone remembering to add it here."""

import re

import pytest

from griot import stats

_INDEX_STATUS = {"points_count": 10, "embed_profile": "jina-code", "last_indexed_at": None}

# A heading is a line that does not start with a space: a label ending in a
# colon, the padding, then the value.
_HEADING = re.compile(r"^(?P<label>[A-Z][A-Za-z ]*:)(?P<pad> +)\S")


def _everything():
    """A report in which every section that can appear does: attention,
    scope, index, indexing, spend, queries, MCP tools, MCP resources,
    quality and the golden set."""
    runs = [
        {"timestamp": "2026-08-19T10:00:00+00:00", "indexed": 3, "skipped": 1, "failed": 0,
         "spend_today_usd": 0.10, "collection": "c"},
        {"timestamp": "2026-08-21T10:00:00+00:00", "indexed": 2, "skipped": 4, "failed": 1,
         "spend_today_usd": 0.40, "collection": "c"},
    ]
    queries = [{"timestamp": "2026-08-20T10:00:00+00:00", "duration_seconds": 1.0, "collection": "c",
                "sources": ["repo/a.py", "commit abc", "tag v1"], "num_sources": 3}]
    quality = [
        {"timestamp": "2026-08-19T10:00:00+00:00", "collection": "c", "pass_rate": 1.0},
        {"timestamp": "2026-08-20T10:00:00+00:00", "collection": "c", "pass_rate": 0.5},
    ]
    calls = [{"timestamp": "2026-08-21T10:00:00+00:00", "tool": "griot_search", "ok": True},
             {"timestamp": "2026-08-21T10:00:00+00:00", "tool": "griot://stats", "ok": False}]
    result = stats.compute_stats(runs, queries, _INDEX_STATUS, quality_checks=quality,
                                 tool_calls=calls, collection="c")
    result["attention"] = ["first thing to look at", "second thing to look at"]
    result["golden_set"] = {"cases": 2, "last_total": None}
    return result


def _nothing():
    """The other forms of the headings that change their wording: no
    queries, a quality check outside the window, every profile's scope."""
    result = stats.compute_stats([], [], _INDEX_STATUS, tool_calls=[])
    result["scope"] = "all_profiles"
    result["last_quality_check_at"] = None
    return result


def _headings(text):
    """(label, column where the value starts) for every heading line."""
    found = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            found.append((m["label"], m.end("pad")))
    return found


@pytest.mark.parametrize("width", [None, 80], ids=["plain", "charts"])
@pytest.mark.parametrize("build", [_everything, _nothing], ids=["everything", "nothing"])
def test_every_heading_value_starts_at_the_indent_column(build, width):
    text = stats.format_stats(build(), days=30, width=width)

    headings = _headings(text)
    misaligned = [(label, column) for label, column in headings if column != len(stats._INDENT)]
    # Compared with the continuation lines' indent, not with the other
    # headings: a heading that only lined up with its peers would still sit
    # off the item lines listed under it.
    assert misaligned == []


def test_the_rich_report_shows_every_section():
    """Without this the alignment test could pass by rendering fewer
    headings than the report has."""
    for width in (None, 80):
        labels = [label for label, _ in _headings(stats.format_stats(_everything(), days=30, width=width))]
        assert labels == ["Attention:", "Scope:", "Index:", "Indexing:", "API spend:", "Queries:",
                          "MCP tools:", "MCP reads:", "Quality:", "Golden set:"]


def test_mcp_item_lines_start_at_the_heading_value_column():
    text = stats.format_stats(_everything(), days=30)
    lines = text.splitlines()
    for label, item in (("MCP tools:", "griot_search"), ("MCP reads:", "griot://stats")):
        heading = next(line for line in lines if line.startswith(label))
        item_line = next(line for line in lines if line.lstrip().startswith(item + " "))
        value_column = _HEADING.match(heading).end("pad")
        assert item_line.index(item) == value_column, label
