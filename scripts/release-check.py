#!/usr/bin/env python3
"""Refuses a release whose tag is not the version the package declares.

    GITHUB_REF_NAME=v0.2.0 python3 scripts/release-check.py [repository root]

Run by .github/workflows/release.yml before anything is built. A tag is a
name anyone can type; the version is what pyproject.toml declares and what
`griot --version` answers. The two have to be the same thing, and the
changelog has to have a section for it, or the release says one version
and ships another. Exit status 1 with the reason on stderr; nothing is
changed. Standard library only, so it runs before anything is installed.
"""

import os
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the workflow runs a current Python
    import tomli as tomllib


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    tag = os.environ.get("GITHUB_REF_NAME", "")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    expected = f"v{version}"
    if tag != expected:
        print(f"release-check: the tag is {tag!r} and pyproject.toml declares version {version}: a release is "
              f"tagged {expected}. Bump the version in pyproject.toml and src/griot/__init__.py, or tag again.",
              file=sys.stderr)
        return 1
    changelog = (root / "CHANGELOG.md").read_text()
    heading = re.search(rf"^## \[{re.escape(version)}\].*$", changelog, re.M)
    if heading is None:
        print(f"release-check: CHANGELOG.md has no `## [{version}]` section: what this release changes has to be "
              f"written down before it is published.", file=sys.stderr)
        return 1
    if "unreleased" in heading.group(0).lower():
        print(f"release-check: the CHANGELOG.md section for {version} still says unreleased: `{heading.group(0)}`. "
              f"Give it the release date (`## [{version}] — YYYY-MM-DD`) before tagging.", file=sys.stderr)
        return 1
    print(f"release-check: tag {tag} is version {version}, and the changelog has its section.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
