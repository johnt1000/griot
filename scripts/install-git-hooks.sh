#!/usr/bin/env bash
# Enables this repository's git hooks for THIS clone (git does not carry hook
# settings across clones, so every clone runs this once).
#
# It points git at scripts/git-hooks/, so the hooks are the versioned files
# themselves and stay current with the repository, and creates the local,
# never-committed list of private terms the hooks read. Safe to run again: an
# existing list is left untouched.
#
#   scripts/install-git-hooks.sh          enable the hooks
#   scripts/install-git-hooks.sh --check  report whether they are active and able
#                                         to run; changes nothing. Exit 1 if not.
set -eu

usage() { echo "usage: scripts/install-git-hooks.sh [--check]" >&2; exit 2; }
mode=install
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  *) usage ;;
esac
[ "$#" -le 1 ] || usage

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
. scripts/git-hooks/common.sh

if [ "$mode" = check ]; then
  status=0
  ok() { printf 'ok    %s\n' "$*"; }
  warn() { printf 'warn  %s\n' "$*"; }
  fail() { printf 'FAIL  %s\n' "$*"; status=1; }

  hooks_path=$(git config core.hooksPath || true)
  if [ "$hooks_path" = "scripts/git-hooks" ]; then
    ok "core.hooksPath = $hooks_path"
  else
    fail "core.hooksPath is '${hooks_path:-unset}', expected scripts/git-hooks (run scripts/install-git-hooks.sh)"
  fi
  for hook in pre-commit commit-msg; do
    if [ -x "scripts/git-hooks/$hook" ]; then ok "$hook is executable"; else fail "scripts/git-hooks/$hook is not executable"; fi
  done
  if gitleaks_bin=$(find_gitleaks); then
    ok "gitleaks: $gitleaks_bin"
  else
    fail "gitleaks not found (brew install gitleaks): commits are blocked until it is installed"
  fi
  terms=$(terms_file_path)
  if [ ! -f "$terms" ]; then
    fail "private terms list missing: $terms (run scripts/install-git-hooks.sh)"
  else
    active=$(grep -c -v -E '^[[:space:]]*(#|$)' "$terms" || true)
    if [ "$active" -eq 0 ]; then
      warn "the private list has no active terms: private names are not being checked"
    else
      ok "private list: $active active term(s)"
    fi
  fi
  exit "$status"
fi

git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/pre-commit scripts/git-hooks/commit-msg

terms=$(terms_file_path)
if [ ! -f "$terms" ]; then
  install -m 600 scripts/git-hooks/sensitive-terms.example "$terms"
  echo "created $terms (edit it to add private names; it is never committed)"
fi

if ! find_gitleaks >/dev/null; then
  echo "warning: gitleaks not found. Commits are blocked until it is installed (brew install gitleaks)." >&2
fi

echo "git hooks enabled: pre-commit and commit-msg (core.hooksPath = scripts/git-hooks)"
