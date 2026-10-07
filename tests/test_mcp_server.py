"""Tests for the MCP server — mcp_server.py
must not contain new business logic: each tool is a thin function that calls
common.*/quality_check.* already tested in other files. Here we only test the
formatting/passthrough of each handler (monkeypatching the called functions) and,
in a single level-2 test, that the real MCP protocol (via an in-memory client)
sees the expected tools.

griot_ask is NOT a tool (closed decision — see sections 0 and 2.6); there
is no handler for it here. griot_index_repo IS a tool, but disabled by default
(GRIOT_MCP_ENABLE_INDEX) — conditional registration around the @mcp.tool(),
tested via module reload (see fixture `mcp_server_with_index_enabled`)."""

import importlib
import json
import pathlib
import subprocess
from datetime import datetime, timezone

import pytest

from mcp.client.client import Client
from mcp.server.elicitation import AcceptedElicitation
from mcp_types import ElicitResult

from griot import common, golden_set, harnesses, jobs, logdb, mcp_server, quality_check, repos


class FakeHit:
    """Same shape as the points that common.search returns (ScoredPoint) — same
    as the FakeHit in tests/test_cli.py."""

    def __init__(self, score, payload, id="00000000-0000-0000-0000-000000000001"):
        self.score = score
        self.payload = payload
        self.id = id


# --- griot_search ------------------------------------------------------


def test_griot_search_formats_results_with_source_label(monkeypatch):
    hits = [
        FakeHit(0.87, {"repo": "repo-x", "source_type": "commit", "commit_hash": "a1b2c3d4e5", "content": "fix bug"}),
    ]

    def fake_search(query, limit=5, group_by_document=False, **filters):
        assert query == "how does login work"
        assert limit == 8  # the MCP default: what agents actually ask for
        return hits

    monkeypatch.setattr(common, "search", fake_search)

    output = mcp_server.griot_search(query="how does login work")

    assert output["results"] == [
        {
            "source_label": "commit a1b2c3d4 — repo-x",
            "repo": "repo-x",
            "source_type": "commit",
            "metadata": {"commit_hash": "a1b2c3d4e5"},
            "content": "fix bug",
            "score": 0.87,
        }
    ]
    # security note (prompt injection) needs to be explicit in the output,
    # not just in the tool's description — see the design notes.
    assert "retrieved" in output["note"].lower() or "instruction" in output["note"].lower()


def test_griot_search_empty_collection_returns_empty_list_not_error(monkeypatch):
    """Day 1: empty collection -> results: [], never an exception."""
    monkeypatch.setattr(common, "search", lambda query, limit=5, group_by_document=False, **filters: [])

    output = mcp_server.griot_search(query="anything")

    assert output["results"] == []


def test_griot_search_clamps_limit_to_max(monkeypatch):
    """Cap on limit (section 2.1, [review]) — an absurd value shouldn't
    needlessly inflate the agent's context."""
    seen = {}

    def fake_search(query, limit=5, group_by_document=False, **filters):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(common, "search", fake_search)

    mcp_server.griot_search(query="x", limit=9999)

    assert seen["limit"] <= mcp_server.SEARCH_LIMIT_MAX


def test_griot_search_propagates_runtime_error(monkeypatch):
    """Spend circuit breaker (simulated gemini profile): RuntimeError needs to
    propagate without being swallowed — the conversion to isError is automatic via
    Client (confirmed empirically), there should be no try/except here."""

    def fake_search(query, limit=5, group_by_document=False, **filters):
        raise RuntimeError("Local circuit breaker: today's estimated spend already hit the ceiling")

    monkeypatch.setattr(common, "search", fake_search)

    with pytest.raises(RuntimeError, match="circuit breaker"):
        mcp_server.griot_search(query="x")


# --- griot_spend_status --------------------------------------------------


def test_griot_spend_status_reports_spend_and_ceilings(monkeypatch):
    monkeypatch.setattr(common, "get_spend_today", lambda: 1.2345)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 1.0)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setattr(common, "COLLECTION_NAME", "codebase__jina-code")

    output = mcp_server.griot_spend_status()

    assert output == {
        "spend_today_usd": 1.2345,
        "daily_ceiling_usd": 3.0,
        "velocity_ceiling_usd": 1.0,
        "embed_profile": "jina-code",
        "collection": "codebase__jina-code",
    }


# --- griot_index_status ---------------------------------------------------


# --- griot_repos_list -------------------------------------------------------
# [coverage gap, 2026-08-21] griot_index_repo only accepts a path already
# registered in repos.json, but nothing exposed WHICH paths those are — an
# agent could index a repo yet had no way to discover the ones available to
# it, leaving it to guess or ask the human. Read-only, so it carries none of
# the reasons auth/profiles-delete are deliberately kept off MCP.


def test_griot_repos_list_returns_registered_repos(monkeypatch):
    monkeypatch.setattr(repos, "repo_status", lambda: [
        {"name": "alpha", "path": "/repos/alpha", "exists": True, "is_git": True},
        {"name": "beta", "path": "/repos/beta", "exists": False, "is_git": False},
    ])

    result = mcp_server.griot_repos_list()

    assert result["count"] == 2
    assert [r["path"] for r in result["repos"]] == ["/repos/alpha", "/repos/beta"]


def test_griot_repos_list_reports_missing_and_non_git_entries(monkeypatch):
    """An agent needs to tell "registered and usable" from "registered but
    the directory is gone / isn't a git repo" — indexing the latter fails or
    silently indexes nothing, and the tool should let it see that first."""
    monkeypatch.setattr(repos, "repo_status", lambda: [
        {"name": "gone", "path": "/repos/gone", "exists": False, "is_git": False},
    ])

    entry = mcp_server.griot_repos_list()["repos"][0]

    assert entry["exists"] is False
    assert entry["is_git"] is False


def test_griot_repos_list_is_empty_when_nothing_registered(monkeypatch):
    monkeypatch.setattr(repos, "repo_status", lambda: [])

    result = mcp_server.griot_repos_list()

    assert result == {"repos": [], "count": 0}


def test_griot_repos_list_never_reads_the_vector_store(monkeypatch):
    """Same subprocess/handle isolation every other read-only tool keeps:
    listing repos must not open a Qdrant handle or load the embedding
    model, so it stays cheap and can never collide with an indexing run."""
    def _boom(*args, **kwargs):
        raise AssertionError("griot_repos_list must not touch the vector store")

    monkeypatch.setattr(common, "get_client", _boom)
    monkeypatch.setattr(common, "get_embed_model", _boom)
    monkeypatch.setattr(repos, "repo_status", lambda: [])

    mcp_server.griot_repos_list()


def test_griot_repos_list_docstring_discloses_the_env_roots_caveat():
    """[review finding] The docstring IS the tool description an MCP agent
    reads. Claiming griot_index_repo "only accepts a path registered here"
    is false: jobs.index_path_allowed() also accepts anything under
    GRIOT_MCP_INDEX_ROOTS, which never appears in this list. An agent
    trusting the claim could wrongly conclude a legitimate path is
    forbidden, or tell the user "this is all I may index" when it isn't."""
    assert "GRIOT_MCP_INDEX_ROOTS" in mcp_server.griot_repos_list.__doc__


def test_griot_repos_list_reports_a_corrupted_repos_file_as_an_error():
    """[review finding] repos._load() does a bare json.loads(), so a
    truncated/corrupted repos.json raised JSONDecodeError straight at the
    MCP client. Two things are wrong with that: the raw decoder traceback
    leaks internals, and the alternative of swallowing it would be worse —
    an empty list is indistinguishable from "no repos registered", i.e. a
    lie. RuntimeError is this server's established way to surface a real
    failure (it converts to isError automatically); the message must name
    the file so the user can go fix it."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text('["/repos/alpha", "/repos/tru')

    with pytest.raises(RuntimeError, match="repos.json"):
        mcp_server.griot_repos_list()


def test_griot_repos_list_still_works_with_a_valid_file_on_disk():
    """Guards the test above from passing for the wrong reason — it must be
    the CORRUPTION that raises, not the tool being broken for real files."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    common.REPOS_JSON_PATH.write_text('["/repos/alpha"]')

    result = mcp_server.griot_repos_list()

    assert result["count"] == 1
    assert result["repos"][0]["path"] == "/repos/alpha"


def test_a_corrupted_repos_file_is_never_silently_overwritten():
    """The tempting fix — swallowing the decode error inside repos._load()
    — would make add_repo()/remove_repo() read [] and then WRITE that back,
    destroying whatever the user still had in the file. Registering must
    fail loudly instead."""
    common.secure_mkdir(common.REPOS_JSON_PATH.parent)
    corrupted = '["/repos/alpha", "/repos/tru'
    common.REPOS_JSON_PATH.write_text(corrupted)

    with pytest.raises(Exception):
        repos.add_repo("/tmp")

    assert common.REPOS_JSON_PATH.read_text() == corrupted


# --- management over MCP: confirmation policy -------------------------------
# Three layers, because no single one is sufficient:
#   1. destructiveHint — honest metadata; clients that prompt will prompt,
#      but it is a HINT (and Claude Code's auto mode prompts for nothing).
#   2. a real human answer, collected through the SDK's resolver mechanism
#      (`Resolve`/`Elicit`), but only where the client can ask; a client that
#      cannot must never read as a yes (see tests/test_confirmation.py).
#   3. an explicit confirm argument — works in EVERY client and makes the
#      caller act twice, having seen what the first call warned about.
# Secrets are outside this ladder entirely: confirmation does not make a
# chat a safe channel for a token.


class _FakeCaps:
    def __init__(self, elicitation=None):
        self.elicitation = elicitation


class _FakeCtx:
    """Stands in for the MCP Context. The human's answer no longer travels
    through it: the framework collects it in the tool's Resolve parameter and
    hands it over as `answer=`, so tests supply `answer=_accepted()` (or omit
    it for a client that could not ask). tests/test_confirmation.py covers the
    same flow through a real client on both protocol versions."""

    def __init__(self, elicitation=None):
        self.client_capabilities = _FakeCaps(elicitation)


def _accepted():
    return AcceptedElicitation(data=mcp_server._Ask())


@pytest.mark.anyio
async def test_confirmation_falls_back_to_the_confirm_argument():
    """The layer that makes management usable in clients WITHOUT
    elicitation: instead of refusing outright, the caller is told exactly
    what will happen and must call again with confirm=true."""
    ctx = _FakeCtx(elicitation=None)

    ok, refusal = await mcp_server._confirmed(ctx, "Delete profile 'x'?", confirm=False,
                                              cli_hint="griot profiles delete x")

    assert ok is False
    assert "confirm=true" in refusal
    assert "Delete profile 'x'?" in refusal
    assert "griot profiles delete x" in refusal  # the CLI equivalent, always offered


@pytest.mark.anyio
async def test_confirm_argument_authorizes_without_elicitation():
    ctx = _FakeCtx(elicitation=None)

    ok, refusal = await mcp_server._confirmed(ctx, "Delete?", confirm=True, cli_hint="griot x")

    assert ok is True and refusal is None


# --- griot_profiles_list -----------------------------------------------------
# [asymmetry I introduced] griot_profiles_delete shipped without a way to
# LIST profiles, so an agent could destroy one but had to guess its name.
# Exactly the gap griot_repos_list closed for indexing, reintroduced.


def test_profiles_list_names_every_profile():
    result = mcp_server.griot_profiles_list()

    names = {p["profile"] for p in result["profiles"]}
    assert names == set(common.EMBED_PROFILES)
    assert result["active"] == common.ACTIVE_PROFILE_NAME


def test_profiles_list_marks_which_one_is_active():
    active = [p for p in mcp_server.griot_profiles_list()["profiles"] if p["is_active"]]

    assert len(active) == 1
    assert active[0]["profile"] == common.ACTIVE_PROFILE_NAME


def test_profiles_list_says_whether_a_paid_profile_has_its_credential():
    """An agent choosing a profile needs to know a paid one is unusable
    without its key — otherwise it suggests a switch that fails later."""
    entry = next(p for p in mcp_server.griot_profiles_list()["profiles"]
                 if p["profile"] == "openai-small")

    assert entry["paid"] is True
    assert "credential_configured" in entry


def test_profiles_list_marks_gemini_as_paid():
    """Caught by a protocol-level smoke test, not by the unit tests above.

    'gemini' predates the GRIOT_ prefix and carries NO api_key_env field —
    common.credential_env_vars() special-cases it onto GEMINI_TOKEN. Reading
    api_key_env off the profile dict therefore reported griot's flagship PAID
    profile as free and its credential as configured, which is the worst
    possible direction for the error to point: an agent reads it and suggests
    switching to a profile that bills per query and may not even be usable."""
    entry = next(p for p in mcp_server.griot_profiles_list()["profiles"]
                 if p["profile"] == "gemini")

    assert entry["paid"] is True


def test_profiles_list_agrees_with_the_cli_on_which_profiles_are_paid():
    """Two independent criteria for the same fact, and they must not drift.

    `griot profiles list` classifies a profile as paid by backend == "direct"
    (it talks to a remote API); this tool classifies by having a credential to
    pay with. They agree across every profile today, but nothing enforced it —
    adding a remote profile that needs no key, or a local one that does, would
    make the CLI and the MCP surface disagree about whether a switch costs
    money, silently. Cheaper to fail here than to be told by a bill."""
    by_mcp = {p["profile"] for p in mcp_server.griot_profiles_list()["profiles"] if p["paid"]}
    by_cli = {n for n, p in common.EMBED_PROFILES.items() if p.get("backend") == "direct"}

    assert by_mcp == by_cli


def test_profiles_list_reads_the_active_profile_live(monkeypatch):
    """common.ACTIVE_PROFILE_NAME is resolved ONCE at import time, and an MCP
    server outlives a profile switch — the same staleness the security review
    found in griot_profiles_delete. Reported against the constant, is_active
    would keep naming the profile that was active when the server started."""
    other = next(n for n in common.EMBED_PROFILES if n != common.ACTIVE_PROFILE_NAME)
    monkeypatch.setenv("GRIOT_EMBED_PROFILE", other)

    result = mcp_server.griot_profiles_list()

    assert result["active"] == other
    assert [p["profile"] for p in result["profiles"] if p["is_active"]] == [other]


def test_profiles_list_never_reveals_a_credential():
    """Same rule as griot_auth_guidance: whether a key exists is useful,
    the value is never exposed — not even masked."""
    blob = json.dumps(mcp_server.griot_profiles_list())

    assert "sk-" not in blob
    for entry in mcp_server.griot_profiles_list()["profiles"]:
        assert set(entry) == {"profile", "is_active", "paid", "credential_configured", "collection"}


def test_profiles_list_never_reads_the_vector_store(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("griot_profiles_list must not touch the vector store")

    monkeypatch.setattr(common, "get_client", _boom)
    monkeypatch.setattr(common, "get_embed_model", _boom)

    mcp_server.griot_profiles_list()


# --- management tools --------------------------------------------------------


@pytest.mark.anyio
async def test_repos_add_refuses_the_agent_passable_confirm(monkeypatch, tmp_path):
    """[security] Registering a path WIDENS the indexing allowlist, and
    jobs.index_path_allowed() treats repos.json as authoritative. So the
    chain repos_add(any dir) -> index_repo(that dir) sends its contents to
    a paid embedding API — the exact exfiltration index_path_allowed()'s
    own docstring exists to prevent.

    `confirm=true` is passed by the AGENT, so it protects against mistakes,
    never against a compromised one. Confirmation is not a control for an
    operation that widens a security boundary: that requires a human, or
    the terminal."""
    called = []
    monkeypatch.setattr(repos, "add_repo", lambda p: called.append(p) or p)

    result = await mcp_server.griot_repos_add(str(tmp_path), confirm=True, ctx=_FakeCtx())

    assert called == [], "confirm=true must NOT be enough to widen the allowlist"
    assert result["changed"] is False
    assert "griot repos add" in result["message"]


@pytest.mark.anyio
async def test_repos_add_accepts_a_real_human_confirmation(monkeypatch, tmp_path):
    """Where the client can actually ask a person, a person can authorize
    it — that is a real boundary, unlike an argument the agent supplies."""
    monkeypatch.setattr(repos, "add_repo", lambda p: p)
    ctx = _FakeCtx()

    result = await mcp_server.griot_repos_add(str(tmp_path), ctx=ctx, answer=_accepted())

    assert result["changed"] is True


@pytest.mark.anyio
async def test_removing_a_repo_still_allows_the_confirm_fallback(monkeypatch, tmp_path):
    """Tier matters: removing NARROWS the allowlist. A compromised agent
    abusing it causes annoyance, not exfiltration — so the fallback that
    keeps MCP usable everywhere is appropriate here."""
    monkeypatch.setattr(repos, "remove_repo", lambda p: p)

    result = await mcp_server.griot_repos_remove(str(tmp_path), confirm=True, ctx=_FakeCtx())

    assert result["changed"] is True


@pytest.mark.anyio
async def test_repos_add_does_nothing_until_confirmed(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(repos, "add_repo", lambda p: called.append(p) or p)

    result = await mcp_server.griot_repos_add(str(tmp_path), ctx=_FakeCtx())

    assert called == []
    assert result["changed"] is False
    # No confirm= escape hatch is offered here, unlike every other
    # management tool — see the tool's own [security] note.
    assert "confirm=true" not in result["message"]
    assert "griot repos add" in result["message"]


@pytest.mark.anyio
async def test_repos_add_registers_once_confirmed(monkeypatch, tmp_path):
    monkeypatch.setattr(repos, "add_repo", lambda p: p)
    ctx = _FakeCtx()

    result = await mcp_server.griot_repos_add(str(tmp_path), ctx=ctx, answer=_accepted())

    assert result["changed"] is True
    assert str(tmp_path) in result["message"]


@pytest.mark.anyio
async def test_profiles_delete_names_what_is_lost_before_doing_it(monkeypatch):
    """A destructive operation must state the consequence in the question
    itself — the caller (human or agent) decides on what the message says,
    not on the tool's name."""
    result = await mcp_server.griot_profiles_delete("bge-small", ctx=_FakeCtx())

    assert result["changed"] is False
    assert "bge-small" in result["message"]
    assert "cannot be undone" in result["message"].lower()
    assert "vectors" in result["message"].lower()
    assert "griot profiles delete -- bge-small" in result["message"]


@pytest.mark.anyio
async def test_profiles_delete_refuses_the_agent_passable_confirm(monkeypatch):
    """[security review] Irreversible data loss plus the cost of
    re-embedding, authorized by an argument the AGENT supplies. The user's
    rule is that critical operations are confirmed by the USER — and an
    agent passing confirm=true is not the user. Same reasoning that removed
    the fallback from repos_add; the difference there was confidentiality,
    here it is destruction, and both outrank "keep MCP usable everywhere"."""
    deleted = []
    monkeypatch.setattr(mcp_server.cli, "delete_profile",
                        lambda name, **kw: deleted.append(name) or "deleted")

    result = await mcp_server.griot_profiles_delete("bge-small", confirm=True, ctx=_FakeCtx())

    assert deleted == []
    assert result["changed"] is False
    assert "confirm=true" not in result["message"]
    assert "griot profiles delete -- bge-small" in result["message"]


@pytest.mark.anyio
async def test_profiles_delete_resolves_the_active_profile_live(monkeypatch):
    """[security review] common.ACTIVE_PROFILE_NAME is resolved ONCE at
    import. The MCP server is long-lived: a profile switched during the
    session leaves that constant stale, so the guard could refuse deleting
    a profile that is no longer active — or, worse, fail to refuse the one
    that now IS, corrupting a collection something is still searching.
    cli.delete_profile() already takes an override for exactly this; the
    since-removed caller used it and this path never did.

    Real profile names on purpose: _active_profile() refuses to report one
    that isn't in EMBED_PROFILES, so a placeholder would exercise the
    fallback instead of the switch this test is about."""
    seen = {}
    monkeypatch.setattr(mcp_server.cli, "delete_profile",
                        lambda name, **kw: seen.update(kw) or "deleted")
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setenv("GRIOT_EMBED_PROFILE", "bge-large-en")
    ctx = _FakeCtx()

    await mcp_server.griot_profiles_delete("bge-small", ctx=ctx, answer=_accepted())

    assert seen["active_profile_name"] == "bge-large-en"


@pytest.mark.anyio
async def test_profiles_delete_falls_back_when_the_env_names_no_real_profile(monkeypatch):
    """A GRIOT_EMBED_PROFILE that names nothing must not become a licence to
    delete the profile actually in use: common.py validates the variable at
    import, so a bad value here means it changed afterwards, and treating it
    as authoritative would make every real profile look "not active"."""
    seen = {}
    monkeypatch.setattr(mcp_server.cli, "delete_profile",
                        lambda name, **kw: seen.update(kw) or "deleted")
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setenv("GRIOT_EMBED_PROFILE", "not-a-profile")
    ctx = _FakeCtx()

    await mcp_server.griot_profiles_delete("bge-small", ctx=ctx, answer=_accepted())

    assert seen["active_profile_name"] == "jina-code"


@pytest.mark.anyio
async def test_profiles_delete_runs_when_confirmed(monkeypatch):
    monkeypatch.setattr(mcp_server.cli, "delete_profile", lambda name, **kw: f"deleted {name}")
    ctx = _FakeCtx()

    result = await mcp_server.griot_profiles_delete("bge-small", ctx=ctx, answer=_accepted())

    assert result["changed"] is True


@pytest.mark.anyio
async def test_a_failed_management_call_reports_instead_of_raising(monkeypatch, tmp_path):
    """add_repo raises ValueError for an invalid or duplicate path — the
    agent needs the reason, not a stack trace."""
    monkeypatch.setattr(repos, "add_repo",
                        lambda p: (_ for _ in ()).throw(ValueError("already registered")))

    ctx = _FakeCtx()
    result = await mcp_server.griot_repos_add(str(tmp_path), ctx=ctx, answer=_accepted())

    assert result["changed"] is False
    assert "already registered" in result["message"]


# --- griot_assist_install -----------------------------------------------------
# Installs griot's bundled Claude Code/opencode skills/agents into a
# harness's config dir. Cheap validation (scope/harness) happens BEFORE
# confirmation is even asked for — an invalid argument must never spend a
# human confirmation. human_required=True, same as repos_add/profiles_delete:
# it writes files a FUTURE agent session will load and follow automatically.


@pytest.fixture(autouse=True)
def _assist_install_sees_a_fixed_machine(monkeypatch):
    """Which harnesses are installed, and what their directories hold, belong
    to whoever runs the suite. Both are looked at BEFORE the question now, so
    they are pinned here; tests/test_security_hardening.py exercises the real
    destination check in a directory of its own."""
    monkeypatch.setattr(harnesses, "detect_harnesses",
                        lambda: [h for h in harnesses.HARNESSES if h.id == "claude-code"])
    monkeypatch.setattr(harnesses, "install_refusal", lambda *a, **k: None)


def _fail_if_confirmed_is_called(*args, **kwargs):
    raise AssertionError("_confirmed must not be called — validation should have short-circuited first")


@pytest.mark.anyio
async def test_assist_install_rejects_invalid_scope_without_asking(monkeypatch):
    monkeypatch.setattr(mcp_server, "_confirmed", _fail_if_confirmed_is_called)

    result = await mcp_server.griot_assist_install(scope="nonsense", ctx=_FakeCtx())

    assert result["changed"] is False
    assert result["results"] == []
    assert "scope" in result["message"]


@pytest.mark.anyio
async def test_assist_install_rejects_invalid_harness_without_asking(monkeypatch):
    monkeypatch.setattr(mcp_server, "_confirmed", _fail_if_confirmed_is_called)

    result = await mcp_server.griot_assist_install(harness="nonsense", ctx=_FakeCtx())

    assert result["changed"] is False
    assert result["results"] == []
    assert "harness" in result["message"]


@pytest.mark.anyio
async def test_assist_install_does_nothing_until_confirmed(monkeypatch):
    called = []
    monkeypatch.setattr(harnesses, "install_many", lambda targets, scope, **kw: called.append(1) or [])

    result = await mcp_server.griot_assist_install(ctx=_FakeCtx())

    assert called == []
    assert result["changed"] is False
    assert result["results"] == []
    assert "griot assist install" in result["message"]


@pytest.mark.anyio
async def test_assist_install_refuses_the_agent_passable_confirm(monkeypatch):
    """[security] Same class of trust boundary as repos_add: this writes
    files a future AI coding session in that directory will load and follow
    automatically, unreviewed. confirm=true is an argument the AGENT
    supplies, so it must not be enough on its own."""
    called = []
    monkeypatch.setattr(harnesses, "install_many", lambda targets, scope, **kw: called.append(1) or [])

    result = await mcp_server.griot_assist_install(confirm=True, ctx=_FakeCtx())

    assert called == [], "confirm=true must NOT be enough to install unreviewed instruction files"
    assert result["changed"] is False
    assert "griot assist install" in result["message"]


@pytest.mark.anyio
async def test_assist_install_runs_for_explicit_harness_once_confirmed(monkeypatch):
    recorded = {}

    def fake_install_many(targets, scope, **kwargs):
        recorded["targets"] = [h.id for h in targets]
        recorded["scope"] = scope
        return [{
            "harness": "opencode", "scope": scope,
            "skills_target": "/fake/.opencode/skills", "agents_target": "/fake/.opencode/agents",
            "created": ["skills/griot-onboarding/SKILL.md"], "updated": [], "unchanged": ["agents/griot-setup-assistant.md"],
        }]

    monkeypatch.setattr(harnesses, "install_many", fake_install_many)
    ctx = _FakeCtx()

    result = await mcp_server.griot_assist_install(harness="opencode", scope="global", ctx=ctx, answer=_accepted())

    assert recorded["targets"] == ["opencode"]
    assert recorded["scope"] == "global"
    assert result["changed"] is True
    assert result["results"] == [{
        "harness": "opencode", "scope": "global",
        "skills_target": "/fake/.opencode/skills", "agents_target": "/fake/.opencode/agents",
        "created": ["skills/griot-onboarding/SKILL.md"], "updated": [], "unchanged_count": 1,
    }]


@pytest.mark.anyio
async def test_assist_install_all_detects_and_installs_present_harnesses(monkeypatch):
    fake_claude = next(h for h in harnesses.HARNESSES if h.id == "claude-code")
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda: [fake_claude])
    recorded = {}

    def fake_install_many(targets, scope, **kwargs):
        recorded["targets"] = [h.id for h in targets]
        return [{
            "harness": "claude-code", "scope": scope,
            "skills_target": "/fake/.claude/skills", "agents_target": "/fake/.claude/agents",
            "created": [], "updated": [], "unchanged": [],
        }]

    monkeypatch.setattr(harnesses, "install_many", fake_install_many)
    ctx = _FakeCtx()

    result = await mcp_server.griot_assist_install(ctx=ctx, answer=_accepted())

    assert recorded["targets"] == ["claude-code"]
    assert result["changed"] is False  # nothing created/updated
    assert result["results"][0]["harness"] == "claude-code"


@pytest.mark.anyio
async def test_assist_install_all_with_nothing_detected_reports_clearly(monkeypatch):
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda: [])
    called = []
    monkeypatch.setattr(harnesses, "install_many", lambda targets, scope, **kw: called.append(1) or [])
    ctx = _FakeCtx()

    result = await mcp_server.griot_assist_install(ctx=ctx, answer=_accepted())

    assert called == []
    assert result["changed"] is False
    assert result["results"] == []
    assert "claude-code" in result["message"]
    assert "opencode" in result["message"]


# --- golden set --------------------------------------------------------------
# The curated cases that define what "the index is working" means. Read is
# free; add/remove change curation but widen no boundary and destroy no
# indexed data — so the confirm fallback is appropriate, unlike repos_add.


def test_golden_set_list_returns_the_curated_cases(monkeypatch):
    monkeypatch.setattr(golden_set, "list_cases", lambda: [
        {"query": "how does auth work", "limit": 5, "must_include": [{"source_type": "code"}]},
    ])

    result = mcp_server.griot_golden_set_list()

    assert result["count"] == 1
    assert result["cases"][0]["query"] == "how does auth work"


def test_golden_set_list_is_empty_before_anything_is_curated(monkeypatch):
    monkeypatch.setattr(golden_set, "list_cases", lambda: [])
    assert mcp_server.griot_golden_set_list() == {
        "note": mcp_server.GOLDEN_SET_NOTE, "cases": [], "count": 0}


@pytest.mark.anyio
async def test_golden_set_add_waits_for_confirmation(monkeypatch):
    called = []
    monkeypatch.setattr(golden_set, "add_case", lambda **kw: called.append(kw) or kw)

    result = await mcp_server.griot_golden_set_add(
        "how does auth work", [{"source_type": "code"}], ctx=_FakeCtx())

    assert called == []
    assert result["changed"] is False
    assert "confirm=true" in result["message"]  # fallback IS offered here


@pytest.mark.anyio
async def test_golden_set_add_curates_once_confirmed(monkeypatch):
    monkeypatch.setattr(golden_set, "add_case",
                        lambda **kw: {"query": kw["query"], "limit": kw["limit"],
                                      "must_include": kw["must_include"]})

    result = await mcp_server.griot_golden_set_add(
        "q", [{"source_type": "code"}], confirm=True, ctx=_FakeCtx())

    assert result["changed"] is True


@pytest.mark.anyio
async def test_golden_set_add_caps_the_persisted_limit(monkeypatch):
    """[review finding] griot_search clamps `limit` to SEARCH_LIMIT_MAX; here
    the value is written to disk and spent LATER, by a `griot quality-check`
    the agent never ran. common.search() passes limit straight through to the
    vector store, so a curated case carrying limit=1e9 degrades a CLI command
    on a surface this tool does not own. Durability is exactly why the cap
    matters more here, not less."""
    captured = {}
    monkeypatch.setattr(golden_set, "add_case",
                        lambda **kw: captured.update(kw) or {"query": kw["query"],
                                                             "must_include": kw["must_include"]})

    await mcp_server.griot_golden_set_add(
        "q", [{"source_type": "code"}], limit=1_000_000_000, confirm=True, ctx=_FakeCtx())

    assert captured["limit"] == mcp_server.SEARCH_LIMIT_MAX


@pytest.mark.anyio
async def test_golden_set_add_rejects_a_limit_below_one(monkeypatch):
    """A curated case with limit=0 asks the vector store for nothing and then
    reports every must_include as missing — a permanent false failure."""
    monkeypatch.setattr(golden_set, "add_case",
                        lambda **kw: pytest.fail("must not persist an unusable limit"))

    result = await mcp_server.griot_golden_set_add(
        "q", [{"source_type": "code"}], limit=0, confirm=True, ctx=_FakeCtx())

    assert result["changed"] is False
    assert "limit" in result["message"]


@pytest.mark.anyio
async def test_golden_set_add_does_not_present_a_corrupt_file_as_a_bad_argument(monkeypatch):
    """[review finding] json.JSONDecodeError subclasses ValueError, so a
    corrupted quality_golden_set.json fell into the except clause meant for
    argument validation and came back as changed=False with a parser message.
    An agent reads that as "my query was wrong" and retries forever, while the
    real problem is a file only a human can repair."""
    def _corrupt(**kw):
        raise json.JSONDecodeError("Expecting property name", "{bad", 1)

    monkeypatch.setattr(golden_set, "add_case", _corrupt)

    with pytest.raises(RuntimeError, match="quality_golden_set.json"):
        await mcp_server.griot_golden_set_add(
            "q", [{"source_type": "code"}], confirm=True, ctx=_FakeCtx())


def test_golden_set_list_names_the_file_when_it_is_corrupt(monkeypatch):
    """Same treatment griot_repos_list already got: a parse error naming no
    file leaves the reader with nothing to act on."""
    def _corrupt():
        raise json.JSONDecodeError("Expecting property name", "{bad", 1)

    monkeypatch.setattr(golden_set, "list_cases", _corrupt)

    with pytest.raises(RuntimeError, match="quality_golden_set.json"):
        mcp_server.griot_golden_set_list()


@pytest.mark.anyio
async def test_golden_set_remove_removes_once_confirmed(monkeypatch):
    """[review finding] the only state-changing tool whose SUCCESS path had no
    test — the refusal was covered, the effect was not."""
    removed = []
    monkeypatch.setattr(golden_set, "remove_case",
                        lambda index: removed.append(index) or {"query": "an old case"})

    result = await mcp_server.griot_golden_set_remove(2, confirm=True, ctx=_FakeCtx())

    assert removed == [2]
    assert result["changed"] is True
    assert "an old case" in result["message"]


@pytest.mark.anyio
async def test_golden_set_add_reports_a_validation_error(monkeypatch):
    monkeypatch.setattr(golden_set, "add_case",
                        lambda **kw: (_ for _ in ()).throw(ValueError("query cannot be empty")))

    result = await mcp_server.griot_golden_set_add("", [], confirm=True, ctx=_FakeCtx())

    assert result["changed"] is False
    assert "query cannot be empty" in result["message"]


@pytest.mark.anyio
async def test_golden_set_remove_waits_for_confirmation(monkeypatch):
    called = []
    monkeypatch.setattr(golden_set, "remove_case", lambda i: called.append(i) or {"query": "q"})

    result = await mcp_server.griot_golden_set_remove(1, ctx=_FakeCtx())

    assert called == []
    assert result["changed"] is False


# --- indexing: confirmed, but no human required ------------------------------


@pytest.mark.anyio
async def test_index_repo_waits_for_confirmation(mcp_server_with_index_enabled, monkeypatch):
    """[user-requested] Indexing is recurrent by nature — "I just merged a
    big PR, reindex" — so forcing it to a terminal would be friction with
    no security gain, unlike secrets. It spends money, so it is confirmed;
    it widens NO boundary (the path was already authorized), so the
    confirm fallback stands and no human is required."""
    srv = mcp_server_with_index_enabled
    started = []
    # The allowlist/git checks run first now (see the doomed-path test) — this
    # one is about the confirmation layer, so let the path through.
    monkeypatch.setattr(srv.jobs, "index_job_refusal", lambda p, **kw: None)
    monkeypatch.setattr(srv.jobs, "start_index_job",
                        lambda p, s=None, **kw: started.append(p) or {"started": True})

    result = await srv.griot_index_repo("/repos/alpha", ctx=_FakeCtx())

    assert started == []
    assert result["started"] is False
    assert "confirm=true" in result["reason"]


@pytest.mark.anyio
async def test_index_repo_refuses_a_doomed_path_before_asking_anyone(
        mcp_server_with_index_enabled, monkeypatch):
    """[review finding] Confirmation now runs before start_index_job's checks,
    so on a client with elicitation a person would be asked "Index '/etc'?
    This costs money", answer yes, and only then be told the path was never
    allowed. A prompt that cannot lead anywhere is how click-through is
    trained. Cheap validation first, then the question."""
    srv = mcp_server_with_index_enabled
    asked = []
    monkeypatch.setattr(srv, "_confirmed",
                        lambda *a, **kw: asked.append(a) or (_ for _ in ()).throw(
                            AssertionError("must not ask about a path that cannot be indexed")))

    result = await srv.griot_index_repo("/definitely/not/allowed", ctx=_FakeCtx())

    assert asked == []
    assert result["started"] is False
    assert "allowed repo list" in result["reason"]


@pytest.mark.anyio
async def test_index_repo_runs_once_confirmed(mcp_server_with_index_enabled, monkeypatch):
    srv = mcp_server_with_index_enabled
    monkeypatch.setattr(srv.jobs, "index_job_refusal", lambda p, **kw: None)
    monkeypatch.setattr(srv.jobs, "start_index_job",
                        lambda p, s=None, **kw: {"started": True, "pid": 42, "path": p,
                                           "sources": s, "reason": None})

    result = await srv.griot_index_repo("/repos/alpha", confirm=True, ctx=_FakeCtx())

    assert result["started"] is True
    assert result["pid"] == 42


# --- secrets: never over MCP -------------------------------------------------


def test_auth_tool_never_accepts_a_secret():
    """The tool that exists for credentials takes NO value parameter. A
    token typed into a chat reaches the model provider, the transcript on
    disk, and later context windows — confirmation does not fix a channel."""
    import inspect
    params = inspect.signature(mcp_server.griot_auth_guidance).parameters
    assert not any(p in params for p in ("key", "token", "secret", "value"))


def test_auth_tool_returns_the_cli_commands():
    result = mcp_server.griot_auth_guidance()

    assert "griot auth set" in result["how_to_set"]
    assert result["providers"]  # names the agent can suggest
    assert "never" in result["why_not_here"].lower()


def test_auth_tool_reports_status_without_revealing_anything(monkeypatch):
    """Which providers are configured is useful and not secret; the values
    are neither exposed nor masked-and-exposed — they are simply absent."""
    result = mcp_server.griot_auth_guidance()

    blob = json.dumps(result)
    assert "sk-" not in blob
    for entry in result["providers"]:
        assert set(entry) == {"provider", "configured"}


# --- tool-call recording ----------------------------------------------------
# griot exposes 7 tools and 1 prompt, none validated against real agent use.
# Recording which ones actually get called turns "do these earn their place?"
# into something the first real-agent validation can answer with data.


def test_every_read_only_tool_records_its_call(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda *a, **kw: {
        "points_count": 0, "collection": "c", "embed_profile": "p", "running": False,
        "pid": None, "path": None, "last_indexed": None, "spend_ceiling_exceeded": False})
    monkeypatch.setattr(repos, "repo_status", lambda: [])
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **filters: [])

    mcp_server.griot_search("q")
    mcp_server.griot_spend_status()
    mcp_server.griot_repos_list()
    mcp_server.griot_index_status()
    mcp_server.griot_stats()

    called = [c["tool"] for c in logdb.read_tool_calls(common.LOG_DIR, days=1)]
    assert called == ["griot_search", "griot_spend_status", "griot_repos_list",
                      "griot_index_status", "griot_stats"]
    assert all(c["ok"] for c in logdb.read_tool_calls(common.LOG_DIR, days=1))


def test_a_failing_tool_is_recorded_as_failed_and_still_raises(monkeypatch):
    """A tool called constantly but always failing must not look healthy in
    the counts — and instrumenting it must not swallow the error."""
    monkeypatch.setattr(repos, "repo_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        mcp_server.griot_repos_list()

    call = logdb.read_tool_calls(common.LOG_DIR, days=1)[0]
    assert call["tool"] == "griot_repos_list"
    assert call["ok"] is False
    assert "boom" in call["error"]


def test_tool_call_recording_never_breaks_the_tool(monkeypatch):
    monkeypatch.setattr(repos, "repo_status", lambda: [])
    monkeypatch.setattr(logdb, "write_tool_call",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    assert mcp_server.griot_repos_list() == {"repos": [], "count": 0}


def test_tool_call_recording_keeps_the_mcp_signature_intact():
    """The SDK builds each tool's input schema from its signature — an
    instrumentation wrapper that hid the real parameters would silently
    break every agent's ability to call the tool correctly."""
    import inspect
    params = inspect.signature(mcp_server.griot_search).parameters
    assert "query" in params and "limit" in params


# --- griot_search instrumentation -------------------------------------------
# [real gap, found before the first real-agent validation] log_query() was called
# only by ask.py — the CLI. griot_search, the tool an AGENT actually uses,
# recorded nothing, so `griot stats` would report "Queries: none in the
# period" no matter how much an agent searched. Worse than merely missing:
# the query's embedding DOES go through record_spend(), so the report would
# show money spent against zero queries, which reads like a broken circuit
# breaker rather than missing instrumentation. Logs not captured during a
# validation run are not recoverable afterward.


class _FakeHit:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


def _hits(monkeypatch, *scores):
    hits = [_FakeHit(s, {"repo": "r", "source_type": "code", "file_path": "a.py", "content": "x"})
            for s in scores]
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **filters: hits)
    return hits


def test_griot_search_records_the_query(monkeypatch):
    _hits(monkeypatch, 0.9, 0.7)

    mcp_server.griot_search("how does auth work", limit=5)

    queries = logdb.read_since(common.LOG_DIR, "queries", days=1)
    assert len(queries) == 1
    assert queries[0]["question"] == "how does auth work"
    assert queries[0]["num_sources"] == 2
    assert queries[0]["limit"] == 5


def test_griot_search_marks_the_call_as_coming_from_mcp(monkeypatch):
    """The whole point of the validation is telling agent traffic apart from
    the terminal — without this the two are indistinguishable in the log."""
    _hits(monkeypatch, 0.9)

    mcp_server.griot_search("q")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["via"] == "mcp"


def test_cli_ask_is_marked_as_cli(monkeypatch):
    from griot import ask
    monkeypatch.setattr(ask, "ask", lambda q, model=None, limit=5, mode="vector", **filters: ("answer", []))
    monkeypatch.setattr(common, "get_spend_today", lambda: 0.0)

    ask.main(["some question"])

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["via"] == "cli"


def test_griot_search_records_the_top_score(monkeypatch):
    """The most direct "did retrieval actually find anything relevant?"
    signal — a run of searches whose best score is low says the index is
    not answering, which no count of queries would reveal."""
    _hits(monkeypatch, 0.91, 0.42)

    mcp_server.griot_search("q")

    assert logdb.read_since(common.LOG_DIR, "queries", days=1)[0]["top_score"] == 0.91


def test_griot_search_with_no_results_records_a_null_top_score(monkeypatch):
    monkeypatch.setattr(common, "search", lambda q, limit, group_by_document=False, **filters: [])

    mcp_server.griot_search("nothing matches this")

    record = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]
    assert record["num_sources"] == 0
    assert record["top_score"] is None


def test_griot_search_omits_the_question_when_logging_is_disabled(monkeypatch):
    """GRIOT_LOG_QUESTIONS=false must hold for the MCP path exactly as it
    does for the CLI — questions about work repos are frequently sensitive
    and the setting is the user's answer to that."""
    monkeypatch.setenv("GRIOT_LOG_QUESTIONS", "false")
    _hits(monkeypatch, 0.9)

    mcp_server.griot_search("a sensitive question")

    record = logdb.read_since(common.LOG_DIR, "queries", days=1)[0]
    assert "a sensitive question" not in record["question"]
    assert record["num_sources"] == 1  # metrics still recorded


def test_griot_search_still_returns_results_if_logging_fails(monkeypatch):
    """Instrumentation must never break the thing it observes: a failure to
    record is not a reason to fail the agent's search."""
    _hits(monkeypatch, 0.9)
    monkeypatch.setattr(common, "log_query", lambda **kw: (_ for _ in ()).throw(OSError("disk full")))

    result = mcp_server.griot_search("q")

    assert len(result["results"]) == 1


# --- griot_stats ------------------------------------------------------------
# The remaining read-only gap: griot_spend_status and griot_index_status
# each expose a slice, but nothing gave an agent the full report — queries,
# latency, source breakdown, reuse, quality trend.


def test_griot_stats_returns_the_full_report(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda: {"points_count": 7, "embed_profile": "jina-code"})
    common.log_run_summary(script="index_code.py", indexed=1, skipped=9, failed=0, spend_today_usd=0.02)

    result = mcp_server.griot_stats()

    assert result["points_count"] == 7
    assert result["total_indexed"] == 1
    assert result["reuse_rate"] == 0.9
    assert result["days"] == 30


def test_griot_stats_honors_the_days_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(common, "get_index_status", lambda: {"points_count": 0, "embed_profile": "jina-code"})
    real_load = mcp_server.stats.load_window

    def _spy(days, *args, **kwargs):
        seen["days"] = days
        return real_load(days, *args, **kwargs)

    monkeypatch.setattr(mcp_server.stats, "load_window", _spy)

    result = mcp_server.griot_stats(days=7)

    assert seen["days"] == 7
    assert result["days"] == 7


def test_griot_stats_includes_the_quality_trend(monkeypatch):
    monkeypatch.setattr(common, "get_index_status", lambda: {"points_count": 0, "embed_profile": "jina-code"})
    logdb.write_quality_check(common.LOG_DIR, {
        # The active collection's: by default the trend is about the index the state lines describe.
        "timestamp": datetime.now(timezone.utc).isoformat(), "collection": common.COLLECTION_NAME,
        "self_check": {"sampled": 10, "passed": 8, "failed": 2, "failures": [], "avg_score": 0.7},
    })

    result = mcp_server.griot_stats()

    assert result["quality_trend"][0]["pass_rate"] == 0.8


def test_griot_stats_rejects_a_nonsensical_window(monkeypatch):
    """A negative or zero window silently returns an empty report, which an
    agent would read as "no activity" rather than "you asked wrong"."""
    with pytest.raises(ValueError, match="days"):
        mcp_server.griot_stats(days=0)


# --- MCP prompt (becomes a slash command in the client) ---------------------


def test_stats_prompt_is_registered():
    """[user-requested] An MCP prompt surfaces in Claude Code as
    /mcp__griot__stats — the closest thing to the /usage screen
    that travels WITH the server instead of living in one project's
    .claude/commands/."""
    assert callable(mcp_server.griot_stats_report)


def test_stats_prompt_mentions_the_tool_it_depends_on():
    """A prompt only injects text — the actual work is a tool call, so the
    text has to name the tool or the agent has to guess."""
    assert "griot_stats" in mcp_server.griot_stats_report()


def test_stats_prompt_passes_the_window_through():
    assert "7" in mcp_server.griot_stats_report(days=7)


@pytest.mark.anyio
async def test_async_tools_actually_run_through_the_protocol():
    """[two real bugs, caught only here] The unit tests above call these
    tools directly and await the result themselves, which hid both:

    1. `@_records_call` was sync, so wrapping an async tool returned the
       coroutine WITHOUT awaiting it — the SDK got a coroutine where it
       expected the output dict and the call failed validation.
    2. `ctx` was an unannotated parameter, so the SDK never injected the
       Context and instead published `ctx` in the tool's input schema for
       an agent to pass.

    Awaiting a coroutine yourself makes (1) invisible; only a real call
    through the protocol exercises what a client actually does."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_profiles_delete"]
        assert "ctx" not in (tool.input_schema.get("properties") or {}), "Context leaked into the schema"

        result = await client.call_tool("griot_profiles_delete", {"profile": "bge-small"})

        assert result.is_error is False
        assert result.structured_content["changed"] is False  # unconfirmed: nothing happened


@pytest.mark.anyio
async def test_every_async_tool_runs_through_the_protocol():
    """[review finding] The test above pinned ONE async tool; every async tool
    added since inherited the same two failure modes without a guard, and the
    direct-call tests can't see either — they await the coroutine themselves,
    which is precisely what hid the bugs the first time.

    griot_index_repo is covered separately (it registers conditionally, so it
    is absent from this client) — see
    test_index_repo_runs_through_the_protocol_when_enabled."""
    from mcp.client.client import Client

    async_tools = {
        "griot_repos_add": {"path": "/tmp/nowhere"},
        "griot_repos_remove": {"path": "/tmp/nowhere"},
        "griot_profiles_delete": {"profile": "bge-small"},
        "griot_golden_set_add": {"query": "q", "must_include": [{"source_type": "code"}]},
        "griot_golden_set_remove": {"index": 1},
        "griot_assist_install": {},
    }

    async with Client(mcp_server.mcp) as client:
        schemas = {t.name: t for t in (await client.list_tools()).tools}
        for name, args in async_tools.items():
            assert "ctx" not in (schemas[name].input_schema.get("properties") or {}), \
                f"Context leaked into {name}'s schema"
            assert schemas[name].output_schema, f"{name} has no output_schema"

            result = await client.call_tool(name, args)

            # Unconfirmed and over a client with no back-channel: every one of
            # them must refuse in a structured way, never raise, never act.
            assert result.is_error is False, f"{name} failed through the protocol"
            assert result.structured_content["changed"] is False, f"{name} acted unconfirmed"


@pytest.mark.anyio
async def test_index_repo_runs_through_the_protocol_when_enabled(mcp_server_with_index_enabled):
    """griot_index_repo became async at the same time it gained confirmation,
    and its conditional registration keeps it out of the default client — so
    the guard above cannot reach it."""
    from mcp.client.client import Client

    async with Client(mcp_server_with_index_enabled.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_index_repo"]
        assert "ctx" not in (tool.input_schema.get("properties") or {})
        assert tool.output_schema

        result = await client.call_tool("griot_index_repo", {"path": "/tmp/nowhere"})

        assert result.is_error is False
        assert result.structured_content["started"] is False


@pytest.mark.anyio
async def test_prompt_is_registered_as_plain_stats():
    """The client composes the slash command as mcp__<server>__<prompt>, so
    the server segment ALREADY says "griot" — naming the prompt
    griot_stats_report produced /mcp__griot__griot_stats_report, saying it
    twice. Only this last segment is ours to choose."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}

    # The set itself is pinned by test_all_four_prompts_are_registered_...;
    # this one is about the NAME, so it asserts only that.
    assert "stats" in names
    assert not any(n.startswith("griot") for n in names)


def test_griot_index_status_passes_through_common(monkeypatch):
    fake_status = {
        "points_count": 42,
        "collection": "codebase__jina-code",
        "embed_profile": "jina-code",
        "running": False,
        "pid": None,
        "path": None,
        "last_indexed": None,
        "spend_ceiling_exceeded": False,
    }

    def fake_get_index_status(collection=None):
        assert collection is None
        return fake_status

    monkeypatch.setattr(common, "get_index_status", fake_get_index_status)

    assert mcp_server.griot_index_status() == fake_status


def test_griot_index_status_forwards_explicit_collection(monkeypatch):
    seen = {}

    def fake_get_index_status(collection=None):
        seen["collection"] = collection
        return {
            "points_count": 0, "collection": collection, "embed_profile": "jina-code",
            "running": False, "pid": None, "path": None, "last_indexed": None,
            "spend_ceiling_exceeded": False,
        }

    monkeypatch.setattr(common, "get_index_status", fake_get_index_status)

    mcp_server.griot_index_status(collection="codebase__gemini")

    assert seen["collection"] == "codebase__gemini"


# --- griot_quality_check ---------------------------------------------------


def test_griot_quality_check_passes_through_and_uses_small_default(monkeypatch):
    seen = {}

    def fake_run_self_check(collection, sample_size=quality_check.SELF_CHECK_SAMPLE_SIZE, min_score=quality_check.SELF_CHECK_MIN_SCORE):
        seen["collection"] = collection
        seen["sample_size"] = sample_size
        return {"sampled": 3, "passed": 3, "failed": 0, "avg_score": 0.99, "failures": []}

    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check", fake_run_self_check)

    output = mcp_server.griot_quality_check(golden_set=False)

    assert seen["collection"] == common.COLLECTION_NAME
    # The curated half has its own file: tests/test_quality_check_golden_set.py.
    assert {k: output[k] for k in ("sampled", "passed", "failed", "avg_score", "failures")} == {
        "sampled": 3, "passed": 3, "failed": 0, "avg_score": 0.99, "failures": []}
    assert output["golden_check"] is None
    # the MCP default needs to be SMALLER than the CLI's (30) — limits cost if the
    # active profile is paid.
    assert seen["sample_size"] == mcp_server.QUALITY_CHECK_DEFAULT_SAMPLE_SIZE
    assert mcp_server.QUALITY_CHECK_DEFAULT_SAMPLE_SIZE < quality_check.SELF_CHECK_SAMPLE_SIZE


def test_griot_quality_check_honors_explicit_sample_size(monkeypatch):
    seen = {}

    def fake_run_self_check(collection, sample_size=quality_check.SELF_CHECK_SAMPLE_SIZE, min_score=quality_check.SELF_CHECK_MIN_SCORE):
        seen["sample_size"] = sample_size
        return {"sampled": 1, "passed": 1, "failed": 0, "avg_score": 0.99, "failures": []}

    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check", fake_run_self_check)

    mcp_server.griot_quality_check(sample_size=7)

    assert seen["sample_size"] == 7


# --- Level 2: real MCP protocol, in-memory client -------------------------


@pytest.mark.anyio
async def test_list_tools_exposes_the_read_only_tools_by_default():
    """Without GRIOT_MCP_ENABLE_INDEX, griot_index_repo doesn't even appear registered —
    see test_griot_index_repo_registered_when_enabled for the opposite case."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        result = await client.list_tools()
        names = {t.name for t in result.tools}

    assert names == {
        "griot_search",
        "griot_spend_status",
        "griot_index_status",
        "griot_quality_check",
        "griot_repos_list",
        "griot_stats",
        "griot_golden_set_list",
        "griot_profiles_list",
        "griot_config_list",
        "griot_audit",
        "griot_golden_set_suggest",
        "griot_index_preview",  # what a run WOULD do: there whether or not indexing is enabled
        # Management surface: state-changing, gated by _confirmed()
        "griot_golden_set_add",
        "griot_golden_set_remove",
        "griot_repos_add",
        "griot_repos_remove",
        "griot_profiles_delete",
        "griot_assist_install",
        # Credentials: guidance only — this one can never set a value
        "griot_auth_guidance",
    }
    # closed decision (section 0/2.6): griot_ask is never an MCP tool.
    assert "griot_ask" not in names
    assert "griot_index_repo" not in names


@pytest.mark.anyio
async def test_assist_install_exposes_expected_schema_and_refuses_unconfirmed_through_the_protocol(monkeypatch):
    """Dedicated protocol-level check beyond the generic 'every async tool'
    sweep: confirms the real input_schema (not just that ctx doesn't leak
    into it) has harness/scope/confirm with the right defaults, and that an
    unconfirmed call refuses cleanly through a real client with no
    elicitation back-channel. Does not exercise confirm=True here — that
    would write real files; the mocked-install path is covered at level 1
    (test_assist_install_runs_for_explicit_harness_once_confirmed)."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_assist_install"]
        props = tool.input_schema.get("properties") or {}
        assert "ctx" not in props, "Context leaked into the schema"
        assert props["harness"]["default"] == "all"
        assert props["scope"]["default"] == "local"
        assert props["confirm"]["default"] is False
        assert tool.output_schema, "griot_assist_install has no output_schema"

        result = await client.call_tool("griot_assist_install", {})

    assert result.is_error is False
    assert result.structured_content["changed"] is False
    assert "griot assist install" in result.structured_content["message"]


@pytest.mark.anyio
async def test_call_tool_griot_spend_status_gera_structured_content_real(monkeypatch):
    """Finding from the review of mcp_server.py: only list_tools() was tested at
    level 2 — nothing confirmed that outputSchema/structuredContent (the actual
    benefit of returning a TypedDict instead of a loose dict, see the module's
    docstring) actually arrives through the protocol, not just via a direct
    call in Python."""
    from mcp.client.client import Client

    monkeypatch.setattr(common, "get_spend_today", lambda: 1.5)
    monkeypatch.setattr(common, "SPEND_CEILING_USD", 3.0)
    monkeypatch.setattr(common, "SPEND_VELOCITY_CEILING_USD", 1.0)
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "jina-code")
    monkeypatch.setattr(common, "COLLECTION_NAME", "codebase__jina-code")

    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["griot_spend_status"].output_schema is not None

        result = await client.call_tool("griot_spend_status", {})

    assert result.is_error is not True
    assert result.structured_content == {
        "spend_today_usd": 1.5,
        "daily_ceiling_usd": 3.0,
        "velocity_ceiling_usd": 1.0,
        "embed_profile": "jina-code",
        "collection": "codebase__jina-code",
    }


@pytest.mark.anyio
async def test_call_tool_griot_search_runtime_error_becomes_iserror_via_client(monkeypatch):
    """Level 2 of what test_griot_search_propagates_runtime_error already covers at
    level 1: the RuntimeError -> isError=True conversion is automatic via
    Client (mcp_server.py's docstring) — it was never actually confirmed
    going through the real protocol, only assumed."""
    from mcp.client.client import Client

    def fake_search(query, limit=5, group_by_document=False, **filters):
        raise RuntimeError("Local circuit breaker: today's estimated spend already hit the ceiling")

    monkeypatch.setattr(common, "search", fake_search)

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_search", {"query": "x"})

    assert result.is_error is True
    assert "circuit breaker" in result.content[0].text


# --- griot_index_repo (the design notes — disabled by default) ----------


@pytest.fixture
def mcp_server_with_index_enabled(monkeypatch, tmp_path):
    """griot_index_repo is registered conditionally (an if around the
    @mcp.tool() at module import time) — the only way to test both states is
    to reload the module with GRIOT_MCP_ENABLE_INDEX set/unset. Always
    reloads back to the default state in teardown, even if the test fails,
    so the conditional registration doesn't leak into subsequent tests.

    [finding H2, 2026-08-19 audit] also allowlists the test's tmp_path
    via GRIOT_MCP_INDEX_ROOTS — since the fix, the tool refuses any path
    outside repos.json/GRIOT_MCP_INDEX_ROOTS, so tests exercising the
    OTHER paths (invalid git, lock held, subprocess) need to pass the gate
    first. The gate's OWN tests delete the env var."""
    monkeypatch.setenv("GRIOT_MCP_ENABLE_INDEX", "true")
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(tmp_path))
    importlib.reload(mcp_server)
    try:
        yield mcp_server
    finally:
        monkeypatch.delenv("GRIOT_MCP_ENABLE_INDEX", raising=False)
        monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
        importlib.reload(mcp_server)


@pytest.mark.anyio
async def test_griot_index_repo_registered_when_enabled(mcp_server_with_index_enabled):
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}

    assert "griot_index_repo" in names


@pytest.mark.anyio
async def test_griot_index_repo_rejects_nonexistent_path(mcp_server_with_index_enabled, tmp_path):
    result = await mcp_server.griot_index_repo(path=str(tmp_path / "does-not-exist"), confirm=True)
    assert result["started"] is False
    assert result["pid"] is None
    assert "invalid" in result["reason"]


@pytest.mark.anyio
async def test_griot_index_repo_rejects_non_git_directory(mcp_server_with_index_enabled, tmp_path):
    plain_dir = tmp_path / "just-a-directory"
    plain_dir.mkdir()
    result = await mcp_server.griot_index_repo(path=str(plain_dir), confirm=True)
    assert result["started"] is False
    assert "invalid" in result["reason"]


@pytest.mark.anyio
async def test_griot_index_repo_rejects_when_already_indexing(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("first commit")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": True, "pid": 999, "path": "/repos/x"})

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)

    assert result["started"] is False
    assert "already in progress" in result["reason"]


@pytest.mark.anyio
async def test_griot_index_repo_starts_subprocess_with_correct_command(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("first commit")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    captured = {}
    real_popen = subprocess.Popen

    class FakeProc:
        pid = 12345
        returncode = None

        def poll(self):
            return None  # still running — didn't die immediately

    def fake_popen(cmd, **kwargs):
        # the handler's own git rev-parse validation uses
        # subprocess.run (same module, same global Popen) — only intercepts the
        # Popen for the indexing command, lets the real git run underneath.
        if cmd[0] == "git":
            return real_popen(cmd, **kwargs)
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = await mcp_server.griot_index_repo(path=str(repo.path), sources=["code", "commits"], confirm=True)

    assert result == {"started": True, "path": str(repo.path), "pid": 12345, "sources": ["code", "commits"], "reason": None}
    cmd = captured["cmd"]
    assert cmd[-4:] == ["index", "all", "--path", str(repo.path)] or "--sources" in cmd
    assert "--path" in cmd and str(repo.path) in cmd
    assert "--sources" in cmd and "code,commits" in cmd
    assert captured["kwargs"]["start_new_session"] is True
    # never inherit stdout/stderr from the MCP server — it would corrupt the JSON-RPC transport
    assert captured["kwargs"]["stdout"] is not None
    assert captured["kwargs"]["stderr"] is not None


@pytest.mark.anyio
async def test_griot_index_repo_releases_memoized_client_before_spawning(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    """[real finding, 2026-08-20] confirmed empirically: if the MCP server
    has already opened/memoized the active collection (get_client(), triggered by
    any prior read tool in that same session), the indexing subprocess —
    which needs to open the SAME directory — dies with
    'failed to open WAL ... WouldBlock' (Qdrant Edge's process-level mutual
    exclusion). griot_index_repo needs to release the server's handle
    (release_client()) BEFORE spawning, otherwise the server locks up its own
    tool as soon as any search has already run in the session."""
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    calls = []
    monkeypatch.setattr(common, "release_client", lambda: calls.append("released"))

    real_popen = subprocess.Popen

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        if cmd[0] == "git":
            return real_popen(cmd, **kwargs)
        calls.append("spawned")
        return FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)

    assert result["started"] is True
    assert calls == ["released", "spawned"]  # release BEFORE spawn, not after


@pytest.mark.anyio
async def test_griot_index_repo_default_sources_excludes_gitlab(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    real_popen = subprocess.Popen

    class FakeProc:
        pid = 1
        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        return real_popen(cmd, **kwargs) if cmd[0] == "git" else FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)
    assert result["sources"] == ["code", "commits", "tags", "branches"]
    assert "gitlab" not in result["sources"]


# --- path allowlist gate (finding H2 from the 2026-08-19 audit) ------
# Without this, an agent with an injected prompt could call
# griot_index_repo("/Users/<user>") and exfiltrate the entire filesystem to the
# paid embedding API (the content becomes searchable points via griot_search).


@pytest.mark.anyio
async def test_griot_index_repo_rejects_path_outside_allowlist(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    monkeypatch.setattr(common, "load_repos", lambda: [])

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)

    assert result["started"] is False
    assert result["pid"] is None
    assert "GRIOT_MCP_INDEX_ROOTS" in result["reason"]


@pytest.mark.anyio
async def test_griot_index_repo_rejects_when_no_allowlist_configured_at_all(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    """repos.json missing + env var absent = refusal (fail closed), not a
    wide-open pass."""
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)

    def no_repos():
        raise FileNotFoundError()

    monkeypatch.setattr(common, "load_repos", no_repos)

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)
    assert result["started"] is False


@pytest.mark.anyio
async def test_griot_index_repo_allows_path_registered_in_repos_json(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    monkeypatch.setattr(common, "load_repos", lambda: [str(repo.path)])
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    real_popen = subprocess.Popen

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        return real_popen(cmd, **kwargs) if cmd[0] == "git" else FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)
    assert result["started"] is True


@pytest.mark.anyio
async def test_griot_index_repo_symlink_escaping_allowed_root_is_rejected(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    """Symlink INSIDE the allowed root pointing outside of it: the path is
    resolved (Path.resolve) BEFORE the gate, so the real destination is what
    counts — the link isn't an escape hatch."""
    from conftest import GitRepo
    outside = tmp_path.parent / f"{tmp_path.name}-outside-root"
    outside.mkdir(exist_ok=True)
    repo = GitRepo(outside / "repo-outside")
    repo.commit("c")
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    link = allowed_root / "link-pointing-outside"
    link.symlink_to(repo.path)
    monkeypatch.setenv("GRIOT_MCP_INDEX_ROOTS", str(allowed_root))
    monkeypatch.setattr(common, "load_repos", lambda: [])

    try:
        result = await mcp_server.griot_index_repo(path=str(link), confirm=True)
        assert result["started"] is False
    finally:
        import shutil
        shutil.rmtree(outside, ignore_errors=True)


@pytest.mark.anyio
async def test_griot_index_repo_reports_failure_when_process_dies_immediately(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    from conftest import GitRepo
    repo = GitRepo(tmp_path / "repo")
    repo.commit("c")
    monkeypatch.setattr(common, "index_lock_status", lambda: {"running": False, "pid": None, "path": None})

    real_popen = subprocess.Popen

    class DeadProc:
        pid = 777
        returncode = 1
        def poll(self):
            return 1  # already dead

    def fake_popen(cmd, **kwargs):
        return real_popen(cmd, **kwargs) if cmd[0] == "git" else DeadProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)

    result = await mcp_server.griot_index_repo(path=str(repo.path), confirm=True)
    assert result["started"] is False
    assert result["pid"] is None


# --- executable entrypoint (python -m griot.mcp_server) --------------------


def test_main_runs_mcp_server(monkeypatch):
    """Real finding: mcp_server.py only defined the tools, it never had a way
    to actually run the server — `python -m griot.mcp_server` (used in the
    README/plan's configuration examples) executed the module and exited
    without doing anything, because it was missing a call to mcp.run() (stdio,
    the SDK's default)."""
    calls = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append((a, kw)))
    # multi is the default, so main() would otherwise leave a real reaper
    # thread running for the rest of the test session.
    monkeypatch.setattr(mcp_server, "_start_idle_reaper", lambda *a, **kw: None)
    monkeypatch.setattr(common, "ENVIRONMENT_WAS_NARROWED", True)  # as in a process started as the server

    mcp_server.main()

    assert calls


@pytest.mark.anyio
async def test_griot_index_repo_allowlist_rejection_does_not_reveal_path_existence(mcp_server_with_index_enabled, monkeypatch, tmp_path):
    """[review] pins the ORDER of the gate: a path that's both nonexistent AND
    outside the allowlist gets the allowlist's refusal, not the 'invalid'
    message — otherwise the refusal would work as a filesystem-existence
    oracle."""
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    monkeypatch.setattr(common, "load_repos", lambda: [])

    result = await mcp_server.griot_index_repo(path=str(tmp_path / "does-not-exist-and-not-allowed"), confirm=True)

    assert result["started"] is False
    assert "GRIOT_MCP_INDEX_ROOTS" in result["reason"]
    assert "invalid" not in result["reason"]


# --- the three prompts added after /mcp__griot__stats ------------------------
# A prompt injects text; the work is still a tool call. So each test asserts
# two things: that the text names the tools it depends on (an agent left to
# guess picks wrong), and that the non-obvious judgement is actually carried
# — otherwise the prompt is a shortcut for something the agent already did.


def test_history_prompt_drives_a_multi_source_investigation():
    """The reason this prompt exists: griot indexes seven source types, and
    "why is this like this" is almost never answered by one of them — code
    says what, the commit says when, the MR says who argued, the issue says
    what problem started it. Left alone an agent calls griot_search once and
    stops, which is also all `griot ask` does (ask.py: one search, one
    synthesis).

    It is also the answer to a gap the earlier decision left open: griot_ask
    was refused as a tool because the caller already has an LLM. That was
    right, and it removed the retrieval STRATEGY along with the synthesis.
    A prompt puts the strategy back without paying for a second model."""
    text = mcp_server.griot_history_report("why does auth retry twice")

    # The instruction to search REPEATEDLY is the reason this prompt exists;
    # naming the tool is not enough (griot ask names it too and searches once).
    assert "several times" in text
    assert "Do NOT answer from one search" in text
    assert "griot_search" in text
    assert "why does auth retry twice" in text
    for source in ("commit", "merge_request", "issue"):
        assert source in text


def test_history_prompt_asks_for_sources_not_just_an_answer():
    """An unsourced history is indistinguishable from a plausible invention,
    and the payloads carry the identifiers that make it checkable."""
    text = mcp_server.griot_history_report("anything")

    assert "source_label" in text
    assert "cite" in text.lower()


def test_health_prompt_separates_the_two_kinds_of_check():
    """The judgement no agent makes on its own: the self-check measures a
    MECHANICAL property (indexed points retrieve themselves) and the golden
    set measures whether real questions get answered. Either can pass while
    the other fails, and the two failures mean different things."""
    text = mcp_server.griot_health_report()

    assert "griot_quality_check" in text
    assert "griot_golden_set_list" in text
    assert "mechanical" in text.lower()


def test_health_prompt_does_not_let_a_golden_set_that_was_not_run_pass():
    """griot_quality_check used to run the SELF-CHECK only, and the prompt
    sent the curated half to a terminal so that the agent would not report a
    measurement it never received. The tool now runs both; what is left of
    that rule is the case where the curated cases were NOT run (none exist,
    too many for one call): `golden_check` is null, and null is not a pass."""
    text = mcp_server.griot_health_report()

    assert "golden_check" in text and "golden_set_not_run" in text
    assert "self-check" in text.lower()
    assert "Do not report that as passing" in text


def test_health_prompt_warns_that_the_check_can_cost_money():
    """griot_quality_check embeds one query per sampled point — free on a
    local profile, billed on a paid one. The agent should say so before
    firing it, since the prompt is what triggers the call."""
    text = mcp_server.griot_health_report()

    # [review finding] This used to be `"griot_spend_status" in text or
    # "cost" in text.lower()` — the weaker half carried it alone, so deleting
    # the whole spend instruction still passed as long as the word "cost"
    # survived anywhere. Assert the instruction, not a vocabulary.
    assert "griot_spend_status" in text
    assert "billed" in text.lower()


def test_overview_prompt_names_the_tools_it_summarizes():
    text = mcp_server.griot_overview_report()

    for tool in ("griot_repos_list", "griot_index_status", "griot_stats"):
        assert tool in text


def test_overview_prompt_calls_out_unusable_entries():
    """repo_status() reports exists/is_git rather than filtering — a repo
    whose directory is gone still lists, and indexing it fails or silently
    indexes nothing. That distinction is the whole point of the summary."""
    text = mcp_server.griot_overview_report()

    assert "is_git" in text
    assert "exists" in text


@pytest.mark.anyio
async def test_all_four_prompts_are_registered_without_the_griot_prefix():
    """The client composes mcp__<server>__<prompt>, so the server segment
    already says griot — see test_prompt_is_registered_as_plain_stats."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}

    assert names == {"stats", "history", "health", "overview"}


@pytest.mark.anyio
async def test_index_status_survives_a_locked_collection_through_the_protocol(monkeypatch):
    """[review finding, BLOCKER] I fixed this None in the CLI report and left
    the MCP tool broken on the identical case.

    common.get_index_status() returns points_count=None when another process
    holds the collection open — the try/except that produces it exists so
    "the whole tool would not break". But IndexStatusOutput declared
    `points_count: int`, so pydantic rejected the None and broke the tool one
    layer up, turning a degraded read into a hard isError.

    Not hypothetical: it is the flow griot_index_repo's own docstring
    recommends. start_index_job releases the handle, the subprocess takes the
    shard, and the very next griot_index_status hits WouldBlock. Both new
    prompts (health, overview) open by calling this tool."""
    from mcp.client.client import Client

    monkeypatch.setattr(common, "get_index_status",
                        lambda collection=None: {"points_count": None, "collection": "c",
                                                 "embed_profile": "jina-code", "running": False,
                                                 "pid": None, "path": None, "last_indexed": None,
                                                 "spend_ceiling_exceeded": False})

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_index_status", {})

    assert result.is_error is False, "a locked collection must degrade, not fail"
    assert result.structured_content["points_count"] is None


@pytest.mark.anyio
async def test_history_prompt_names_only_a_filter_the_tool_has():
    """[review finding, then a real change] The prompt once told the agent to
    search "so the different source types can each contribute" when
    griot_search took only a query and a limit: the agent followed it,
    believed it had filtered, and reported coverage it never had. The tool
    has a `source_types` argument now and the prompt uses it, so the two are
    held together here: the argument the prompt names is in the tool's real
    schema, and every kind it tells the agent to pass is one the filter
    accepts."""
    import re
    text = mcp_server.griot_history_report("q")
    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]

    assert "source_types" in text and "source_types" in tool.input_schema["properties"]
    asked_for = set(re.findall(r'"([a-z_]+)"', " ".join(re.findall(r"\[[^\]]*\]", text))))
    assert asked_for and asked_for <= set(common.SOURCE_TYPES), asked_for - set(common.SOURCE_TYPES)
    assert "no filter" not in text.lower()
    # It lists what each kind knows; every kind it lists has to be in one of
    # the searches it asks for, or the agent is told about a kind and never
    # sent to look at it.
    assert asked_for == set(common.SOURCE_TYPES), set(common.SOURCE_TYPES) - asked_for


def test_health_prompt_asks_the_tool_that_knows_about_paid_profiles():
    """[review finding] griot_spend_status returns spend, ceilings and the
    profile NAME — no boolean saying the profile costs money. The tool that
    answers "is a paid profile active" is griot_profiles_list, whose entries
    carry paid and is_active. Asking the wrong tool left the agent inferring
    'paid' from a string."""
    text = mcp_server.griot_health_report()

    assert "griot_profiles_list" in text


def test_health_prompt_checks_cost_before_spending():
    """[review finding] The step read "Call griot_quality_check. FIRST call
    griot_spend_status" — the paid imperative before the cheap one. A model
    reading numbered steps may fire in the order it read them, which is the
    opposite of the rule the rest of this surface follows."""
    text = mcp_server.griot_health_report()
    cost_check = min(text.index("griot_profiles_list"), text.index("griot_spend_status"))

    assert cost_check < text.index("griot_quality_check")


def test_overview_prompt_attributes_freshness_to_the_tool_that_reports_it():
    """[review finding] "whether the index looks current, from griot_stats" —
    StatsOutput carries no timestamp of the last run. The field that answers
    it is last_indexed, from griot_index_status, which the prompt already
    calls."""
    text = mcp_server.griot_overview_report()

    assert "last_indexed" in text
    assert "current, from griot_stats" not in text


def test_quality_check_tool_records_its_run_for_the_trend(monkeypatch, tmp_path):
    """[review finding] THE SAME REGRESSION AS THE ORPHANED QUALITY-CHECK WRITER, one surface over.

    Back then, deleting the previous front end deleted the only caller of
    write_quality_check(), leaving the trend reading a table nothing filled;
    the fix was to record from the CLI. But griot_quality_check calls
    run_self_check() directly, so an MCP check never reaches
    _record_for_trend — and the new /mcp__griot__health prompt turns that
    into the recommended path. Run it ten times, then ask for stats, and
    quality_trend is empty while griot_stats' own docstring promises it.

    "I checked the reader and not the writer", again — this time on a surface
    that did not exist when the rule was written."""
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda collection, sample_size: {"passed": 9, "sampled": 10, "failed": 1,
                                                         "collection": collection, "failures": []})
    recorded = []
    monkeypatch.setattr(quality_check, "_record_for_trend",
                        lambda collection, self_check, golden_check: recorded.append(collection))

    mcp_server.griot_quality_check(sample_size=10)

    assert len(recorded) == 1, "an MCP quality check must land in the same trend the CLI writes"


def test_quality_check_tool_caps_the_sample_size(monkeypatch):
    """[review finding] Every sample is one embedding call. griot_search caps
    its limit and griot_golden_set_add caps its own, and the comment on
    QUALITY_CHECK_DEFAULT_SAMPLE_SIZE says the default exists to limit cost on
    a paid profile — but the default protects and the PARAMETER did not. An
    agent passing sample_size=100000 embeds until the circuit breaker cuts it
    off, which bounds the damage at a day's ceiling rather than preventing it.
    The health prompt promotes this tool to the recommended path."""
    seen = {}
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(quality_check, "run_self_check",
                        lambda collection, sample_size: seen.update(n=sample_size) or
                        {"passed": 1, "sampled": 1, "failed": 0, "collection": collection,
                         "avg_score": 0.99, "failures": []})
    monkeypatch.setattr(quality_check, "_record_for_trend", lambda *a, **kw: None)

    mcp_server.griot_quality_check(sample_size=100_000)

    assert seen["n"] == mcp_server.QUALITY_CHECK_SAMPLE_MAX


@pytest.mark.anyio
async def test_every_prompt_renders_through_the_protocol():
    """[review finding] The suite pinned list_prompts but never get_prompt, so
    nothing exercised what a client actually does — the same gap that let the
    async-tool bugs through twice. A prompt whose function raises, or whose
    required argument is misdeclared, lists fine and fails on use."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        for name, args in [("stats", {}), ("history", {"question": "why"}),
                           ("health", {}), ("overview", {})]:
            result = await client.get_prompt(name, args)

            text = result.messages[0].content.text
            assert text.strip(), f"{name} rendered empty"
            # Every prompt drives tool calls; one that names none is inert.
            assert "griot_" in text, f"{name} names no tool to call"


@pytest.mark.anyio
async def test_search_exposes_document_grouping_through_the_protocol():
    """[user-requested] Opt-in on the tool, so the caller that knows what it
    is asking decides. Default stays off — 22 searches a session already run
    through here, and flipping their meaning on three sample queries of mine
    would be changing what everyone gets on thin evidence."""
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]

    props = tool.input_schema.get("properties") or {}
    assert "group_by_document" in props
    assert props["group_by_document"].get("default") is False


def test_search_passes_grouping_through_to_common(monkeypatch):
    seen = {}
    monkeypatch.setattr(common, "search",
                        lambda query, limit, group_by_document=False, **filters:
                        seen.update(g=group_by_document) or [])

    mcp_server.griot_search("q", group_by_document=True)

    assert seen["g"] is True


def test_search_limit_cap_applies_to_grouped_results_too(monkeypatch):
    """SEARCH_LIMIT_MAX bounds what the AGENT receives. With grouping the
    store is queried for several times `limit` internally, so the cap has to
    be applied to the requested limit before that multiplication — otherwise
    a large limit turns into a much larger fetch."""
    seen = {}
    monkeypatch.setattr(common, "search",
                        lambda query, limit, group_by_document=False, **filters:
                        seen.update(limit=limit) or [])

    mcp_server.griot_search("q", limit=10_000, group_by_document=True)

    assert seen["limit"] == mcp_server.SEARCH_LIMIT_MAX


def test_history_prompt_asks_for_breadth():
    """The prompt exists to spread across source types, and grouping is what
    makes a slot budget reach more of them — measured: on a focused query it
    surfaced a release and a commit that concentration had pushed out."""
    text = mcp_server.griot_history_report("q")

    assert "group_by_document" in text


# --- idle release in multi mode ----------------------------------------------
# get_client() only checks idleness when it is called again, and then reopens
# on the spot, so an idle server kept the collection forever and every other
# process (a shell `griot index`, another session) died with WouldBlock. The
# server now runs its own reaper; these pin down when it may and may not close.


_FAR_FUTURE = 10 ** 12


def _multi_mode(monkeypatch, idle=30.0):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "multi")
    monkeypatch.setattr(common, "IDLE_RELEASE_SECONDS", idle)


def test_client_idle_seconds_is_none_without_a_handle_and_counts_from_last_use(monkeypatch):
    """The reaper reads idleness through this public call, not through
    common.py's private handle and timestamp."""
    _multi_mode(monkeypatch)
    assert common.client_idle_seconds(now=_FAR_FUTURE) is None

    common.get_client()
    assert common.client_idle_seconds(now=common._client_last_used_at + 12.5) == 12.5

    common.release_client()
    assert common.client_idle_seconds(now=_FAR_FUTURE) is None


def test_idle_reaper_releases_a_handle_idle_past_the_window(monkeypatch):
    _multi_mode(monkeypatch)
    common.get_client()

    mcp_server._release_if_idle(now=common._client_last_used_at + 31)

    assert common._client is None


def test_idle_reaper_keeps_a_handle_used_within_the_window(monkeypatch):
    _multi_mode(monkeypatch)
    common.get_client()

    mcp_server._release_if_idle(now=common._client_last_used_at + 29)

    assert common._client is not None


def test_idle_reaper_does_nothing_in_single_mode(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")
    common.get_client()

    mcp_server._release_if_idle(now=_FAR_FUTURE)

    assert common._client is not None


def test_idle_reaper_never_closes_the_handle_under_a_running_sync_tool(monkeypatch):
    # Sync tools run in SDK worker threads, so the reaper can fire mid-call;
    # closing the shard under a search in progress would crash that call.
    _multi_mode(monkeypatch)
    common.get_client()
    seen = {}

    @mcp_server._records_call
    def busy_tool():
        mcp_server._release_if_idle(now=_FAR_FUTURE)
        seen["open"] = common._client is not None
        return {}

    busy_tool()

    assert seen["open"] is True


def test_idle_reaper_never_closes_the_handle_under_a_running_async_tool(monkeypatch):
    import asyncio

    _multi_mode(monkeypatch)
    common.get_client()
    seen = {}

    @mcp_server._records_call
    async def busy_tool():
        mcp_server._release_if_idle(now=_FAR_FUTURE)
        seen["open"] = common._client is not None
        return {}

    asyncio.run(busy_tool())

    assert seen["open"] is True


def test_idle_reaper_can_release_again_once_the_tool_finished(monkeypatch):
    _multi_mode(monkeypatch)
    common.get_client()

    @mcp_server._records_call
    def quick_tool():
        return {}

    quick_tool()
    mcp_server._release_if_idle(now=_FAR_FUTURE)

    assert common._client is None


def test_idle_reaper_counts_a_tool_that_raised_as_finished(monkeypatch):
    _multi_mode(monkeypatch)
    common.get_client()

    @mcp_server._records_call
    def failing_tool():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing_tool()
    mcp_server._release_if_idle(now=_FAR_FUTURE)

    assert common._client is None


def test_idle_reaper_thread_is_not_started_in_single_mode(monkeypatch):
    monkeypatch.setattr(common, "CONCURRENCY_MODE", "single")

    assert mcp_server._start_idle_reaper() is None


def test_idle_reaper_thread_releases_on_its_own_in_multi_mode(monkeypatch):
    import time

    _multi_mode(monkeypatch, idle=0.2)
    common.get_client()

    stop = mcp_server._start_idle_reaper(interval=0.05)
    try:
        deadline = time.time() + 3
        while common._client is not None and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()

    assert common._client is None


def test_main_starts_the_idle_reaper_before_serving(monkeypatch):
    order = []
    monkeypatch.setattr(mcp_server, "_start_idle_reaper", lambda *a, **k: order.append("reaper"))
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **k: order.append("run"))
    monkeypatch.setattr(common, "ENVIRONMENT_WAS_NARROWED", True)  # as in a process started as the server

    mcp_server.main()

    assert order == ["reaper", "run"]


def test_every_registered_tool_goes_through_records_call():
    # The idle reaper only knows a tool is running because _records_call
    # counts it, so a tool registered without the decorator could have the
    # collection closed underneath it. Read from the source: the SDK's
    # registry does not expose the undecorated function.
    import re

    lines = pathlib.Path(mcp_server.__file__).read_text().splitlines()
    unwrapped, seen = [], 0
    # A bare @mcp.tool/@mcp.prompt would also skip _tool/_prompt, which clean
    # the docstring the server sends: indented on Python < 3.13 otherwise.
    bare = [line.strip() for line in lines if line.lstrip().startswith(("@mcp.tool(", "@mcp.prompt("))]
    assert bare == [], "register through _tool/_prompt"
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("@_tool("):
            continue
        seen += 1
        j, wrapped = i + 1, False
        while not re.match(r"\s*(async )?def ", lines[j]):
            wrapped |= lines[j].strip() == "@_records_call"
            j += 1
        if not wrapped:
            unwrapped.append(re.match(r"\s*(?:async )?def (\w+)", lines[j]).group(1))

    assert seen >= 16, "the scan found too few tools to be trusted"
    assert unwrapped == []


@pytest.mark.parametrize("idle, expected", [(30.0, 1.0), (1.0, 0.5), (0.4, 0.5), (600.0, 1.0)])
def test_the_reaper_checks_often_enough_that_the_idle_window_is_what_you_wait(monkeypatch, idle, expected):
    # The documented wait is the idle window itself; a coarse tick (it was up
    # to 5s) let a shell command that waited out ~12s miss the release by a
    # hair. The floor keeps a tiny test window from turning into a busy loop.
    monkeypatch.setattr(common, "IDLE_RELEASE_SECONDS", idle)

    assert mcp_server._reaper_interval() == expected


@pytest.mark.anyio
async def test_the_search_limit_default_agents_see_in_the_schema_is_eight():
    # Verified through the protocol: the default is read off the tool's signature, so the schema an
    # agent receives is what decides it, not a constant somewhere.
    from mcp.client.client import Client

    async with Client(mcp_server.mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["griot_search"]

    assert tool.input_schema["properties"]["limit"]["default"] == 8 == mcp_server.SEARCH_LIMIT_DEFAULT


def test_the_search_limit_default_sits_inside_the_cap():
    assert 1 <= mcp_server.SEARCH_LIMIT_DEFAULT <= mcp_server.SEARCH_LIMIT_MAX


# --- griot_index_repo: the human is asked through a real client -------------------
# The other tools have this matrix in tests/test_confirmation.py; this one
# lives here because it needs the module-reload fixture above.


def _git_repo(path):
    """A real git repository: index_job_refusal() rejects anything else
    before a human is asked, which is the behaviour under test elsewhere."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return str(path)


# --- an index run and the other calls in flight ------------------------------------------
# The run needs the collection to itself: its subprocess opens the same
# directory. griot_index_repo used to close the server's handle whatever else
# was running, which took the index from under a search in progress.


def _index_stubs(srv, monkeypatch, started):
    monkeypatch.setattr(srv.jobs, "index_job_refusal", lambda p, **kw: None)

    def start(p, s=None, *, release=None, **kw):
        busy = release() if release else None
        if busy:
            return {"started": False, "reason": busy, "path": None, "pid": None, "sources": None}
        started.append(p)
        return {"started": True, "path": p, "pid": 1, "sources": ["code"], "reason": None}

    monkeypatch.setattr(srv.jobs, "start_index_job", start)


@pytest.mark.anyio
async def test_index_repo_is_refused_before_asking_while_another_call_stays_in_flight(
        mcp_server_with_index_enabled, monkeypatch):
    """A person's yes must be able to change the outcome: a run that cannot
    start is said so before anyone is asked."""
    srv = mcp_server_with_index_enabled
    started, asked = [], []
    _index_stubs(srv, monkeypatch, started)
    monkeypatch.setattr(srv, "_WAIT_FOR_OTHER_CALLS_SECONDS", 0.2)

    async def confirmed(*a, **k):
        asked.append(1)
        return True, None

    monkeypatch.setattr(srv, "_confirmed", confirmed)
    srv._tool_started()  # a search that does not finish
    try:
        result = await srv.griot_index_repo("/repos/alpha", confirm=True, ctx=_FakeCtx())
    finally:
        srv._tool_finished()
    assert result["started"] is False and "another griot tool call" in result["reason"]
    assert asked == [] and started == []
    assert srv._alone_queue == [], "a call that gave up waiting does not stay in the queue"


@pytest.mark.anyio
async def test_index_repo_waits_for_a_call_that_is_about_to_finish(mcp_server_with_index_enabled, monkeypatch):
    """A search takes a fraction of a second: refusing outright would waste
    the question and the answer."""
    import threading
    import time as _time
    srv = mcp_server_with_index_enabled
    started = []
    _index_stubs(srv, monkeypatch, started)
    monkeypatch.setattr(srv, "_WAIT_FOR_OTHER_CALLS_SECONDS", 5.0)
    srv._tool_started()

    def finishes_soon():
        _time.sleep(0.2)
        srv._tool_finished()

    threading.Thread(target=finishes_soon).start()
    result = await srv.griot_index_repo("/repos/alpha", confirm=True, ctx=_FakeCtx())
    assert result["started"] is True and started == ["/repos/alpha"]


@pytest.mark.anyio
async def test_index_repo_is_not_started_when_a_call_arrives_during_the_question_and_stays(
        mcp_server_with_index_enabled, monkeypatch):
    srv = mcp_server_with_index_enabled
    started = []
    _index_stubs(srv, monkeypatch, started)
    monkeypatch.setattr(srv, "_WAIT_FOR_OTHER_CALLS_SECONDS", 0.2)

    async def confirmed_while_a_search_arrives(*a, **k):
        srv._tool_started()
        return True, None

    monkeypatch.setattr(srv, "_confirmed", confirmed_while_a_search_arrives)
    try:
        result = await srv.griot_index_repo("/repos/alpha", confirm=True, ctx=_FakeCtx())
    finally:
        srv._tool_finished()
    assert result["started"] is False and started == []
    assert "another griot tool call" in result["reason"] and "not started" in result["reason"]


@pytest.mark.anyio
async def test_index_repo_never_closes_the_index_under_a_call_that_slipped_in(mcp_server_with_index_enabled, monkeypatch):
    """Between the last wait and the start of the run there is no lock: what
    the run is given to let go of the collection must still look at the
    calls in flight, and must not wait inside the event loop."""
    import time as _time
    srv = mcp_server_with_index_enabled
    monkeypatch.setattr(srv.jobs, "index_job_refusal", lambda p, **kw: None)
    monkeypatch.setattr(srv, "_WAIT_FOR_OTHER_CALLS_SECONDS", 5.0)
    closed, seen = [], {}
    monkeypatch.setattr(srv.common, "release_client", lambda: closed.append(1))

    def start(p, s=None, *, release=None, **kw):
        srv._tool_started()  # a search arrives right now
        try:
            began = _time.monotonic()
            seen["busy"] = release() if release else (srv.common.release_client() or None)
            seen["waited"] = _time.monotonic() - began
        finally:
            srv._tool_finished()
        return {"started": not seen["busy"], "reason": seen["busy"], "path": None, "pid": None, "sources": None}

    monkeypatch.setattr(srv.jobs, "start_index_job", start)
    result = await srv.griot_index_repo("/repos/alpha", confirm=True, ctx=_FakeCtx())
    assert result["started"] is False and "another griot tool call" in result["reason"]
    assert closed == [] and seen["waited"] < 1.0


async def _index_via_client(srv, mode, path, action, monkeypatch, with_callback=True):
    started, asked = [], []
    monkeypatch.setattr(srv.jobs, "start_index_job",
                        lambda p, sources=None, **kw: started.append(p) or {"started": True, "path": p, "pid": 1,
                                                                       "sources": ["code"], "reason": None})

    async def callback(ctx, params):
        asked.append(params.message)
        return ElicitResult(action=action, content={})

    kwargs = {"mode": mode}
    if with_callback:
        kwargs["elicitation_callback"] = callback
    async with Client(srv.mcp, **kwargs) as client:
        result = await client.call_tool("griot_index_repo", {"path": path})
    return started, asked, result.structured_content


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_index_repo_starts_only_on_a_human_accept(mcp_server_with_index_enabled, monkeypatch, tmp_path, mode):
    srv = mcp_server_with_index_enabled
    started, asked, _ = await _index_via_client(srv, mode, _git_repo(tmp_path), "accept", monkeypatch)
    assert started == [str(tmp_path)] and len(asked) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_index_repo_does_not_start_on_a_no(mcp_server_with_index_enabled, monkeypatch, tmp_path, mode, action):
    srv = mcp_server_with_index_enabled
    started, _, out = await _index_via_client(srv, mode, _git_repo(tmp_path), action, monkeypatch)
    assert started == [] and out["started"] is False
    assert "Nothing was changed" in out["reason"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_index_repo_never_reads_a_client_that_cannot_ask_as_a_yes(mcp_server_with_index_enabled, monkeypatch, tmp_path, mode):
    srv = mcp_server_with_index_enabled
    started, _, out = await _index_via_client(srv, mode, _git_repo(tmp_path), "accept", monkeypatch, with_callback=False)
    assert started == [] and "confirm=true" in out["reason"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_index_repo_asks_nobody_about_a_path_it_will_refuse(mcp_server_with_index_enabled, monkeypatch, mode):
    srv = mcp_server_with_index_enabled
    monkeypatch.delenv("GRIOT_MCP_INDEX_ROOTS", raising=False)
    started, asked, out = await _index_via_client(srv, mode, "/definitely/not/allowed", "accept", monkeypatch)
    assert asked == [] and started == [] and out["started"] is False


@pytest.mark.anyio
async def test_index_repo_answer_parameter_is_not_in_the_schema(mcp_server_with_index_enabled):
    async with Client(mcp_server_with_index_enabled.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools["griot_index_repo"].input_schema["properties"]) == {"path", "sources", "confirm"}


@pytest.mark.anyio
async def test_quality_check_tool_fails_a_blank_sample_through_the_protocol(monkeypatch):
    """[debt 10] A sampled point with no text used to be counted as passed,
    so an index whose samples were all blank got a clean bill from the tool
    the health prompt recommends. Through the protocol, against a real
    index: the failure has to survive the output schema, not only exist in
    the dict the function returns."""
    import hashlib
    import random

    def fake_embed(texts, **kwargs):
        vectors = []
        for text in texts:
            rng = random.Random(int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32))
            vectors.append([rng.uniform(-1, 1) for _ in range(common.EMBED_DIM)])
        return vectors

    monkeypatch.setattr(common, "embed_texts", fake_embed)
    common.index_documents([{"id": f"repo:code:f{i}.py:0", "content": " ",
                             "metadata": {"source_type": "code", "repo": "repo", "file_path": f"f{i}.py",
                                          "chunk_index": 0}} for i in range(2)])

    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("griot_quality_check", {"sample_size": 2, "golden_set": False})

    assert result.is_error is False
    out = result.structured_content
    assert (out["sampled"], out["passed"], out["failed"]) == (2, 0, 2)
    assert all("no text" in f["reason"] for f in out["failures"])
    assert [r["self_check"]["passed"] for r in logdb.read_since(common.LOG_DIR, "quality_checks", days=1)] == [0]
