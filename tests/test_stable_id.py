import uuid

from griot import common


def test_stable_id_is_deterministic():
    key = "meu-projeto:commit:abc123"
    assert common.stable_id(key) == common.stable_id(key)


def test_stable_id_differs_for_different_keys():
    assert common.stable_id("a") != common.stable_id("b")


def test_stable_id_is_a_valid_uuid_string():
    result = common.stable_id("repo:tag:v1.0.0")
    parsed = uuid.UUID(result)  # should not raise
    assert str(parsed) == result
