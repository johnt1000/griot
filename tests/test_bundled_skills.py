"""Read-only checks over the REAL bundled skills that `griot assist install`
ships. Kept apart from test_harnesses.py on purpose: that file only ever
uses a fixture tree, so nothing there notices a shipped SKILL.md that a
harness would refuse to load."""

import re

import pytest

from griot import harnesses

_SKILLS_ROOT = harnesses._resources_root() / "skills"


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must open with a --- frontmatter block"
    fields = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def test_operations_skill_is_bundled():
    assert (_SKILLS_ROOT / "griot-operations" / "SKILL.md").is_file()


@pytest.mark.parametrize("skill_dir", sorted(p for p in _SKILLS_ROOT.iterdir() if p.is_dir()), ids=lambda p: p.name)
def test_every_bundled_skill_declares_its_own_name_and_a_description(skill_dir):
    # Claude Code keys a skill by the frontmatter name; a mismatch with the
    # directory installs a file that is listed under the wrong name or not at all.
    fields = _frontmatter((skill_dir / "SKILL.md").read_text())
    assert fields.get("name") == skill_dir.name
    assert fields.get("description")
