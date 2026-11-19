"""Every text griot shows a person or an agent ends on its next step.

griot-interaction-policy, section 2: what happened, why, and the next step
LAST, as a command that can be pasted. This is the mechanical guard over the
texts the suite can reach: every deliberate error class (griot.GriotError
and its subclasses, each built through the real code that raises it), the
named texts (notices, the golden-set mode notes, the missing-credential
refusal), the doctor's fixes, and the CLI errors that only a real process
prints. A new GriotError subclass fails test_every_griot_error_is_registered
until it is added to REGISTRY with the texts it really carries.

The rules, decided from the policy and the texts griot already writes:

1. The last line. The last non-empty line of a text is two spaces and a
   command whose first word is one of COMMAND_WORDS (a Python interpreter
   given by its path counts: `/Users/you/.venv/bin/python -m pip ...` is the
   upgrade inside a virtual environment). That is how every griot warning
   ends (notices.prose), it pastes, and a host that reads the last lines of
   stderr as the reason (jobs.py) still finds a command there.
   Two kinds of text may end otherwise, each marked in the registry:
   - AGENT: a text only an agent reads (an MCP tool error, note, message or
     reason) may end with a sentence that names its step: a backticked
     command, a `griot_*` tool or `/mcp`. An agent calls tools rather than a
     shell, and a client shows such a text as one paragraph.
   - ARGUMENT: a refusal of an argument may end with what a valid argument
     is (the valid values, the range, the form): the corrected argument is
     the next step, and the command to run again is the caller's own.
2. No command after a comment. No line holds a shell comment (whitespace,
   '#', a space) followed by a word of COMMAND_WORDS: pasted, the command
   after it never runs. A reference such as '#169' is not a comment. A doctor
   fix holds no comment at all: _with_condition appends the next condition's
   command after it, and a comment would swallow that one too.
3. Length. A text an agent can read (every GriotError: the MCP server hands
   its text to the agent; the MCP notes and reasons) is at most 2,000
   characters: a client cuts long results, and the end, where the step is,
   is what gets lost.
"""

import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import griot
from griot import cli, common, doctor, golden_set, mcp_server, notices, repos

COMMAND_WORDS = frozenset({"griot", "git", "chmod", "rm", "mv", "export", "unset", "pipx", "uv", "python3", "direnv"})
AGENT_TEXT_LIMIT = 2000
TERMINAL, AGENT, ARGUMENT = "terminal", "agent", "argument"

_COMMENT = re.compile(r"(?:^|\s)#\s+(.*)$")


def _is_command_word(word: str) -> bool:
    word = word.strip("`'\"()")
    return word in COMMAND_WORDS or bool(re.fullmatch(r"python3?(\.[0-9]+)?", os.path.basename(word)))


def _is_command(line: str) -> bool:
    words = line.split()
    return bool(words) and _is_command_word(words[0])


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _names_a_step(sentence: str) -> bool:
    backticked = re.findall(r"`([^`]+)`", sentence)
    return (any(_is_command(command) for command in backticked) or bool(re.search(r"\bgriot_[a-z_]+", sentence))
            or "/mcp" in sentence)


def _comment_violations(text: str, *, any_comment: bool = False) -> list[str]:
    found = []
    for line in text.splitlines():
        match = _COMMENT.search(line)
        if match is None:
            continue
        if any_comment or any(_is_command_word(word) for word in match.group(1).split()):
            found.append(f"a '#' comment on {line!r}")
    return found


def violations(text: str, kind: str = TERMINAL, *, agent_reads: bool = False) -> list[str]:
    """What breaks the rules above in `text`, as phrases (empty: none)."""
    found = _comment_violations(text)
    last = _last_line(text)
    if not (last.startswith("  ") and not last.startswith("   ") and _is_command(last)):
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", last.strip()) if s]
        if kind == AGENT and sentences and _names_a_step(sentences[-1]):
            pass
        elif kind == ARGUMENT and last.strip():
            pass
        else:
            found.append(f"the last line is not a command: {last!r}")
    if (agent_reads or kind == AGENT) and len(text) > AGENT_TEXT_LIMIT:
        found.append(f"{len(text)} characters, more than {AGENT_TEXT_LIMIT} for an agent")
    return found


def fix_violations(fix: str | None) -> list[str]:
    """A doctor fix: every line a command that pastes as it is (no
    placeholder), no comment at all."""
    if fix is None:
        return []
    found = _comment_violations(fix, any_comment=True)
    found += [f"a fix line that is not a command: {line!r}" for line in fix.splitlines()
              if line.strip() and not _is_command(line)]
    # A placeholder does not paste: the shell reads `<name>` as a redirect.
    # One is left on purpose: the path of a repository to register is one
    # only the person knows (common.REGISTER_COMMAND), and pasted as it is
    # the shell refuses it without running anything.
    found += [f"a placeholder in {line!r}" for line in fix.splitlines()
              if re.search(r"<[^>\s]+>", line) and line != common.REGISTER_COMMAND]
    return found


# --- the rules themselves ------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Something failed.\n  griot doctor",
    "Something failed for the reasons in #169.\n  griot index all",
    "Upgrade it:\n  /Users/you/.venv/bin/python -m pip install --upgrade griot-rag",
])
def test_a_text_ending_on_a_command_passes(text):
    assert violations(text) == []


@pytest.mark.parametrize("text, kind", [
    ("Something failed.\n  griot index all   # then griot index keywords", TERMINAL),
    ("Something failed. Run `griot doctor`.", TERMINAL),
    ("Something failed.", AGENT),
    ("Something failed.\n  " + "x" * AGENT_TEXT_LIMIT, AGENT),
])
def test_a_text_that_breaks_a_rule_fails(text, kind):
    assert violations(text, kind) != []


def test_the_agent_limit_is_exactly_2000_characters():
    ending = "Something failed.\n  griot doctor"
    at_the_limit = "x" * (AGENT_TEXT_LIMIT - len(ending)) + ending
    assert len(at_the_limit) == 2000 and violations(at_the_limit, AGENT) == []
    assert violations("x" + at_the_limit, AGENT) != []
    # A text a person reads only is not held to it; one an agent may read is.
    assert violations("x" + at_the_limit) == []
    assert violations("x" + at_the_limit, agent_reads=True) != []


def test_an_agent_text_may_end_on_a_sentence_naming_a_tool_or_command():
    assert violations("Nothing is indexed. Call griot_index_repo first.", AGENT) == []
    assert violations("Nothing is indexed (`griot index all`).", AGENT) == []
    assert violations("x" * (AGENT_TEXT_LIMIT - 20) + " Call griot_stats.", AGENT) == []


def test_a_fix_holds_no_comment_and_only_commands():
    assert fix_violations("griot auth list\ngriot index all") == []
    assert fix_violations("griot index all   # and read what it says") != []
    assert fix_violations("check the token, then griot index all") != []
    assert fix_violations("griot config set <name> <value>") != []


# --- every deliberate error class ----------------------------------------------------------------


def _raised(call) -> str:
    """The text of the error `call` raises on purpose. A tool's GriotError
    reaches its caller as the SDK's ToolError, with the same text."""
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        call()
    except (griot.GriotError, ToolError) as e:
        return str(e)
    raise AssertionError("nothing was raised")


def _refusal(call, adding):
    """A refused add or remove as every surface says it: repos.refusal_text()
    puts the next command after the error's own text."""
    try:
        call()
    except griot.GriotError as e:
        return repos.refusal_text(e, repos.next_after_refused(e, adding=adding))
    raise AssertionError("nothing was raised")


def _usage_not_a_directory(mp, tmp):
    return _refusal(lambda: repos.check_add(str(tmp / "not-there")), adding=True)


def _usage_not_registered(mp, tmp):
    mp.setattr(repos, "_load", lambda: ["/Users/you/code/app"])
    return _refusal(lambda: repos.check_remove("/Users/you/code/other"), adding=False)


def _profiles_delete(profile):
    """The refusal as `griot profiles delete` prints it: the CLI adds the
    next command where the shared sentence does not end on one."""
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert cli._cmd_profiles_delete(SimpleNamespace(profile=profile)) == 2
    return err.getvalue()


def _usage_unknown_profile(mp, tmp):
    return _profiles_delete("no-such-profile")


def _usage_no_collection(mp, tmp):
    other = next(name for name in common.EMBED_PROFILES if name != common.ACTIVE_PROFILE_NAME)
    mp.setattr(common, "collection_exists", lambda collection: False)
    return _profiles_delete(other)


def _active_profile(mp, tmp):
    return _profiles_delete(common.ACTIVE_PROFILE_NAME)


def _config_amount(mp, tmp):
    mp.setenv("GRIOT_SPEND_CEILING_USD", "nan")
    return _raised(lambda: common._amount_env("GRIOT_SPEND_CEILING_USD", "3.0"))


def _config_count(mp, tmp):
    mp.setenv("GRIOT_MCP_SEARCH_LIMIT", "many")
    return _raised(lambda: common._count_env("GRIOT_MCP_SEARCH_LIMIT", "10"))


def _config_dangling_link(mp, tmp):
    link = tmp / "config" / "griot" / ".env"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(tmp / "gone" / "deeper" / ".env")
    mp.setattr(common, "ENV_PATH", link)
    return _raised(common._env_file_behind_links)


def _config_server_not_narrowed(mp, tmp):
    mp.setattr(common, "ENVIRONMENT_WAS_NARROWED", False)
    return _raised(lambda: mcp_server.main([]))


def _gemini_missing(mp, tmp):
    mp.setattr(common, "GEMINI_TOKEN", None)
    mp.setattr(common, "_keychain_value", lambda name: None)
    return _raised(common._require_gemini_token)


def _openai_missing(mp, tmp):
    mp.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    mp.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    mp.setattr(common, "credential", lambda name: None)
    return _raised(lambda: common._embed_texts_openai_compatible(["x"]))


def _chat_missing(mp, tmp):
    name = next(n for n, p in common.CHAT_PROFILES.items() if p["backend"] == "openai_compatible_chat")
    mp.setattr(common, "ACTIVE_CHAT_PROFILE_NAME", name)
    mp.setattr(common, "ACTIVE_CHAT_PROFILE", common.CHAT_PROFILES[name])
    mp.setattr(common, "credential", lambda env: None)
    return _raised(lambda: common._chat_completion_openai_compatible("x"))


def _spend_daily(mp, tmp):
    from griot import logdb
    mp.setattr(logdb, "read_spend_today", lambda *a: 5.0)
    mp.setattr(common, "SPEND_CEILING_USD", 3.0)
    return _raised(common.check_spend_ceiling)


def _spend_velocity(mp, tmp):
    from griot import logdb
    mp.setattr(logdb, "read_spend_today", lambda *a: 0.0)
    mp.setattr(logdb, "read_spend_velocity", lambda *a: 9.0)
    return _raised(common.check_spend_ceiling)


def _spend_not_an_amount(mp, tmp):
    return _raised(lambda: common.record_spend(float("nan")))


def _no_shard(mp):
    def load(path):
        raise Exception("failed to open WAL: WouldBlock")
    mp.setattr(common, "qe", SimpleNamespace(EdgeShard=SimpleNamespace(load=load)))


def _busy_load(mp, tmp):
    _no_shard(mp)
    return _raised(lambda: common._load_shard(tmp, retry=False))


def _busy_load_retried(mp, tmp):
    _no_shard(mp)
    mp.setattr(common, "_LOCK_RETRY_DELAYS", ())
    return _raised(lambda: common._load_shard(tmp, retry=True))


def _busy_opening(mp, tmp):
    held, release = threading.Event(), threading.Event()

    def hold():
        with common._client_lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(10)
    try:
        return _raised(lambda: common.get_client(wait=False))
    finally:
        release.set()
        holder.join()


def _busy_cli(role):
    def build(mp, tmp):
        holders = [{"pid": 4242, "command": "griot x", "started": None, "role": role}] if role else []
        mp.setattr(common, "find_collection_holders", lambda path: holders)
        error = common.CollectionBusyError("griot_x", tmp, "held")
        return cli._busy_message(error, ["search", "how is it done", "--profile", "jina-code"])
    return build


def _filter_string(mp, tmp):
    return _raised(lambda: common._filter_names("repos", "app"))


def _filter_too_many(mp, tmp):
    return _raised(lambda: common._filter_names("repos", [f"r{i}" for i in range(common.SEARCH_FILTER_MAX_VALUES + 1)]))


def _filter_not_names(mp, tmp):
    return _raised(lambda: common._filter_names("repos", [""]))


def _filter_unknown_kind(mp, tmp):
    return _raised(lambda: common.checked_search_filters(None, ["nope"]))


def _filter_not_indexed(registered):
    def build(mp, tmp):
        mp.setattr(common, "repository_is_indexed", lambda client, repo: False)
        mp.setattr(common, "load_repos", lambda: ["/Users/you/code/app"])
        return _raised(lambda: common.search_filter(None, ["app" if registered else "other"], []))
    return build


def _unknown_mode(mp, tmp):
    return _raised(lambda: common._checked_mode("fuzzy"))


def _query_without_words(mp, tmp):
    mp.setattr(common, "_keyword_model", lambda: SimpleNamespace(embed_query=lambda q: SimpleNamespace(indices=[])))
    return _raised(lambda: common._keyword_query("the of"))


def _keyword_unavailable(mp, tmp):
    return str(common.keyword_search_unavailable("griot_x"))


def _embedding_failed(mp, tmp):
    mp.setattr(common, "embed_texts", lambda texts: [None for _ in texts])
    common._note_embedding_failure(None)
    return _raised(lambda: common.search("anything"))


def _mcp_argument(mp, tmp):
    return _raised(lambda: mcp_server.griot_golden_set_suggest(str(tmp), limit=0))


def _nothing_indexed_quality(mp, tmp):
    mp.setattr(common, "collection_exists", lambda collection: False)
    return _raised(lambda: mcp_server.griot_quality_check())


def _nothing_indexed_audit(mp, tmp):
    from griot import redaction
    mp.setattr(redaction, "audit_index", lambda **k: {"indexed": False, "scanned": 0, "places": []})
    return _raised(lambda: mcp_server.griot_audit())


def _unreadable_golden_set(mp, tmp):
    return str(mcp_server._corrupt_golden_set(ValueError("Expecting value")))


def _unreadable_repos(mp, tmp):
    return str(mcp_server.UnreadableFile(repos.unreadable_sentence(ValueError("Expecting value"))))


def _check_incomplete(mp, tmp):
    from griot import quality_check
    mp.setattr(common, "collection_exists", lambda collection: True)
    mp.setattr(quality_check, "run_self_check", lambda collection, sample_size: {
        "sampled": 2, "passed": 2, "failed": 0, "avg_score": 0.99, "failures": []})
    mp.setattr(mcp_server, "_curated_cases_to_run", lambda: ([{"query": "q", "must_include": [{"repo": "r"}]}], None))
    mp.setattr(quality_check, "record_for_trend", lambda *a, **k: None)

    def stops(cases):
        raise common.EmbeddingFailed("The query could not be embedded with profile 'x': the call returned nothing.")
    mp.setattr(quality_check, "run_golden_set", stops)
    return _raised(lambda: mcp_server.griot_quality_check())


def _search_timed_out(mp, tmp):
    started = threading.Event()

    def slow():
        started.wait(5)
        return []
    mp.setattr(common, "SEARCH_TIMEOUT_SECONDS", 0.05)
    try:
        return _raised(lambda: mcp_server._search_within_deadline(slow))
    finally:
        started.set()


def _installation_removed(original):
    def build(mp, tmp):
        mp.setattr(mcp_server, "_installation_removed", lambda: "/Users/you/.local/pipx/venvs/griot-rag")
        return str(mcp_server._explained_if_installation_removed(original))
    return build


# Every GriotError subclass, by qualified name, with the texts it carries:
# (label, kind, builder(monkeypatch, tmp_path) -> text).
REGISTRY = {
    "griot.GriotError": [],  # the base: raised only through its subclasses
    "griot.UsageError": [
        ("repos add: not a directory", TERMINAL, _usage_not_a_directory),
        ("repos remove: not registered", TERMINAL, _usage_not_registered),
        ("profiles delete: unknown profile", TERMINAL, _usage_unknown_profile),
        ("profiles delete: never indexed", TERMINAL, _usage_no_collection),
    ],
    "griot.cli.ActiveProfileError": [("profiles delete: the active profile", TERMINAL, _active_profile)],
    "griot.ConfigurationError": [
        ("an amount that is not one", TERMINAL, _config_amount),
        ("a count that is not one", TERMINAL, _config_count),
        ("a .env link that leads nowhere", TERMINAL, _config_dangling_link),
        ("a server that read its configuration first", TERMINAL, _config_server_not_narrowed),
    ],
    # Raised while common.py is imported: read from a real process below
    # (test_an_unknown_profile_in_the_environment_ends_on_a_command).
    "griot.UnknownEmbedProfile": [],
    "griot.common.CredentialMissing": [
        ("gemini", TERMINAL, _gemini_missing),
        ("an openai-compatible embedding profile", TERMINAL, _openai_missing),
        ("a chat profile", TERMINAL, _chat_missing),
    ],
    "griot.common.SpendCircuitBreakerError": [
        ("the daily ceiling", TERMINAL, _spend_daily),
        ("the velocity ceiling", TERMINAL, _spend_velocity),
        ("a cost that is not an amount", TERMINAL, _spend_not_an_amount),
    ],
    "griot.common.CollectionBusyError": [
        ("an open that collided", AGENT, _busy_load),
        ("an open that collided after retries", AGENT, _busy_load_retried),
        ("another call opening it", AGENT, _busy_opening),
        ("the CLI's message, an MCP server", TERMINAL, _busy_cli("mcp")),
        ("the CLI's message, an index run", TERMINAL, _busy_cli("index")),
        ("the CLI's message, another holder", TERMINAL, _busy_cli(None)),
    ],
    "griot.common.SearchFilterError": [
        ("a filter that is a string", ARGUMENT, _filter_string),
        ("too many names", ARGUMENT, _filter_too_many),
        ("a name that is empty", ARGUMENT, _filter_not_names),
        ("an unknown source type", ARGUMENT, _filter_unknown_kind),
        ("a registered repository not indexed", TERMINAL, _filter_not_indexed(True)),
        ("an unknown repository", TERMINAL, _filter_not_indexed(False)),
        ("an unknown mode", ARGUMENT, _unknown_mode),
        ("a query with no word to match", ARGUMENT, _query_without_words),
    ],
    "griot.common.SearchModeUnavailable": [("keyword search not built", TERMINAL, _keyword_unavailable)],
    "griot.common.EmbeddingFailed": [("the query was not embedded", TERMINAL, _embedding_failed)],
    "griot.mcp_server.ArgumentRefused": [
        ("a limit below 1", ARGUMENT, _mcp_argument),
    ],
    "griot.mcp_server.NothingIndexed": [
        ("quality check on nothing", AGENT, _nothing_indexed_quality),
        ("audit of nothing", AGENT, _nothing_indexed_audit),
    ],
    "griot.mcp_server.UnreadableFile": [
        ("the golden set", AGENT, _unreadable_golden_set),
        ("repos.json", AGENT, _unreadable_repos),
    ],
    "griot.mcp_server.CheckIncomplete": [("the golden set stopped", AGENT, _check_incomplete)],
    "griot.mcp_server.SearchTimedOut": [("griot_search timed out", AGENT, _search_timed_out)],
    "griot.mcp_server.InstallationRemoved": [
        ("over a griot error", AGENT, _installation_removed(common.EmbeddingFailed("The query could not be embedded."))),
        ("over a crash", AGENT, _installation_removed(OSError(2, "No such file", "/Users/you/.venv/x"))),
    ],
}


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def _qualified(cls) -> str:
    module = cls.__module__
    return f"griot.{cls.__name__}" if module == "griot" else f"{module}.{cls.__name__}"


def test_every_griot_error_is_registered():
    known = {_qualified(griot.GriotError)} | {_qualified(c) for c in _subclasses(griot.GriotError)
                                              if c.__module__.startswith("griot")}
    assert sorted(known - set(REGISTRY)) == [], "add each new GriotError to REGISTRY with the texts it carries"


@pytest.mark.parametrize("label, kind, build", [entry for entries in REGISTRY.values() for entry in entries],
                         ids=[entry[0] for entries in REGISTRY.values() for entry in entries])
def test_each_error_ends_on_its_next_step(label, kind, build, monkeypatch, tmp_path):
    text = build(monkeypatch, tmp_path)
    assert violations(text, kind, agent_reads=True) == [], text


# --- the named texts -----------------------------------------------------------------------------


def _notice_builders():
    report = {"repo": "app", "path": "/Users/you/code/app", "behind": True, "commits_behind": 3,
              "behind_sources": ["commits"]}
    return [
        notices.behind(report),
        notices.keyword_vectors_missing("griot_x"),
        notices.quality_older_than_index("2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00"),
        notices.legacy_env([("RAG_EMBED_PROFILE", "GRIOT_EMBED_PROFILE"), ("RAG_X", "GRIOT_X")]),
        notices.legacy_layout([("/Users/you/code/griot/.env", "/Users/you/.config/griot/.env")]),
    ]


@pytest.mark.parametrize("index", range(5))
def test_every_notice_ends_on_its_command(index):
    notice = _notice_builders()[index]
    assert violations(notices.prose(notice)) == []
    assert fix_violations(notice.action) == []


@pytest.mark.parametrize("count", [1, 3])
def test_the_footer_ends_on_its_command(count):
    text = notices.footer_text(count)
    assert violations(text) == [], text
    assert len(text.splitlines()) == 2, "one block: a count and the command"


def test_the_golden_set_mode_notes_end_on_their_command():
    assert violations(golden_set.vector_fallback_note(notices.keyword_vectors_missing("griot_x")), AGENT) == []
    text = golden_set.unreadable_config_note("griot_x")
    assert "griot_x" in text
    assert violations(text, agent_reads=True) == [], text


def test_the_missing_credential_texts_end_on_their_command(monkeypatch, capsys):
    text = common.missing_credential_text("GEMINI_TOKEN", "paid profile 'gemini'")
    assert text.endswith("\n  griot auth set gemini")
    assert violations(text, agent_reads=True) == []
    unknown = common.missing_credential_text("SOME_CUSTOM_KEY", "paid profile 'custom'")
    assert unknown.endswith("\n  griot doctor")

    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(common, "credential", lambda name: None)
    assert common.refuse_missing_credential() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and violations(captured.err) == []


def test_a_platform_token_that_is_missing_ends_on_its_command(monkeypatch):
    from griot import platforms
    monkeypatch.setattr(common, "credential", lambda name: None)
    with pytest.raises(ValueError) as error:
        platforms._require_token("GITHUB_TOKEN")
    assert str(error.value).endswith("\n  griot auth set github")
    assert "not found in the environment" not in str(error.value)


def test_the_auth_guidance_has_no_command_after_a_comment():
    guidance = mcp_server.griot_auth_guidance()
    for key, value in guidance.items():
        if isinstance(value, str):
            assert _comment_violations(value) == [], key
    assert guidance["how_to_set"] == "griot auth set <provider>"


def test_an_index_run_that_dies_at_once_says_how_to_see_why(monkeypatch, tmp_path):
    from griot import jobs

    class Dead:
        pid, returncode = 1, 3

        def poll(self):
            return 3

    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **k: Dead())
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    monkeypatch.setattr(jobs, "index_job_refusal", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_repo_name_if_uniquely_registered", lambda path: "app")
    started = jobs.start_index_job(str(tmp_path), release=lambda: None)
    assert started["started"] is False
    assert violations(started["reason"], agent_reads=True) == [], started["reason"]
    assert _last_line(started["reason"]).strip().startswith("griot index all --repo app")


# --- griot doctor's fixes ------------------------------------------------------------------------


def _doctor_states():
    """(label, a callable(monkeypatch, tmp_path) -> the rows to check)."""
    from griot import freshness

    def unknown_profile(mp, tmp):
        error = griot.UnknownEmbedProfile("unknown", "bogus", ["jina-code", "openai-small"])
        return [doctor.check_settings(tmp / ".env", error)]

    def broken_setting(mp, tmp):
        env = tmp / ".env"
        env.write_text("GRIOT_SPEND_CEILING_USD=lots\n")
        env.chmod(0o600)
        return [doctor.check_settings(env)]

    def dangling_link(mp, tmp):
        env = tmp / ".env"
        env.symlink_to(tmp / "gone" / "x" / ".env")
        return [doctor.check_settings(env)]

    def open_before(mp, tmp):
        env = tmp / ".env"
        env.write_text("")
        env.chmod(0o600)
        return [doctor.check_settings(env, None, 0o644)]

    def open_now(mp, tmp):
        env = tmp / ".env"
        env.write_text("")
        env.chmod(0o644)
        return [doctor.check_settings(env)]

    def overridden_and_in_file(mp, tmp):
        mp.setattr(common, "keychain_status", lambda: {"available": True, "backend": "macOS", "installed": True})
        mp.setattr(common, "credential_env_vars", lambda: {"openai": "GRIOT_OPENAI_API_KEY", "github": "GITHUB_TOKEN"})
        origins = {"GRIOT_OPENAI_API_KEY": {"source": "environment", "stored": "file", "shadows_stored": True,
                                            "exported_in": ["~/.zshrc:3"], "direnv_in": []},
                   "GITHUB_TOKEN": {"source": "file", "stored": "file", "shadows_stored": False,
                                    "exported_in": [], "direnv_in": []}}
        mp.setattr(common, "credential_origin", lambda var: origins[var])
        return [doctor.check_credentials(common)]

    def dead_run(mp, tmp):
        mp.setattr(common, "get_index_status", lambda **k: {
            "points_count": 12, "points_error": None, "repositories": [], "keyword_search": False,
            "collection": "griot_x", "last_indexed": {"timestamp": "2026-10-01T00:00:00+00:00", "error": "boom"}})
        return [doctor.check_index(common)]

    def refused(cause):
        def build(mp, tmp):
            repo = tmp / "app"
            repo.mkdir()
            report = {"repo": "app", "path": str(repo), "behind": False, "commits_behind": 0, "behind_sources": [],
                      "missing_sources": [], "platform_refused": True, "sources": {}}
            if cause == "not_found":
                report["platform_not_found"] = True
            elif cause == "other":
                report["platform_refusal_causes"] = {"other": ["503"]}
            elif cause == "mixed":
                report["platform_refusal_causes"] = {"token": ["401"], "other": ["503"]}
            mp.setattr(common, "load_repos", lambda: [str(repo)])
            mp.setattr(freshness, "repository_freshness", lambda *a, **k: [report])
            return [doctor.check_repositories(common)]
        return build

    def gone_repositories(mp, tmp):
        mp.setattr(common, "load_repos", lambda: ["/Users/you/code/gone-a", "/Users/you/code/gone-b"])
        return [doctor.check_repositories(common)]

    def server_environment(mp, tmp):
        from griot import config
        setting = next(s for s in config.SETTINGS if s.variable == "GRIOT_SPEND_CEILING_USD")
        mp.setattr(common, "ENVIRONMENT_BEFORE_ENV_FILE", {setting.variable: "9"})
        mp.setattr(config, "measured_from_environment", lambda s, raw: ("it raises the ceiling", None))
        return [doctor.check_server_environment(common)]

    def release(candidates, development=None):
        def build(mp, tmp):
            mp.setattr(griot, "__version__", "0.2.1")
            mp.setattr(doctor, "latest_release", lambda: "9.0.0")
            mp.setattr(common, "update_check_enabled", lambda: True)
            mp.setattr(doctor, "development_install", lambda: development)
            prefix = ("/Users/you/.local/pipx/venvs/griot-rag" if candidates == 1 else sys.base_prefix)
            mp.setattr(sys, "prefix", prefix)
            return [doctor.check_release(common)]
        return build

    def spend(mp, tmp):
        mp.setattr(common, "get_spend_today", lambda: 3.0)
        mp.setattr(common, "SPEND_CEILING_USD", 3.0)
        return [doctor.check_spend(common)]

    def everything(mp, tmp):
        return doctor.run_checks()

    return [
        ("an unknown profile", unknown_profile), ("a broken setting", broken_setting),
        ("a .env link to nowhere", dangling_link), ("a file that was open", open_before),
        ("a file that is open", open_now), ("credentials overridden and in the file", overridden_and_in_file),
        ("a dead run", dead_run), ("a token refusal", refused("token")), ("a 404", refused("not_found")),
        ("a platform failure", refused("other")), ("mixed refusals", refused("mixed")),
        ("repositories gone", gone_repositories), ("a server environment ignored", server_environment),
        ("a newer release, one installer", release(1)), ("a newer release, three candidates", release(3)),
        ("a newer release, a development install", release(1, "an editable install of file:///Users/you/code/griot")),
        ("spend at the ceiling", spend), ("every check of a fresh install", everything),
    ]


@pytest.fixture
def _offline_doctor(monkeypatch):
    from griot import harnesses
    import requests
    monkeypatch.setattr(harnesses, "HARNESSES", [])

    def offline():
        raise requests.ConnectionError("the suite does not ask PyPI")
    monkeypatch.setattr(doctor, "latest_release", offline)


@pytest.mark.parametrize("label, build", _doctor_states(), ids=[label for label, _ in _doctor_states()])
def test_every_doctor_fix_is_one_command_per_line(label, build, monkeypatch, tmp_path, _offline_doctor):
    rows = build(monkeypatch, tmp_path)
    for row in rows:
        assert fix_violations(row["fix"]) == [], (row["check"], row["fix"])


def test_a_platform_refusal_ends_on_one_command_per_arrow(monkeypatch, tmp_path, _offline_doctor):
    rows = dict(_doctor_states())["a token refusal"](monkeypatch, tmp_path)
    assert rows[0]["fix"] == "griot auth list\ngriot index platform"


def test_several_upgrade_candidates_are_one_per_line(monkeypatch, tmp_path, _offline_doctor):
    row = dict(_doctor_states())["a newer release, three candidates"](monkeypatch, tmp_path)[0]
    assert len(row["fix"].splitlines()) == 3
    assert "run the one that installed griot" in row["detail"]


def test_a_development_install_ends_on_pulling_the_checkout(monkeypatch, tmp_path, _offline_doctor):
    row = dict(_doctor_states())["a newer release, a development install"](monkeypatch, tmp_path)[0]
    assert row["fix"] == "git -C /Users/you/code/griot pull"


# --- the CLI, through real processes -------------------------------------------------------------


def _griot(tmp_path, *args, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GRIOT_", "RAG_", "XDG_", "NO_COLOR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
           and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update(GRIOT_CONFIG_DIR=str(tmp_path / "isolated" / "config"), GRIOT_DATA_DIR=str(tmp_path / "isolated" / "data"),
               GRIOT_UPDATE_CHECK="false", PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "griot.cli", *args], env=env, capture_output=True, text=True,
                          timeout=180, stdin=subprocess.DEVNULL)


def test_an_unknown_profile_in_the_environment_ends_on_a_command(tmp_path):
    done = _griot(tmp_path, "repos", "list", extra_env={"GRIOT_EMBED_PROFILE": "bogus"})
    assert done.returncode == 2 and done.stdout == ""
    assert violations(done.stderr) == [], done.stderr


def test_update_says_what_kept_pypi_from_answering_then_the_command(tmp_path):
    # Port 9 (discard) is closed on a machine that runs no such service: the
    # proxy refuses the connection, and nothing leaves the machine.
    done = _griot(tmp_path, "update", extra_env={"HTTPS_PROXY": "http://127.0.0.1:9"})
    assert done.returncode == 1 and done.stdout == ""
    assert "the proxy in HTTPS_PROXY refused the connection" in done.stderr, done.stderr
    assert "Once the proxy in HTTPS_PROXY answers, run it again:\n  griot update\n" in done.stderr, done.stderr
    assert violations(done.stderr) == [], done.stderr


def test_a_stopped_index_all_says_what_did_not_run_and_how_to_resume(tmp_path):
    # A path that is not a directory: the code source refuses it (exit 1),
    # and nothing is read or embedded.
    done = _griot(tmp_path, "index", "all", "--path", str(tmp_path / "gone"), "--sources", "code,commits", "--dry-run")
    assert done.returncode != 0
    assert "commits did not run" in done.stderr, done.stderr
    last = _last_line(done.stderr).strip()
    assert shlex.split(last)[:3] == ["griot", "index", "all"] and "--sources" in last, done.stderr
    assert violations(done.stderr) == [], done.stderr


def test_a_missing_credential_on_search_is_one_error_not_a_traceback(tmp_path):
    done = _griot(tmp_path, "search", "how is it done", "--mode", "vector", "--profile", "openai-small")
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.endswith("\n  griot auth set openai\n"), done.stderr


# --- through the protocol ------------------------------------------------------------------------


async def _call(tool, arguments):
    from mcp.client.client import Client
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    return result


def _text(result) -> str:
    return " ".join(getattr(part, "text", "") for part in result.content)


@pytest.mark.anyio
async def test_a_search_filter_refusal_reads_as_received():
    result = await _call("griot_search", {"query": "how", "source_types": ["nope"]})
    assert result.is_error is True
    assert "The kinds of source are:" in _text(result)
    assert violations(_text(result), ARGUMENT, agent_reads=True) == []


@pytest.mark.anyio
async def test_a_repository_not_indexed_reads_with_its_command_as_received(monkeypatch):
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(common, "get_client", lambda **k: None)
    monkeypatch.setattr(common, "repository_is_indexed", lambda client, repo: False)
    monkeypatch.setattr(common, "load_repos", lambda: ["/Users/you/code/app"])
    result = await _call("griot_search", {"query": "how", "repos": ["app"]})
    assert result.is_error is True
    assert _text(result).endswith("\n  griot index all --repo app"), _text(result)
    assert violations(_text(result), agent_reads=True) == []


@pytest.mark.anyio
async def test_a_missing_credential_reads_with_its_command_as_received(monkeypatch):
    monkeypatch.setattr(common, "ACTIVE_PROFILE_NAME", "openai-small")
    monkeypatch.setattr(common, "ACTIVE_PROFILE", common.EMBED_PROFILES["openai-small"])
    monkeypatch.setattr(common, "credential", lambda name: None)
    monkeypatch.setattr(common, "collection_exists", lambda collection: True)
    monkeypatch.setattr(common, "get_client", lambda **k: None)
    result = await _call("griot_search", {"query": "how", "mode": "vector"})
    assert result.is_error is True
    # The SDK puts "Error executing tool <name>: " before griot's text.
    assert _text(result).endswith("paid profile 'openai-small' needs GRIOT_OPENAI_API_KEY, and it is not set. "
                                  "Nothing was sent. A credential is set in a terminal:\n  griot auth set openai")


@pytest.mark.anyio
async def test_the_auth_guidance_reads_without_a_comment(monkeypatch):
    result = await _call("griot_auth_guidance", {})
    guidance = result.structured_content
    assert guidance["how_to_set"] == "griot auth set <provider>"
    assert "where_it_is_stored" in guidance and "griot auth list" in guidance["where_it_is_stored"]
    for value in guidance.values():
        if isinstance(value, str):
            assert _comment_violations(value) == []
