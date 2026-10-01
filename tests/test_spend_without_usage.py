"""A paid call whose answer says nothing about tokens is still counted.

The cost of a call was the token count the provider reported times the
price. A provider that reported none (the field missing, null, zero) made
the cost zero: nothing was added to the day's total, and the spend ceiling,
which is the only budget these calls have, never came any closer. The
ceiling stayed open exactly when it had nothing to go by.

Without a reported count, the size of what was sent is what there is: the
call is counted from it, as an estimate, and said to be one."""

import math

import pytest

from griot import common


class _Answer:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _api(monkeypatch, body):
    """The API answers every call with `body` (a function of the request, or a dict)."""
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs.get("json"))
        return _Answer(body(kwargs.get("json")) if callable(body) else body)

    monkeypatch.setattr(common, "_http_post", post)
    return calls


@pytest.fixture
def spent(monkeypatch):
    """What record_spend() was given, in order."""
    recorded = []
    real = common.record_spend

    def record(cost):
        recorded.append(cost)
        real(cost)

    monkeypatch.setattr(common, "record_spend", record)
    return recorded


@pytest.fixture
def openai_embeddings(monkeypatch):
    profile = {**common.EMBED_PROFILES["openai-small"]}
    monkeypatch.setattr(common, "ACTIVE_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")
    return profile


@pytest.fixture
def gemini_embeddings(monkeypatch):
    profile = {**common.EMBED_PROFILES["gemini"]}
    monkeypatch.setattr(common, "ACTIVE_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "gemini")
    monkeypatch.setattr(common, "EMBED_DIM", profile["dim"])
    monkeypatch.setattr(common, "GEMINI_TOKEN", "not-a-real-token")
    return profile


def _openai_vectors(usage="absent"):
    def body(request):
        answer = {"data": [{"index": i, "embedding": [0.1]} for i in range(len(request["input"]))]}
        if usage != "absent":
            answer["usage"] = usage
        return answer
    return body


def _estimate(texts, price) -> float:
    size = sum(len(text.encode("utf-8")) for text in texts)
    return max(1, math.ceil(size / common.FALLBACK_BYTES_PER_TOKEN)) / 1_000_000 * price


TEXTS = ["a chunk of code " * 60, "another chunk " * 50]


# --- embeddings ---------------------------------------------------------------------------------


@pytest.mark.parametrize("usage", ["absent", None, {}, {"total_tokens": None}, {"total_tokens": 0},
                                   {"total_tokens": "many"}, {"total_tokens": True}, {"prompt_tokens": 12},
                                   "not an object", []])
def test_an_embedding_answer_without_a_token_count_is_counted_from_its_size(monkeypatch, openai_embeddings, spent, usage):
    _api(monkeypatch, _openai_vectors(usage))
    vectors = common.embed_texts(TEXTS)
    assert all(vector is not None for vector in vectors), "the vectors are good: only the count is missing"
    assert spent == [pytest.approx(_estimate(TEXTS, openai_embeddings["price_per_1m_tokens"]))]
    assert spent[0] > 0 and common.get_spend_today() == pytest.approx(spent[0])


def test_a_reported_count_is_what_is_billed(monkeypatch, openai_embeddings, spent):
    _api(monkeypatch, _openai_vectors({"total_tokens": 1000}))
    common.embed_texts(TEXTS)
    assert spent == [pytest.approx(1000 / 1_000_000 * openai_embeddings["price_per_1m_tokens"])]


def test_gemini_embeddings_without_a_token_count_are_counted_too(monkeypatch, gemini_embeddings, spent):
    """An answer that holds the vectors and nothing else."""
    _api(monkeypatch, lambda request: {"embeddings": [{"values": [0.1] * gemini_embeddings["dim"]} for _ in request["requests"]]})
    assert all(vector is not None for vector in common.embed_texts(TEXTS))
    assert spent == [pytest.approx(_estimate(TEXTS, gemini_embeddings["price_per_1m_tokens"]))]


def test_text_that_costs_more_than_a_token_per_character_is_not_undercounted_by_characters(monkeypatch, openai_embeddings, spent):
    """Counted in bytes: a line of Chinese is three times its length."""
    _api(monkeypatch, _openai_vectors())
    texts = ["汉" * 300]
    common.embed_texts(texts)
    assert spent[0] == pytest.approx(math.ceil(900 / common.FALLBACK_BYTES_PER_TOKEN) / 1_000_000 * openai_embeddings["price_per_1m_tokens"])


# --- the ceiling closes -------------------------------------------------------------------------


def test_the_ceiling_is_reached_by_calls_that_report_nothing(monkeypatch, openai_embeddings):
    """It never was: every such call added zero."""
    _api(monkeypatch, _openai_vectors())
    one_call = _estimate(TEXTS, openai_embeddings["price_per_1m_tokens"])
    monkeypatch.setattr(common, "SPEND_CEILING_USD", one_call * 2.5)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 1000.0)
    for _ in range(3):
        common.embed_texts(TEXTS)
    with pytest.raises(RuntimeError, match="circuit breaker"):
        common.embed_texts(TEXTS)


# --- chat ---------------------------------------------------------------------------------------


@pytest.fixture
def openai_chat(monkeypatch):
    profile = {**common.CHAT_PROFILES["openai"], "price_per_1m_tokens": 2.0}
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "openai")
    monkeypatch.setenv(profile["api_key_env"], "not-a-real-key")
    return profile


@pytest.fixture
def gemini_chat(monkeypatch):
    profile = {**common.CHAT_PROFILES["gemini"]}
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", "gemini")
    monkeypatch.setattr(common, "GEMINI_TOKEN", "not-a-real-token")
    return profile


PROMPT = "What does the retry policy do? " * 20
REPLY = "It waits and tries again. " * 10


def test_a_chat_answer_without_a_token_count_is_counted_from_the_prompt_and_the_reply(monkeypatch, openai_chat, spent):
    _api(monkeypatch, {"choices": [{"message": {"content": REPLY}}]})
    assert common.chat_completion(PROMPT) == REPLY
    assert spent == [pytest.approx(_estimate([PROMPT, REPLY], openai_chat["price_per_1m_tokens"]))]


def test_a_gemini_chat_answer_without_a_token_count_is_counted_too(monkeypatch, gemini_chat, spent):
    _api(monkeypatch, {"candidates": [{"content": {"parts": [{"text": REPLY}]}}]})
    assert common.chat_completion(PROMPT) == REPLY
    assert spent == [pytest.approx(_estimate([PROMPT, REPLY], gemini_chat["price_per_1m_tokens"]))]


def test_a_chat_answer_that_reports_its_tokens_is_billed_by_them(monkeypatch, openai_chat, spent):
    _api(monkeypatch, {"choices": [{"message": {"content": REPLY}}], "usage": {"total_tokens": 500}})
    common.chat_completion(PROMPT)
    assert spent == [pytest.approx(500 / 1_000_000 * 2.0)]


# --- it is said, and what was refused stays refused ---------------------------------------------


def test_it_is_said_once_that_the_spend_is_an_estimate(monkeypatch, openai_embeddings):
    _api(monkeypatch, _openai_vectors())
    said = []
    monkeypatch.setattr(common, "log_and_print", lambda message, **kw: said.append(message))
    monkeypatch.setattr(common, "_estimated_spend_was_said", False)
    for _ in range(3):
        common.embed_texts(TEXTS)
    about = [message for message in said if "estimate" in message]
    assert len(about) == 1 and "did not report" in about[0]


@pytest.mark.parametrize("tokens", [-5, float("nan"), float("inf")])
def test_a_count_that_is_a_number_and_not_an_amount_still_stops_the_run(monkeypatch, openai_embeddings, tokens):
    """Not "nothing reported": something wrong reported. It is not papered
    over with an estimate."""
    _api(monkeypatch, _openai_vectors({"total_tokens": tokens}))
    with pytest.raises(RuntimeError, match="not an amount"):
        common.embed_texts(TEXTS)


@pytest.mark.parametrize("which", ["openai", "gemini"])
def test_a_chat_answer_with_no_reply_in_it_is_counted_before_it_fails(monkeypatch, request, spent, which):
    """The call was made and billed. That its answer cannot be read is a
    second problem, not a reason to leave the first uncounted."""
    profile = request.getfixturevalue(f"{which}_chat")
    _api(monkeypatch, {"something": "else"})
    with pytest.raises((KeyError, IndexError, TypeError)):
        common.chat_completion(PROMPT)
    assert spent == [pytest.approx(_estimate([PROMPT], profile["price_per_1m_tokens"]))]


def test_the_estimate_errs_on_the_side_of_counting_more():
    """A ceiling that is reached late is the one that hurts. The size of an
    index run is estimated at 3.5 characters per token, which was measured;
    a call nobody reported is counted at fewer bytes per token than that."""
    assert common.FALLBACK_BYTES_PER_TOKEN < common.CHARS_PER_TOKEN_ESTIMATE


@pytest.mark.parametrize("texts", [[""], ["", ""]])
def test_a_call_with_nothing_to_size_is_still_not_free(monkeypatch, openai_embeddings, spent, texts):
    _api(monkeypatch, _openai_vectors())
    common.embed_texts(texts)
    assert spent == [pytest.approx(1 / 1_000_000 * openai_embeddings["price_per_1m_tokens"])] and spent[0] > 0
