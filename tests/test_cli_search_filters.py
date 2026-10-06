"""`griot search` takes the filters `griot_search` has.

The MCP tool could be narrowed to repositories and kinds of source, and
grouped by document; the command a person runs to see what an agent sees
could not. Same search function, same rules: a value that cannot match is an
error, not an empty result."""

import hashlib
import random

import pytest

from griot import cli, common


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


DOCS = [
    _doc("alpha", "code", "a.py:0", "alpha code about retries", file_path="a.py", chunk_index=0),
    _doc("alpha", "code", "a.py:1", "alpha code about retries, second part", file_path="a.py", chunk_index=1),
    _doc("alpha", "commit", "abc", "alpha commit about retries", commit_hash="abcdef1234"),
    _doc("beta", "code", "b.py:0", "beta code about retries", file_path="b.py", chunk_index=0),
]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(DOCS)
    common.release_lock()


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(common, "search", lambda query, **kwargs: calls.append((query, kwargs)) or [])
    return calls


def _labels(capsys) -> list[str]:
    return [line.split("] ", 1)[1] for line in capsys.readouterr().out.splitlines() if line.startswith("[")]


# --- what reaches the search --------------------------------------------------------------


def test_without_flags_nothing_is_narrowed(recorded):
    assert cli.main(["search", "retries"]) == 0
    query, kwargs = recorded[0]
    assert query == "retries"
    assert kwargs == {"limit": 5, "group_by_document": False, "repos": None, "source_types": None, "diverse": True,
                      "mode": "vector"}


def test_each_flag_reaches_the_search(recorded):
    assert cli.main(["search", "retries", "--repo", "alpha", "--source-type", "code", "--group-by-document",
                     "--limit", "3"]) == 0
    assert recorded[0][1] == {"limit": 3, "group_by_document": True, "repos": ["alpha"], "source_types": ["code"],
                              "diverse": True, "mode": "vector"}


def test_a_flag_given_twice_is_both_values(recorded):
    assert cli.main(["search", "retries", "--repo", "alpha", "--repo", "beta",
                     "--source-type", "commit", "--source-type", "merge_request"]) == 0
    assert recorded[0][1]["repos"] == ["alpha", "beta"]
    assert recorded[0][1]["source_types"] == ["commit", "merge_request"]


# --- against a real index -----------------------------------------------------------------


def test_a_repository_filter_prints_only_that_repository(index, capsys):
    assert cli.main(["search", "retries", "--limit", "10", "--repo", "beta"]) == 0
    assert _labels(capsys) == ["beta/b.py"]


def test_a_source_filter_prints_only_that_kind(index, capsys):
    assert cli.main(["search", "retries", "--limit", "10", "--source-type", "commit"]) == 0
    labels = _labels(capsys)
    assert len(labels) == 1 and labels[0].startswith("commit abcdef12")


def test_grouping_prints_one_line_per_document(index, capsys):
    assert cli.main(["search", "retries", "--limit", "10", "--repo", "alpha", "--group-by-document"]) == 0
    labels = _labels(capsys)
    assert sorted(labels) == sorted(set(labels)) and len(labels) == 2, "a.py once, the commit once"


@pytest.mark.parametrize("flags,named", [
    (["--source-type", "pull_request"], "merge_request"),
    (["--repo", "gamma"], "gamma"),
])
def test_a_filter_that_cannot_match_is_an_error_not_an_empty_result(index, capsys, flags, named):
    """"No results." would read as "nothing about this there"."""
    assert cli.main(["search", "retries", *flags]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ") and named in captured.err
    assert "No results." not in captured.out and "Traceback" not in captured.err


def test_two_values_that_exist_and_match_nothing_together_are_no_results(index, capsys):
    assert cli.main(["search", "retries", "--repo", "beta", "--source-type", "commit"]) == 0
    assert "No results." in capsys.readouterr().out


def test_an_error_that_is_not_about_the_filters_is_not_swallowed(monkeypatch):
    """Only what the search refuses about its arguments becomes a usage
    error. Anything else keeps its own handling."""
    def broken(query, **kwargs):
        raise RuntimeError("the query could not be embedded")

    monkeypatch.setattr(common, "search", broken)
    with pytest.raises(RuntimeError):
        cli.main(["search", "retries", "--repo", "alpha"])


@pytest.mark.parametrize("limit", ["0", "-3", "many"])
def test_a_limit_that_is_not_a_positive_number_is_a_usage_error(limit, capsys, recorded):
    """A negative one reached the vector store, multiplied, and came back
    as an overflow traceback."""
    with pytest.raises(SystemExit) as stop:
        cli.main(["search", "retries", f"--limit={limit}"])
    assert stop.value.code == 2 and "--limit" in capsys.readouterr().err
    assert recorded == []


# --- the help says what the flags take ----------------------------------------------------


def test_the_help_names_the_flags_without_loading_the_index(capsys, monkeypatch):
    with pytest.raises(SystemExit) as stop:
        cli.main(["search", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert stop.value.code == 0
    assert "--repo" in out and "--source-type" in out and "--group-by-document" in out
    for kind in ("code", "commit", "merge_request", "issue"):
        assert kind in out, "the kinds are listed where a person looks for them"


def test_the_kinds_the_help_lists_are_the_ones_the_search_accepts():
    """The parser must not import the index to build its help, so it keeps
    its own copy of the list. This holds the copy to the original."""
    assert tuple(cli.SEARCH_SOURCE_TYPES) == tuple(common.SOURCE_TYPES)
