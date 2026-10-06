"""What the build runs and installs is pinned.

The continuous integration named its actions by a tag (`actions/checkout@v4`),
which whoever owns the action can move to other code, and installed the
dependencies with whatever versions were newest that day, although the
repository carries a lock file with a hash for every package. A release of
any dependency, or a moved tag, ran in the build with nobody having looked.

An action is named by the commit it is, everything installed comes from the
lock (the build backend included), and a bot proposes the updates so that
each one is a reviewed change. For someone installing griot, the two
dependencies whose interfaces griot is written against (the vector store
and the MCP SDK) are held below the version that may change them.

The workflows are read as YAML, not matched line by line: a check that goes
by how a line is written is passed by writing the line another way."""

import re
import sys
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # the floor griot supports
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(path for path in WORKFLOW_DIR.iterdir() if path.suffix in (".yml", ".yaml"))
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())

# Every command of a workflow that fetches or installs code is one of these,
# word for word. Each was looked at: it installs from uv.lock, or under a
# constraint made from it, or installs nothing at all. A new one fails the
# test until it is added here, which is the moment to look at it.
COMMANDS_LOOKED_AT = {
    "uv sync --locked --extra dev",
    "uv run --no-sync pytest -q",
    "uv export --locked --only-group build --output-file /tmp/build.txt",
    "uv build --build-constraints /tmp/build.txt --require-hashes",
    "uv sync --locked --only-group build",
    "uv run --no-sync twine check dist/*",
    "uv export --locked --no-dev --no-emit-project --output-file /tmp/locked.txt",
    "uv venv /tmp/fresh",
    "uv pip install --python /tmp/fresh/bin/python --constraint /tmp/locked.txt dist/*.whl",
    # gitleaks itself is installed by scripts/install-gitleaks.sh, pinned by
    # version and checksum (tests/test_pipeline.py); this only runs it.
    '"$RUNNER_TEMP/gitleaks/gitleaks" git --no-banner --redact --config .gitleaks.toml '
    '--log-opts="--all --diff-merges=first-parent" .',
    # dependabot-lock.yml. The rewrite: the pinned uv on pyproject.toml and
    # uv.lock, building nothing; it resolves metadata from the index and
    # installs nothing, and scripts/lock-versions-unchanged.py refuses any
    # change of version, source or hash it would make.
    "uv lock --upgrade-package griot-rag --no-build",
    "if git diff --quiet -- uv.lock; then changed=false; else changed=true; fi",
    # The commit and the push of that file: git only, fetching nothing.
    'git -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" '
    'commit --quiet -m "chore(deps): write uv.lock with the uv the workflows pin [dependabot skip]" -- uv.lock',
    'git -c http.https://github.com/.extraheader="AUTHORIZATION: basic $(printf \'x-access-token:%s\' "$TOKEN" '
    '| base64 -w0)" push origin "HEAD:refs/heads/$BRANCH"',
}
# A program that brings code or packages from somewhere else.
FETCHES = re.compile(r"(?<![\w.-])(pip3?|uvx?|pipx|conda|npm|npx|yarn|pnpm|curl|wget|apt|apt-get|brew|cargo|gem|docker|git)"
                     r"(?![\w.-])")


def _documents():
    return [(workflow, yaml.safe_load(workflow.read_text())) for workflow in WORKFLOWS]


def _every(node, key):
    """Every value under `key`, at any depth."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            yield from _every(value, key)
    elif isinstance(node, list):
        for item in node:
            yield from _every(item, key)


def _uses():
    return [(workflow.name, value) for workflow, document in _documents() for value in _every(document, "uses")]


def _steps():
    return [(workflow.name, step) for workflow, document in _documents()
            for job in document["jobs"].values() for step in job.get("steps", [])]


def _commands():
    """Each command a workflow runs: one per line, a line that ends in a
    backslash joined to the next."""
    found = []
    for workflow, document in _documents():
        for script in _every(document, "run"):
            for line in re.sub(r"\\\n\s*", " ", script).splitlines():
                line = " ".join(line.split())
                if line and not line.startswith("#"):
                    found.append((workflow.name, line))
    return found


# --- the actions ------------------------------------------------------------------------------


def test_there_is_something_to_check():
    assert WORKFLOWS and len(_uses()) >= 4 and len(_commands()) >= 8


@pytest.mark.parametrize("where,used", _uses())
def test_an_action_is_named_by_the_commit_it_is(where, used):
    """A tag or a branch can be moved to other code by whoever owns the
    action; a commit cannot. (An image or a local action has no commit to
    name: neither is used, and one that appears is looked at then.)"""
    if str(used).startswith("./.github/workflows/"):
        # A workflow of this repository, at the same commit: nothing to pin.
        assert (ROOT / str(used)).is_file(), f"{where}: {used} does not exist"
        return
    action, _, ref = str(used).rpartition("@")
    assert re.fullmatch(r"[\w.-]+/[\w./-]+", action), f"{where}: {used} is not an action of a repository"
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{where}: {used} is not named by a commit"


@pytest.mark.parametrize("where,used", _uses())
def test_the_version_that_commit_is_stands_beside_it(where, used):
    """For a person to read, and for the bot that proposes the next one."""
    if str(used).startswith("./.github/workflows/"):
        return  # a workflow of this repository has no version of its own
    text = (WORKFLOW_DIR / where).read_text()
    assert re.search(re.escape(str(used)) + r"[ \t]+#[ \t]*v\d+\.\d+\.\d+[ \t]*$", text, re.M), f"{where}: {used}"


def test_a_workflow_has_no_more_rights_than_it_needs():
    """Two exceptions, named: the job that publishes to PyPI holds an OpenID
    token (`id-token: write`), in a protected environment, and nothing else;
    the job that commits Dependabot's rewritten lock holds `contents: write`
    and runs no action but checkout and download-artifact
    (tests/test_dependabot_lock.py holds the rest of it)."""
    for workflow, document in _documents():
        assert document.get("permissions") == {"contents": "read"}, workflow.name
        for name, job in document["jobs"].items():
            wider = {what: level for what, level in (job.get("permissions") or {}).items() if level not in ("read", "none")}
            publishes = any("pypa/gh-action-pypi-publish@" in str(step.get("uses", "")) for step in job.get("steps", []))
            if publishes:
                assert wider == {"id-token": "write"} and job.get("environment"), f"{workflow.name}: job {name}"
                continue
            if workflow.name == "dependabot-lock.yml" and wider:
                actions = {str(step["uses"]).split("@")[0] for step in job.get("steps", []) if "uses" in step}
                assert wider == {"contents": "write"} and actions <= {"actions/checkout", "actions/download-artifact"}, (
                    f"{workflow.name}: job {name} asks for {wider} and uses {actions}")
                continue
            assert not wider, f"{workflow.name}: job {name} asks for {wider}"
        triggers = document.get("on", document.get(True))  # YAML 1.1 reads a bare `on` as true
        named = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
        assert not named & {"pull_request_target", "workflow_run"}, "runs with the repository's rights on code from a fork"


def test_a_checkout_leaves_no_credential_behind():
    """The token checkout uses stays in .git/config for every later step
    unless it is told otherwise, and later steps run what was installed."""
    checkouts = [(where, step) for where, step in _steps() if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    for where, step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False, f"{where}: {step}"


# --- what a workflow installs -------------------------------------------------------------------


@pytest.mark.parametrize("where,command", [(w, c) for w, c in _commands() if FETCHES.search(c)])
def test_a_command_that_fetches_or_installs_was_looked_at(where, command):
    assert command in COMMANDS_LOOKED_AT, (
        f"{where}: `{command}` fetches or installs code and is not on the list in this test. Add it there once it "
        "installs from uv.lock (or under a constraint made from it), never the newest version of the day.")


def test_the_list_holds_nothing_the_workflows_no_longer_run():
    """An entry nobody runs is a permission nobody is using."""
    assert COMMANDS_LOOKED_AT == {command for _, command in _commands() if FETCHES.search(command)}


def test_the_tests_run_on_what_the_lock_file_says():
    """`--locked`: from uv.lock, and the build stops when the lock no longer
    matches pyproject.toml."""
    commands = [command for _, command in _commands()]
    assert "uv sync --locked --extra dev" in commands and not any("pip install -e" in command for command in commands)


def test_the_installer_of_the_build_is_itself_a_fixed_version():
    installs = [(where, step) for where, step in _steps() if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert installs, "uv is what installs everything else"
    for where, step in installs:
        version = str((step.get("with") or {}).get("version"))
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{where}: setup-uv installs uv {version}"


def test_contributing_names_the_uv_the_workflows_pin_for_writing_the_lock():
    """Two versions of uv write the same resolution differently (where a
    Python marker goes), and both forms pass `--locked`, so nothing in CI
    notices which one wrote uv.lock; the next `uv lock` on the other version
    rewrites dozens of unrelated lines. Dependabot writes the lock with its
    own uv, so the drift comes back on its own: dependabot-lock.yml rewrites
    it there (tests/test_dependabot_lock.py), and everywhere else the guard
    is a person running the pinned uv, which CONTRIBUTING has to name. The version is
    read from the workflows so that raising it there fails here until the
    instruction says the same."""
    versions = {str((step.get("with") or {}).get("version")) for _, step in _steps()
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")}
    assert len(versions) == 1, f"the workflows pin more than one uv: {sorted(versions)}"
    pinned = versions.pop()
    text = " ".join((ROOT / "CONTRIBUTING.md").read_text().split())
    # Any version named anywhere in the file must be the pinned one: a stale
    # number left in one sentence would send someone back to the other form.
    named = set(re.findall(r"\buv@([0-9][^\s`]*)", text))
    assert named <= {pinned}, f"CONTRIBUTING.md names uv {sorted(named - {pinned})}, the workflows pin {pinned}"
    # The plain command, closed by its backtick, so that the restore recipe
    # below (which starts with the same words) cannot stand in for it.
    assert f"`uvx uv@{pinned} lock`" in text, f"CONTRIBUTING.md does not tell how to run uv {pinned} to write the lock"
    # How to bring a lock another uv wrote back to the pinned form without
    # moving any pin: re-resolving only the project itself.
    assert f"uvx uv@{pinned} lock --upgrade-package {PROJECT['project']['name']}" in text


def test_the_build_backend_is_in_the_lock_too():
    """[build-system] is resolved when the package is built, outside the
    lock. The `build` group repeats it so that the lock pins it, and the
    packaging job hands the lock to the build as a constraint."""
    backend = {Requirement(text).name for text in PROJECT["build-system"]["requires"]}
    group = {Requirement(text).name for text in PROJECT["dependency-groups"]["build"]}
    assert backend and backend <= group
    assert backend <= {package["name"] for package in LOCK["package"]}


# --- the lock file ------------------------------------------------------------------------------


def _asked(requirements, where):
    return {(Requirement(text).name, tuple(sorted(Requirement(text).extras)), str(Requirement(text).specifier), where)
            for text in requirements}


def _locked(entries, where=None):
    found = set()
    for entry in entries:
        extra = re.search(r"extra == '([a-z-]+)'", entry.get("marker", ""))  # the Python part is uv's own spelling
        specifier = str(Requirement(f"x{entry.get('specifier', '')}").specifier)
        found.add((entry["name"], tuple(sorted(entry.get("extras", []))), specifier, where or (extra[1] if extra else None)))
    return found


def test_the_lock_file_is_what_the_project_file_asks_for():
    """Read from the two files, with no resolver and no network: the lock
    records the requirements it was made from. A dependency added to the
    project and not to the lock would be installed by nobody's choice (and
    `uv sync --locked` stops the build on it)."""
    project = PROJECT["project"]
    own = next(package for package in LOCK["package"] if package["name"] == project["name"])

    asked = _asked(project["dependencies"], None)
    for extra, requirements in project.get("optional-dependencies", {}).items():
        asked |= _asked(requirements, extra)
    assert _locked(own["metadata"]["requires-dist"]) == asked

    for group, requirements in PROJECT.get("dependency-groups", {}).items():
        assert _locked(own["metadata"]["requires-dev"][group], group) == _asked(requirements, group)


def test_every_locked_package_has_a_hash():
    def files(package):
        return package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])

    from_an_index = [package for package in LOCK["package"] if package.get("source", {}).get("registry")]
    without = [package["name"] for package in from_an_index
               if not files(package) or not all(str(file.get("hash", "")).startswith("sha256:") for file in files(package))]
    assert len(from_an_index) > 50 and without == []


# --- for someone who installs griot -------------------------------------------------------------


@pytest.mark.parametrize("name,below", [("qdrant-edge-py", "<0.9"), ("mcp", "<3")])
def test_a_dependency_whose_interface_griot_is_written_against_has_a_ceiling(name, below):
    """Before 1.0 a minor version may change the interface, and a major one
    always may. griot calls both of these directly, all over."""
    requirement = next(r for r in map(Requirement, PROJECT["project"]["dependencies"]) if r.name == name)
    bounds = {str(part) for part in requirement.specifier}
    assert below in bounds and any(bound.startswith(">=") for bound in bounds), str(requirement)


# --- and the updates come as changes to review --------------------------------------------------


def test_a_bot_proposes_the_updates_of_the_actions_and_of_the_lock():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert config["version"] == 2
    assert sorted(update["package-ecosystem"] for update in config["updates"]) == ["github-actions", "uv"]
    assert all(update["directory"] == "/" and update["schedule"]["interval"] for update in config["updates"])
