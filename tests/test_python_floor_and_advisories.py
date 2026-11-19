"""The oldest Python griot supports, and the advisories the lock is held past.

Pillow's fixes for its 2026 advisories exist only in Pillow 13, which needs
Python 3.11; while griot declared 3.10, no lock could hold it, and
Dependabot's security job failed every week without opening a pull request.
filelock's fix (4.2.1) and certifi's (2026.10.18) were held back the same way
or simply not yet taken. The floor is 3.11 now, and the lock holds each of
them at or past its fix.

Pillow 13 came in by moving the lock to the releases an install from the
wheel already resolved (qdrant-client 1.20, fastembed 0.9.1, which allows
Pillow 13), not by overriding fastembed's cap: an override shapes only the
lock and installs made from inside the checkout, so CI would test a
combination no user gets."""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())

# The first version of each package that carries the fix for the advisories
# open against the lock on 2026-11-19.
FIXED_IN = {"pillow": "13.0.0", "filelock": "4.2.1", "certifi": "2026.10.18"}


def _locked_versions(name):
    return [Version(package["version"]) for package in LOCK["package"] if package["name"] == name]


def test_the_floor_is_python_3_11():
    declared = SpecifierSet(PROJECT["project"]["requires-python"])
    assert "3.10.99" not in declared and "3.11.0" in declared


def test_no_classifier_names_a_python_below_the_floor():
    named = [c for c in PROJECT["project"]["classifiers"] if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)]
    assert named and "Programming Language :: Python :: 3.10" not in named


def test_the_lock_resolves_for_the_same_floor():
    assert SpecifierSet(LOCK["requires-python"]) == SpecifierSet(PROJECT["project"]["requires-python"])


def test_the_dev_extra_no_longer_carries_a_tomllib_backport():
    dev = [Requirement(each).name for each in PROJECT["project"]["optional-dependencies"]["dev"]]
    assert "tomli" not in dev


def test_every_locked_version_is_at_or_past_its_fix():
    for name, fixed in FIXED_IN.items():
        versions = _locked_versions(name)
        assert versions, f"{name} is not in uv.lock"
        assert all(v >= Version(fixed) for v in versions), f"{name}: {versions} below {fixed}"


def test_the_lock_holds_the_fastembed_that_allows_pillow_13():
    """fastembed 0.8 capped Pillow below 13, so a lock back on it could only
    hold Pillow 13 through an override. 0.9.1's embeddings were compared with
    0.8.0's before the lock moved (bge-small identical, jina-code within float
    noise), so existing indexes stay valid."""
    versions = _locked_versions("fastembed")
    assert versions, "fastembed is not in uv.lock"
    assert all(v >= Version("0.9.1") for v in versions), f"fastembed: {versions} below 0.9.1"


def test_an_install_from_the_wheel_cannot_land_on_the_fastembed_that_caps_pillow():
    """The lock is not what a `pipx install griot-rag` reads: the wheel's own
    requirement is. qdrant-client 1.20 is the first whose fastembed extra
    needs fastembed 0.9, the first that allows Pillow 13; without the lower
    bound a resolver or a cache could still hand an install fastembed 0.8 and
    Pillow 12, with the advisories the lock no longer has."""
    declared = [Requirement(each) for each in PROJECT["project"]["dependencies"]]
    qdrant = [each for each in declared if each.name == "qdrant-client"]
    assert qdrant, "qdrant-client is not a dependency"
    assert "1.19.99" not in qdrant[0].specifier and "1.20.0" in qdrant[0].specifier


def test_the_lock_holds_what_an_install_from_the_wheel_can_resolve():
    """No override or constraint of the lock's own: whatever it holds, a
    `pipx install griot-rag` could resolve too, so CI tests what users run."""
    assert not PROJECT.get("tool", {}).get("uv", {}).get("override-dependencies")
    assert not LOCK.get("manifest", {}).get("overrides")
