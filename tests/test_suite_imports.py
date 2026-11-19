"""Tests share helpers as `from test_<file> import ...`, never through a
`tests.` package path.

CI runs `uv run --no-sync pytest`, which does not put the repository root
on sys.path, so `from tests.test_x import y` fails there with
ModuleNotFoundError while `python -m pytest` (which does add it) passes
locally. It reached CI twice on 2026-11-19 before anyone saw it; pytest's
own rootdir insertion is what makes the bare form work everywhere."""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PACKAGE_PATH = re.compile(r"^\s*(from|import)\s+tests\b", re.M)


def test_no_test_imports_a_helper_through_the_tests_package():
    offenders = [p.name for p in sorted(TESTS.glob("*.py")) if PACKAGE_PATH.search(p.read_text())]
    assert offenders == [], f"use `from test_<file> import ...` instead, in: {offenders}"
