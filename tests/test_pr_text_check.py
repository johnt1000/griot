"""A pull request's title and description credit no AI assistant either.

The git hooks refuse a commit message that credits an assistant
(ATTRIBUTION_PATTERN in scripts/git-hooks/common.sh), but the title and the
description of a pull request are written on GitHub, and the squash commit
GitHub builds from them lands on main without any hook having run. A check on
every pull request applies the same pattern to both.

The title and the body are text anyone who opens a pull request chooses, so
they reach the script as environment variables and never as `${{ }}` inside a
`run:` script, where GitHub pastes them into the shell code itself."""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "pr-text.yml"
SCRIPT = ROOT / "scripts" / "check-pr-text.sh"
COMMON = ROOT / "scripts" / "git-hooks" / "common.sh"


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _triggers(workflow):
    return workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true


def _check_step():
    steps = [step for job in _workflow()["jobs"].values() for step in job["steps"]
             if "scripts/check-pr-text.sh" in step.get("run", "")]
    assert len(steps) == 1
    return steps[0]


# --- the workflow ---------------------------------------------------------------------------------


def test_it_runs_on_every_pull_request_and_again_when_its_text_is_edited():
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request"}, "never pull_request_target: that runs with the repository's rights"
    # Without `edited`, a title or a body changed after the check passed would
    # never be looked at again.
    assert {"opened", "edited", "synchronize", "reopened"} <= set(triggers["pull_request"]["types"])


def test_the_title_and_the_body_reach_the_script_through_the_environment():
    step = _check_step()
    assert step["env"] == {"PR_TITLE": "${{ github.event.pull_request.title }}",
                           "PR_BODY": "${{ github.event.pull_request.body }}"}


def test_no_run_script_of_the_workflow_has_an_expression_in_it():
    """GitHub replaces `${{ }}` in a `run:` script before the shell sees it:
    a title with a quote and a command in it would be run."""
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step


def test_the_workflow_reads_and_writes_nothing_else():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())


def test_the_pattern_comes_from_the_hooks_and_is_not_copied():
    """One definition: a pattern copied here would drift from the one the
    hooks apply."""
    text = SCRIPT.read_text()
    assert re.search(r'^\. "[^"]*git-hooks/common\.sh"$', text, re.M)
    assert "$ATTRIBUTION_PATTERN" in text
    pattern = re.search(r"^ATTRIBUTION_PATTERN='(.*)'$", COMMON.read_text(), re.M)[1]
    assert pattern not in text
    # Nor a piece of it: a second, partial list is still a second list.
    assert not re.search(r"co-authored-by|claude-session|noreply@|session_", text, re.I)


# --- the check itself, run for real ------------------------------------------------------------------


def _run(title, body, cwd=ROOT):
    env = {"PATH": "/usr/bin:/bin", "PR_TITLE": title, "PR_BODY": body}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=cwd)


DEPENDABOT_BODY = """Bumps [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) from 10.2.0 to 10.3.0.
<details>
<summary>Release notes</summary>
<p><em>Sourced from <a href="https://github.com/astral-sh/setup-uv/releases">astral-sh/setup-uv's releases</a>.</em></p>
<blockquote>
<h2>v10.3.0</h2>
<ul>
<li>Bump the actions group by <a href="https://github.com/dependabot"><code>@dependabot</code></a></li>
</ul>
</blockquote>
</details>
<details>
<summary>Commits</summary>
<ul>
<li><a href="https://github.com/astral-sh/setup-uv/commit/0123456"><code>0123456</code></a> Release 10.3.0</li>
</ul>
</details>
<br />

[![Dependabot compatibility score](https://dependabot-badges.githubapp.com/badges/compatibility_score?dependency-name=astral-sh/setup-uv)](https://docs.github.com/en/github/managing-security-vulnerabilities/about-dependabot-security-updates#about-compatibility-scores)

Dependabot will resolve any conflicts with this PR as long as you don't alter it yourself.

---

<details>
<summary>Dependabot commands and options</summary>
<br />

You can trigger Dependabot actions by commenting on this PR:
- `@dependabot rebase` will rebase this PR
- `@dependabot ignore this major version` will close this PR and stop Dependabot creating any more for this major version
</details>
"""


@pytest.mark.parametrize("title,body", [
    ("fix: the search returns the newest commit first", "Two lines.\r\n\r\nWith Windows line endings.\r\n"),
    ("docs: what Claude reviewed in the release", "Co-authored-by: Claude Martin <claude.martin@example.com>"),
    ("feat: griot installs its skills for Claude Code", "griot now supports Claude Code and opencode.\n"),
    ("build(deps): bump astral-sh/setup-uv from 10.2.0 to 10.3.0", DEPENDABOT_BODY),
    ("chore: a pull request with no description", ""),
], ids=["plain", "a person named Claude", "mentions Claude Code", "dependabot", "empty body"])
def test_text_that_credits_no_assistant_passes(title, body):
    done = _run(title, body)

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("title,body,where", [
    ("fix: something", "Body.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n", "description, line 3"),
    ("fix: something", "Body.\r\n\r\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\r\n", "description, line 3"),
    ("fix: something", "See https://claude.ai/code/session_01AbCdEf", "description, line 1"),
    ("fix: something", "Claude-Session: https://example.com/x", "description, line 1"),
    ("fix: something (Generated with Claude Code)", "", "title"),
], ids=["generated with", "co-author", "session link", "session trailer", "in the title"])
def test_text_that_credits_an_assistant_is_refused_and_says_where(title, body, where):
    done = _run(title, body)

    assert done.returncode == 1
    assert where in done.stderr and "credit" in done.stderr.lower()


def test_without_the_title_the_check_fails_instead_of_passing():
    """The variables missing means the workflow no longer hands them over:
    passing then would pass every pull request unread."""
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PR_BODY": "x"}, cwd=ROOT)

    # 2, not 1: "nothing was read" is a broken workflow, not a finding.
    assert done.returncode == 2 and "PR_TITLE" in done.stderr and "nothing was checked" in done.stderr


def test_without_the_body_variable_the_check_fails_instead_of_passing():
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PR_TITLE": "fix: x"}, cwd=ROOT)

    assert done.returncode == 2 and "PR_BODY" in done.stderr and "nothing was checked" in done.stderr


def test_a_title_that_is_shell_code_is_only_read(tmp_path):
    # Run in a scratch directory: if the text ever were run, what it creates
    # lands there and not in the checkout.
    done = _run('fix: $(touch pwned) `touch pwned2` x"; touch pwned3; echo "', "$(touch pwned4) `touch pwned5`", cwd=tmp_path)

    assert done.returncode == 0, done.stderr
    assert not any((tmp_path / name).exists() for name in ("pwned", "pwned2", "pwned3", "pwned4", "pwned5"))
