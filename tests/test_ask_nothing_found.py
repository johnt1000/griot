"""`griot ask` stops when the search finds nothing, and takes the limits
`griot search` takes.

Filters whose values each exist can still match nothing together (a
repository with no commits indexed, asked about its commits), and an empty
index matches nothing at all. `griot search` says "No results." there; `ask`
called the chat model anyway, which can be paid, over an empty context, and
printed whatever it made of nothing. And `--limit` took any integer, so 0 or
a negative limit got through where `griot search` refuses it."""

import hashlib
import random

import pytest

from griot import cli, common, logdb


def _vector(text: str, dim: int) -> list[float]:
    rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
    return [rng.uniform(-1, 1) for _ in range(dim)]


def _doc(repo: str, source_type: str, key: str, text: str, **meta) -> dict:
    return {"id": f"{repo}:{source_type}:{key}", "content": text,
            "metadata": {"source_type": source_type, "repo": repo, **meta}}


# "beta" has code and no commits: `--repo beta --source-type commit` names two
# values that each exist, and nothing indexed matches both.
DOCS = [
    _doc("alpha", "code", "a.py:0", "alpha code about retries", file_path="a.py", chunk_index=0),
    _doc("alpha", "commit", "abc", "alpha commit about retries", commit_hash="abcdef1234"),
    _doc("beta", "code", "b.py:0", "beta code about retries", file_path="b.py", chunk_index=0),
]


@pytest.fixture
def fake_embedding(monkeypatch):
    monkeypatch.setattr(common, "embed_texts", lambda texts, **kw: [_vector(t, common.EMBED_DIM) for t in texts])


@pytest.fixture
def index(fake_embedding):
    common.index_documents(DOCS)
    common.release_lock()


@pytest.fixture
def chat(monkeypatch):
    prompts = []
    monkeypatch.setattr(common, "chat_completion", lambda prompt, model=None: prompts.append(prompt) or "answer")
    return prompts


# --- nothing found: no chat call ----------------------------------------------------------


@pytest.mark.parametrize("mode", [None, "vector", "keyword", "hybrid"])
def test_filters_that_match_nothing_together_stop_before_the_chat_model(index, chat, capsys, mode):
    rc = cli.main(["ask", "retries", "--repo", "beta", "--source-type", "commit",
                   *(["--mode", mode] if mode else [])])
    out = capsys.readouterr().out
    assert chat == [], "the chat model is not called over an empty context"
    assert rc == 0, "the status `griot search` gives the same search"
    first, why = out.splitlines()
    assert first == "No results."
    # Why: the filters narrowed it, and each value exists on its own.
    assert "repository beta" in why and "source type commit" in why


def test_search_says_the_same_for_the_same_filters(index, capsys):
    """`ask` says nothing found the way `griot search` does."""
    assert cli.main(["search", "retries", "--repo", "beta", "--source-type", "commit"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "No results."


def test_an_empty_index_stops_before_the_chat_model(fake_embedding, chat, capsys):
    rc = cli.main(["ask", "retries"])
    out = capsys.readouterr().out
    assert chat == []
    assert rc == 0
    # Exactly that: no answer (nor a None where one would be), and no reason
    # about filters, since nothing narrowed this search.
    assert out.splitlines() == ["No results."]


def test_a_question_that_found_nothing_is_still_logged(index, chat):
    """As griot_search logs a search that found nothing: `griot stats` and
    `golden-set review` read the log, and a question that found nothing is
    one of the things it is for."""
    assert cli.main(["ask", "retries", "--repo", "beta", "--source-type", "commit"]) == 0
    row = logdb.read_latest(common.LOG_DIR, "queries")
    assert row["question"] == "retries"
    assert row["via"] == "cli"
    assert row["num_sources"] == 0 and row["results"] == []
    assert row["repos"] == ["beta"] and row["source_types"] == ["commit"]


def test_show_sources_on_nothing_found_lists_no_source(index, chat, capsys):
    assert cli.main(["ask", "retries", "--repo", "beta", "--source-type", "commit", "--show-sources"]) == 0
    out = capsys.readouterr().out
    assert chat == []
    assert not [line for line in out.splitlines() if line.startswith("- ")]


def test_a_search_that_finds_something_still_reaches_the_chat_model(index, chat, capsys):
    assert cli.main(["ask", "retries", "--repo", "beta"]) == 0
    assert len(chat) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "answer"
    assert "No results." not in out


# --- --limit takes what search takes ------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-3", "two"])
def test_a_limit_search_refuses_ask_refuses_with_the_same_message(chat, capsys, value):
    with pytest.raises(SystemExit) as searched:
        cli.main(["search", "retries", "--limit", value])
    search_err = capsys.readouterr().err.strip().splitlines()[-1]
    with pytest.raises(SystemExit) as asked:
        cli.main(["ask", "retries", "--limit", value])
    ask_err = capsys.readouterr().err.strip().splitlines()[-1]
    assert asked.value.code == searched.value.code == 2
    assert chat == []
    # The program name differs (each parser has its own); what follows it is
    # the same refusal.
    assert ask_err.split(": ", 1)[1] == search_err.split(": ", 1)[1]
    assert "--limit" in ask_err


def test_a_limit_of_one_is_taken(index, chat, capsys):
    assert cli.main(["ask", "retries", "--limit", "1", "--show-sources"]) == 0
    assert len([line for line in capsys.readouterr().out.splitlines() if line.startswith("- ")]) == 1
