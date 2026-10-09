#!/usr/bin/env python3
"""Says whether every statement of a document at an earlier commit is still
somewhere in the documents now.

    python3 scripts/check-docs-move.py REV [FILE] [-- TARGET ...]

FILE defaults to README.md, the targets to README.md and docs/*.md. Written
when the README's reference material moved into guides under docs/: a move
that is meant to change no claim can be checked rather than trusted. The old
text is read with `git show REV:FILE` and cut into statements (each sentence
of a paragraph or list item, each table row, each line of a code block, each
heading); each must appear in the targets once both sides are compared with
whitespace collapsed (the guides are rewrapped) and link targets dropped
(links are rewritten to where their file is now; their text is compared).
Headings are compared with the targets' headings.

Prints each statement it could not find and exits 1 when there is one; a
statement changed on purpose is then for the person reading the output to
account for. Standard library only. Changes nothing.
"""

import re
import subprocess
import sys
from pathlib import Path

_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_SENTENCE_END = re.compile(r"(?<=[.)])\s+(?=[A-Z`*(])")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


def normalize(text: str) -> str:
    return " ".join(_LINK_TARGET.sub("]", text).split())


def statements(markdown: str) -> list[tuple[str, str]]:
    """(kind, text) for every statement of a document."""
    found, fence, paragraph = [], False, []

    def flush():
        if paragraph:
            text = " ".join(paragraph)
            text = text[2:] if text.startswith("- ") else text
            found.extend(("sentence", s) for s in _SENTENCE_END.split(text) if s.strip())
            paragraph.clear()

    for line in markdown.split("\n"):
        if line.startswith("```"):
            flush()
            fence = not fence
        elif fence:
            if line.strip():
                found.append(("code", line))
        elif heading := _HEADING.match(line):
            flush()
            found.append(("heading", heading[1]))
        elif line.startswith("|"):
            flush()
            if not re.fullmatch(r"[|\-\s]*", line):
                found.append(("table row", line))
        elif not line.strip() or line.startswith("- "):
            flush()
            if line.strip():
                paragraph.append(line)
        else:
            paragraph.append(line.strip())
    flush()
    return found


def missing(old: str, targets: list[str]) -> list[tuple[str, str]]:
    corpus = normalize("\n".join(targets))
    headings = {normalize(h[1]) for t in targets for h in map(_HEADING.match, t.split("\n")) if h}
    lost = []
    for kind, text in statements(old):
        here = normalize(text) in headings if kind == "heading" else normalize(text) in corpus
        if not here:
            lost.append((kind, text))
    return lost


def main(argv: list[str]) -> int:
    targets_at = argv.index("--") if "--" in argv else len(argv)
    args, target_args = argv[:targets_at], argv[targets_at + 1:]
    if not args:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    rev, file = args[0], args[1] if len(args) > 1 else "README.md"
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                               check=True).stdout.strip())
    old = subprocess.run(["git", "show", f"{rev}:{file}"], cwd=root, capture_output=True, text=True,
                         check=True).stdout
    paths = [root / t for t in target_args] or [root / "README.md", *sorted((root / "docs").glob("*.md"))]
    lost = missing(old, [p.read_text(encoding="utf-8") for p in paths])
    for kind, text in lost:
        print(f"not found ({kind}): {text}")
    print(f"{len(statements(old)) - len(lost)} of {len(statements(old))} statements of {rev}:{file} found")
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
