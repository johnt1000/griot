"""scripts/check-docs-move.py finds a statement a move lost, and only that.

It was written to check that moving the README's reference sections into
guides changed no claim: a checker that passes whatever it is given would
make that check worthless."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-docs-move.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_docs_move", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD = """# Tool

## Setup

Run `tool init` once. It writes [a file](docs/x.md) at `~/.tool`.

- **Fast** — local. No server.

| Name | Use |
|---|---|
| `A` | the first |

```bash
tool go   # go
```
"""


def test_a_rewrapped_move_with_links_rewritten_loses_nothing():
    check = _module()
    moved = ["# Tool\n\n- **Fast** — local.\n  No server.\n",
             "## Setup\n\nRun `tool init`\nonce. It writes [a file](x.md)\nat `~/.tool`.\n\n"
             "| Name | Use |\n|---|---|\n| `A` | the first |\n\n```bash\ntool go   # go\n```\n"]
    assert check.missing(OLD, moved) == []


def test_each_kind_of_statement_lost_is_reported():
    check = _module()
    moved = ["# Tool\n\n## Set up\n\nRun `tool init` once. It writes [a file](x.md) at `~/.config`.\n\n"
             "- **Fast** — local.\n\n| `A` | the second |\n\n```bash\ntool go\n```\n"]
    assert check.missing(OLD, moved) == [
        ("heading", "Setup"),
        ("sentence", "It writes [a file](docs/x.md) at `~/.tool`."),
        ("sentence", "No server."),
        ("table row", "| Name | Use |"),
        ("table row", "| `A` | the first |"),
        ("code", "tool go   # go"),
    ]
