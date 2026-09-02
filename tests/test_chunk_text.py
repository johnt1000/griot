import pytest

from griot import common


def test_empty_text_returns_no_chunks():
    assert common.chunk_text("") == []


def test_short_text_returns_single_chunk():
    text = "hello world"
    assert common.chunk_text(text, max_chars=1500, overlap=200) == [text]


def test_text_shorter_than_overlap_returns_single_chunk():
    text = "abc"
    assert common.chunk_text(text, max_chars=10, overlap=5) == [text]


def test_exact_max_chars_length_produces_single_chunk():
    """Regression: text with exactly max_chars produced a spurious 2nd chunk
    (a duplicate of the overlap) because the loop didn't detect that the 1st
    chunk had already covered the entire text."""
    text = "x" * 1500
    chunks = common.chunk_text(text, max_chars=1500, overlap=200)
    assert chunks == [text]


@pytest.mark.parametrize("length", [1301, 1400, 1499, 1500])
def test_no_spurious_trailing_chunk_near_boundary(length):
    """Same regression, sweeping the whole window where it manifested: text
    whose length falls within the last `overlap` chars of a full chunk."""
    text = "y" * length
    chunks = common.chunk_text(text, max_chars=1500, overlap=200)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_multi_chunk_text_covers_everything_with_overlap():
    text = "".join(f"{i:04d}" for i in range(1000))  # 4000 deterministic chars
    chunks = common.chunk_text(text, max_chars=1500, overlap=200)
    assert len(chunks) > 1
    # each chunk (except the last) is exactly max_chars long
    for chunk in chunks[:-1]:
        assert len(chunk) == 1500
    # reconstruction: concatenating while removing the overlap matches the original
    rebuilt = chunks[0]
    for chunk in chunks[1:]:
        rebuilt += chunk[200:]
    assert rebuilt == text


def test_last_chunk_is_not_a_near_duplicate_of_previous():
    text = "z" * 3000
    chunks = common.chunk_text(text, max_chars=1500, overlap=200)
    assert chunks[-1] != chunks[-2]


def test_overlap_greater_or_equal_to_max_chars_raises():
    with pytest.raises(ValueError):
        common.chunk_text("qualquer coisa", max_chars=100, overlap=100)
    with pytest.raises(ValueError):
        common.chunk_text("qualquer coisa", max_chars=100, overlap=150)
