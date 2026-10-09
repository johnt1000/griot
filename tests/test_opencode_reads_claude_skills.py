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


# --- copies left by an earlier install ----------------------------------------------------------
# An earlier griot deleted these without asking (#110), so a hand edit there
# was lost. Now an install never deletes them: they are listed, with why they
# are redundant and how to remove them, and only the CLI at a terminal asks.


def test_an_install_never_deletes_the_copies_an_earlier_one_left(claude, opencode, home):
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    edited = oc_skills / _skill_files()[0]
    edited.write_text("my own edit")

    result = harnesses.install_many([claude, opencode], "global", home=home)[1]

    assert _files(oc_skills) == _skill_files()
    assert edited.read_text() == "my own edit"
    assert "removed" not in result


def test_the_copies_left_are_listed_with_why_and_how_to_remove_them(claude, opencode, home):
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    (oc_skills / "not-griot").mkdir()
    (oc_skills / "not-griot" / "SKILL.md").write_text("theirs")

    [entry] = harnesses.redundant_copies([claude, opencode], "global", home=home)

    assert entry["harness"] == "opencode"
    assert entry["skills_target"] == str(oc_skills)
    assert entry["files"] == _skill_files()
    assert entry["paths"] == [str(oc_skills / rel) for rel in _skill_files()]
    assert str(home / ".claude" / "skills") in entry["note"] and "twice" in entry["note"]
    assert "griot assist install --scope global --harness all" in entry["note"]
    assert "kept" in entry["note"].lower()


def test_an_outdated_copy_is_listed_as_well(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    stale = home / ".config" / "opencode" / "skills" / _skill_files()[0]
    stale.parent.mkdir(parents=True)
    stale.write_text("an older griot's text")
    [entry] = harnesses.redundant_copies([opencode], "global", home=home)
    assert entry["files"] == [_skill_files()[0]]
    assert "--harness opencode" in entry["note"]


def test_nothing_skipped_means_no_copies_to_report(opencode, home):
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    assert harnesses.redundant_copies([opencode], "global", home=home) == []


def test_opencode_directory_that_is_claude_codes_through_a_link_has_no_copy_to_report(opencode, home):
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "skills").symlink_to(home / ".claude" / "skills")
    _put_griot_skills(home / ".claude" / "skills")
    assert harnesses.redundant_copies([opencode], "global", home=home) == []


def test_a_link_where_a_copy_was_is_not_offered_for_removal(opencode, home, tmp_path):
    _put_griot_skills(home / ".claude" / "skills")
    target = tmp_path / "somewhere" / "precious.md"
    target.parent.mkdir()
    target.write_text("precious")
    link = home / ".config" / "opencode" / "skills" / _skill_files()[0]
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    [entry] = harnesses.redundant_copies([opencode], "global", home=home)
    removed, _ = harnesses.remove_redundant_copies(opencode, [opencode], "global", home=home)

    assert entry["paths"] == [] and str(link) in entry["note"]
    assert removed == []
    assert link.is_symlink() and target.read_text() == "precious"


@pytest.mark.parametrize("scope", ["global", "local"])
def test_a_claude_code_install_alone_reports_the_opencode_copies(claude, home, tmp_path, scope):
    project = tmp_path / "project"
    project.mkdir()
    oc_skills = (home / ".config" / "opencode" / "skills" if scope == "global"
                 else project / ".opencode" / "skills")
    _put_griot_skills(oc_skills)

    [entry] = harnesses.redundant_copies([claude], scope, home=home, cwd=project)

    assert entry["harness"] == "opencode"
    assert entry["files"] == _skill_files()
    assert "being installed for Claude Code" in entry["note"]
    assert "--harness claude-code" in entry["note"]


def test_a_claude_code_install_alone_with_no_opencode_copies_reports_nothing(claude, home):
    assert harnesses.redundant_copies([claude], "global", home=home) == []


def test_removing_deletes_the_copies_and_the_directories_they_emptied_only(claude, opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    oc_skills = home / ".config" / "opencode" / "skills"
    _put_griot_skills(oc_skills)
    (oc_skills / "not-griot").mkdir()
    (oc_skills / "not-griot" / "SKILL.md").write_text("theirs")
    name = _skill_files()[0].split("/")[0]
    (oc_skills / name / "my-notes.md").write_text("mine")

    removed, left = harnesses.remove_redundant_copies(opencode, [claude, opencode], "global", home=home)

    assert removed == _skill_files() and left == []
    assert _files(oc_skills) == [f"{name}/my-notes.md", "not-griot/SKILL.md"]
    assert sorted(p.name for p in oc_skills.iterdir()) == sorted([name, "not-griot"])


def test_removal_never_empties_a_project_directory_that_leads_outside(claude, opencode, tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside-skills"
    _put_griot_skills(outside)
    (project / ".opencode").mkdir(parents=True)
    (project / ".opencode" / "skills").symlink_to(outside)

    removed, left = harnesses.remove_redundant_copies(opencode, [opencode, claude], "local", cwd=project)

    assert removed == [] and left
    assert _files(outside) == _skill_files(), "files outside the project are not griot's to delete"


# --- the environment griot reads is not opencode's --------------------------------------------


def test_the_note_names_the_variables_it_read_and_their_values(claude, opencode, home, monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE", "0")
    note = harnesses.install_many([claude, opencode], "global", home=home)[1]["skills_note"]
    assert "OPENCODE_DISABLE_CLAUDE_CODE='0'" in note
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS unset" in note
    assert "OPENCODE_DISABLE_EXTERNAL_SKILLS unset" in note
    assert "not opencode's" in note


def test_a_variable_that_gave_opencode_its_own_copy_is_named(claude, opencode, home, monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    result = harnesses.install_many([claude, opencode], "global", home=home)[1]
    assert result["skills_skipped"] == []
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS='1'" in result["skills_note"]
    assert "not opencode's" in result["skills_note"]


def test_the_copies_note_states_the_same_assumption(opencode, home):
    _put_griot_skills(home / ".claude" / "skills")
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    [entry] = harnesses.redundant_copies([opencode], "global", home=home)
    assert "OPENCODE_DISABLE_EXTERNAL_SKILLS unset" in entry["note"] and "not opencode's" in entry["note"]
    assert "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1" in entry["note"], "how to give opencode its own copy"


def test_claude_code_has_no_assumption_to_state(claude, home, monkeypatch):
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    assert harnesses.install(claude, "global", home=home)["skills_note"] is None


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


def _terminal(monkeypatch, *, interactive, answers=()):
    """A terminal (or none) that answers in turn, recording each question."""
    monkeypatch.setattr(harnesses, "_is_interactive", lambda: interactive)
    prompts, queue = [], list(answers)

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not interactive:
            raise AssertionError("asked with no terminal to ask on")
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def _install(home, harness="opencode", **kw):
    kw.setdefault("mcp", "no")
    return harnesses.cmd_install("global", harness, ask_instructions=False, ask_tools=False, home=home, **kw)


def _left_copies(home):
    _put_griot_skills(home / ".claude" / "skills")
    _put_griot_skills(home / ".config" / "opencode" / "skills")


def test_at_a_terminal_it_lists_the_copies_and_a_yes_removes_them(home, monkeypatch, capsys):
    _left_copies(home)
    prompts = _terminal(monkeypatch, interactive=True, answers=["y"])

    assert _install(home) == 0

    out = capsys.readouterr().out
    assert len(prompts) == 1 and "[y/N]" in prompts[0]
    before_answer = out.split("removed (")[0]
    assert all(str(home / ".config" / "opencode" / "skills" / rel) in before_answer for rel in _skill_files())
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert f"removed ({len(_skill_files())})" in out
    assert "copies left by an earlier install: removed" in out.split("\nSummary")[1]


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe", EOFError(), KeyboardInterrupt()])
def test_anything_but_yes_keeps_them(home, monkeypatch, capsys, answer):
    _left_copies(home)
    _terminal(monkeypatch, interactive=True, answers=[answer])
    assert _install(home) == 0
    out = capsys.readouterr().out
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert "copies left by an earlier install: kept" in out.split("\nSummary")[1]


def test_with_no_terminal_they_are_kept_and_reported(home, monkeypatch, capsys):
    _left_copies(home)
    _terminal(monkeypatch, interactive=False)

    assert _install(home) == 0

    out = capsys.readouterr().out
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert all(str(home / ".config" / "opencode" / "skills" / rel) in out for rel in _skill_files())
    assert "twice" in out and "griot assist install --scope global --harness opencode" in out
    assert "copies left by an earlier install: kept" in out.split("\nSummary")[1]


def test_skills_only_asks_nothing_and_keeps_them(home, monkeypatch, capsys):
    _left_copies(home)
    prompts = _terminal(monkeypatch, interactive=True, answers=["y"])
    assert harnesses.cmd_install("global", "opencode", skills_only=True, home=home) == 0
    assert prompts == []
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert "copies left by an earlier install: kept" in capsys.readouterr().out.split("\nSummary")[1]


def test_claude_code_alone_at_a_terminal_offers_to_remove_opencodes_copies(home, monkeypatch, capsys):
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    prompts = _terminal(monkeypatch, interactive=True, answers=["yes"])

    assert _install(home, harness="claude-code") == 0

    out = capsys.readouterr().out
    assert len(prompts) == 1
    assert _files(home / ".config" / "opencode" / "skills") == []
    assert _files(home / ".claude" / "skills") == _skill_files()
    assert "opencode: copies left by an earlier install: removed" in out.split("\nSummary")[1]


def test_claude_code_alone_with_no_terminal_reports_opencodes_copies(home, monkeypatch, capsys):
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    _terminal(monkeypatch, interactive=False)

    assert _install(home, harness="claude-code") == 0

    out = capsys.readouterr().out
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()
    assert "being installed for Claude Code" in out
    assert "griot assist install --scope global --harness claude-code" in out


def test_the_command_to_run_again_is_the_one_that_was_run(home, monkeypatch, capsys):
    """`--harness all` that found only Claude Code: running it again with
    `--harness claude-code` would be a different command than the person's."""
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: [_harness("claude-code")])
    _terminal(monkeypatch, interactive=False)
    assert _install(home, harness="all") == 0
    assert "griot assist install --scope global --harness all" in capsys.readouterr().out


def test_an_opencode_directory_griot_cannot_place_is_not_looked_into(claude, home, tmp_path, monkeypatch, capsys):
    """XDG_CONFIG_HOME relative: opencode would read it against whatever
    directory it starts in, so where its copies are is a guess, and a guess
    is not offered for deletion."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    _put_griot_skills(tmp_path / "relative" / "opencode" / "skills")
    # Nor the default place, which is not where that opencode reads.
    _put_griot_skills(home / ".config" / "opencode" / "skills")
    prompts = _terminal(monkeypatch, interactive=True, answers=["y"])

    assert harnesses.redundant_copies([claude], "global", home=home) == []
    assert _install(home, harness="claude-code") == 0
    assert prompts == []
    assert _files(tmp_path / "relative" / "opencode" / "skills") == _skill_files()
    assert _files(home / ".config" / "opencode" / "skills") == _skill_files()


def test_nothing_to_remove_asks_nothing(home, monkeypatch, capsys):
    prompts = _terminal(monkeypatch, interactive=True)
    assert _install(home) == 0
    assert prompts == []
    assert "copies left by an earlier install" not in capsys.readouterr().out


@pytest.mark.anyio
async def test_the_tool_keeps_the_copies_and_says_so_through_the_protocol(tmp_path, monkeypatch):
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
    assert "deleted" not in asked[0] and "kept" in asked[0]
    oc = next(r for r in out["results"] if r["harness"] == "opencode")
    assert oc["skills_skipped"] == _skill_files()
    assert "removed" not in oc
    assert str(project / ".claude" / "skills") in oc["note"]
    assert _files(project / ".opencode" / "skills") == _skill_files(), "the tool never deletes them"
    [kept] = out["copies_kept"]
    assert kept["harness"] == "opencode"
    assert kept["paths"] == [str(project / ".opencode" / "skills" / rel) for rel in _skill_files()]
    assert "twice" in kept["note"] and "griot assist install" in kept["note"]
    assert "kept" in out["message"]
    description = listed["griot_assist_install"].description
    assert "~/.claude/skills" in description and "never deletes" in description


@pytest.mark.anyio
async def test_the_tool_for_claude_code_alone_reports_opencodes_copies(tmp_path, monkeypatch):
    from mcp_types import ElicitResult
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    _put_griot_skills(project / ".opencode" / "skills")

    async def accept(ctx, params):
        return ElicitResult(action="accept", content={})

    async with Client(mcp_server.mcp, elicitation_callback=accept) as client:
        result = await client.call_tool("griot_assist_install", {"harness": "claude-code", "scope": "local"})

    out = result.structured_content
    assert [r["harness"] for r in out["results"]] == ["claude-code"]
    [kept] = out["copies_kept"]
    assert kept["harness"] == "opencode" and len(kept["paths"]) == len(_skill_files())
    assert _files(project / ".opencode" / "skills") == _skill_files()


@pytest.mark.anyio
async def test_a_run_that_only_found_copies_changed_nothing(tmp_path, monkeypatch):
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
    assert out["changed"] is False
    assert len(out["copies_kept"][0]["paths"]) == len(_skill_files())
