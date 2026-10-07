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


def test_an_index_of_two_long_documents_gives_six_and_not_more(fake_embedding):
    """Three chunks of each of two documents is everything there is to give:
    the search looks further, finds nothing else, and returns six for a
    `limit` of eight rather than breaking the per-document rule."""
    _index([_code("alpha", f"long{d}.py", i, f"retry policy document {d} part {i}") for d in range(2) for i in range(30)])
    assert len(common.search("retry policy", limit=8, diverse=True)) == 6


# --- when the first window is not enough -------------------------------------------------
#
# The vectors above are random per text, so which chunk ranks where is luck.
# These tests need a ranking: the query is one fixed vector, and a text is
# that vector plus noise whose size its marker sets (smaller is closer).

_NOISE = {"[closest]": 0.01, "[near]": 0.05, "[mid]": 0.3, "[far]": 1.0}


def _ranked_vector(text: str, dim: int, noise: float | None = None) -> list[float]:
    base = _vector("the query", dim)
    if noise is None:
        noise = next((size for marker, size in _NOISE.items() if marker in text), 0.0)
    jitter = _vector(text, dim)
    return [b + noise * j for b, j in zip(base, jitter)]


@pytest.fixture
def ranked_embedding(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_ranked_vector(t, common.EMBED_DIM) for t in texts])


@pytest.fixture
def store_queries(monkeypatch):
    """How many times the search asked the store, and for how many points."""
    asked = []
    real = common.get_client

    class Counting:
        def __init__(self, shard):
            self._shard = shard

        def query(self, request):
            asked.append(request.limit)
            return self._shard.query(request)

        def __getattr__(self, name):
            return getattr(self._shard, name)

    monkeypatch.setattr(common, "get_client", lambda *a, **kw: Counting(real(*a, **kw)))
    return asked


def _long(repo: str, documents: int, chunks: int, marker: str = "[near]") -> list[dict]:
    return [_code(repo, f"long{d}.py", i, f"{marker} document {d} part {i}") for d in range(documents) for i in range(chunks)]


MID = [_code("alpha", f"mid{i}.py", 0, f"[mid] another file number {i}") for i in range(10)]


def test_when_two_long_documents_fill_the_first_window_the_list_is_still_full(ranked_embedding, store_queries):
    """Debt 9: the first window (six times `limit`) is all chunks of two long
    documents, so it has six slots to give. There is more in the index, and
    the reader asked for eight."""
    _index(_long("alpha", 2, 30) + MID)
    hits = common.search("the query", limit=8, diverse=True)
    assert len(hits) == 8
    assert sum(1 for h in hits if h.payload["file_path"].startswith("long")) == 6, "the cap still holds"
    assert len(store_queries) == 2 and store_queries[1] > store_queries[0]


def test_the_list_is_the_best_of_the_wider_window_in_its_order(ranked_embedding):
    _index(_long("alpha", 2, 30) + MID)
    hits = common.search("the query", limit=8, diverse=True)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    raw = [str(h.id) for h in common.search("the query", limit=70)]
    kept = [str(h.id) for h in hits]
    assert kept == [i for i in raw if i in set(kept)], "a subsequence of the plain ranking"
    assert [h.payload["file_path"] for h in hits[6:]] == [h.payload["file_path"] for h in
                                                          common.search("the query", limit=70)[60:62]], \
        "after the capped documents, the next best of everything the store holds"


def test_a_first_window_that_fills_the_list_is_the_only_query(fake_embedding, store_queries):
    """The common case must not pay for the rare one."""
    many = [_code("alpha", f"file{i}.py", 0, f"retry policy in file number {i}") for i in range(30)]
    _index(many)
    assert len(common.search("retry policy", limit=3, diverse=True)) == 3
    assert store_queries == [3 * 6], "the store had more than the window (30 points), and was not asked again"


def test_the_search_stops_when_the_store_has_nothing_more(ranked_embedding, store_queries):
    """A window that came back short of what was asked for is the whole
    index: asking again would bring nothing new."""
    _index(_long("alpha", 2, 30))
    assert len(common.search("the query", limit=8, diverse=True)) == 6
    assert len(store_queries) == 2


def test_the_search_looks_a_bounded_number_of_windows_further(ranked_embedding, store_queries):
    """Two documents so long that no window griot is willing to fetch gets
    past them: the list stays short rather than the search going on."""
    _index(_long("alpha", 2, 300) + MID)
    assert len(common.search("the query", limit=8, diverse=True)) == 6
    # Spelled out rather than read from SEARCH_MAX_EXTRA_WINDOWS: a test that
    # reads the constant it checks agrees with any value of it.
    assert store_queries == [48, 96, 192]


def test_a_grouped_search_fills_its_documents_from_a_wider_window_too(ranked_embedding, store_queries):
    _index(_long("alpha", 2, 20) + MID)
    hits = common.search("the query", limit=5, group_by_document=True, diverse=True)
    assert len(hits) == 5 and len(set(_documents(hits))) == 5
    assert len(store_queries) == 2


def test_a_copy_found_in_a_later_window_is_named_in_also_in(ranked_embedding, monkeypatch):
    """The runbook is among the best matches; its copy in another repository
    ranks far below, past the first window. When the search had to look
    further anyway, the copy it found there is named, not shown again."""
    runbook = "[closest] the runbook, copied into two repositories"
    _index(_long("alpha", 2, 24) + MID + [_code("alpha", "docs/runbook.md", 0, runbook)])
    monkeypatch.setattr(common, "embed_texts",
                        lambda texts, **kw: [_ranked_vector(t, common.EMBED_DIM, noise=0.2) for t in texts])
    _index([_code("beta", "docs/runbook.md", 0, runbook)])
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_ranked_vector(t, common.EMBED_DIM) for t in texts])
    plain = [h.payload["repo"] for h in common.search("the query", limit=60) if h.payload["file_path"] == "docs/runbook.md"]
    assert plain == ["alpha", "beta"] and len(common.search("the query", limit=48)) == 48
    beta_rank = [h.payload["repo"] for h in common.search("the query", limit=60)].index("beta")
    assert beta_rank >= 48, "the copy is past the first window"
    hits = common.search("the query", limit=8, diverse=True)
    assert len(hits) == 8
    runbooks = [h for h in hits if h.payload["file_path"] == "docs/runbook.md"]
    assert len(runbooks) == 1 and runbooks[0].also_in == ["beta/docs/runbook.md"]


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


def test_what_measures_retrieval_is_not():
    """The retrieval evaluation (recall@k) needs every point in the store's
    order. The golden set is no longer here: its cases assert what a reader
    gets, so it searches as readers do, and the self-check's raw search is
    held by behaviour in test_golden_set_searches_like_readers.py."""
    source = (Path(common.__file__).parent / "retrieval_eval.py").read_text()
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
async def test_the_tool_returns_the_limit_when_two_long_documents_lead(ranked_embedding):
    """Debt 9 through the protocol: the agent asked for eight."""
    _index(_long("alpha", 2, 30) + MID)
    results = await _search({"query": "the query", "limit": 8})
    assert len(results) == 8
    assert sum(1 for r in results if r["metadata"]["file_path"].startswith("long")) == 6


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


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
def test_a_keyword_or_hybrid_search_fills_its_list_from_a_wider_window_too(ranked_embedding, store_queries, mode):
    """The wider window (debt 9) and the search modes landed separately: the
    window has to be asked in the mode of the search, or a keyword search
    would come back short where a vector one is full."""
    long_docs = [_code("alpha", f"long{d}.py", i, f"[near] widget widget widget document {d} part {i}")
                 for d in range(2) for i in range(30)]
    mids = [_code("alpha", f"mid{i}.py", 0, f"[mid] widget file number {i}") for i in range(10)]
    _index(long_docs + mids)

    hits = common.search("widget", limit=8, diverse=True, mode=mode)

    assert len(hits) == 8
    assert sum(1 for h in hits if h.payload["file_path"].startswith("long")) == 6, "the cap still holds"
    # A hybrid window asks the store once per ranking, both for the same
    # number of points (the two searches the store's fusion ran as prefetches).
    per_window = 2 if mode == "hybrid" else 1
    windows = store_queries[::per_window]
    assert store_queries == [limit for limit in windows for _ in range(per_window)]
    assert len(windows) == 2 and windows[1] > windows[0]
