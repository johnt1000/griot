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
import pytest
import yaml
from mcp.client.client import Client

from griot import mcp_server, platforms

import test_confirmation_policy_docs as policy_docs  # noqa: E402  the passages its tests hold counts in

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


def _expose_less_opening() -> re.Match:
    """The opening of the paragraph that counts the tools exposing less than
    their command."""
    opening = re.search(r"^(\w+) tools expose less than their CLI counterpart", COVERAGE.read_text(), flags=re.M)
    assert opening, "the paragraph introducing the tools that expose less than their command"
    return opening


def test_the_coverage_document_counts_the_tools_that_expose_less():
    text = COVERAGE.read_text()
    opening = _expose_less_opening()
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


# CONTRIBUTING for contributors; the getting-started guide (the README's
# installation notes before they moved there) for users.
@pytest.mark.parametrize("path", ["CONTRIBUTING.md", "docs/getting-started.md"])
def test_every_text_on_what_ci_runs_names_every_python_and_system(path):
    text = _flat(ROOT / path)
    sentence = [s for s in _sentences(text) if s.startswith("CI runs the suite on")]
    assert len(sentence) == 1, sentence
    for entry in _ci_matrix():
        assert entry["python"] in sentence[0], (entry, sentence[0])
    if any(entry["os"].startswith("macos") for entry in _ci_matrix()):
        assert "macOS" in sentence[0], sentence[0]
    if any(entry["os"].startswith("ubuntu") for entry in _ci_matrix()):
        assert "Linux" in sentence[0], sentence[0]


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
             "docs/platforms.md": _flat(ROOT / "docs" / "platforms.md"),
             "griot-indexing": _flat(SKILLS / "griot-indexing" / "SKILL.md")}
    for name, text in texts.items():
        assert "codeberg.org" in text, name


# --- the prompts the server offers: docs/mcp.md and the griot-workflows skill ----------

MCP_GUIDE = ROOT / "docs" / "mcp.md"

# The guide for people, and the skill each install copies into the user's
# agent directory, where a stale table outlives the release that fixed it.
PROMPT_TABLES = [MCP_GUIDE, SKILLS / "griot-workflows" / "SKILL.md"]


def _prompts_paragraph_and_table(path: Path) -> tuple[str, set[str]]:
    """The paragraph that introduces the prompt table, and the slash commands
    the table lists."""
    blocks = path.read_text().split("\n\n")
    at = next(i for i, b in enumerate(blocks) if "| `/mcp__griot__" in b)
    intro, table = blocks[at - 1], blocks[at]
    return " ".join(intro.split()), set(re.findall(r"^\| `/mcp__griot__([a-z_]+)`", table, re.M))


@pytest.mark.parametrize("path", PROMPT_TABLES, ids=lambda p: p.name if p == MCP_GUIDE else p.parent.name)
def test_no_prompt_table_counts_the_prompts_by_hand(path):
    """A hand-written count goes stale the day a prompt is added (CLAUDE.md:
    do not keep an inventory in prose); the paragraph names the server's own
    prompt list as the inventory instead."""
    intro, _ = _prompts_paragraph_and_table(path)
    counted = re.search(rf"\b({'|'.join(_NUMBER_WORDS)}|\d+)\s+(MCP\s+)?prompts\b", intro, re.I)
    assert counted is None, intro
    assert "prompt list" in intro, intro


@pytest.mark.parametrize("path", PROMPT_TABLES, ids=lambda p: p.name if p == MCP_GUIDE else p.parent.name)
def test_every_prompt_table_lists_the_prompts_the_server_has(path):
    """The table lists every prompt the server offers, and no other."""
    async def listed():
        async with Client(mcp_server.mcp) as client:
            return {p.name for p in (await client.list_prompts()).prompts}

    _, tabled = _prompts_paragraph_and_table(path)
    assert tabled == anyio.run(listed)


# --- no hand-written count of the server's tools, prompts or resources -----------------

# The texts an agent or a user reads about the server: every bundled skill,
# agent and instructions block, and the guides. lessons-and-debts.md is a
# dated history, where a count is what was true that day.
_COUNTED_TEXTS = sorted((ROOT / "src" / "griot" / "resources").rglob("*.md")) + sorted(
    p for p in (ROOT / "docs").glob("*.md") if p.name != "lessons-and-debts.md")

# A number word, up to two words of description, then the plural noun:
# "four MCP prompts", "Five tools expose", "two read-only tools".
_COUNT = re.compile(r"\b(" + "|".join(_NUMBER_WORDS[2:]) + r"|\d+)\s+(?:[\w`-]+\s+){0,2}"
                    r"(tools|prompts|resources|slash commands)\b", re.I)

# Counts a test already holds, so they cannot go stale without one failing,
# each only in the passage of the one file that test reads (the function it
# calls gives the passage): tests/test_confirmation_policy_docs.py derives the
# human-only and the not-pre-approved read-only ones from the server's
# confirmation policy, and the coverage document's "tools expose less" count
# is held to the bullets under it. The same words anywhere else are a count
# nothing holds.
_HELD_COUNTS = [(ROOT / path, phrase, passage) for path, phrase, passage in policy_docs.HELD_COUNTS] + [
    (COVERAGE, "tools expose less", lambda: _expose_less_opening().group(0))]


def _held(path: Path, text: str, at: int) -> bool:
    """Whether the count starting at `at` in `text` (the flattened contents
    of `path`) lies in a passage a test holds it in."""
    for held_path, phrase, passage in _HELD_COUNTS:
        if held_path != path or not re.compile(rf"\w+ {re.escape(phrase)}\b", re.I).match(text, at):
            continue
        # The holding tests read a paragraph or sentence with its lines
        # joined, as _flat does, so the passage is found verbatim. A passage
        # missing from the text holds nothing here; its own test fails then.
        held = " ".join(passage().split())
        start = text.find(held)
        if start >= 0 and start <= at < start + len(held):
            return True
    return False


def _hand_counts(path: Path, text: str) -> list[str]:
    """The counts of tools, prompts or resources in `text` (the flattened
    contents of `path`) that no test holds."""
    return [m.group(0) for m in _COUNT.finditer(text) if not _held(path, text, m.start())]


@pytest.mark.parametrize("path", _COUNTED_TEXTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_text_counts_the_servers_tools_prompts_or_resources_by_hand(path):
    """CLAUDE.md: do not keep an inventory in prose; the server's list_tools(),
    list_prompts() and list_resources() are the inventory. A count written
    by hand goes stale the day one is added, and nothing fails when it does."""
    counts = _hand_counts(path, _flat(path))
    assert counts == [], counts


# A count each holding test reads, as the scan finds it: the phrase, the
# part of it _COUNT matches, and the one file a test holds it in.
_HELD_PHRASES = [("three human-only tools", "three human-only tools", COVERAGE),
                 ("four read-only tools", "four read-only tools", ROOT / "docs" / "mcp.md"),
                 ("Five tools expose less than their CLI counterpart", "Five tools", COVERAGE)]


@pytest.mark.parametrize("phrase, count, held_in", _HELD_PHRASES)
def test_a_held_count_in_a_text_no_test_reads_is_a_hand_count(phrase, count, held_in):
    """A test holds such a count only in the one file it reads: the same
    words in any other text are a count nothing holds."""
    others = [path for path in _COUNTED_TEXTS if path != held_in]
    assert len(others) == len(_COUNTED_TEXTS) - 1
    for path in others:
        assert _hand_counts(path, f"Some words. {phrase}. More words.") == [count], path


@pytest.mark.parametrize("path, phrase, count", [
    (COVERAGE, "Two human-only tools", "Two human-only tools"),
    (COVERAGE, "Two tools expose less than their CLI counterpart", "Two tools"),
    (ROOT / "docs" / "mcp.md", "two read-only tools", "two read-only tools"),
])
def test_a_held_phrase_outside_the_passage_its_test_reads_is_a_hand_count(path, phrase, count):
    """In the file a test reads, only the passage it reads is held: the
    phrase written again elsewhere in that file is a count nothing holds."""
    assert _hand_counts(path, f"{phrase} here. " + _flat(path)) == [count]
    assert _hand_counts(path, _flat(path) + f" {phrase} here.") == [count]


def test_a_held_passage_lets_through_only_its_own_phrase(monkeypatch):
    """A passage holds the count its test reads, not every count written
    in it: another count there is one nothing holds."""
    passage = "Two human-only tools ask a person, and three prompts exist."
    monkeypatch.setattr(sys.modules[__name__], "_HELD_COUNTS", [(COVERAGE, "human-only tools", lambda: passage)])
    assert _hand_counts(COVERAGE, passage) == ["three prompts"]


def test_a_passage_missing_from_the_text_holds_nothing(monkeypatch):
    """When the text no longer has the passage, its counts are no longer
    held, wherever they sit in the text (the start included)."""
    monkeypatch.setattr(sys.modules[__name__], "_HELD_COUNTS",
                        [(COVERAGE, "human-only tools", lambda: "Two human-only tools ask a person, at length.")])
    assert _hand_counts(COVERAGE, "Two human-only tools ask.") == ["Two human-only tools"]


@pytest.mark.parametrize("path, phrase, passage", _HELD_COUNTS, ids=lambda v: v if isinstance(v, str) else None)
def test_each_held_count_is_a_count_the_scan_would_otherwise_fail(path, phrase, passage):
    """An exemption that lets nothing through is one a later edit can widen
    unnoticed; each pair names a count the scan finds in its passage."""
    held = " ".join(passage().split())
    found = [m.group(0) for m in _COUNT.finditer(held)
             if re.compile(rf"\w+ {re.escape(phrase)}\b", re.I).match(held, m.start())]
    assert found, (path, phrase, held)
    assert _hand_counts(path, _flat(path)) == []


# --- docs/getting-started.md: when CI runs ----------------------------------------------


def _ci_triggers() -> dict:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    # YAML 1.1 reads a bare `on` key as the boolean true.
    return ci["on"] if "on" in ci else ci[True]


def test_the_getting_started_guide_says_when_ci_runs():
    """It said CI runs "on every push", but ci.yml runs on pushes to the
    branches it names, on pull requests, by hand, and when the release
    workflow calls it."""
    guide = ROOT / "docs" / "getting-started.md"
    paragraph = next(" ".join(b.split()) for b in guide.read_text().split("\n\n") if "CI runs the suite on" in b)
    triggers = _ci_triggers()
    branches = (triggers.get("push") or {}).get("branches")
    if branches:
        assert not re.search(r"\bevery push(?! to `)", _flat(guide)), paragraph
        for branch in branches:
            assert f"push to `{branch}`" in paragraph, (branch, paragraph)
    assert ("pull request" in paragraph) == ("pull_request" in triggers), paragraph
    assert ("by hand" in paragraph) == ("workflow_dispatch" in triggers), paragraph
    assert ("release" in paragraph) == ("workflow_call" in triggers), paragraph


