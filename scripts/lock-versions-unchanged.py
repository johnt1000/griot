#!/usr/bin/env python3
"""Refuse a rewrite of uv.lock that changes what it installs.

    lock-versions-unchanged.py BEFORE AFTER

.github/workflows/dependabot-lock.yml rewrites Dependabot's uv.lock with the
uv the workflows pin, and commits the result with no person reading it. That
is acceptable only for a change of form: where a Python marker goes, the
`revision` line. So every package is compared by what decides what gets
installed (name, version, source and the hash of every file), and the
dependency edges and markers, which is what the two versions of uv write
differently, are left out.

Exit 0 when nothing installed differs, 1 naming each package that does, and
2 when a file cannot be read (an error is never a pass)."""

import sys

try:
    import tomllib
except ImportError:  # Python 3.10, the floor griot supports
    import tomli as tomllib


def _installed(path):
    with open(path, "rb") as handle:
        lock = tomllib.load(handle)
    packages = {}
    for package in lock.get("package", []):
        files = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
        # Keyed by name AND version: uv locks two versions of one package
        # when markers split the resolution.
        key = (package["name"], package.get("version"))
        packages[key] = (repr(sorted(package.get("source", {}).items())),
                         tuple(sorted(str(file.get("hash")) for file in files)))
    return packages


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        before, after = _installed(argv[1]), _installed(argv[2])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        print(f"cannot read a lock file: {error}", file=sys.stderr)
        return 2
    changed = sorted({name for name, _ in before.keys() ^ after.keys()}
                     | {name for (name, version), what in before.items() if after.get((name, version), what) != what})
    if changed:
        print("the rewrite changes what uv.lock installs, not only how it is written: " + ", ".join(changed),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
