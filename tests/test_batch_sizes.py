"""Each embedding profile has the batch sizes that suit it.

There were two numbers for every profile. An API profile sent 50 texts per
request whatever the API: 50 is what Gemini's batch call was tuned for, and
an OpenAI-compatible endpoint takes far more, so an index run made two and
a half times the requests it needed. A local profile gave the model 128
texts at once, which was taken from a rule of thumb and never measured on
the chunks griot actually cuts (up to 1500 characters): measured, a batch
of 128 was slower than a batch of 8 and took several times the memory, and
on the default model it did not finish in fifteen minutes what 8 did in
under one."""

import math
import os
import subprocess
import sys

import pytest

from griot import common

LOCAL = [name for name, profile in common.EMBED_PROFILES.items() if profile["backend"] == "local"]
API = [name for name, profile in common.EMBED_PROFILES.items() if profile["backend"] != "local"]
# The longest text one document can hold (common.chunk_text's default).
CHUNK_CHARS = 1500


def _fresh(profile: str, tmp_path) -> dict:
    """The batch sizes a new process uses under `profile`: they are fixed
    when griot loads."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRIOT_", "RAG_"))}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "c"), GRIOT_DATA_DIR=str(tmp_path / "d"), GRIOT_EMBED_PROFILE=profile)
    done = subprocess.run([sys.executable, "-c", "from griot import common; "
                           "print(common.INDEX_BATCH_SIZE, common.EMBED_CALL_BATCH_SIZE)"],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-1500:]
    index, embed = done.stdout.split()
    return {"index": int(index), "embed": int(embed)}


# --- what each profile gets -------------------------------------------------------------------


@pytest.mark.parametrize("name", LOCAL)
def test_a_local_model_is_given_a_few_texts_at_a_time(name):
    """Measured on chunks of this repository: throughput is flat from 4 to
    16 texts per batch and falls after that, while memory grows with every
    step. 8 is the size that costs nothing in speed and little in memory."""
    assert common.batch_sizes(common.EMBED_PROFILES[name])["embed"] == 8


@pytest.mark.parametrize("name", LOCAL)
def test_a_local_run_still_takes_documents_by_the_hundred(name):
    """The round (hash check, sort by length, write) is another matter: a
    large round sorts more texts together, so each small batch pads less."""
    assert common.batch_sizes(common.EMBED_PROFILES[name])["index"] == 128


def test_gemini_keeps_the_size_its_batch_call_was_tuned_for():
    sizes = common.batch_sizes(common.EMBED_PROFILES["gemini"])
    assert sizes["index"] == 50 and sizes["index"] <= 100, "batchEmbedContents takes at most 100 requests"


def test_an_openai_compatible_endpoint_gets_as_many_texts_as_are_safe_in_one_request():
    sizes = common.batch_sizes(common.EMBED_PROFILES["openai-small"])
    assert sizes["index"] == 128
    # The limits of the embeddings endpoint: 2048 inputs and 300,000 tokens
    # per request. A round of chunks of code is one request (a character of
    # code is about a token at most); text that costs more is cut by bytes.
    assert sizes["index"] <= 2048 and sizes["index"] * CHUNK_CHARS <= 300_000


@pytest.mark.parametrize("name", API)
def test_an_api_profile_has_no_local_batch_to_speak_of(name):
    assert "embed_batch_size" not in common.EMBED_PROFILES[name]


def test_a_profile_added_later_without_sizes_gets_the_cautious_ones():
    assert common.batch_sizes({"backend": "local", "model": "x", "dim": 1}) == {"index": 128, "embed": 8}
    assert common.batch_sizes({"backend": "direct", "model": "x", "dim": 1})["index"] == 50


# --- and a process uses them ------------------------------------------------------------------


@pytest.mark.parametrize("profile,expected", [("jina-code", {"index": 128, "embed": 8}),
                                              ("gemini", {"index": 50, "embed": 8}),
                                              ("openai-small", {"index": 128, "embed": 8})])
def test_a_process_takes_the_sizes_of_its_profile(profile, expected, tmp_path):
    assert _fresh(profile, tmp_path) == expected


def test_an_index_run_on_an_openai_compatible_profile_makes_fewer_requests(monkeypatch):
    """300 documents: six requests of 50 before, three of at most 128 now."""
    import requests

    profile = {**common.EMBED_PROFILES["openai-small"]}
    monkeypatch.setattr(common, "ACTIVE_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "EMBED_DIM", profile["dim"])
    monkeypatch.setattr(common, "COLLECTION_NAME", common.collection_name_for("openai-small"))
    monkeypatch.setattr(common, "INDEX_BATCH_SIZE", common.batch_sizes(profile)["index"])
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")
    sent = []

    class _Answer:
        status_code = 200

        def __init__(self, texts):
            self._texts = texts

        def raise_for_status(self):
            pass

        def json(self):
            return {"usage": {"total_tokens": 10},
                    "data": [{"index": i, "embedding": [0.1] * profile["dim"]} for i in range(len(self._texts))]}

    def post(url, **kwargs):
        sent.append(len(kwargs["json"]["input"]))
        return _Answer(kwargs["json"]["input"])

    monkeypatch.setattr(common, "_http_post", post)
    documents = [{"id": f"repo:code:f{n}.py:0", "content": f"text number {n}",
                  "metadata": {"source_type": "code", "repo": "repo", "file_path": f"f{n}.py", "chunk_index": 0}}
                 for n in range(300)]
    assert common.index_documents(documents) == (300, 0, 0)
    assert sent == [128, 128, 44] and len(sent) == math.ceil(300 / 128)


# --- a request is held to what the endpoint takes, whatever the text ------------------------------
# 128 texts of 1500 characters are under 300,000 tokens only while a
# character is at most one token. That holds for code and for English; a
# chunk of Chinese costs more tokens than it has characters. A token is
# never shorter than one byte, so the bytes of a request bound its tokens.


class _Endpoint:
    """Stands in for the embeddings endpoint; remembers each request."""

    def __init__(self, dim, fail_request=None):
        self.requests, self.dim, self.fail_request = [], dim, fail_request

    def __call__(self, url, **kwargs):
        texts = kwargs["json"]["input"]
        self.requests.append(list(texts))
        endpoint = self

        class Answer:
            status_code = 500 if len(endpoint.requests) == endpoint.fail_request else 200

            def raise_for_status(self):
                if self.status_code != 200:
                    import requests
                    raise requests.HTTPError(response=self)

            def json(self):
                return {"usage": {"total_tokens": 7},
                        "data": [{"index": i, "embedding": [float(len(t))] * endpoint.dim} for i, t in enumerate(texts)]}

        return Answer()


@pytest.fixture
def openai_profile(monkeypatch):
    import requests

    profile = {**common.EMBED_PROFILES["openai-small"]}
    monkeypatch.setattr(common, "ACTIVE_PROFILE", profile)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setenv("GRIOT_OPENAI_API_KEY", "not-a-real-key")

    def use(endpoint):
        monkeypatch.setattr(common, "_http_post", endpoint)
        return endpoint

    return type("Profile", (), {"profile": profile, "use": staticmethod(use), "dim": profile["dim"]})()


def _bytes(texts) -> int:
    return sum(len(text.encode("utf-8")) for text in texts)


def test_a_round_of_ordinary_chunks_is_one_request(openai_profile):
    endpoint = openai_profile.use(_Endpoint(openai_profile.dim))
    texts = ["x" * CHUNK_CHARS for _ in range(128)]
    assert len(common.embed_texts(texts)) == 128 and len(endpoint.requests) == 1


def test_a_round_of_text_that_costs_more_than_a_token_per_character_is_split(openai_profile):
    endpoint = openai_profile.use(_Endpoint(openai_profile.dim))
    texts = [("汉" * CHUNK_CHARS)[: CHUNK_CHARS - n] for n in range(128)]  # each a different length
    vectors = common.embed_texts(texts)
    assert len(endpoint.requests) > 1
    assert all(_bytes(request) <= common.EMBED_PROFILES["openai-small"]["request_token_limit"] for request in endpoint.requests)
    assert [vector[0] for vector in vectors] == [float(len(text)) for text in texts], "every text keeps its own vector"
    assert [text for request in endpoint.requests for text in request] == texts, "and nothing is sent twice or left out"


def test_a_text_larger_than_a_request_on_its_own_still_goes(openai_profile, monkeypatch):
    monkeypatch.setitem(openai_profile.profile, "request_token_limit", 1000)
    endpoint = openai_profile.use(_Endpoint(openai_profile.dim))
    texts = ["a" * 400, "b" * 5000, "c" * 400, "d" * 400]
    vectors = common.embed_texts(texts)
    assert [len(request) for request in endpoint.requests] == [1, 1, 2]
    assert [vector[0] for vector in vectors] == [400.0, 5000.0, 400.0, 400.0]


def test_a_request_that_fails_takes_only_its_own_texts_with_it(openai_profile, monkeypatch):
    monkeypatch.setitem(openai_profile.profile, "request_token_limit", 1000)
    endpoint = openai_profile.use(_Endpoint(openai_profile.dim, fail_request=2))
    texts = ["a" * 600, "b" * 600, "c" * 600]
    vectors = common.embed_texts(texts)
    assert len(endpoint.requests) == 3
    assert vectors[0] is not None and vectors[1] is None and vectors[2] is not None


def test_what_each_request_cost_is_recorded(openai_profile, monkeypatch):
    monkeypatch.setitem(openai_profile.profile, "request_token_limit", 1000)
    openai_profile.use(_Endpoint(openai_profile.dim))
    spent = []
    monkeypatch.setattr(common, "record_spend", lambda cost: spent.append(cost))
    common.embed_texts(["a" * 600, "b" * 600, "c" * 600])
    assert len(spent) == 3 and all(cost > 0 for cost in spent)


def test_a_text_too_large_at_the_start_of_a_round_makes_no_empty_request(openai_profile, monkeypatch):
    monkeypatch.setitem(openai_profile.profile, "request_token_limit", 1000)
    endpoint = openai_profile.use(_Endpoint(openai_profile.dim))
    vectors = common.embed_texts(["b" * 5000, "a" * 400])
    assert [len(request) for request in endpoint.requests] == [1, 1]
    assert [vector[0] for vector in vectors] == [5000.0, 400.0]


def test_the_spend_ceiling_is_looked_at_before_every_request(openai_profile, monkeypatch):
    """One round can now be several paid requests: the ceiling that used to
    be checked once per round is checked once per request."""
    monkeypatch.setitem(openai_profile.profile, "request_token_limit", 1000)
    openai_profile.use(_Endpoint(openai_profile.dim))
    checked = []
    monkeypatch.setattr(common, "check_spend_ceiling", lambda: checked.append(1))
    common.embed_texts(["a" * 600, "b" * 600, "c" * 600])
    assert len(checked) == 3
