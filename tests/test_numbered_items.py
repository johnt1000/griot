"""A pull request, merge request or issue asked for by its number.

"PR #14 changes and reason" is a natural agent query, and in a blind judging
of real searches it found nothing in any mode: the stored text of a pull
request was its title and description, so the number lived only in the
payload (`mr_iid`), where neither the embedding nor the keyword vector sees
it. Keyword search for "#14" found only the pull request whose title said
"Follow-up to #14". The agent fell back to the platform's own CLI.

The documents here are built by index_platform's own builders, so what is
tested is the text griot really stores.

What the text can promise is the keyword ranking: the number is a rare word,
and BM25 ranks the item that holds it first. Vector search cannot be held to
it: measured with a real local model (bge-small, these same items), vector
search did not rank #14 in the top five for any spelling of the number, with
or without it in the text, and hybrid, which fuses both rankings with equal
weight, brought #14 up from absent to third. Hence the server instructions
send a lookup by number to `mode=keyword`, and hybrid is held here only to
finding the item at all. The fake embedding is a bag of words, the kindest a
model can be to a number, and still no stand-in for the real one.
"""

import hashlib
import math
import re

import pytest
from mcp.client.client import Client

from griot import common, index_platform, mcp_server

# Near misses on purpose: #140 and #1 share digits with 14, #20 names #14 in
# its title, #1 and #140 hold the query's other words ("changes", "reason").
PULL_REQUESTS = [
    {"iid": 1, "title": "Change the reason shown when an upload fails", "state": "merged",
     "description": "Shows why the upload failed instead of a code."},
    {"iid": 14, "title": "Retry uploads when the storage answers 503", "state": "merged",
     "description": "The uploader gave up after one failure. It now retries three times with backoff, "
                    "because the storage drops connections under load."},
    {"iid": 20, "title": "Follow-up to #14: retry downloads too", "state": "merged",
     "description": "Same backoff as the uploader."},
    {"iid": 140, "title": "Raise the upload timeout", "state": "open",
     "description": "Changes the timeout; the reason is the slow storage. These changes are small."},
]
# A repository's other pull requests. They matter to keyword ranking: its IDF
# is taken over the whole collection, and in a real one "change" and "reason"
# are common words while a given number is rare. Without them, four pull
# requests would make "changes" rarer than "14".
_SUBJECTS = ["the login form", "the cache", "the docs", "the build", "the logger", "the parser",
             "the settings page", "the API client", "the release notes", "the test fixtures",
             "the error messages", "the CI pipeline"]
PULL_REQUESTS += [
    {"iid": 200 + i, "title": f"Change {subject}", "state": "merged",
     "description": f"Changes {subject}. The reason: it was out of date."}
    for i, subject in enumerate(_SUBJECTS)
]

ISSUES = [
    {"iid": 14, "title": "Uploads fail under load", "state": "closed", "description": "The storage answers 503."},
    {"iid": 140, "title": "Slow uploads", "state": "open", "description": "Uploads take minutes."},
]


def _bag_of_words(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@pytest.fixture
def numbered_index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_bag_of_words(t, common.EMBED_DIM) for t in texts])
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests", lambda *a, **kw: PULL_REQUESTS)
    monkeypatch.setattr(index_platform.platforms, "fetch_issues", lambda *a, **kw: ISSUES)
    documents = (index_platform.build_mr_documents("shop", "github", "acme/shop", None)
                 + index_platform.build_issue_documents("shop", "github", "acme/shop", None))
    common.index_documents(documents)
    common.release_lock()
    return documents


def _numbers(hits, field):
    return [(h.payload or {}).get(field) for h in hits]


@pytest.mark.parametrize("query", ["PR #14 changes and reason", "PR #14", "#14", "!14", "MR !14",
                                   "merge request 14", "pull request #14"])
def test_keyword_search_ranks_the_pull_request_with_that_number_first(numbered_index, query):
    """Not #140 or #1, which share its digits, nor #20, which names it."""
    hits = common.search(query, limit=4, source_types=["merge_request"], mode="keyword")
    assert hits and _numbers(hits, "mr_iid")[0] == 14, (query, _numbers(hits, "mr_iid"))


def test_keyword_search_ranks_the_issue_with_that_number_first(numbered_index):
    hits = common.search("issue #14", limit=2, source_types=["issue"], mode="keyword")
    assert hits and _numbers(hits, "issue_iid")[0] == 14, _numbers(hits, "issue_iid")


def test_the_default_hybrid_search_finds_the_pull_request_by_number(numbered_index):
    hits = common.search("PR #14", limit=5, source_types=["merge_request"], mode="hybrid")
    assert 14 in _numbers(hits, "mr_iid"), _numbers(hits, "mr_iid")


def test_the_number_goes_on_the_line_a_preview_shows_with_the_title(numbered_index):
    """`griot search` previews a result by its first line: the number goes on
    the title's line, not on a line of its own that would push the title out."""
    first_lines = {(d["metadata"]["source_type"], d["metadata"].get("mr_iid") or d["metadata"].get("issue_iid")):
                   d["content"].splitlines()[0] for d in numbered_index}
    assert first_lines[("merge_request", 14)] == \
        "PR #14 (pull request / merge request !14): Retry uploads when the storage answers 503"
    assert first_lines[("issue", 14)] == "Issue #14: Uploads fail under load"


def test_an_item_without_a_number_keeps_its_title_alone(monkeypatch):
    monkeypatch.setattr(index_platform.platforms, "fetch_pull_requests",
                        lambda *a, **kw: [{"iid": None, "title": "A title", "state": "open"}])
    (document,) = index_platform.build_mr_documents("shop", "github", "acme/shop", None)
    assert document["content"] == "A title"


@pytest.mark.anyio
async def test_the_tool_finds_a_pull_request_by_number_the_way_the_instructions_say(numbered_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "PR #14 changes and reason", "mode": "keyword",
                                                         "source_types": ["merge_request"]})
    assert result.is_error is False
    first = result.structured_content["results"][0]
    assert first["metadata"]["mr_iid"] == 14, [r["metadata"]["mr_iid"] for r in result.structured_content["results"]]
    assert first["source_label"].startswith("MR !14 ")
    assert first["content"].startswith("PR #14 ")
