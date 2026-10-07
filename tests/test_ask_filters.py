"""`griot ask` takes the filters `griot search` has.

`griot search` and griot_search could be narrowed to repositories and kinds of
source; `griot ask` searched everything, so a question about one repository
paid for a synthesis over context from all of them. Same flags, same search
function, same rules: a value that cannot match is an error, refused before
the chat model is called, not an answer written over nothing."""

import hashlib
import random

import pytest

from griot import cli, common, golden_set, logdb


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


# Two repositories, two kinds of source; every text holds the word "retries"
# so every mode finds every point when nothing narrows the search.
DOCS = [
    _doc("alpha", "code", "a.py:0", "alpha code about retries", file_path="a.py", chunk_index=0),
    _doc("alpha", "commit", "abc", "alpha commit about retries", commit_hash="abcdef1234"),
    _doc("beta", "code", "b.py:0", "beta code about retries", file_path="b.py", chunk_index=0),
    _doc("beta", "commit", "def", "beta commit about retries", commit_hash="def0123456"),
]


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])
    common.index_documents(DOCS)
    common.release_lock()


@pytest.fixture
def chat(monkeypatch):
    prompts = []
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: prompts.append(prompt) or "answer")
    return prompts


def _sources(out: str) -> list[str]:
    """The labels `--show-sources` prints, one per line after the marker."""
    listed = out.split("--- sources ---", 1)[1].splitlines()
    return [line[2:].rsplit(" (score=", 1)[0] for line in listed if line.startswith("- ")]


def _ask(*args) -> int:
    return cli.main(["ask", "retries", "--limit", "10", "--show-sources", *args])


# --- against a real index -----------------------------------------------------------------


def test_without_flags_ask_searches_every_repository_and_kind(index, chat, capsys):
    assert _ask() == 0
    assert len(_sources(capsys.readouterr().out)) == len(DOCS)


@pytest.mark.parametrize("mode", [None, "vector", "keyword", "hybrid"])
def test_a_repository_filter_keeps_the_context_to_that_repository(index, chat, capsys, mode):
    """Whatever the mode, the default included: the filter applies to the
    search the mode runs, as it does for `griot search`."""
    assert _ask("--repo", "beta", *(["--mode", mode] if mode else [])) == 0
    out = capsys.readouterr().out
    sources = _sources(out)
    assert sorted(sources) == sorted(["beta/b.py", "commit def01234 — beta"])
    assert "alpha" not in chat[0], "nothing from another repository reaches the paid synthesis"
    assert f"Mode: {mode or 'hybrid'}" in out


def test_a_source_filter_keeps_the_context_to_that_kind(index, chat, capsys):
    assert _ask("--source-type", "commit") == 0
    sources = _sources(capsys.readouterr().out)
    assert len(sources) == 2 and all(s.startswith("commit ") for s in sources)


def test_a_flag_given_twice_is_both_values(index, chat, capsys):
    assert _ask("--repo", "alpha", "--repo", "beta", "--source-type", "code", "--source-type", "commit") == 0
    assert len(_sources(capsys.readouterr().out)) == len(DOCS)


def test_both_filters_together_narrow_to_what_matches_both(index, chat, capsys):
    assert _ask("--repo", "alpha", "--source-type", "code") == 0
    assert _sources(capsys.readouterr().out) == ["alpha/a.py"]


@pytest.mark.parametrize("flags,named", [
    (["--source-type", "pull_request"], "merge_request"),
    (["--repo", "gamma"], "gamma"),
])
def test_a_filter_that_cannot_match_is_an_error_and_pays_for_nothing(index, chat, capsys, monkeypatch, flags,
                                                                    named):
    """An answer written over an empty context would read as "nothing about
    this there", and it would cost a chat call to say it."""
    embedded = []
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: embedded.append(texts) or [])
    assert _ask(*flags) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ") and named in captured.err
    assert "Traceback" not in captured.err and "answer" not in captured.out
    assert chat == [], "the chat model is never called"
    assert embedded == [], "the query is not embedded either"


# --- what the log says ----------------------------------------------------------------------


def test_a_narrowed_question_is_logged_as_narrowed(index, chat):
    """`griot golden-set review` makes cases from logged questions, and a case
    is checked over every repository: a narrowed one must say so in the log,
    as griot_search's does, or it becomes a case that fails for good."""
    assert _ask("--repo", "beta", "--source-type", "code") == 0
    row = logdb.read_latest(common.LOG_DIR, "queries")
    assert row["repos"] == ["beta"] and row["source_types"] == ["code"]
    assert golden_set._unlike_the_check(row), "review does not offer it as a case"


def test_an_unnarrowed_question_is_logged_without_filters(index, chat):
    assert _ask() == 0
    row = logdb.read_latest(common.LOG_DIR, "queries")
    assert row.get("repos") is None and row.get("source_types") is None
    assert golden_set._unlike_the_check(row) == []


# --- the help says what the flags take ----------------------------------------------------


def test_the_help_names_the_flags_and_the_kinds(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["ask", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert stop.value.code == 0
    assert "--repo" in out and "--source-type" in out
    for kind in common.SOURCE_TYPES:
        assert kind in out, "the kinds are listed where a person looks for them"
