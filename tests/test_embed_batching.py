"""Tests for the local-embedding quick win (plan, section 10.2, items 1 and 3):
explicit batch_size + sorting by length in embed_texts() (backend
"local"), with the critical guarantee that the output order matches the
INPUT order (the caller, index_documents, does zip(to_embed, vectors) assuming
index-to-index correspondence)."""

from griot import common


class _FakeVector:
    """Replaces the numpy object that real fastembed returns — it only
    needs to expose .tolist(), which is all embed_texts() uses from it."""

    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


class _FakeEmbedModel:
    """Mock of TextEmbedding: .embed() returns one vector per text, in the SAME
    order the texts arrived in (real fastembed contract) — the vector
    encodes the text's own length, so tests can check
    correspondence without needing to track full textual content. It also
    records the kwargs received, to confirm that batch_size was passed through."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, **kwargs):
        self.calls.append({"texts": list(texts), "kwargs": kwargs})
        for t in texts:
            yield _FakeVector([float(len(t))])


def _local_profile(monkeypatch, fake_model):
    monkeypatch.setattr(common, "ACTIVE_PROFILE", {"backend": "local", "model": "fake-model", "dim": 1})
    monkeypatch.setattr(common, "get_embed_model", lambda: fake_model)


def test_embed_texts_local_output_order_matches_input_order(monkeypatch):
    fake_model = _FakeEmbedModel()
    _local_profile(monkeypatch, fake_model)

    texts = [
        "mid length text here",           # 21 chars
        "x",                               # 1 char (shortest)
        "a very long chunk of code " * 10,  # much longer
        "commit curto",                    # 13 chars
    ]

    vectors = common.embed_texts(texts)

    # Each output vector must correspond to the text at the SAME index in the
    # input list, not to the internal order (by length) in which the fake
    # model processed the texts.
    assert vectors == [[float(len(t))] for t in texts]

    # Confirms that internal processing really happened in a different order
    # (by length) — otherwise the assertion above wouldn't prove anything about the unsort.
    processed_order = fake_model.calls[0]["texts"]
    assert processed_order != texts
    assert processed_order == sorted(texts, key=len)


def test_embed_texts_local_passes_explicit_batch_size(monkeypatch):
    fake_model = _FakeEmbedModel()
    _local_profile(monkeypatch, fake_model)

    common.embed_texts(["a", "bb", "ccc"])

    assert fake_model.calls[0]["kwargs"].get("batch_size") == common.EMBED_CALL_BATCH_SIZE


def test_embed_texts_local_empty_list_does_not_call_embed(monkeypatch):
    fake_model = _FakeEmbedModel()
    _local_profile(monkeypatch, fake_model)

    assert common.embed_texts([]) == []
    assert fake_model.calls == []


def test_embed_texts_local_single_item(monkeypatch):
    fake_model = _FakeEmbedModel()
    _local_profile(monkeypatch, fake_model)

    result = common.embed_texts(["a single text"])

    assert result == [[float(len("a single text"))]]


# --- get_embed_model(): explicit cache_dir -------------------------------


def test_get_embed_model_uses_data_dir_for_cache(monkeypatch, tmp_path):
    """Real finding: without an explicit cache_dir, fastembed defaults to
    tempfile.gettempdir()/fastembed_cache — a model of hundreds of MB to a
    few GB can disappear if the OS cleans up the temp directory, forcing a
    silent re-download. cache_dir needs to point inside $GRIOT_DATA_DIR,
    alongside the rest of griot's data (persistent, survives /tmp cleanup)."""
    monkeypatch.setattr(common, "_embed_model", None)
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)

    captured = {}

    class FakeTextEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(common, "_text_embedding_class", lambda: FakeTextEmbedding)

    common.get_embed_model()

    assert captured["cache_dir"] == str(tmp_path / "models")
