"""A release is a tag, and what the tag publishes is what the tests ran on.

The package is published to PyPI as `griot-rag` (the name `griot` is
somebody else's there; the command and the import stay `griot`). Pushing a
tag `vX.Y.Z` runs `.github/workflows/release.yml`: it builds the
distribution from the lock, like CI does, checks that the tag is the
version the package declares and that the changelog has a section for it,
and publishes through PyPI's trusted publishing (an OpenID token the job is
granted for that one step; no API token lives in the repository). The
version is declared in one place and read everywhere."""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import griot

if sys.version_info >= (3, 11):
    import tomllib
else:  # the floor griot supports
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
RELEASE = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
DISTRIBUTION = "griot-rag"


def _steps(job):
    return RELEASE["jobs"][job]["steps"]


def _commands(job):
    return "\n".join(step.get("run", "") for step in _steps(job))


# --- the name and the version, in one place ---------------------------------------------------------


def test_the_distribution_is_griot_rag_and_the_command_and_import_stay_griot():
    assert PROJECT["name"] == DISTRIBUTION
    assert PROJECT["scripts"] == {"griot": "griot.cli:main"}
    assert griot.__name__ == "griot"


def test_the_version_the_package_declares_is_the_one_the_code_says():
    assert PROJECT["version"] == griot.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", PROJECT["version"])


def test_the_changelog_has_a_section_for_the_version():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{PROJECT['version']}]" in changelog


def test_the_readme_installs_the_distribution_by_name():
    readme = (ROOT / "README.md").read_text()
    assert f"pipx install {DISTRIBUTION}" in readme
    assert f"v{PROJECT['version']}" in readme, "the status line names the current version"


# --- the workflow -----------------------------------------------------------------------------------


def test_a_release_is_a_version_tag():
    triggers = RELEASE.get("on", RELEASE.get(True))
    assert list(triggers) == ["push"] and triggers["push"] == {"tags": ["v*"]}


def test_the_token_that_publishes_is_granted_to_the_publishing_job_alone():
    assert RELEASE["permissions"] == {"contents": "read"}
    jobs = RELEASE["jobs"]
    publishing = [name for name, job in jobs.items()
                  if any("pypa/gh-action-pypi-publish@" in str(step.get("uses", "")) for step in job.get("steps", []))]
    assert publishing == ["publish"]
    assert jobs["publish"]["permissions"] == {"id-token": "write", "contents": "read"}
    assert jobs["publish"]["environment"] == "pypi", "an environment the repository can protect"
    for name, job in jobs.items():
        if name != "publish":
            assert "id-token" not in (job.get("permissions") or {})


def test_the_publishing_step_gets_no_password():
    [step] = [step for step in _steps("publish") if "pypa/gh-action-pypi-publish@" in str(step.get("uses", ""))]
    assert not {"password", "user"} & set(step.get("with") or {}), "trusted publishing, not a token in a secret"


def test_what_is_published_is_what_was_built_from_the_lock():
    build = _commands("build")
    assert "uv export --locked --only-group build --output-file /tmp/build.txt" in build
    assert "uv build --build-constraints /tmp/build.txt --require-hashes" in build
    assert "uv run --no-sync twine check dist/*" in build
    assert RELEASE["jobs"]["publish"]["needs"] == "build"
    uploads = [s for s in _steps("build") if "actions/upload-artifact@" in str(s.get("uses", ""))]
    downloads = [s for s in _steps("publish") if "actions/download-artifact@" in str(s.get("uses", ""))]
    assert uploads and downloads and uploads[0]["with"]["name"] == downloads[0]["with"]["name"]


def _cache_inputs(step):
    """The inputs by which a setup action turns its built-in cache on:
    setup-uv's `enable-cache`, setup-python's and setup-node's `cache`."""
    return {key: value for key, value in (step.get("with") or {}).items() if key in ("enable-cache", "cache")}


def test_no_job_of_the_release_reads_or_writes_a_cache():
    """A cache is written by other runs (any push to main, any CI run on
    another tag) and restored here into the job that builds what PyPI
    serves: one poisoned entry would be published under the maintainer's
    name. Every step of the release's own jobs stays off the cache, and
    setup-uv turns its cache on by itself on a hosted runner when
    `enable-cache` is left out, so it is told. The CI this workflow calls
    keeps its cache: nothing it produces is published."""
    for name, job in RELEASE["jobs"].items():
        for step in job.get("steps", []):
            used = str(step.get("uses", ""))
            assert not used.startswith("actions/cache"), f"job {name}: {used}"
            inputs = _cache_inputs(step)
            assert all(value in (False, "false", "") for value in inputs.values()), f"job {name}: {used} {inputs}"
            if used.startswith("astral-sh/setup-uv@"):
                assert inputs.get("enable-cache") is False, f"job {name}: setup-uv caches unless told not to"


def test_one_release_of_a_tag_runs_at_a_time_and_none_is_cut_short():
    """Two runs of the same tag (a re-run, a tag deleted and pushed again)
    must not publish side by side; and a run is never cancelled for the
    next one, because cancelling the publishing job between the sdist and
    the wheel leaves on PyPI a version that can never be uploaded whole."""
    concurrency = RELEASE.get("concurrency") or {}
    assert "github.ref" in str(concurrency.get("group", "")), concurrency
    assert concurrency.get("cancel-in-progress") is False
    # Not the group the called CI uses: a caller and a callee in one group
    # wait for each other forever.
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert concurrency["group"] != ci["concurrency"]["group"]


def test_nothing_else_shares_the_group_of_the_ci_a_release_calls():
    """The called CI keeps its own group, `ci-<ref>`, which cancels the run
    in flight. On a tag that ref is the release's alone only while ci.yml
    does not run on a tag push by itself: if it did, that run and the
    release's own CI would cancel each other, and the release would stop
    before it built anything."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert not _runs_on_a_tag_push(ci), ci.get("on", ci.get(True))


def _runs_on_a_tag_push(workflow):
    """Whether a push of a tag starts this workflow. GitHub runs a `push`
    trigger on every ref when it has no ref filter at all (`on: push`,
    `on: [push]`, a bare `push:`, or one filtered by paths only), and on
    branches alone when it filters branches but not tags; any tag filter
    may match a tag."""
    # PyYAML reads the bare key `on` as True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        on = [on]
    if isinstance(on, list):
        return "push" in on
    if not isinstance(on, dict) or "push" not in on:
        return False
    push = on["push"] or {}
    if "tags" in push or "tags-ignore" in push:
        return True
    return "branches" not in push and "branches-ignore" not in push


@pytest.mark.parametrize("on,runs", [
    ({"push": {"branches": ["main"]}}, False),
    ({"push": {"branches-ignore": ["wip/**"]}, "pull_request": None}, False),
    ({"pull_request": None}, False),
    ({"push": None}, True),
    ({"push": {"paths": ["src/**"]}}, True),
    ({"push": {"branches": ["main"], "tags": ["v*"]}}, True),
    ({"push": {"tags-ignore": ["x"]}}, True),
    ({"push": {"branches": ["main"], "tags-ignore": ["x"]}}, True),
    ("push", True),
    (["pull_request", "push"], True),
])
def test_a_tag_push_is_told_apart_from_a_branch_push(on, runs):
    # The key as PyYAML reads it from a file, and as it is written.
    assert _runs_on_a_tag_push({True: on}) is runs
    assert _runs_on_a_tag_push({"on": on}) is runs


def test_the_publishing_token_says_why_it_is_granted():
    """A permission beyond reading is the first line someone auditing the
    workflow asks about; the answer sits on that line."""
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    lines = [line for line in text.splitlines() if re.match(r"\s*id-token:\s*write\b", line)]
    assert lines and all(re.search(r"#\s*\S", line) for line in lines), lines


def test_a_tag_that_is_not_the_declared_version_is_refused_before_anything_is_built():
    # The check is a script of its own, so that it can be run here; it reads
    # pyproject.toml and CHANGELOG.md and compares with the tag. It runs in
    # the first job, which the build waits for through the CI
    # (tests/test_pipeline.py holds that order).
    assert "python3 scripts/release-check.py ." in _commands("tag")
    assert "release-check.py" not in _commands("build"), "checked once, in the job everything waits for"


def _on_main(root):
    """`root` as a repository whose HEAD is main's: what the release check
    reads besides the two files (tests/test_pipeline.py covers the refusal)."""
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "M", "GIT_AUTHOR_EMAIL": "m@example.com",
           "GIT_COMMITTER_NAME": "M", "GIT_COMMITTER_EMAIL": "m@example.com"}
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "release"],
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


@pytest.mark.parametrize("tag,ok", [("v0.2.0", True), ("v0.2.1", False), ("0.2.0", False), ("v0.2.0-rc1", False)])
def test_the_release_check_itself(tmp_path, tag, ok, monkeypatch):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "griot-rag"\nversion = "0.2.0"\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0] — 2026-10-03\n\n- something\n")
    _on_main(root)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "release-check.py"), str(root)],
                          env={"GITHUB_REF_NAME": tag, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)

    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok:
        assert "0.2.0" in done.stderr and tag in done.stderr


def test_the_release_check_wants_a_changelog_section(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "griot-rag"\nversion = "0.2.0"\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n- something\n")
    _on_main(root)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "release-check.py"), str(root)],
                          env={"GITHUB_REF_NAME": "v0.2.0", "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)

    assert done.returncode != 0 and "CHANGELOG" in done.stderr


def test_the_release_check_refuses_a_section_still_marked_unreleased(tmp_path):
    """CONTRIBUTING says to date the heading before tagging; this is what
    makes a tag pushed without that step stop instead of publishing a
    changelog that says the version was never released."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "griot-rag"\nversion = "0.2.0"\n')
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0] — unreleased\n\n- something\n")
    _on_main(root)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "release-check.py"), str(root)],
                          env={"GITHUB_REF_NAME": "v0.2.0", "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)

    assert done.returncode != 0 and "unreleased" in done.stderr and "YYYY-MM-DD" in done.stderr


def test_the_release_check_passes_on_this_repository_once_its_section_is_dated(tmp_path):
    """This repository, with the one step CONTRIBUTING leaves to the
    maintainer done: the heading of the current version given a date."""
    import shutil
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy(ROOT / "pyproject.toml", root / "pyproject.toml")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    dated = re.sub(rf"^(## \[{re.escape(PROJECT['version'])}\]).*$", r"\1 — 2026-10-03", changelog, count=1, flags=re.M)
    (root / "CHANGELOG.md").write_text(dated)
    _on_main(root)
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "release-check.py"), str(root)],
                          env={"GITHUB_REF_NAME": f"v{PROJECT['version']}", "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True)

    assert done.returncode == 0, done.stdout + done.stderr


def test_contributing_says_how_a_release_is_made():
    text = (ROOT / "CONTRIBUTING.md").read_text()
    assert "## Releasing" in text and "pyproject.toml" in text and "git tag" in text and DISTRIBUTION in text
