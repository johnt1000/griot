"""`griot_audit` says where the index holds credential-looking values.

`griot audit` at a terminal lists them: places and rule names, never the
values. An agent asked "is anything sensitive in what you indexed?" had no
way to find out. The tool answers with the same places, for the same reason
the command exists: what an older version stored raw stays in the store
until its repository is indexed again.

The values are assembled from pieces for the reason given in test_redaction.py."""

import hashlib
import json

import pytest
import qdrant_edge as qe
from mcp.client.client import Client

from griot import common, mcp_server, redaction


def T(*parts: str) -> str:
    return "".join(parts)


TOKEN = T("ghp_", "A1b2C3d4E5f6G7h8I9j0", "K1l2M3n4O5p6Q7r8")


def _store_raw(doc_id, content, **metadata):
    """A point as an older version left it: stored without the detectors."""
    client = common.get_client()
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        id=common.stable_id(doc_id), vector={"dense": [0.1] * common.EMBED_DIM},
        payload={**metadata, "content": content, "content_hash": hashlib.md5(content.encode()).hexdigest()})]))
    client.flush()


def _code(repo, path, content, chunk=0):
    _store_raw(f"{repo}:code:{path}:{chunk}", content, source_type="code", repo=repo, file_path=path, chunk_index=chunk)


async def _audit(**arguments):
    async with Client(mcp_server.mcp) as client:
        return await client.call_tool("griot_audit", arguments)


# --- what it finds ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_lists_the_places_and_the_rules_and_never_the_values():
    _code("alpha", "config.yml", "token: " + TOKEN)
    _code("alpha", "clean.py", "x = 1")
    result = await _audit()
    out = result.structured_content
    assert (out["total"], out["scanned"], out["complete"]) == (1, 2, True)
    assert len(out["places"]) == 1
    place = out["places"][0]
    assert "alpha/config.yml" in place["where"] and place["repo"] == "alpha" and place["source_type"] == "code"
    assert place["rules"] == ["github-token"] and place["count"] == 1
    assert TOKEN not in json.dumps(out) and TOKEN not in str(result.content)
    assert out["profile"] == common.ACTIVE_PROFILE_NAME


@pytest.mark.anyio
async def test_several_values_in_one_place_are_counted_and_the_rules_named_once():
    _code("alpha", "config.yml", "token: " + TOKEN, chunk=0)
    _code("alpha", "config.yml", "other: " + TOKEN + "\nagain: " + TOKEN, chunk=1)
    out = (await _audit()).structured_content
    assert out["total"] == 3 and sum(place["count"] for place in out["places"]) == 3
    assert all(place["rules"] == ["github-token"] for place in out["places"])


@pytest.mark.anyio
async def test_it_says_how_many_each_repository_has():
    _code("alpha", "a.yml", "token: " + TOKEN)
    _code("beta", "b.yml", "token: " + TOKEN)
    _code("beta", "c.yml", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert out["by_repo"] == [{"repo": "beta", "count": 2}, {"repo": "alpha", "count": 1}]


@pytest.mark.anyio
async def test_a_clean_index_is_said_to_be_clean_with_what_was_read():
    _code("alpha", "clean.py", "x = 1")
    out = (await _audit()).structured_content
    assert (out["total"], out["places"], out["scanned"], out["complete"]) == (0, [], 1, True)


@pytest.mark.anyio
async def test_it_says_what_to_do_about_a_finding():
    _code("alpha", "config.yml", "token: " + TOKEN)
    note = (await _audit()).structured_content["note"]
    assert "rotate" in note and "index" in note and "never" in note.lower()


# --- nothing to read is not "clean" -----------------------------------------------------------


@pytest.mark.anyio
async def test_with_nothing_indexed_it_is_an_error_and_creates_nothing():
    """Zero findings over zero points reads as a clean index."""
    assert not common.collection_exists(common.COLLECTION_NAME)
    result = await _audit()
    assert result.is_error is True and "nothing is indexed" in str(result.content).lower()
    assert not common.collection_exists(common.COLLECTION_NAME), "an audit must not create what it audits"


@pytest.mark.anyio
async def test_an_empty_collection_is_an_error_too():
    common.get_client()
    result = await _audit()
    assert result.is_error is True and "nothing is indexed" in str(result.content).lower()


# --- one repository at a time ---------------------------------------------------------------


@pytest.mark.anyio
async def test_it_can_be_narrowed_to_repositories():
    _code("alpha", "a.yml", "token: " + TOKEN)
    _code("beta", "b.yml", "token: " + TOKEN)
    out = (await _audit(repos=["beta"])).structured_content
    assert [place["repo"] for place in out["places"]] == ["beta"] and out["scanned"] == 1


@pytest.mark.anyio
async def test_a_repository_with_nothing_indexed_is_an_error_not_a_clean_result():
    _code("alpha", "a.yml", "token: " + TOKEN)
    result = await _audit(repos=["never-indexed"])
    assert result.is_error is True and "never-indexed" in str(result.content)


# --- bounded ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_past_the_ceiling_it_stops_and_says_it_did_not_read_everything(monkeypatch):
    """Every stored chunk is read and matched: on a large index that is
    long, and the index is held meanwhile. A partial reading must not pass
    for a whole one."""
    monkeypatch.setattr(mcp_server, "AUDIT_MAX_POINTS", 3)
    for n in range(5):
        _code("alpha", f"f{n}.py", "x = 1")
    out = (await _audit()).structured_content
    assert out["complete"] is False and out["scanned"] == 3
    assert "repos" in out["note"] and "griot audit" in out["note"], "how to read the rest"


@pytest.mark.anyio
async def test_at_exactly_the_ceiling_everything_was_read(monkeypatch):
    monkeypatch.setattr(mcp_server, "AUDIT_MAX_POINTS", 3)
    for n in range(3):
        _code("alpha", f"f{n}.py", "x = 1")
    out = (await _audit()).structured_content
    assert out["complete"] is True and out["scanned"] == 3


@pytest.mark.anyio
async def test_more_places_than_are_shown_are_counted(monkeypatch):
    monkeypatch.setattr(mcp_server, "AUDIT_PLACES_SHOWN", 2)
    for n in range(5):
        _code("alpha", f"f{n}.yml", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert len(out["places"]) == 2 and out["places_omitted"] == 3 and out["total"] == 5
    assert out["by_repo"] == [{"repo": "alpha", "count": 5}], "the totals are of everything found"


class _Counting:
    """The index, counting what is read from it."""

    def __init__(self, real, pages=None):
        self.real, self.read, self.calls, self.pages = real, 0, 0, pages

    def scroll(self, request):
        self.calls += 1
        assert self.calls < 50, "the reading does not end"
        if self.pages is not None:
            return self.pages(request)
        points, offset = self.real.scroll(request)
        self.read += len(points)
        return points, offset

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_the_reading_really_stops_at_the_ceiling(monkeypatch):
    """Reporting `scanned=3` while reading the whole index would keep the
    number and lose what the ceiling is for."""
    for n in range(10):
        _code("alpha", f"f{n}.py", "x = 1")
    index = _Counting(common.get_client())
    monkeypatch.setattr(common, "get_client", lambda: index)
    monkeypatch.setattr(redaction, "AUDIT_PAGE", 2)
    found = redaction.audit_index(max_points=3)
    assert (found["scanned"], found["complete"]) == (3, False)
    assert index.read <= 4, "one point past the ceiling, to know there is more, and no further"


@pytest.mark.parametrize("ceiling,complete", [(3, False), (4, False), (5, False), (6, True), (7, True)])
def test_the_ceiling_inside_a_page_and_at_its_edge(monkeypatch, ceiling, complete):
    for n in range(6):
        _code("alpha", f"f{n}.py", "x = 1")
    monkeypatch.setattr(redaction, "AUDIT_PAGE", 2)
    found = redaction.audit_index(max_points=ceiling)
    assert (found["scanned"], found["complete"]) == (min(ceiling, 6), complete)


def test_the_ceiling_counts_only_what_the_filter_lets_through():
    for n in range(4):
        _code("alpha", f"f{n}.py", "x = 1")
        _code("beta", f"f{n}.py", "x = 1")
    found = redaction.audit_index(repos=["beta"], max_points=4)
    assert (found["scanned"], found["complete"]) == (4, True)
    assert redaction.audit_index(repos=["beta"], max_points=3)["complete"] is False


def test_a_page_with_nothing_in_it_ends_the_reading(monkeypatch):
    _code("alpha", "a.py", "x = 1")
    index = _Counting(common.get_client(), pages=lambda request: ([], "an offset that never ends"))
    monkeypatch.setattr(common, "get_client", lambda: index)
    assert redaction.audit_index()["scanned"] == 0 and index.calls == 1


@pytest.mark.anyio
async def test_two_rules_in_one_place_are_both_named_and_the_places_come_in_order():
    jwt = T("eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", ".", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
    _code("alpha", "z.yml", "token: " + TOKEN + "\nsession: " + jwt)
    _code("alpha", "a.yml", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert [place["where"] for place in out["places"]] == sorted(place["where"] for place in out["places"])
    assert out["places"][-1]["rules"] == ["github-token", "jwt"] and out["places"][-1]["count"] == 2
    assert jwt not in json.dumps(out)


@pytest.mark.anyio
async def test_the_note_says_which_profile_was_read_and_that_places_were_left_out(monkeypatch):
    monkeypatch.setattr(mcp_server, "AUDIT_PLACES_SHOWN", 1)
    _code("alpha", "a.yml", "token: " + TOKEN)
    _code("alpha", "b.yml", "token: " + TOKEN)
    note = (await _audit()).structured_content["note"]
    assert common.ACTIVE_PROFILE_NAME in note and "each profile has its own" in note
    assert "1 more place" in note


# --- what a stored name can do ----------------------------------------------------------------


@pytest.mark.anyio
async def test_a_stored_name_cannot_carry_a_credential_or_control_characters_out():
    _store_raw("r:code:x:0", "token: " + TOKEN, source_type="code", repo="r\x1b[31m‮",
               file_path="deploy/" + TOKEN + ".sh", chunk_index=0)
    result = await _audit()
    assert result.structured_content["places"], "the point is reported: the assertions below are about its names"
    text = json.dumps(result.structured_content) + str(result.content)
    assert TOKEN not in text and "\\u001b" not in text and "\x1b" not in text and "‮" not in text


@pytest.mark.parametrize("name", ["fix_" + TOKEN, "fix_" + TOKEN + "_old", "fix/" + TOKEN])
@pytest.mark.anyio
async def test_a_token_in_a_branch_name_does_not_come_out_whatever_it_is_glued_to(name):
    _store_raw("r:branch:x", "token: " + TOKEN, source_type="branch", repo="r", branch_name=name)
    result = await _audit()
    assert result.structured_content["places"]
    assert TOKEN not in json.dumps(result.structured_content) + str(result.content)


@pytest.mark.anyio
async def test_a_very_long_name_is_not_returned_whole():
    _store_raw("r:branch:long", "token: " + TOKEN, source_type="branch", repo="r", branch_name="b" * 50_000)
    place = (await _audit()).structured_content["places"][0]
    assert len(place["where"]) <= 300


@pytest.mark.anyio
async def test_a_point_whose_fields_have_another_type_is_still_reported():
    """One odd point used to stop the whole audit with a TypeError."""
    _store_raw("r:commit:odd", "token: " + TOKEN, source_type="commit", repo="r", commit_hash=12345678)
    _code("alpha", "config.yml", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert out["total"] == 2 and len(out["places"]) == 2


@pytest.mark.anyio
async def test_a_point_whose_text_is_not_text_is_read_past():
    client = common.get_client()
    client.update(qe.UpdateOperation.upsert_points([qe.Point(
        id=common.stable_id("r:code:odd:0"), vector={"dense": [0.1] * common.EMBED_DIM},
        payload={"source_type": "code", "repo": "r", "file_path": "odd.py", "content": 12345})]))
    client.flush()
    _code("alpha", "config.yml", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert (out["scanned"], out["total"]) == (2, 1)


@pytest.mark.anyio
async def test_a_point_without_the_usual_fields_does_not_break_it():
    _store_raw("odd:1", "token: " + TOKEN)
    out = (await _audit()).structured_content
    assert out["total"] == 1 and out["places"][0]["repo"] is None


# --- the surface ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_it_is_read_only_and_still_asked_about():
    """It changes nothing, and it reads every stored chunk while holding the
    index, to tell an agent where credentials are: a person says yes."""
    async with Client(mcp_server.mcp) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "griot_audit")
    assert tool.annotations.read_only_hint is True
    assert "griot_audit" not in mcp_server.tools_safe_to_preapprove()


def test_the_terminal_command_says_the_same(capsys):
    _code("alpha", "config.yml", "token: " + TOKEN)
    _code("alpha", "clean.py", "x = 1")
    assert redaction.main([]) == 1
    out = capsys.readouterr().out
    assert "alpha/config.yml" in out and "github-token" in out and TOKEN not in out and "clean.py" not in out
    found = redaction.audit_index()
    assert found["total"] == 1 and found["scanned"] == 2 and found["complete"] is True
