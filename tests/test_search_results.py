"""What a search result carries, and which results fill the slots.

A result used to be a label and a text. The label cut the commit hash to
eight characters and left out the date and the author that are stored with
every commit, so "when did this change" could not be answered from a search.
And the slots: one long file could take all of them, and a text indexed in
two places (a copied file, a fork) took two."""

import hashlib
import random
import re
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import ask, common, mcp_server

FULL_HASH = "0123456789abcdef0123456789abcdef01234567"


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


def _code(repo: str, path: str, i: int, text: str) -> dict:
    return _doc(repo, "code", f"{path}:{i}", text, file_path=path, chunk_index=i)


LONG_FILE = [_code("alpha", "long.py", i, f"retry policy, part {i} of the long file") for i in range(5)]
OTHERS = [_code("alpha", f"other{i}.py", 0, f"retry policy in another file number {i}") for i in range(4)]
COPIED = "the same runbook text, copied into two repositories"
COPIES = [_code("alpha", "docs/runbook.md", 0, COPIED), _code("beta", "docs/runbook.md", 0, COPIED)]
COMMIT = _doc("alpha", "commit", FULL_HASH, "fix: retry policy backs off", commit_hash=FULL_HASH,
              author="Ada Lovelace", date="2026-03-04T10:00:00+00:00", chunk_index=0)


@pytest.fixture
def fake_embedding(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])


def _index(docs):
    common.index_documents(docs)
    common.release_lock()


def _documents(hits):
    return [common.document_key(h.payload) for h in hits]


# --- which results fill the slots --------------------------------------------------------


def test_a_plain_search_is_unchanged(fake_embedding):
    """The quality check and the golden set measure retrieval itself: they
    must keep getting every point, in the store's order."""
    _index(LONG_FILE + OTHERS)
    assert len(common.search("retry policy", limit=9)) == 9


def test_one_document_takes_at_most_three_slots(fake_embedding):
    _index(LONG_FILE + OTHERS)
    hits = common.search("retry policy", limit=9, diverse=True)
    from_long = [h for h in hits if h.payload["file_path"] == "long.py"]
    assert len(from_long) == common.SEARCH_MAX_CHUNKS_PER_DOCUMENT == 3
    assert len(hits) == 7, "three of the long file and the four others"


def test_the_slots_a_long_document_gives_up_go_to_the_next_best(fake_embedding):
    """`limit` is still how many results come back: the search asks the store
    for more than `limit`, or a capped document would just shorten the list."""
    _index(LONG_FILE + OTHERS)
    assert len(common.search("retry policy", limit=6, diverse=True)) == 6


def test_the_kept_chunks_are_the_best_ones_in_the_same_order(fake_embedding):
    _index(LONG_FILE + OTHERS)
    raw = [str(h.id) for h in common.search("retry policy", limit=9)]
    kept = [str(h.id) for h in common.search("retry policy", limit=9, diverse=True)]
    assert kept == [i for i in raw if i in set(kept)], "a subsequence of the plain ranking"
    scores = [h.score for h in common.search("retry policy", limit=9, diverse=True)]
    assert scores == sorted(scores, reverse=True)


def test_a_text_indexed_in_two_places_comes_back_once_and_names_the_other(fake_embedding):
    _index(COPIES + OTHERS)
    hits = common.search(COPIED, limit=6, diverse=True)
    copies = [h for h in hits if h.payload["file_path"] == "docs/runbook.md"]
    assert len(copies) == 1
    other = "beta" if copies[0].payload["repo"] == "alpha" else "alpha"
    assert copies[0].also_in == [f"{other}/docs/runbook.md"]
    assert all(h.also_in == [] for h in hits if h is not copies[0])


def test_a_block_repeated_inside_one_file_is_not_another_place(fake_embedding):
    """Two chunks of the same file with the same text: once is enough, and
    the file does not list itself as somewhere else."""
    _index([_code("alpha", "repeats.md", 0, "the same block"), _code("alpha", "repeats.md", 1, "the same block")])
    hits = common.search("the same block", limit=5, diverse=True)
    assert len(hits) == 1 and hits[0].also_in == []


def _commit(repo, commit_hash, text, date):
    return _doc(repo, "commit", commit_hash, text, commit_hash=commit_hash, author="Ada", date=date, chunk_index=0)


def test_two_commits_with_the_same_message_are_two_results(fake_embedding):
    """A commit stores its message, not its hash or date, so "fix typo"
    twice is the same TEXT. It is not the same thing: folded, a dated step
    of a history would vanish into a label."""
    _index([_commit("alpha", "a" * 40, "fix typo", "2026-01-01"), _commit("alpha", "b" * 40, "fix typo", "2026-02-02")])
    hits = common.search("fix typo", limit=5, diverse=True)
    assert sorted(h.payload["date"] for h in hits) == ["2026-01-01", "2026-02-02"]
    assert all(h.also_in == [] for h in hits)


def test_the_same_commit_in_two_repositories_is_one_result(fake_embedding):
    """A fork or a mirror: the same hash, the same message, two repositories."""
    _index([_commit("alpha", "c" * 40, "feat: retries", "2026-01-01"), _commit("fork", "c" * 40, "feat: retries", "2026-01-01")])
    hits = common.search("feat: retries", limit=5, diverse=True)
    assert len(hits) == 1 and len(hits[0].also_in) == 1


def test_a_commit_and_a_file_with_the_same_text_are_two_results(fake_embedding):
    _index([_commit("alpha", "d" * 40, "same words", "2026-01-01"), _code("alpha", "notes.md", 0, "same words")])
    assert len(common.search("same words", limit=5, diverse=True)) == 2


def test_limit_is_a_ceiling_when_the_best_matches_are_a_few_long_documents(fake_embedding):
    """The store is asked for six times `limit`. When that whole window is
    chunks of two documents, there are six slots to give, not eight: the
    contract is "up to `limit`", and this holds it in place."""
    _index([_code("alpha", f"long{d}.py", i, f"retry policy document {d} part {i}") for d in range(2) for i in range(30)])
    assert len(common.search("retry policy", limit=8, diverse=True)) == 6


def test_a_plain_search_still_returns_both_copies(fake_embedding):
    _index(COPIES + OTHERS)
    hits = common.search(COPIED, limit=6)
    assert len([h for h in hits if h.payload["file_path"] == "docs/runbook.md"]) == 2


def test_grouping_is_one_per_document_and_folds_copies_too(fake_embedding):
    _index(LONG_FILE + COPIES)
    hits = common.search("retry policy", limit=6, group_by_document=True, diverse=True)
    assert len(hits) == 2, "the long file once, the copied text once"
    assert len(set(_documents(hits))) == 2


def test_a_diverse_result_offers_what_every_reader_of_a_result_uses(fake_embedding):
    _index(OTHERS)
    hit = common.search("retry policy", limit=1, diverse=True)[0]
    assert isinstance(hit.score, float) and isinstance(hit.payload, dict) and hit.id is not None
    assert ask.source_label(hit.payload).startswith("alpha/other")


def test_the_filters_apply_before_the_slots_are_filled(fake_embedding):
    _index(LONG_FILE + OTHERS + COPIES)
    hits = common.search("retry policy", limit=10, diverse=True, repos=["beta"])
    assert [h.payload["repo"] for h in hits] == ["beta"]
    assert hits[0].also_in == [], "the copy in the other repository was filtered out, not folded in"


# --- who searches which way --------------------------------------------------------------


def test_the_command_line_search_is_capped_and_names_the_other_place(fake_embedding, capsys):
    """A person at a terminal reads results too."""
    from griot import cli
    _index(LONG_FILE + COPIES)
    assert cli.main(["search", "retry policy", "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert out.count("alpha/long.py") == 3
    assert out.count("docs/runbook.md") == 2, "once as a result, once as the other place"
    assert "same text in: " in out


def test_the_chat_model_gets_the_same_arrangement_and_no_more_than_the_limit(fake_embedding, monkeypatch):
    """`griot ask` pays per token of context: the wider fetch must not
    reach the prompt."""
    seen = {}
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: seen.update(prompt=prompt) or "answer")
    _index(LONG_FILE + OTHERS + COPIES)
    _, results = ask.ask("retry policy", model=None, limit=5)
    assert len(results) == 5
    assert seen["prompt"].count("[alpha/long.py]") <= 3
    assert seen["prompt"].count("docs/runbook.md]") <= 1
    assert seen["prompt"].count("\n\n---\n\n") == 5, "five chunks and the question, nothing from the wider fetch"


@pytest.mark.parametrize("module", ["quality_check", "retrieval_eval", "golden_set"])
def test_what_measures_retrieval_is_not(module):
    source = (Path(common.__file__).parent / f"{module}.py").read_text()
    assert "common.search(" in source and "diverse" not in source


# --- what a result carries, through the protocol -----------------------------------------


async def _search(arguments):
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", arguments)
    assert result.is_error is False, result.content
    return result.structured_content["results"]


@pytest.mark.anyio
async def test_a_code_result_says_which_file_and_which_part(fake_embedding):
    _index(OTHERS)
    first = (await _search({"query": "retry policy", "limit": 1}))[0]
    assert first["metadata"]["file_path"].startswith("other") and first["metadata"]["chunk_index"] == 0
    assert first["repo"] == "alpha" and first["source_type"] == "code"


@pytest.mark.anyio
async def test_a_commit_result_carries_the_whole_hash_the_date_and_the_author(fake_embedding):
    _index([COMMIT])
    first = (await _search({"query": "fix: retry policy backs off"}))[0]
    assert first["metadata"] == {"commit_hash": FULL_HASH, "author": "Ada Lovelace",
                                 "date": "2026-03-04T10:00:00+00:00", "chunk_index": 0}


@pytest.mark.anyio
async def test_metadata_does_not_repeat_the_text_or_the_fields_beside_it(fake_embedding):
    _index([COMMIT])
    metadata = (await _search({"query": "anything"}))[0]["metadata"]
    assert not {"content", "content_hash", "repo", "source_type"} & set(metadata)


@pytest.mark.anyio
async def test_a_name_in_the_metadata_is_shown_like_the_label_is(fake_embedding):
    """A path comes from the repository: it can hold a credential-shaped
    value or an escape sequence, and the label already replaces both."""
    token = "ghp_" + "A1b2C3d4E5f6" + "G7h8I9j0K1l2" + "M3n4O5p6Q7r8"
    _index([_code("alpha", f"notes/{token}.md", 0, "one"), _code("alpha", "a\x1b[31m\nb.py", 0, "two")])
    results = await _search({"query": "one", "limit": 5})
    paths = [r["metadata"]["file_path"] for r in results]
    assert not any(token in p for p in paths) and any("[REDACTED:" in p for p in paths)
    assert all(p.isprintable() for p in paths)


@pytest.mark.anyio
async def test_a_copied_text_names_the_other_place_in_the_result(fake_embedding):
    _index(COPIES + OTHERS)
    results = await _search({"query": COPIED, "limit": 6})
    copies = [r for r in results if r["metadata"]["file_path"] == "docs/runbook.md"]
    assert len(copies) == 1 and len(copies[0]["also_in"]) == 1
    assert all("also_in" not in r for r in results if r is not copies[0]), "left out when there is nothing to say"


@pytest.mark.anyio
async def test_the_tool_caps_one_document_at_three(fake_embedding):
    _index(LONG_FILE + OTHERS)
    results = await _search({"query": "retry policy", "limit": 9})
    assert len([r for r in results if r["metadata"]["file_path"] == "long.py"]) == 3


@pytest.mark.anyio
async def test_the_schema_describes_the_new_fields():
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]
    properties = tool.output_schema["$defs"]["SearchResult"]["properties"]
    assert {"metadata", "also_in"} <= set(properties)
    assert "also_in" not in tool.output_schema["$defs"]["SearchResult"].get("required", [])


@pytest.mark.anyio
async def test_every_stored_field_the_description_names_is_one_an_indexer_writes():
    """The description tells the agent which fields to expect in `metadata`.
    A field it names and nothing writes sends the agent after nothing."""
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]
    paragraph = next(p for p in tool.description.split("\n\n") if "`metadata`" in p)
    named = set(re.findall(r"`([a-z_]+)`", paragraph)) - {"metadata", "also_in"}
    written = set()
    for module in ("index_code", "index_commits", "index_tags", "index_branches", "index_platform"):
        written |= set(re.findall(r'"([a-z_]+)":', (Path(common.__file__).parent / f"{module}.py").read_text()))
    assert named and named <= written, named - written


@pytest.mark.anyio
async def test_the_search_is_recorded_with_what_was_returned(fake_embedding):
    from griot import logdb
    _index(LONG_FILE + OTHERS)
    await _search({"query": "retry policy", "limit": 9})
    row = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]
    assert row["num_sources"] == 7 == len(row["sources"])
