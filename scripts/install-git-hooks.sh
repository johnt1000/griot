#!/usr/bin/env bash
# Enables this repository's git hooks for THIS clone (git does not carry hook
# settings across clones, so every clone runs this once).
#
# It points git at scripts/git-hooks/, so the hooks are the versioned files
# themselves and stay current with the repository, and creates the local,
# never-committed list of private terms the hooks read. Safe to run again: an
# existing list is left untouched.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/pre-commit scripts/git-hooks/commit-msg

common_dir=$(cd "$(git rev-parse --git-common-dir)" && pwd)
terms="$common_dir/sensitive-terms.txt"
if [ ! -f "$terms" ]; then
  install -m 600 scripts/git-hooks/sensitive-terms.example "$terms"
  echo "created $terms (edit it to add private names; it is never committed)"
fi

if ! command -v gitleaks >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/gitleaks ] && [ ! -x /usr/local/bin/gitleaks ]; then
  echo "warning: gitleaks not found. Commits are blocked until it is installed (brew install gitleaks)." >&2
fi

echo "git hooks enabled: pre-commit and commit-msg (core.hooksPath = scripts/git-hooks)"
