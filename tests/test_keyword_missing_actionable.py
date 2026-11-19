"""A collection without keyword vectors tells each reader what to do about it.

Agents follow the server instructions and pass mode=keyword for a hash or an
exact name; on an index built before keyword vectors that was refused and the
agent retried in vector mode, which does not find those strings (MRR@10 0.01
for hashes against 0.80 in hybrid). The refusal stays a refusal, but it says
what to do NOW; the user is told in doctor and stats, in the same words.

Real tiny Edge indexes (tests/test_keyword_search.py's fixtures)."""

import pytest
from mcp.client.client import Client

from griot import common, doctor, mcp_server, stats
from test_keyword_search import fake_embeddings, index, legacy_index  # noqa: F401


def _refusal(mode="keyword"):
    with pytest.raises(common.SearchModeUnavailable) as error:
        common.search("acquire_lock", mode=mode)
    return str(error.value)


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_the_refusal_names_the_command_and_what_to_do_now(legacy_index, mode):
    message = _refusal(mode)
    assert "`griot index keywords`" in message
    assert "grep" in message and "git log" in message, "an exact string is searched in the repository"
    assert "mode 'vector'" in message, "a descriptive question still has vector search"
    assert "free disk" in message, "the disk need stays"
    assert "embeds nothing" in message


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_the_refusal_limits_grep_to_the_working_repository_and_reads_neutrally(legacy_index, mode):
    message = _refusal(mode)
    assert "repository you are working in" in message, "grep and log cover only the current checkout"
    assert "other registered repositories" in message, "the index spans more than the checkout"
    assert "The user" not in message and "yourself" not in message, "a person reads this in the CLI too"


def test_the_shared_sentence_names_the_command_and_the_collection():
    sentence = common.keyword_search_missing_sentence("some-collection")
    assert "`griot index keywords`" in sentence and "'some-collection'" in sentence
    assert "embeds nothing" in sentence


def test_the_doctor_warns_with_the_command_and_stays_exit_zero(legacy_index):
    check = {c["check"]: c for c in doctor.run_checks()}["index"]
    assert check["status"] == "warn"
    assert "griot index keywords" in check["fix"]
    assert common.keyword_search_missing_sentence(common.COLLECTION_NAME) in check["detail"]
    assert doctor.exit_status([check]) == 0, "a warning does not turn the install red"


def test_the_doctor_is_ok_when_keyword_vectors_are_built(index):
    check = {c["check"]: c for c in doctor.run_checks()}["index"]
    assert check["status"] == "ok" and "keyword" not in check["detail"]


def test_stats_attention_has_the_sentence_only_without_keyword_vectors(legacy_index):
    sentence = common.keyword_search_missing_sentence(common.COLLECTION_NAME)
    result = stats.compute_stats([], [], common.get_index_status())
    assert sentence in result["attention"]


def test_stats_attention_is_quiet_when_keyword_vectors_are_built(index):
    result = stats.compute_stats([], [], common.get_index_status())
    assert not any("keyword" in line for line in result["attention"])


def test_stats_attention_is_quiet_for_an_unreadable_config():
    # None is "cannot say": nothing to tell anyone to build.
    result = stats.compute_stats([], [], {"keyword_search": None, "collection": "c"})
    assert not any("keyword" in line for line in result["attention"])


@pytest.mark.anyio
async def test_the_agent_receives_the_actionable_refusal(legacy_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "acquire_lock", "mode": "keyword"})
    text = result.content[0].text
    assert result.is_error
    assert "griot index keywords" in text and "grep" in text and "mode 'vector'" in text


@pytest.mark.anyio
async def test_griot_stats_carries_the_attention_sentence(legacy_index):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_stats", {})
    attention = result.structured_content["attention"]
    assert common.keyword_search_missing_sentence(common.COLLECTION_NAME) in attention
