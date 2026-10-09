"""Facts the documents state that the code or the pipeline decides.

Each of these was found wrong once by an audit of the documents (2026-10-09):
a sentence went on saying what the code did before a change, while nothing
held it to the code. Each check reads the fact from where it is decided (the
CI workflow, pyproject.toml, the coverage table itself, the server through a
real client) and the claim from the one paragraph that makes it, so prose
around it can change freely."""

import re
import sys
from pathlib import Path

import anyio
import yaml
from mcp.client.client import Client

from griot import mcp_server, platforms

if sys.version_info >= (3, 11):
    import tomllib
else:  # the floor griot supports
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "griot" / "resources" / "skills"
COVERAGE = ROOT / "docs" / "mcp-capability-coverage.md"

_NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "eleven", "twelve"]


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def _sentences(text: str) -> list[str]:
    """Split on a full stop followed by a space only: `3.10`, `~/.config`
    and `common.py` are not ends of sentences."""
    return [s.strip() for s in re.split(r"(?<=\.)\s+", text) if s.strip()]


def _number(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS.index(word.lower())


# --- `griot index keywords` after an interruption ----------------------------------


def test_the_keyword_build_is_described_as_resuming_after_an_interruption():
    """build_keyword_index() resumes a copy an interruption left behind,
    writing only what is missing or changed; two texts said it starts over,
    which tells a person with a large index to expect the whole copy again."""
    texts = {"docs/indexing-model.md": ROOT / "docs" / "indexing-model.md",
             "griot-troubleshooting": SKILLS / "griot-troubleshooting" / "SKILL.md"}
    for name, path in texts.items():
        text = _flat(path)
        hits = [s for s in _sentences(text) if re.search(r"\binterrupt(?:ion|ed)\b", s)]
        about_the_copy = [s for s in hits if "start" in s or "resum" in s]
        assert about_the_copy, f"{name}: no sentence says what a run after an interruption does"
        for sentence in about_the_copy:
            assert "start" not in sentence.replace("started", ""), f"{name}: {sentence}"
            assert "resum" in sentence, f"{name}: {sentence}"


# --- the MCP capability count ------------------------------------------------------


def _coverage_rows():
    section = COVERAGE.read_text().split("## Coverage", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|") and not line.startswith("|---")]
    header, *body = rows
    assert [c.strip() for c in header.split("|")[1:3]] == ["Capability", "In SDK"]
    return section, [[c.strip() for c in row.split("|")[1:5]] for row in body]


def _used(rows):
    """Rows whose "griot uses" cell is a bold yes or count: the table's own
    way of marking a capability griot uses."""
    return [row for row in rows if row[2].startswith("**")]


def test_the_coverage_count_is_the_tables_own():
    section, rows = _coverage_rows()
    used = _used(rows)
    assert used and len(used) < len(rows), "the table marks no capability as used, or all of them"
    stated = re.findall(r"\b(\w+) of (\w+)\b", section.split(rows[-1][0], 1)[1])
    assert stated, "no 'N of M' after the table"
    used_word, total_word = stated[0]
    assert (_number(used_word), _number(total_word)) == (len(used), len(rows)), stated[0]


def test_the_roadmap_counts_the_capabilities_as_the_table_does():
    _, rows = _coverage_rows()
    text = _flat(ROOT / "ROADMAP.md")
    stated = re.findall(r"griot uses (\w+) of the ~?(\w+) capabilities", text)
    assert len(stated) == 1, stated
    assert (_number(stated[0][0]), _number(stated[0][1])) == (len(_used(rows)), len(rows)), stated


def test_the_roadmap_counts_the_protocol_gaps_it_has_left_alike():
    """One paragraph said one gap was left (elicit_url) while the status
    above it still spoke of deciding about two."""
    text = _flat(ROOT / "ROADMAP.md")
    assert "The one gap left worth revisiting is `elicit_url`" in text
    assert not re.search(r"\b(two|three|four) protocol gaps\b", text)


# --- what the coverage document says about the server -----------------------------


def test_the_coverage_document_has_each_heading_once():
    headings = re.findall(r"^#+ .+$", COVERAGE.read_text(), flags=re.M)
    repeated = {h for h in headings if headings.count(h) > 1}
    assert not repeated, repeated


def test_the_coverage_document_counts_the_tools_that_expose_less():
    text = COVERAGE.read_text()
    opening = re.search(r"^(\w+) tools expose less than their CLI counterpart", text, flags=re.M)
    assert opening, "the paragraph introducing the tools that expose less than their command"
    after = text[opening.end():].split("\n### ", 1)[0]
    bullets = re.findall(r"^- `(griot_[a-z_]+)`", after, flags=re.M)
    assert _number(opening.group(1)) == len(bullets), (opening.group(1), bullets)
    # The ones the table above already marks as bounded per call.
    assert {"griot_audit", "griot_golden_set_suggest", "griot_quality_check", "griot_index_repo"} <= set(bullets)


def test_the_coverage_document_does_not_say_the_wait_forgets_a_run_on_restart():
    """The registry of runs is a file in the data directory (jobs.py), so a
    restarted server finds a run it started; the document said it did not."""
    text = _flat(COVERAGE)
    assert "survive a restart" not in text
    assert "`.index_jobs.json`" in text


def test_the_mcp_only_paragraph_names_tools_the_server_has():
    """The paragraph on what only MCP offers once named a tool in a garbled
    sentence and promised two read-only views nothing plans."""
    async def listed():
        async with Client(mcp_server.mcp) as client:
            return {t.name for t in (await client.list_tools()).tools}

    names = anyio.run(listed) | {"griot_index_wait", "griot_index_repo"}  # registered with GRIOT_MCP_ENABLE_INDEX
    section = COVERAGE.read_text().split("### MCP only", 1)[1].split("\n## ", 1)[0]
    named = set(re.findall(r"`(griot_[a-z_]+)`", section))
    assert named and named <= names, named - names
    assert "planned" not in section


# --- SECURITY.md: where credentials live ---------------------------------------------


def test_security_says_credentials_go_to_the_keychain_first():
    """keyring is a default dependency and `griot auth set` stores in the OS
    keychain when one is reachable; the file is the fallback."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert any(dep.startswith("keyring") for dep in project["dependencies"])
    section = _flat(ROOT / "SECURITY.md").split("## What stays on disk", 1)[1].split("Protections applied", 1)[0]
    sentence = [s for s in _sentences(section) if s.startswith("Credentials")]
    assert len(sentence) == 1, sentence
    assert "keychain" in sentence[0] and "0600" in sentence[0], sentence[0]
    assert sentence[0].index("keychain") < sentence[0].index(".env"), sentence[0]


# --- CONTRIBUTING.md and pyproject.toml: the Pythons CI runs --------------------------


def _ci_matrix():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return ci["jobs"]["test"]["strategy"]["matrix"]["include"]


def test_contributing_names_every_python_and_system_ci_tests_on():
    text = _flat(ROOT / "CONTRIBUTING.md")
    sentence = [s for s in _sentences(text) if s.startswith("CI runs the suite on")]
    assert len(sentence) == 1, sentence
    for entry in _ci_matrix():
        assert entry["python"] in sentence[0], (entry, sentence[0])
    if any(entry["os"].startswith("macos") for entry in _ci_matrix()):
        assert "macOS" in sentence[0], sentence[0]


def test_the_classifiers_name_every_python_ci_tests_on():
    classifiers = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["classifiers"]
    for entry in _ci_matrix():
        assert f"Programming Language :: Python :: {entry['python']}" in classifiers, entry


# --- the real-service checks of the platform adapters --------------------------------


def test_every_text_on_what_ran_against_a_real_platform_names_the_codeberg_check():
    """The Gitea/Forgejo release author was checked against codeberg.org's
    public API; the roadmap said so, and the module docstring and the
    indexing skill still said those adapters had only met mocks."""
    texts = {"ROADMAP.md": _flat(ROOT / "ROADMAP.md"),
             "platforms.py": " ".join((platforms.__doc__ or "").split()),
             "griot-indexing": _flat(SKILLS / "griot-indexing" / "SKILL.md")}
    for name, text in texts.items():
        assert "codeberg.org" in text, name
