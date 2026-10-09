"""opencode reads skills from Claude Code's directory too, so griot installed
for both harnesses made opencode show each griot skill twice (a "duplicate
skill name" warning, and whichever copy loaded last won).

Evidence, opencode's own source (anomalyco/opencode, branch dev, read at
commit 388406238bd5 on 2026-10-08):
- packages/opencode/src/skill/index.ts, `discoverSkills()`: unless
  OPENCODE_DISABLE_EXTERNAL_SKILLS, it scans `<home>/.claude/skills/**/SKILL.md`
  (skipped when OPENCODE_DISABLE_CLAUDE_CODE_SKILLS or
  OPENCODE_DISABLE_CLAUDE_CODE is set) and `<home>/.agents/skills/**/SKILL.md`,
  then the same two names in every directory from the project up to its
  worktree root, and only then opencode's own config directories.
- `<home>` is `Global.Path.home` (packages/core/src/global.ts), which is
  `os.homedir()`: CLAUDE_CONFIG_DIR is NOT followed there.
- `add()` logs "duplicate skill name" and overwrites the earlier entry.
- Agents are read only from opencode's own config directories
  (packages/opencode/src/config/agent.ts), never from `~/.claude/agents`, so
  opencode's agent file is still installed.
- packages/opencode/src/effect/runtime-flags.ts reads those flags with
  Effect's Config.boolean (true/yes/on/1/y, any case).

Every test uses a temporary home and project; the conftest clears the
variables of whoever runs the suite."""

import os
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import harnesses, mcp_server


def _harness(harness_id):
    return next(h for h in harnesses.HARNESSES if h.id == harness_id)


@pytest.fixture
def claude():
    return _harness("claude-code")


@pytest.fixture
def opencode():
    return _harness("opencode")


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


def _skill_files() -> list[str]:
    root = harnesses._resources_root() / "skills"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _put_griot_skills(skills_dir: Path) -> None:
    root = harnesses._resources_root() / "skills"
    for rel in _skill_files():
        dest = skills_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((root / rel).read_bytes())


def test_the_bundle_has_skills_to_skip():
    assert _skill_files(), "nothing below would test anything"


# --- global scope -----------------------------------------------------------------------------


def test_installed_for_both_opencode_gets_its_agent_and_no_second_copy_of_the_skills(claude, opencode, home):
    results = harnesses.install_many([claude, opencode], "global", home=home)

    assert _files(home / ".claude" / "skills") == _skill_files()
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert _files(home / ".config" / "opencode" / "agents") == ["griot-setup-assistant.md"]
    oc = next(r for r in results if r["harness"] == "opencode")
    assert oc["skills_skipped"] == _skill_files()
    assert oc["created"] == ["griot-setup-assistant.md"]
    assert str(home / ".claude" / "skills") in oc["skills_note"]
    cc = next(r for r in results if r["harness"] == "claude-code")
    assert cc["skills_skipped"] == [] and cc["skills_note"] is None


def test_the_order_of_the_harnesses_does_not_matter(claude, opencode, home):
    harnesses.install_many([opencode, claude], "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert _files(home / ".claude" / "skills") == _skill_files()


def test_opencode_alone_skips_the_skills_claude_code_already_has(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    result = harnesses.install(opencode, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert result["skills_skipped"] == _skill_files()


def test_opencode_alone_with_nothing_in_claude_code_gets_its_own_copy(opencode, home):
    (home / ".claude" / "skills" / "someone-elses").mkdir(parents=True)
    (home / ".claude" / "skills" / "someone-elses" / "SKILL.md").write_text("x")
    result = harnesses.install(opencode, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert result["skills_skipped"] == [] and result["skills_note"] is None


def test_only_the_skills_claude_code_has_are_skipped(opencode, home):
    first, *rest = sorted({rel.split("/")[0] for rel in _skill_files()})
    _put_griot_skills(home / ".claude" / "skills")
    for name in rest:
        for p in sorted((home / ".claude" / "skills" / name).rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        (home / ".claude" / "skills" / name).rmdir()
    result = harnesses.install(opencode, "global", home=home)
    assert {rel.split("/")[0] for rel in _files(home / ".config" / "opencode" / "skills")} == set(rest)
    assert {rel.split("/")[0] for rel in result["skills_skipped"]} == {first}


def test_the_agents_directory_counts_too(opencode, home):
    _put_griot_skills(home / ".agents" / "skills")
    result = harnesses.install(opencode, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert str(home / ".agents" / "skills") in result["skills_note"]


@pytest.mark.parametrize("variable", ["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "OPENCODE_DISABLE_CLAUDE_CODE",
                                      "OPENCODE_DISABLE_EXTERNAL_SKILLS"])
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "y"])
def test_when_opencode_does_not_read_claude_code_skills_it_gets_its_own(claude, opencode, home, monkeypatch,
                                                                         variable, value):
    monkeypatch.setenv(variable, value)
    result = harnesses.install_many([claude, opencode], "global", home=home)[1]
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert result["skills_skipped"] == []


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_flag_that_is_off_changes_nothing(claude, opencode, home, monkeypatch, value):
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", value)
    harnesses.install_many([claude, opencode], "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []


def test_disabling_claude_code_skills_does_not_disable_the_agents_directory(opencode, home, monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    _put_griot_skills(home / ".agents" / "skills")
    _put_griot_skills(home / ".claude" / "skills")
    result = harnesses.install(opencode, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert str(home / ".agents" / "skills") in result["skills_note"]
    assert str(home / ".claude" / "skills") not in result["skills_note"]


def test_disabling_external_skills_disables_the_agents_directory_too(opencode, home, monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_EXTERNAL_SKILLS", "1")
    _put_griot_skills(home / ".agents" / "skills")
    harnesses.install(opencode, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()


def test_claude_code_moved_by_its_variable_is_not_where_opencode_reads(claude, opencode, home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    result = harnesses.install_many([claude, opencode], "global", home=home)[1]
    assert _files(elsewhere / "skills") == _skill_files()
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files(), \
        "opencode reads ~/.claude whatever CLAUDE_CONFIG_DIR says, so it needs its own copy"
    assert result["skills_skipped"] == []


def test_claude_code_variable_naming_the_default_place_is_the_same_place(claude, opencode, home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    harnesses.install_many([claude, opencode], "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []


@pytest.mark.parametrize("order", ["claude-first", "opencode-first"])
def test_claude_code_directory_reached_through_a_link_is_the_same_place(claude, opencode, home, tmp_path, monkeypatch,
                                                                        order):
    dotfiles = tmp_path / "dotfiles" / "claude"
    dotfiles.mkdir(parents=True)
    (home / ".claude").symlink_to(dotfiles)
    # Named through a link of its own: neither side of the comparison is the
    # resolved path, so both have to be resolved.
    (tmp_path / "alias").symlink_to(dotfiles.parent)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alias" / "claude"))
    both = [claude, opencode] if order == "claude-first" else [opencode, claude]
    harnesses.install_many(both, "global", home=home)
    assert _files(home / ".config" / "opencode" / "skills") == []


def test_opencode_directory_that_is_claude_codes_through_a_link_keeps_its_one_copy(opencode, home):
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "skills").symlink_to(home / ".claude" / "skills")
    _put_griot_skills(home / ".claude" / "skills")

    result = harnesses.install(opencode, "global", home=home, alongside=[opencode])

    assert _files(home / ".claude" / "skills") == _skill_files(), "the only copy is not a duplicate"
    assert result["skills_skipped"] == [] and result["removed"] == []


# --- copies left by an earlier install ----------------------------------------------------------


def test_the_copies_an_earlier_install_left_in_opencode_are_removed(claude, opencode, home):
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    (oc_skills / "not-griot").mkdir()
    (oc_skills / "not-griot" / "SKILL.md").write_text("theirs")

    result = harnesses.install_many([claude, opencode], "global", home=home)[1]

    assert _files(oc_skills) == ["not-griot/SKILL.md"]
    assert result["removed"] == _skill_files()
    assert sorted(p.name for p in oc_skills.iterdir()) == ["not-griot"], "the emptied skill directories go too"


def test_an_outdated_copy_is_removed_as_well(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    stale = home / ".config" / "opencode" / "skills" / _skill_files()[0]
    stale.parent.mkdir(parents=True)
    stale.write_text("an older griot's text")
    result = harnesses.install(opencode, "global", home=home)
    assert not stale.exists()
    assert result["removed"] == [_skill_files()[0]]
    assert (home / ".config" / "opencode" / "skills").is_dir(), "opencode's own skills directory stays"


def test_a_file_of_the_users_inside_a_griot_skill_directory_is_kept(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    name = _skill_files()[0].split("/")[0]
    (oc_skills / name / "my-notes.md").write_text("mine")
    harnesses.install(opencode, "global", home=home)
    assert _files(oc_skills) == [f"{name}/my-notes.md"]


def test_a_link_where_a_copy_was_is_not_followed_nor_removed(opencode, home, tmp_path):
    _put_griot_skills(home / ".claude" / "skills")
    target = tmp_path / "somewhere" / "precious.md"
    target.parent.mkdir()
    target.write_text("precious")
    link = home / ".config" / "opencode" / "skills" / _skill_files()[0]
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    result = harnesses.install(opencode, "global", home=home)

    assert link.is_symlink() and target.read_text() == "precious"
    assert result["removed"] == []
    assert str(link) in result["skills_note"]


def test_a_project_directory_that_leads_outside_through_a_link_is_not_emptied(claude, opencode, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside-skills"
    _put_griot_skills(outside)
    (project / ".opencode").mkdir(parents=True)
    (project / ".opencode" / "skills").symlink_to(outside)

    harnesses.install_many([opencode, claude], "local", cwd=project)

    assert _files(outside) == _skill_files(), "files outside the project are not griot's to delete"


def test_nothing_skipped_means_nothing_removed(opencode, home):
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    result = harnesses.install(opencode, "global", home=home)
    assert _files(oc_skills) == _skill_files() and result["removed"] == []


# --- local scope -------------------------------------------------------------------------------


def test_installed_for_both_in_a_project_opencode_reads_the_claude_code_copy(claude, opencode, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    result = harnesses.install_many([claude, opencode], "local", cwd=project)[1]
    assert _files(project / ".claude" / "skills") == _skill_files()
    assert _files(project / ".opencode" / "skills") == []
    assert _files(project / ".opencode" / "agents") == ["griot-setup-assistant.md"]
    assert str(project / ".claude" / "skills") in result["skills_note"]


def test_a_global_claude_code_copy_does_not_cover_a_local_install(opencode, home, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _put_griot_skills(home / ".claude" / "skills")
    project = tmp_path / "project"
    project.mkdir()
    harnesses.install(opencode, "local", cwd=project)
    assert _files(project / ".opencode" / "skills") == _skill_files()


# --- what the person is told ---------------------------------------------------------------------


def test_the_command_says_why_and_how_to_get_a_copy_anyway(home, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: list(harnesses.HARNESSES))
    assert harnesses.cmd_install("global", "all", skills_only=True, home=home) == 0
    out = capsys.readouterr().out
    assert str(home / ".claude" / "skills") in out
    assert "twice" in out and "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS" in out
    summary = out.split("\nSummary")[1]
    assert "opencode: agent in" in summary and "skills read from" in summary


def test_the_command_skips_them_whichever_harness_comes_first(home, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    monkeypatch.setattr(harnesses, "detect_harnesses",
                        lambda candidates=None: [_harness("opencode"), _harness("claude-code")])
    assert harnesses.cmd_install("global", "all", skills_only=True, home=home) == 0
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert "being installed for Claude Code" in capsys.readouterr().out


def test_the_summary_says_how_many_when_only_some_are_read_from_elsewhere(home, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    one = _skill_files()[0]
    (home / ".claude" / "skills" / one).parent.mkdir(parents=True)
    (home / ".claude" / "skills" / one).write_text("x")
    assert harnesses.cmd_install("global", "opencode", skills_only=True, home=home) == 0
    summary = capsys.readouterr().out.split("\nSummary")[1]
    assert "skills and agent in" in summary and "1 of the skills read from" in summary


def test_the_first_directory_opencode_reads_is_the_one_named(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    _put_griot_skills(home / ".agents" / "skills")
    result = harnesses.install(opencode, "global", home=home)
    assert str(home / ".claude" / "skills") in result["skills_note"]
    assert str(home / ".agents" / "skills") not in result["skills_note"]


def test_the_command_lists_what_it_removed(home, monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: False)
    _put_griot_skills(home / ".claude" / "skills")
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    assert harnesses.cmd_install("global", "opencode", skills_only=True, home=home) == 0
    out = capsys.readouterr().out
    assert f"removed ({len(_skill_files())})" in out
    assert all(rel in out for rel in _skill_files())


@pytest.mark.anyio
async def test_the_tool_says_so_through_the_protocol(tmp_path, monkeypatch):
    from mcp_types import ElicitResult
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: list(harnesses.HARNESSES))
    _put_griot_skills(project / ".opencode" / "skills")
    asked = []

    async def accept(ctx, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "all", "scope": "local"})
        listed = {t.name: t for t in (await client.list_tools()).tools}

    out = result.structured_content
    real = Path(os.path.realpath(project))
    assert "twice" in asked[0] and str(real / ".claude" / "skills") in asked[0]
    oc = next(r for r in out["results"] if r["harness"] == "opencode")
    assert oc["skills_skipped"] == _skill_files()
    assert oc["removed"] == _skill_files()
    assert str(project / ".claude" / "skills") in oc["note"]
    cc = next(r for r in out["results"] if r["harness"] == "claude-code")
    assert cc["skills_skipped"] == [] and cc["removed"] == [] and cc["note"] is None
    assert _files(project / ".opencode" / "skills") == []
    assert "~/.claude/skills" in listed["griot_assist_install"].description


@pytest.mark.anyio
async def test_a_run_that_only_removed_copies_changed_something(tmp_path, monkeypatch):
    from mcp_types import ElicitResult
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _put_griot_skills(project / ".claude" / "skills")
    harnesses.install(_harness("opencode"), "local", cwd=project)  # its agent is there already
    _put_griot_skills(project / ".opencode" / "skills")

    async def accept(ctx, params):
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "opencode", "scope": "local"})

    out = result.structured_content
    assert out["results"][0]["created"] == [] and out["results"][0]["updated"] == []
    assert out["results"][0]["removed"] == _skill_files()
    assert out["changed"] is True
