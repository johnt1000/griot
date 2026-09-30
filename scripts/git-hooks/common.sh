# Shared by pre-commit, commit-msg, scripts/audit-history.sh and
# scripts/install-git-hooks.sh. Sourced, never run. The caller's working
# directory must be inside the repository.

# Files that never belong in the repository (ERE, matched ignoring case), and
# the one exception. One definition, so the hook that refuses them at commit
# time and the audit that looks for them in history cannot drift apart.
FORBIDDEN_PATHS_RE='(^|/)\.env(rc)?($|\.)|(^|/)id_(rsa|dsa|ecdsa|ed25519)$|\.(db|sqlite3?|pem|key|p12|pfx)$|(^|/)(logs|qdrant_data)/|(^|/)(repos|quality_golden_set)\.json$|\.spend_state\.json$|\.griot\.lock$'
ALLOWED_PATHS_RE='\.env\.example$'

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
# first: under GNU grep an empty pattern matches every line.
write_active_terms() {
  grep -v -E '^[[:space:]]*(#|$)' "$1" > "$2"
  [ -s "$2" ]
}
