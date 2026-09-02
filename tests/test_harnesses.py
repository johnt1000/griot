"""Tests for `griot assist install` — copies griot's bundled skill/agent
files (src/griot/resources/{skills,agents/<harness-id>}/) into whichever
supported harness(es) (Claude Code, opencode) are detected on the machine,
local (<cwd>/.claude or .opencode) or global (~/.claude or
~/.config/opencode). Every test uses a fixture content tree (never the real,
possibly-still-being-written package resources) and redirects BOTH home and
cwd to tmp_path, and detect_harnesses() is always exercised with fake
Harness instances (never shutil.which/real ~/.claude against the actual
machine) — this must never touch the real ~/.claude, ~/.config/opencode, or
this repo's own .claude/.opencode/.
"""

import stat
from pathlib import Path

import pytest

from griot import harnesses


def _make_fixture_root(tmp_path):
    root = tmp_path / "packaged_resources"

    skill_dir = root / "skills" / "griot-onboarding"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Griot Onboarding\n")

    for harness_id, content in (("fake-a", "# Fake A Agent\n"), ("fake-b", "# Fake B Agent\n")):
        agents_dir = root / "agents" / harness_id
        agents_dir.mkdir(parents=True)
        (agents_dir / "griot-setup-assistant.md").write_text(content)

    return root


def _make_fake_harness(harness_id, *, detect=lambda: True):
    return harnesses.Harness(
        id=harness_id,
        display_name=harness_id.title(),
        local_skills_dir=lambda cwd: cwd / f".{harness_id}" / "skills",
        local_agents_dir=lambda cwd: cwd / f".{harness_id}" / "agents",
        global_skills_dir=lambda home: home / f".{harness_id}-global" / "skills",
        global_agents_dir=lambda home: home / f".{harness_id}-global" / "agents",
        agent_content_subdir=harness_id,
        detect=detect,
    )


def test_install_local_creates_skill_and_agent_files_in_harness_dirs(tmp_path, monkeypatch):
    fixture_root = _make_fixture_root(tmp_path)
    monkeypatch.setattr(harnesses, "_resources_root", lambda: fixture_root)
    fake_a = _make_fake_harness("fake-a")

    cwd = tmp_path / "project"
    cwd.mkdir()

    result = harnesses.install(fake_a, "local", cwd=cwd)

    skill_dest = cwd / ".fake-a" / "skills" / "griot-onboarding" / "SKILL.md"
    agent_dest = cwd / ".fake-a" / "agents" / "griot-setup-assistant.md"

    assert skill_dest.read_text() == "# Griot Onboarding\n"
    assert agent_dest.read_text() == "# Fake A Agent\n"

    assert stat.S_IMODE(skill_dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent_dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(skill_dest.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(agent_dest.parent.stat().st_mode) == 0o700

    assert result["harness"] == "fake-a"
    assert result["scope"] == "local"
    assert result["skills_target"] == str(cwd / ".fake-a" / "skills")
    assert result["agents_target"] == str(cwd / ".fake-a" / "agents")
    assert result["created"] == sorted(["griot-onboarding/SKILL.md", "griot-setup-assistant.md"])
    assert result["updated"] == []
    assert result["unchanged"] == []


def test_install_again_with_identical_content_reports_unchanged(tmp_path, monkeypatch):
    fixture_root = _make_fixture_root(tmp_path)
    monkeypatch.setattr(harnesses, "_resources_root", lambda: fixture_root)
    fake_a = _make_fake_harness("fake-a")
    cwd = tmp_path / "project"
    cwd.mkdir()

    harnesses.install(fake_a, "local", cwd=cwd)
    skill_dest = cwd / ".fake-a" / "skills" / "griot-onboarding" / "SKILL.md"
    mtime_before = skill_dest.stat().st_mtime_ns

    result = harnesses.install(fake_a, "local", cwd=cwd)

    assert result["created"] == []
    assert result["updated"] == []
    assert result["unchanged"] == sorted(["griot-onboarding/SKILL.md", "griot-setup-assistant.md"])
    assert skill_dest.stat().st_mtime_ns == mtime_before


def test_install_reports_updated_when_source_content_changed(tmp_path, monkeypatch):
    fixture_root = _make_fixture_root(tmp_path)
    monkeypatch.setattr(harnesses, "_resources_root", lambda: fixture_root)
    fake_a = _make_fake_harness("fake-a")
    cwd = tmp_path / "project"
    cwd.mkdir()

    harnesses.install(fake_a, "local", cwd=cwd)
    (fixture_root / "skills" / "griot-onboarding" / "SKILL.md").write_text("# Griot Onboarding v2\n")

    result = harnesses.install(fake_a, "local", cwd=cwd)

    skill_dest = cwd / ".fake-a" / "skills" / "griot-onboarding" / "SKILL.md"
    assert result["updated"] == ["griot-onboarding/SKILL.md"]
    assert result["unchanged"] == ["griot-setup-assistant.md"]
    assert result["created"] == []
    assert skill_dest.read_text() == "# Griot Onboarding v2\n"


def test_install_global_scope_targets_harness_global_dirs(tmp_path, monkeypatch):
    fixture_root = _make_fixture_root(tmp_path)
    monkeypatch.setattr(harnesses, "_resources_root", lambda: fixture_root)
    fake_a = _make_fake_harness("fake-a")
    home = tmp_path / "fake_home"
    home.mkdir()

    result = harnesses.install(fake_a, "global", home=home)

    assert result["scope"] == "global"
    assert result["skills_target"] == str(home / ".fake-a-global" / "skills")
    assert result["agents_target"] == str(home / ".fake-a-global" / "agents")
    assert (home / ".fake-a-global" / "skills" / "griot-onboarding" / "SKILL.md").exists()
    assert (home / ".fake-a-global" / "agents" / "griot-setup-assistant.md").exists()


def test_install_many_installs_each_harness_into_its_own_dirs_without_cross_contamination(tmp_path, monkeypatch):
    """The most important test: agent_content_subdir must actually isolate
    the two harnesses' agent content — installing both must never let
    fake-b's agent content land in fake-a's directory or vice versa."""
    fixture_root = _make_fixture_root(tmp_path)
    monkeypatch.setattr(harnesses, "_resources_root", lambda: fixture_root)
    fake_a = _make_fake_harness("fake-a")
    fake_b = _make_fake_harness("fake-b")
    cwd = tmp_path / "project"
    cwd.mkdir()

    results = harnesses.install_many([fake_a, fake_b], "local", cwd=cwd)

    assert [r["harness"] for r in results] == ["fake-a", "fake-b"]

    agent_a = cwd / ".fake-a" / "agents" / "griot-setup-assistant.md"
    agent_b = cwd / ".fake-b" / "agents" / "griot-setup-assistant.md"
    assert agent_a.read_text() == "# Fake A Agent\n"
    assert agent_b.read_text() == "# Fake B Agent\n"

    # Skills are shared content but still copied into each harness's own dir.
    skill_a = cwd / ".fake-a" / "skills" / "griot-onboarding" / "SKILL.md"
    skill_b = cwd / ".fake-b" / "skills" / "griot-onboarding" / "SKILL.md"
    assert skill_a.read_text() == skill_b.read_text() == "# Griot Onboarding\n"


def test_install_invalid_scope_raises_value_error(tmp_path):
    fake_a = _make_fake_harness("fake-a")
    with pytest.raises(ValueError):
        harnesses.install(fake_a, "nonsense", cwd=tmp_path, home=tmp_path)


def test_detect_harnesses_returns_only_detected_ones():
    detected = _make_fake_harness("detected", detect=lambda: True)
    not_detected = _make_fake_harness("not-detected", detect=lambda: False)

    result = harnesses.detect_harnesses([detected, not_detected])

    assert result == [detected]


def test_main_install_with_explicit_harness_skips_detection(monkeypatch):
    install_calls = []
    detect_calls = []

    def fake_install(harness, scope, **kwargs):
        install_calls.append((harness.id, scope))
        return {
            "harness": harness.id, "scope": scope,
            "skills_target": "/fake/skills", "agents_target": "/fake/agents",
            "created": [], "updated": [], "unchanged": [],
        }

    def fake_detect_harnesses(candidates=None):
        detect_calls.append(candidates)
        return []

    monkeypatch.setattr(harnesses, "install", fake_install)
    monkeypatch.setattr(harnesses, "detect_harnesses", fake_detect_harnesses)

    rc = harnesses.main(["install", "--harness", "opencode", "--scope", "global"])

    assert rc == 0
    assert install_calls == [("opencode", "global")]
    assert detect_calls == []


def test_main_install_default_harness_all_uses_detection(monkeypatch):
    install_calls = []

    def fake_install(harness, scope, **kwargs):
        install_calls.append(harness.id)
        return {
            "harness": harness.id, "scope": scope,
            "skills_target": "/fake/skills", "agents_target": "/fake/agents",
            "created": [], "updated": [], "unchanged": [],
        }

    fake_claude = _make_fake_harness("claude-code")
    monkeypatch.setattr(harnesses, "install", fake_install)
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: [fake_claude])

    rc = harnesses.main(["install"])

    assert rc == 0
    assert install_calls == ["claude-code"]


def test_main_install_no_harness_detected_prints_message_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(harnesses, "detect_harnesses", lambda candidates=None: [])
    calls = []
    monkeypatch.setattr(harnesses, "install", lambda *a, **k: calls.append(a))

    rc = harnesses.main(["install"])

    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "claude-code" in out
    assert "opencode" in out


def test_known_harnesses_are_claude_code_and_opencode():
    ids = {h.id for h in harnesses.HARNESSES}
    assert ids == {"claude-code", "opencode"}
