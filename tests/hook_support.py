"""Helpers shared by the tests that run this repository's git scripts for real
(test_git_hooks.py, test_audit_history.py): a throwaway repository plus the
isolated environment every git call in it uses."""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
HOOKS = SCRIPTS / "git-hooks"


class Repo:
    """A throwaway repository and the environment its git calls share."""

    def __init__(self, path, env):
        self.path, self.env = path, env

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def __truediv__(self, other):
        return self.path / other


def make_env(gitleaks_bin, author_email="maintainer@example.com"):
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Maintainer", "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": "Maintainer", "GIT_COMMITTER_EMAIL": author_email,
        "GITLEAKS_BIN": gitleaks_bin,
    }


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)


def write_script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)
