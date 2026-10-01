"""`griot assist install` can offer to pre-approve griot's read-only tools.

An agent that has to ask before every search mostly does not search: each
call waited on a person, and in real sessions that wait was most of what a
search cost. The harness keeps a list of tools that may run without asking;
this adds griot's read-only ones to it.

It widens what an agent may do without a person, so it is classified by that
effect: a typed "y" at a terminal, the exact rules shown first, no flag that
answers for the person, never from an MCP tool. And it edits a settings file
that belongs to the user: nothing in it but the allow list may change, and a
file griot cannot read as settings is left alone."""

import json
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import harnesses, mcp_server


@pytest.fixture
def claude():
    return next(h for h in harnesses.HARNESSES if h.id == "claude-code")


@pytest.fixture
def terminal(monkeypatch):
    """An interactive terminal whose person answers what the test says."""
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    asked = []

    def answer(text):
        monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or text)
        return asked

    return answer


def _settings(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _allowed(path: Path) -> list[str]:
    return json.loads(path.read_text())["permissions"]["allow"]


# --- which tools --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_tools_the_server_itself_marks_read_only_are_offered():
    """The list comes from the server, not from a copy of it kept here."""
    async with Client(mcp_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    offered = mcp_server.tools_safe_to_preapprove()
    assert offered and offered == sorted(offered)
    for name in offered:
        assert tools[name].annotations.read_only_hint is True, name
        assert not (tools[name].meta or {}).get("anthropic/requiresUserInteraction"), name
    assert "griot_search" in offered and "griot_index_status" in offered


def test_nothing_that_changes_state_is_offered():
    offered = set(mcp_server.tools_safe_to_preapprove())
    assert not offered & {"griot_repos_add", "griot_repos_remove", "griot_profiles_delete", "griot_golden_set_add",
                          "griot_golden_set_remove", "griot_assist_install", "griot_index_repo"}


def test_the_quality_check_keeps_asking_although_it_is_marked_read_only():
    """It embeds one query per sampled point, billed on a paid profile, and
    records a trend point: more than a read, and more than one search costs."""
    assert "griot_quality_check" not in mcp_server.tools_safe_to_preapprove()


def test_the_rules_are_in_the_form_the_harness_matches(claude):
    rules = harnesses.tool_rules(claude)
    assert "mcp__griot__griot_search" in rules
    assert all(rule.startswith("mcp__griot__griot_") for rule in rules)
    assert len(rules) == len(mcp_server.tools_safe_to_preapprove())


# --- where --------------------------------------------------------------------------------


def test_global_scope_is_the_users_settings_and_local_scope_the_personal_project_file(claude, tmp_path):
    assert harnesses.settings_file(claude, "global", home=tmp_path, cwd=tmp_path / "p") == tmp_path / ".claude" / "settings.json"
    assert harnesses.settings_file(claude, "local", home=tmp_path, cwd=tmp_path / "p") == \
        tmp_path / "p" / ".claude" / "settings.local.json", "the personal file, not the one a team commits"


def test_a_harness_without_a_known_settings_file_is_not_offered(tmp_path, terminal):
    opencode = next(h for h in harnesses.HARNESSES if h.id == "opencode")
    terminal("y")
    assert harnesses.offer_tool_approval(opencode, "global", home=tmp_path, cwd=tmp_path) == "n/a"


# --- the question -------------------------------------------------------------------------


def test_it_shows_the_file_and_every_rule_before_asking(claude, tmp_path, terminal, capsys):
    asked = terminal("n")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "declined"
    out = capsys.readouterr().out
    assert str(_settings(tmp_path)) in out
    for rule in harnesses.tool_rules(claude):
        assert rule in out
    assert "without asking" in out and "every project" in out
    assert len(asked) == 1 and "[y/N]" in asked[0]
    assert not _settings(tmp_path).exists(), "a no writes nothing"


def test_it_says_what_a_search_costs_and_what_keeps_asking(claude, tmp_path, terminal, capsys):
    terminal("n")
    harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path)
    out = " ".join(capsys.readouterr().out.split())
    assert "embeds the query" in out and "paid" in out
    # Not "they keep asking": whether the harness asks depends on the user's
    # other rules and mode. What griot can promise is what it adds.
    assert "adds no rule" in out and "quality check" in out and "keep asking" not in out
    assert "do not change your index" in out and "They only read" not in out, "each call is logged: not only a read"


def test_it_says_that_a_rule_names_a_server_not_griot_itself(claude, tmp_path, terminal, capsys):
    """`mcp__griot__...` allows whatever server is called griot, a project's
    own included. The person deciding has to be told."""
    terminal("n")
    harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path)
    out = " ".join(capsys.readouterr().out.split())
    assert "any MCP server named `griot`" in out


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe", "yes please"])
def test_anything_but_a_yes_writes_nothing(claude, tmp_path, terminal, answer):
    terminal(answer)
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "declined"
    assert not _settings(tmp_path).exists()


def test_without_a_terminal_nobody_is_asked_and_nothing_is_written(claude, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "not-interactive"
    assert not _settings(tmp_path).exists()
    assert "in a terminal" in capsys.readouterr().out


def test_told_not_to_ask_it_does_not(claude, tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although told not to"))
    assert harnesses.offer_tool_approval(claude, "global", ask=False, home=tmp_path, cwd=tmp_path) == "skipped"


def test_there_is_no_flag_that_answers_yes():
    with pytest.raises(SystemExit):
        harnesses.main(["install", "--allow-tools"])
    with pytest.raises(SystemExit):
        harnesses.main(["install", "--yes"])


# --- the write ----------------------------------------------------------------------------


def test_a_yes_creates_the_file_with_the_rules(claude, tmp_path, terminal, capsys):
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "created"
    assert _allowed(_settings(tmp_path)) == harnesses.tool_rules(claude)
    assert _settings(tmp_path).stat().st_mode & 0o777 == 0o600, "a settings file griot creates is private"
    out = capsys.readouterr().out
    assert "To undo" in out and str(_settings(tmp_path)) in out


def test_everything_else_in_the_file_is_kept(claude, tmp_path, terminal):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    before = {"model": "opus", "permissions": {"allow": ["Bash(git status)"], "deny": ["Read(./.env)"],
                                               "defaultMode": "plan"},
              "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}, "env": {"A": "ação"}}
    path.write_text(json.dumps(before, indent=4, ensure_ascii=False))
    path.chmod(0o600)
    terminal("yes")

    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "added"

    after = json.loads(path.read_text())
    assert after["permissions"]["allow"] == ["Bash(git status)", *harnesses.tool_rules(claude)], "appended, in order"
    after["permissions"]["allow"] = before["permissions"]["allow"]
    assert after == before, "nothing else changed"
    assert list(after) == list(before), "nor the order of the keys"
    assert path.stat().st_mode & 0o777 == 0o600, "it is the user's file: its mode is kept"
    assert "ação" in path.read_text(), "non-ASCII text is written as it was, not escaped"


def test_rules_already_there_are_not_added_twice(claude, tmp_path, terminal):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    some = harnesses.tool_rules(claude)[:2]
    path.write_text(json.dumps({"permissions": {"allow": some}}))
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "added"
    allowed = _allowed(path)
    assert sorted(allowed) == sorted(harnesses.tool_rules(claude)) and len(allowed) == len(set(allowed))


def test_when_every_rule_is_there_nobody_is_asked(claude, tmp_path, monkeypatch, capsys):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": harnesses.tool_rules(claude)}}))
    before = path.read_text()
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although there is nothing to add"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "current"
    assert path.read_text() == before
    assert "nothing to add" in capsys.readouterr().out


@pytest.mark.parametrize("covering", ["mcp__griot", "mcp__griot__*"])
def test_a_rule_that_already_allows_the_whole_server_is_enough(claude, tmp_path, monkeypatch, covering):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": [covering]}}))
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although the server is allowed"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "current"


@pytest.mark.parametrize("listed", ["deny", "ask"])
def test_a_tool_the_user_denies_or_wants_asked_is_left_out_and_said(claude, tmp_path, terminal, capsys, listed):
    """Their rule says the opposite of ours, and theirs is a decision."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {listed: ["mcp__griot__griot_search"]}}))
    terminal("y")

    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "added"

    allowed = _allowed(path)
    assert "mcp__griot__griot_search" not in allowed and len(allowed) == len(harnesses.tool_rules(claude)) - 1
    assert json.loads(path.read_text())["permissions"][listed] == ["mcp__griot__griot_search"]
    out = capsys.readouterr().out
    assert "Left out" in out and f"`{listed}`" in out
    offered, left = out.split("Left out")[0], out.split("Left out")[1].split("\n")[0]
    assert left.count("mcp__griot__") == 1, "only the one they decided about"
    assert "mcp__griot__griot_search" not in offered, "what is shown as added is what will be added"


@pytest.mark.parametrize("listed", ["deny", "ask"])
def test_a_server_the_user_denies_or_wants_asked_is_not_offered_at_all(claude, tmp_path, monkeypatch, capsys, listed):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {listed: ["mcp__griot"]}}))
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although the user decided otherwise"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "current"
    assert "Left out" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{not json", "[]", '"text"', '{"permissions": []}', '{"permissions": {"allow": "x"}}',
                                     '{"permissions": {"allow": [1]}}', '{"permissions": {"deny": {}}}',
                                     '// a comment\n{"permissions": {}}'])
def test_a_file_that_is_not_settings_as_griot_understands_them_is_left_alone(claude, tmp_path, monkeypatch, capsys, content):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked about a file it cannot edit safely"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "malformed"
    assert path.read_text() == content
    out = capsys.readouterr().out
    assert "NOT touched" in out and "mcp__griot__griot_search" in out, "the rules are given, to add by hand"


def test_a_file_that_is_not_utf8_is_left_alone(claude, tmp_path, monkeypatch):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe{}")
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "malformed"
    assert path.read_bytes() == b"\xff\xfe{}"


def test_a_write_that_fails_leaves_the_file_as_it_was(claude, tmp_path, terminal, monkeypatch, capsys):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"model": "opus"}')
    terminal("y")

    def fails(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(harnesses.os, "replace", fails)
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "failed"
    out = capsys.readouterr().out
    assert "NOT written" in out and "disk full" in out and "mcp__griot__griot_search" in out, "the rules, to add by hand"
    assert path.read_text() == '{"model": "opus"}'
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"], "no temporary file left behind"


def test_a_directory_that_cannot_be_written_is_a_message_not_a_traceback(claude, tmp_path, terminal, capsys):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude").chmod(0o500)
    terminal("y")
    try:
        assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "failed"
    finally:
        (tmp_path / ".claude").chmod(0o700)
    assert "NOT written" in capsys.readouterr().out


def test_a_settings_directory_that_is_a_file_is_a_message_not_a_traceback(claude, tmp_path, terminal, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".claude").write_text("not a directory")
    asked = terminal("y")
    # Known before asking, so nobody is asked: a yes could not change it.
    assert harnesses.offer_tool_approval(claude, "local", home=tmp_path / "home", cwd=project) == "unsafe"
    assert asked == [] and "not a directory" in capsys.readouterr().out
    assert (project / ".claude").read_text() == "not a directory"


def test_text_that_cannot_be_written_back_leaves_the_file_alone(claude, tmp_path, terminal):
    """A lone surrogate is valid in JSON text and cannot be encoded."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    content = '{"note": "\\ud800"}'
    path.write_text(content)
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) in ("failed", "malformed")
    assert path.read_text() == content


def test_a_failed_approval_is_not_a_failed_install(claude, tmp_path, terminal, monkeypatch):
    _quiet_install(monkeypatch)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude").chmod(0o500)
    terminal("y")
    try:
        assert harnesses.cmd_install("global", "claude-code", home=tmp_path) == 0
    finally:
        (tmp_path / ".claude").chmod(0o700)


def test_a_file_with_a_key_given_twice_is_left_alone(claude, tmp_path, monkeypatch, capsys):
    """Read and written back, one of the two values would be gone."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    content = '{"model": "opus", "model": "sonnet", "permissions": {"allow": []}}'
    path.write_text(content)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked about a file it cannot edit safely"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "malformed"
    assert path.read_text() == content and "twice" in capsys.readouterr().out


@pytest.mark.parametrize("mode,reason", [(0o400, "read-only"), (0o000, "cannot be read")])
def test_a_file_the_user_protected_is_left_alone(claude, tmp_path, monkeypatch, capsys, mode, reason):
    """A rename replaces a read-only file without complaint. Someone made it
    read-only for a reason."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"model": "opus"}')
    path.chmod(mode)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked about a file it must not edit"))
    try:
        assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "malformed"
        assert reason in capsys.readouterr().out
    finally:
        path.chmod(0o600)
    assert path.read_text() == '{"model": "opus"}'


@pytest.mark.parametrize("listed,pattern,left_out", [
    ("deny", "mcp__griot__griot_*", "all"), ("deny", "mcp__*", "all"), ("ask", "mcp__griot__griot_s*", "some"),
    ("ask", "mcp__griot__*", "all"),
])
def test_a_pattern_of_the_users_that_covers_a_tool_is_a_decision_too(claude, tmp_path, terminal, listed, pattern, left_out):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {listed: [pattern]}}))
    terminal("y")
    outcome = harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path)
    allowed = json.loads(path.read_text())["permissions"].get("allow", [])
    if left_out == "all":
        assert outcome == "current" and allowed == []
    else:
        assert outcome == "added"
        assert "mcp__griot__griot_search" not in allowed and "mcp__griot__griot_stats" not in allowed
        assert "mcp__griot__griot_index_status" in allowed


def test_an_allow_pattern_for_griots_tools_is_enough(claude, tmp_path, monkeypatch):
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": ["mcp__griot__griot_*"]}}))
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although the tools are allowed"))
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "current"


def test_an_allow_pattern_without_a_server_does_not_count(claude, tmp_path, terminal):
    """The harness ignores an allow rule whose server part is a wildcard, so
    it allows nothing and griot's rules are still needed."""
    path = _settings(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": ["mcp__*"]}}))
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "added"
    assert "mcp__griot__griot_search" in _allowed(path)


@pytest.mark.parametrize("how", ["skipped", "unsafe", "n/a"])
def test_the_server_is_not_loaded_when_nothing_will_be_offered(claude, tmp_path, monkeypatch, how):
    """Listing the tools imports the whole server. Not for a question that
    will not be asked."""
    monkeypatch.setattr(harnesses, "tool_rules", lambda harness: pytest.fail("loaded the server for nothing"))
    project = tmp_path / "project"
    project.mkdir()
    if how == "skipped":
        assert harnesses.offer_tool_approval(claude, "global", ask=False, home=tmp_path, cwd=project) == "skipped"
    elif how == "unsafe":
        (project / ".claude").symlink_to(tmp_path)
        assert harnesses.offer_tool_approval(claude, "local", home=tmp_path, cwd=project) == "unsafe"
    else:
        opencode = next(h for h in harnesses.HARNESSES if h.id == "opencode")
        assert harnesses.offer_tool_approval(opencode, "global", home=tmp_path, cwd=project) == "n/a"


def test_in_the_home_directory_a_linked_settings_file_is_written_through(claude, tmp_path, terminal):
    """A dotfiles checkout: the link must stay a link."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"model": "opus"}')
    (tmp_path / ".claude").mkdir()
    _settings(tmp_path).symlink_to(real)
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "global", home=tmp_path, cwd=tmp_path) == "added"
    assert _settings(tmp_path).is_symlink() and "mcp__griot__griot_search" in real.read_text()


# --- in a project -------------------------------------------------------------------------


def test_local_scope_writes_the_personal_project_file_and_says_to_keep_it_out_of_git(claude, tmp_path, terminal, capsys):
    project = tmp_path / "project"
    project.mkdir()
    terminal("y")
    assert harnesses.offer_tool_approval(claude, "local", home=tmp_path / "home", cwd=project) == "created"
    assert _allowed(project / ".claude" / "settings.local.json") == harnesses.tool_rules(claude)
    assert not (project / ".claude" / "settings.json").exists()
    out = capsys.readouterr().out
    assert "this project" in out and "version control" in out


def test_a_project_file_that_is_already_there_is_pointed_out_before_the_question(claude, tmp_path, terminal, capsys):
    """A repository can ship this file with permissions and hooks of its
    own. Adding to it must not read as vouching for it."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    shipped = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]},
               "permissions": {"allow": ["Bash(*)"]}}
    (project / ".claude" / "settings.local.json").write_text(json.dumps(shipped))
    terminal("n")
    harnesses.offer_tool_approval(claude, "local", home=tmp_path / "home", cwd=project)
    out = " ".join(capsys.readouterr().out.split())
    assert "already exists" in out and "read it" in out
    assert "1 other allow rule" in out and "hooks" in out


def test_a_project_file_that_git_tracks_is_said_to_be_shared(claude, git_repo, terminal, capsys):
    """settings.local.json is meant to be personal. Tracked, the approval
    would be committed for everyone who clones."""
    import subprocess
    git_repo.commit("first")
    (git_repo.path / ".claude").mkdir()
    (git_repo.path / ".claude" / "settings.local.json").write_text("{}")
    # Forced: a machine with Claude Code ignores this file globally, which is
    # exactly why a tracked one is worth a warning.
    subprocess.run(["git", "-C", str(git_repo.path), "add", "-f", ".claude/settings.local.json"], check=True)
    subprocess.run(["git", "-C", str(git_repo.path), "commit", "-q", "-m", "settings"], check=True)
    terminal("n")
    harnesses.offer_tool_approval(claude, "local", home=git_repo.path.parent / "home", cwd=git_repo.path)
    assert "tracked by git" in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("linked", [".claude", ".claude/settings.local.json"])
def test_in_a_project_a_link_is_not_written_through(claude, tmp_path, terminal, capsys, linked):
    """A repository can ship `.claude/settings.local.json` as a link to the
    user's own settings, or `.claude` as a link to their home one."""
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "settings.local.json").write_text('{"model": "opus"}')
    link = project / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside if linked == ".claude" else outside / "settings.local.json")
    terminal("y")

    assert harnesses.offer_tool_approval(claude, "local", home=tmp_path / "home", cwd=project) == "unsafe"

    assert (outside / "settings.local.json").read_text() == '{"model": "opus"}'
    assert "NOT touched" in capsys.readouterr().out


# --- through the command, and never through the server ---------------------------------------


def _quiet_install(monkeypatch):
    monkeypatch.setattr(harnesses, "install", lambda harness, scope, home=None, cwd=None: {
        "harness": harness.id, "scope": scope, "skills_target": "s", "agents_target": "a",
        "created": [], "updated": [], "unchanged": []})
    monkeypatch.setattr(harnesses, "install_refusal", lambda *a, **k: None)
    monkeypatch.setattr(harnesses, "offer_instructions", lambda *a, **k: "n/a")
    monkeypatch.setattr(harnesses, "offer_mcp_server", lambda *a, **k: "skipped")


def test_the_command_offers_it_after_installing(claude, tmp_path, terminal, monkeypatch):
    _quiet_install(monkeypatch)
    terminal("y")
    assert harnesses.cmd_install("global", "claude-code", home=tmp_path) == 0
    assert _allowed(_settings(tmp_path)) == harnesses.tool_rules(claude)


def test_the_flag_that_skips_the_question_skips_it(tmp_path, monkeypatch):
    _quiet_install(monkeypatch)
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although told not to"))
    assert harnesses.main(["install", "--scope", "global", "--harness", "claude-code", "--no-allow-tools"]) == 0
    assert not _settings(tmp_path).exists()


def test_a_declined_or_impossible_approval_is_not_a_failed_install(tmp_path, terminal, monkeypatch):
    _quiet_install(monkeypatch)
    terminal("n")
    assert harnesses.cmd_install("global", "claude-code", home=tmp_path) == 0


@pytest.mark.anyio
async def test_the_mcp_tool_never_touches_a_settings_file(tmp_path, monkeypatch):
    """An agent must not be able to widen what an agent may do."""
    from mcp_types import ElicitResult
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(harnesses, "offer_tool_approval",
                        lambda *a, **k: pytest.fail("the MCP tool reached the tool approval"))

    async def accept(ctx, params):
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        for scope in ("local", "global"):
            result = await client.call_tool("griot_assist_install", {"harness": "claude-code", "scope": scope})
            assert result.is_error is False, result.content
    assert not list(tmp_path.rglob("settings*.json"))


def test_no_test_can_write_a_settings_file_outside_its_own_directory():
    """The guard in conftest.py: a test that answers "y" with the real
    harness and the real home must fail, not edit the settings of whoever
    runs the suite."""
    # A path that could not be written anyway: if the guard were ever gone,
    # this fails on a directory that cannot be created, not on a real file.
    with pytest.raises(AssertionError, match="outside the test"):
        harnesses._write_settings(Path("/griot-test-no-such-directory/settings.json"), "{}")
