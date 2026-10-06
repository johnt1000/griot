"""The pipeline: what runs before a change reaches main, and before a release
reaches PyPI.

- A release runs the whole CI on the tagged commit before it builds, and
  refuses a tag on a commit that is not on main: the tag alone used to be
  enough to publish whatever it pointed at.
- The runners are named by version: `ubuntu-latest` changes the operating
  system under every job on a date someone else picks.
- The secret scan runs gitleaks itself, pinned by version and checksum, and
  reads the changes of merge commits too (the action did not, and ran on a
  Node version GitHub is retiring). The test jobs have it as well, so the
  tests that need the real scanner run instead of being skipped.
- The tests run on the newest Python too, and on macOS: the BSD tools there
  behave differently, and that difference has caused real defects.

The names of the jobs main requires are kept: renaming one would leave every
pull request waiting for a check that never comes."""

import platform
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
RELEASE = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
PR_TEXT = yaml.safe_load((ROOT / ".github" / "workflows" / "pr-text.yml").read_text())
INSTALL_GITLEAKS = ROOT / "scripts" / "install-gitleaks.sh"
# The checks main's branch protection requires (set on GitHub, not in this
# repository): keep this list and that setting the same.
REQUIRED_ON_MAIN = ["tests (py3.10)", "tests (py3.13)", "tests (py3.14)", "tests (macos, py3.13)",
                    "secret scan", "package builds and installs"]


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))


def _job_names(workflow):
    names = []
    for job in workflow["jobs"].values():
        name = job.get("name", "")
        include = (job.get("strategy") or {}).get("matrix", {}).get("include")
        if include and "${{" in name:
            names.extend(re.sub(r"\$\{\{\s*matrix\.(\w+)\s*\}\}", lambda m: str(entry[m.group(1)]), name) for entry in include)
        else:
            names.append(name)
    return names


def _runs(job):
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


# --- the checks main requires still exist ---------------------------------------------------------


def test_every_check_main_requires_is_still_a_job_of_ci():
    assert set(REQUIRED_ON_MAIN) <= set(_job_names(CI))


# --- runners ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", [CI, RELEASE, PR_TEXT], ids=["ci", "release", "pr-text"])
def test_every_runner_is_named_by_version(workflow):
    for name, job in workflow["jobs"].items():
        if "uses" in job:
            continue  # a called workflow: its own jobs are checked
        runner = str(job["runs-on"])
        if "${{" in runner:
            include = job["strategy"]["matrix"]["include"]
            runners = {entry["os"] for entry in include}
        else:
            runners = {runner}
        for each in runners:
            assert re.fullmatch(r"(ubuntu|macos)-\d+(\.\d+)?", each), f"{name}: {each}"


# --- the tests ----------------------------------------------------------------------------------------


def test_the_tests_run_on_the_floor_the_current_and_the_newest_python_and_on_macos():
    include = CI["jobs"]["test"]["strategy"]["matrix"]["include"]
    linux = {entry["python"] for entry in include if entry["os"].startswith("ubuntu")}
    mac = {entry["python"] for entry in include if entry["os"].startswith("macos")}
    assert {"3.10", "3.13", "3.14"} <= linux and mac


def test_the_test_jobs_have_the_real_scanner():
    steps = [step for step in CI["jobs"]["test"]["steps"] if "scripts/install-gitleaks.sh" in step.get("run", "")]
    assert steps and all("if" not in step for step in steps), "installed in every test job, unconditionally"


# --- the secret scan --------------------------------------------------------------------------------------


def test_the_secret_scan_runs_gitleaks_itself_over_merges_too():
    secrets = CI["jobs"]["secrets"]
    assert not any("gitleaks/gitleaks-action" in str(step.get("uses", "")) for step in secrets["steps"])
    runs = _runs(secrets)
    assert "scripts/install-gitleaks.sh" in runs
    assert re.search(r'gitleaks"? git .*--log-opts="--all --diff-merges=first-parent"', runs)
    checkout = next(step for step in secrets["steps"] if "actions/checkout@" in str(step.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0


def test_gitleaks_is_installed_by_version_and_checksum():
    text = INSTALL_GITLEAKS.read_text()
    assert re.search(r'^VERSION="\d+\.\d+\.\d+"$', text, re.M)
    assert len(re.findall(r"[0-9a-f]{64}", text)) >= 2, "a checksum for each platform the jobs run on"
    assert text.index("shasum") < text.index("tar "), "checked before anything is unpacked"


@pytest.mark.skipif(f"{platform.system()}-{platform.machine()}" not in ("Linux-x86_64", "Darwin-arm64"),
                    reason="the installer has an archive pinned only for the platforms the jobs run on")
def test_the_installer_refuses_a_download_whose_checksum_does_not_match(tmp_path):
    fake_curl = tmp_path / "bin" / "curl"
    fake_curl.parent.mkdir()
    fake_curl.write_text('#!/bin/sh\nfor a; do out=$a; done\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                         'echo "not the real archive" > "$out"\n')
    fake_curl.chmod(0o755)
    done = subprocess.run(["bash", str(INSTALL_GITLEAKS), str(tmp_path / "out")], capture_output=True, text=True,
                          env={"PATH": f"{fake_curl.parent}:/usr/bin:/bin"})
    assert done.returncode != 0 and "checksum" in done.stderr.lower()
    assert not (tmp_path / "out" / "gitleaks").exists()


# --- the release --------------------------------------------------------------------------------------


def test_ci_can_be_called_by_another_workflow():
    assert "workflow_call" in _triggers(CI)


def test_a_release_runs_the_whole_ci_before_it_builds():
    jobs = RELEASE["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["build"]["needs"] == "ci"
    assert jobs["publish"]["needs"] == "build"


def test_the_release_build_has_the_history_to_tell_whether_the_tag_is_on_main():
    checkout = next(step for step in RELEASE["jobs"]["build"]["steps"] if "actions/checkout@" in str(step.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                               "GIT_AUTHOR_NAME": "M", "GIT_AUTHOR_EMAIL": "m@example.com",
                               "GIT_COMMITTER_NAME": "M", "GIT_COMMITTER_EMAIL": "m@example.com"})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def released_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "griot-rag"\nversion = "0.3.0"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [0.3.0] — 2026-10-06\n\n- something\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _check(repo, sha):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "release-check.py"), str(repo)],
                          env={"GITHUB_REF_NAME": "v0.3.0", "GITHUB_SHA": sha, "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True)


def test_a_tag_on_main_is_released(released_repo):
    done = _check(released_repo, _git(released_repo, "rev-parse", "HEAD"))

    assert done.returncode == 0, done.stderr


def test_a_tag_on_a_commit_that_is_not_on_main_is_refused(released_repo):
    _git(released_repo, "checkout", "-q", "-b", "elsewhere")
    (released_repo / "x").write_text("x\n")
    _git(released_repo, "add", "x")
    _git(released_repo, "commit", "-q", "-m", "not on main")

    done = _check(released_repo, _git(released_repo, "rev-parse", "HEAD"))

    assert done.returncode != 0 and "main" in done.stderr
