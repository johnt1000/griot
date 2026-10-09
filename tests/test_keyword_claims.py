"""What griot says keyword search is for matches what was measured.

Keyword search shipped described as the mode "for an identifier, an error
code or a commit hash that embeddings rank poorly". Measured on 2026-10-06
on two repositories (MRR@10, vector / keyword / hybrid; common.py above
SEARCH_DEFAULT_MODE has the numbers), keyword was far better than vector on
commit hashes (1.00 / 0.01) but WORSE at putting first the file that
defines a function or class name (0.61 / 0.72): it returns only chunks that
mention the name, which is where the name is used. An agent told to search
a name with keyword to find its definition follows the advice and gets the
weaker ranking.

So every text that tells a reader or an agent when to use keyword is held
here to the measured kinds: a sentence about keyword search that names a
kind of name (identifier, function, class, symbol) must say it is for where
the name is USED. The texts are read as they reach their reader: the MCP
server's instructions, tool, prompt and resource descriptions and rendered
prompts over the protocol, the CLI help as argparse prints it, and the
README, the guides under docs/ and the bundled skills from disk."""

import re
from pathlib import Path

import pytest
from mcp.client.client import Client

from griot import ask, cli, mcp_server

ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "src" / "griot" / "resources"

# A sentence naming a kind of name. "ref names" or "naming `griot index
# keywords`" are not advice about searching a name, so a bare "names" does
# not count; "a name", "the name" and "exact name" do.
_NAME_KINDS = re.compile(r"\b(identifiers?|functions?|class(es)?|symbols?|(a|the|exact) names?)\b", re.I)
_KEYWORD = re.compile(r"\bkeyword\b", re.I)


# A whole word: "fused" (hybrid's rankings) holds it too.
_USED = re.compile(r"\bused\b", re.I)


def _sentences(text: str) -> list[str]:
    # Code blocks hold commands, not claims.
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    # argparse help has no full stops between options: each option (a line
    # starting with a dash) is its own claim, or "the sources used as
    # context" of another option would excuse the --mode text.
    parts = re.split(r"(?<=[.!?])\s+|\n\s*\n|\n(?=\s*-)", text)
    return [" ".join(p.split()) for p in parts if p.strip()]


def _unmeasured_claims(text: str) -> list[str]:
    return [s for s in _sentences(text)
            if _KEYWORD.search(s) and _NAME_KINDS.search(s) and not _USED.search(s)]


async def _protocol_texts() -> dict[str, str]:
    async with Client(mcp_server.mcp) as client:
        texts = {"server instructions": client.instructions or ""}
        for tool in (await client.list_tools()).tools:
            texts[f"tool {tool.name}"] = tool.description or ""
        for prompt in (await client.list_prompts()).prompts:
            texts[f"prompt {prompt.name} description"] = prompt.description or ""
            args = {a.name: "why is the lock file there" for a in (prompt.arguments or []) if a.required}
            rendered = await client.get_prompt(prompt.name, args)
            texts[f"prompt {prompt.name}"] = "\n\n".join(
                m.content.text for m in rendered.messages if hasattr(m.content, "text"))
        for resource in (await client.list_resources()).resources:
            texts[f"resource {resource.uri}"] = resource.description or ""
    return texts


def _help(main, argv, capsys) -> str:
    with pytest.raises(SystemExit):
        main(argv)
    return capsys.readouterr().out


def _files() -> dict[str, str]:
    # The README and the guides it points to (its sections moved there), the
    # indexing model and the bundled skills: the maintainer documents quote
    # past claims on purpose.
    guides = [p for p in sorted((ROOT / "docs").glob("*.md"))
              if p.name not in ("lessons-and-debts.md", "mcp-capability-coverage.md")]
    assert ROOT / "docs" / "search.md" in guides, "the guide that says what keyword search is for"
    paths = [ROOT / "README.md", *guides, *sorted(RESOURCES.rglob("*.md"))]
    return {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8") for p in paths}


def test_the_rule_catches_the_claim_that_shipped_with_keyword_search():
    """The check itself, on the sentence the measurement contradicted."""
    shipped = ("`keyword` by the exact words, for an identifier, an error code or a commit "
               "hash that embeddings rank poorly.")
    assert _unmeasured_claims(shipped) == [" ".join(shipped.split())]
    assert not _unmeasured_claims("`keyword` for a commit hash, an error code, or where a name is used.")
    # Words that hold "used" are not the claim, nor is another option's help.
    assert _unmeasured_claims("`hybrid` fused by rank; `keyword` for an identifier.")
    assert _unmeasured_claims("--show-sources the sources used\n  --mode keyword: an identifier")


@pytest.mark.anyio
async def test_no_text_the_server_sends_recommends_keyword_for_a_name_beyond_its_uses():
    for where, text in (await _protocol_texts()).items():
        assert not _unmeasured_claims(text), (where, _unmeasured_claims(text))


def test_the_cli_help_recommends_keyword_only_for_what_it_measured_better_at(capsys):
    for where, text in {"griot search --help": _help(cli.main, ["search", "--help"], capsys),
                        "griot ask --help": _help(ask.main, ["--help"], capsys)}.items():
        assert not _unmeasured_claims(text), (where, _unmeasured_claims(text))


def test_no_doc_or_bundled_skill_recommends_keyword_for_a_name_beyond_its_uses():
    for where, text in _files().items():
        assert not _unmeasured_claims(text), (where, _unmeasured_claims(text))


@pytest.mark.anyio
async def test_the_texts_an_agent_reads_first_name_the_measured_kinds():
    """What keyword IS for, where an agent learns it: a commit hash (where
    it won by most) and where a name is used."""
    texts = await _protocol_texts()
    for where in ("server instructions", "tool griot_search"):
        said = [s.lower() for s in _sentences(texts[where]) if _KEYWORD.search(s)]
        assert any("commit hash" in s for s in said), (where, said)
        assert any(_USED.search(s) for s in said), (where, said)
