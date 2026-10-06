# Shared by pre-commit, commit-msg, pre-push, scripts/audit-history.sh and
# scripts/install-git-hooks.sh. Sourced, never run. The caller's working
# directory must be inside the repository.

# Every text tool these scripts use (grep, cut, tr, sort, sed, awk) reads
# BYTES. In a UTF-8 locale several of them stop at a byte that is not valid
# UTF-8: one stops matching at it, another refuses the whole line, another
# gives up on its input, each with exit status 0 or with nothing a pipeline
# looks at. A file in another encoding was enough for a term on the same line,
# or a name, to go unseen. One setting, here, and not one per command: the
# command that was forgotten is the one that leaks.
#
# git itself reads bytes whatever the locale, and in the caller's locale its
# own searches also ignore the case of an accented letter ("AÇÃO" for a term
# written "ação"), which the system's tools never did. So the two searches
# git does itself, `git grep` and `git log --grep`, run through
# in_caller_locale.
CALLER_LC_ALL=${LC_ALL-}
CALLER_LC_ALL_WAS_SET=${LC_ALL+yes}
export LC_ALL=C
in_caller_locale() {
  if [ -n "$CALLER_LC_ALL_WAS_SET" ]; then
    LC_ALL=$CALLER_LC_ALL "$@"
  else
    env -u LC_ALL "$@"
  fi
}

# Files that never belong in the repository (ERE, matched ignoring case), and
# the one exception. One definition, so the hook that refuses them at commit
# time and the audit that looks for them in history cannot drift apart.
#
# An environment file goes by many names (.env, prod.env, .env-prod, .env~,
# app.env.local, a .env.d/ or .envs/ directory), so the rule is the `.env`
# itself, wherever it stands in a name, when the name ends there or goes on
# with anything that is not a letter or a digit. `env.py`, `.envoy.yml` and
# `environment.md` are not it. A name that carries no `.env` (a bare `env`,
# `.flaskenv`) is not recognised: the rule is about the name, and gitleaks
# is what reads the contents.
FORBIDDEN_PATHS_RE='\.env(rc)?($|[^A-Za-z0-9])|(^|/)\.envs/|(^|/)id_(rsa|dsa|ecdsa|ed25519)$|\.(db|sqlite3?|pem|key|p12|pfx)$|(^|/)(logs|qdrant_data)/|(^|/)(repos|quality_golden_set)\.json$|\.spend_state\.json$|\.index_jobs\.json$|\.griot\.lock$'
# Templates of one, which hold names and no values.
ALLOWED_PATHS_RE='\.env\.(example|sample|template|dist)$'

# forbidden_paths: reads names, one per line, and prints the ones that never
# belong.
forbidden_paths() {
  grep -i -E "$FORBIDDEN_PATHS_RE" | grep -v -i -E "$ALLOWED_PATHS_RE"
}

# The names every hook of this repository goes by: the installer makes them
# executable and its --check looks at each.
HOOK_NAMES='pre-commit commit-msg pre-merge-commit pre-push'

# What makes `git log -p` (which is what gitleaks reads) print the changes of
# a merge commit: without it a merge shows no diff at all, and whatever the
# merge itself added is never scanned.
GITLEAKS_MERGES='--diff-merges=first-parent'

# Prints the path of a usable gitleaks and returns 0, or returns 1.
# GITLEAKS_BIN wins when set, even if it points nowhere: an explicit choice
# that does not work must fail, not fall back to something else.
find_gitleaks() {
  local bin=${GITLEAKS_BIN:-} candidate
  if [ -z "$bin" ]; then
    for candidate in "$(command -v gitleaks 2>/dev/null)" /opt/homebrew/bin/gitleaks /usr/local/bin/gitleaks; do
      if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        bin=$candidate
        break
      fi
    done
  fi
  if [ -n "$bin" ] && [ -x "$bin" ]; then
    printf '%s\n' "$bin"
    return 0
  fi
  return 1
}

# Prints where the private terms list lives: inside the git directory, so it is
# never committed. Shared by every worktree of a repository.
terms_file_path() {
  local root common
  root=$(git rev-parse --show-toplevel) || return 1
  common=$(cd "$root" && cd "$(git rev-parse --git-common-dir)" && pwd) || return 1
  printf '%s/sensitive-terms.txt\n' "$common"
}

# write_active_terms LIST DEST: copies the active terms of LIST into DEST and
# returns 0 when there is at least one. Blank lines and # comments are dropped
# first: under GNU grep an empty pattern matches every line. So are carriage
# returns (a list saved with Windows line endings had one at the end of every
# term, and no term matched anything) and a byte order mark in front of the
# first line (the first term never matched).
write_active_terms() {
  local mark
  mark=$(printf '\357\273\277')
  tr -d '\r' < "$1" | sed "1s/^$mark//" | grep -v -E '^[[:space:]]*(#|$)' > "$2"
  [ -s "$2" ]
}

# A line of a commit message that credits an AI assistant: as co-author, by a
# session link, or as "Generated with/by". The author of a commit here is the
# person who makes it. Naming an assistant otherwise is fine (griot supports
# Claude Code, and a commit says so): only the credit is refused. Extended
# regular expression, matched per line and ignoring case; `#` may lead the
# line, since a message given with -m keeps such a line. A co-author is an
# assistant by the forms assistants sign with (a model name, a vendor's
# noreply address, an agent's name), not by a word a person can carry:
# Claude is a given name, and people work at these vendors.
ATTRIBUTION_PATTERN='^[[:space:]#]*co-authored-by:.*(claude (opus|sonnet|haiku|code|[0-9])|noreply@(anthropic|openai)\.com|chatgpt|copilot|gemini|codex|cursor ?agent|devin[ -]ai|aider|windsurf|codeium)|^[[:space:]#]*claude-session:|claude\.ai/code/session_|generated (with|by)[^a-z]{0,8}(claude|chatgpt|copilot|gemini|codex|cursor|devin|aider|windsurf)'
