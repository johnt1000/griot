"""Every link between the repository's markdown documents leads somewhere.

The README is also the PyPI long description, where a relative link has no
repository to resolve against, so it links the guides under docs/ by their
absolute GitHub URL. A guide renamed, a heading reworded or a section moved
from one document to another would leave those links, and the relative ones
between documents, pointing at nothing without any error. This reads every
tracked markdown file, takes each link to a file of this repository (relative,
or absolute under the repository's GitHub URL), and checks the file exists
and, when the link names an anchor, that the target has a heading with that
anchor as GitHub derives it."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GITHUB = "https://github.com/johnt1000/griot/blob/main/"

# The guides the README's documentation index points readers to; each must
# exist and be linked from the README by its absolute URL.
GUIDES = ["README.md", "getting-started.md", "configuration.md", "credentials.md", "search.md",
          "quality.md", "mcp.md", "platforms.md"]

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"\[(?:[^\]\[]|\[[^\]]*\])*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def _markdown_files() -> list[Path]:
    """The markdown files git tracks or would add: an ignored local note is
    not published, but a new guide not added yet is checked as well."""
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.md"],
                         cwd=ROOT, capture_output=True, check=True)
    return [ROOT / name for name in out.stdout.decode().split("\0") if name]


def _prose(text: str) -> str:
    """The text without code: a link shown inside a code block or span is an
    example, not a link."""
    return _CODE_SPAN.sub("", _FENCE.sub("", text))


def github_anchor(heading: str) -> str:
    """The anchor GitHub gives a heading: the rendered text lowercased, every
    character that is not a letter, digit, space, hyphen or underscore
    dropped, and each space turned into a hyphen."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link keeps its text
    text = text.replace("`", "").replace("*", "")
    text = re.sub(r"[^\w\- ]", "", text.lower())
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor a document's headings give it; a repeated heading gets
    -1, -2 and so on, as on GitHub."""
    seen: dict[str, int] = {}
    result = set()
    for heading in _HEADING.findall(_FENCE.sub("", text)):
        base = github_anchor(heading)
        count = seen.get(base, 0)
        result.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return result


def links(path: Path) -> list[tuple[Path, str]]:
    """Each link from `path` to a file of this repository, as (target file,
    anchor or "")."""
    found = []
    for target in _LINK.findall(_prose(path.read_text(encoding="utf-8"))):
        if target.startswith(GITHUB):
            target, base = target[len(GITHUB):], ROOT
        elif re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue  # another site, or mailto:
        else:
            base = path.parent
        file, _, anchor = target.partition("#")
        found.append(((base / file).resolve() if file else path, anchor))
    return found


def broken_links(path: Path) -> list[str]:
    problems = []
    for target, anchor in links(path):
        where = target.relative_to(ROOT) if target.is_relative_to(ROOT) else target
        if not target.exists():
            problems.append(f"{where}: no such file")
        elif anchor and target.suffix == ".md" and anchor not in anchors(target.read_text(encoding="utf-8")):
            problems.append(f"{where}#{anchor}: no such heading")
    return problems


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_link_in_the_document_resolves(path):
    assert broken_links(path) == []


def test_the_readme_links_every_guide_by_its_absolute_url():
    """Relative links break on PyPI, where the README is the project page."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for guide in GUIDES:
        assert (ROOT / "docs" / guide).exists(), guide
        assert f"]({GITHUB}docs/{guide})" in readme, guide


def test_the_readme_links_no_file_of_the_repository_relatively():
    readme = _prose((ROOT / "README.md").read_text(encoding="utf-8"))
    relative = [t for t in _LINK.findall(readme)
                if not re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) and not t.startswith("#")]
    assert relative == []


# --- the checker itself ---------------------------------------------------------------


def test_anchors_follow_githubs_rule():
    assert github_anchor("Chat profiles (`griot ask` only)") == "chat-profiles-griot-ask-only"
    assert github_anchor("The management surface") == "the-management-surface"
    assert github_anchor("Claude Code / opencode skills and agent") == "claude-code--opencode-skills-and-agent"
    assert anchors("# A\n\n## A\n\n```\n# not a heading\n```\n") == {"a", "a-1"}


def test_the_checker_finds_a_missing_file_and_a_missing_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\n## Real section\n")
    doc = tmp_path / "doc.md"
    doc.write_text("[ok](docs/guide.md#real-section) [gone](docs/missing.md) "
                   "[bad](docs/guide.md#no-section) [abs](" + GITHUB + "docs/guide.md#guide) "
                   "[absgone](" + GITHUB + "docs/other.md) [self](#nowhere) [web](https://example.com/x.md) "
                   "`[code](docs/missing.md)`\n\n```\n[fenced](docs/missing.md)\n```\n")
    assert broken_links(doc) == ["docs/missing.md: no such file", "docs/guide.md#no-section: no such heading",
                                 "docs/other.md: no such file", "doc.md#nowhere: no such heading"]
