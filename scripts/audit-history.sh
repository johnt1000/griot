#!/usr/bin/env bash
# audit-history.sh: audits the history of the repository you run it in for what
# the commit hooks refuse in new commits. The hooks only guard what is about to
# be committed, so they say nothing about a term you add to your private list
# today, or about a leak that predates the hooks. Run this after editing
# .git/sensitive-terms.txt and before making a repository public.
#
# Checks, over every commit reachable from any branch, tag or other ref
# (merge commits included):
#   1. secrets in history (gitleaks, same config as CI)
#   2. files that never belong, at any point in history (.env, keys, databases,
#      logs, collection data)
#   3. private terms in file contents
#   4. private terms in file and directory names, including deleted files
#   5. private terms in commit messages and in annotated tag messages
#   6. private terms in branch and tag names
# Terms come from .git/sensitive-terms.txt. Only LOCATIONS are printed (file,
# revision, date, tag), never the matching text of a file or a message: a
# private term must not be copied into a terminal, a log or an agent's context.
#
# NOT covered: binary files, author names and emails (deliberately), commits
# that are unreachable from any ref, the reflog, and remote branches you never
# fetched. What you push is what becomes public, so audit the refs you will push.
#
# Read-only. Exit codes:
#   0  clean: every check ran and found nothing
#   1  findings
#   2  incomplete: it could not run, or a check could not (no gitleaks, no
#      private list, gitleaks failed). Exit 0 is never given for an audit that
#      skipped a check, because it would read as approval to publish.
# A leak in history is not fixed by this script or by deleting the file: revoke
# any real credential first, then decide how to clean the history.
#
# Cost: the content search reads every revision's tree, so it grows with
# revisions x tree size. It prints a note above SLOW_ABOVE revisions.
set -u

usage() { echo "usage: scripts/audit-history.sh   (run inside the repository to audit)" >&2; exit 2; }
[ "$#" -eq 0 ] || usage

hooks_dir=$(cd "$(dirname "$0")/git-hooks" && pwd)
. "$hooks_dir/common.sh"

root=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "audit-history: run this inside a git repository." >&2; exit 2; }
cd "$root" || exit 2

if ! gitleaks_bin=$(find_gitleaks); then
  echo "audit-history: gitleaks not found, so secrets cannot be checked. Install it (brew install gitleaks) or set GITLEAKS_BIN." >&2
  exit 2
fi

work=$(mktemp -d) || exit 2
trap 'rm -rf "$work"' EXIT
problems=0
incomplete=""   # what could not be checked, joined with "; "
BATCH=200       # revisions per `git grep`, to stay under the argument-length limit
SLOW_ABOVE=1000
# gitleaks exits 1 for a leak by default AND for a fatal error (a malformed config, say), so a broken
# scanner would read as a leak. Asking for a dedicated code for leaks tells the two apart.
LEAK_EXIT=3

revisions=$(git rev-list --all | wc -l | tr -d ' ')
echo "audit-history: $revisions revisions"
if [ "$revisions" -gt "$SLOW_ABOVE" ]; then
  echo "audit-history: note: more than $SLOW_ABOVE revisions, the content search may take minutes."
fi

indent() { sed 's/^/    /'; }

# "path (N revisions, e.g. abc1234)" from "rev:path" lines on stdin.
# Split at the FIRST colon only, so a path that contains one stays whole; the
# whole line is used, so a path that contains a tab does too.
summarise() {
  awk '{
    i = index($0, ":"); rev = substr($0, 1, i - 1); p = substr($0, i + 1)
    if (!((p SUBSEP rev) in seen)) { seen[p SUBSEP rev] = 1; n[p]++; if (!(p in eg)) eg[p] = substr(rev, 1, 7) }
  } END {
    for (p in n) printf "%s (%d revision%s, e.g. %s)\n", p, n[p], (n[p] == 1 ? "" : "s"), eg[p]
  }' | sort
}

# result LABEL STATUS [DETAIL_FILE]: one line per check, details indented under it.
result() {
  printf '%-52s %s\n' "$1" "$2"
  [ -n "${3:-}" ] && indent < "$3"
  case "$2" in FOUND*) problems=$((problems + 1)) ;; esac
}

note_incomplete() { incomplete="${incomplete:+$incomplete; }$1"; }

# Paths that ever existed. -m: without it `git log --name-only` omits merge
# commits, so a file added only by a merge would be invisible.
all_names() { git -c core.quotepath=off log --all -m --name-only --format= | sort -u; }

# --- 1. secrets ---------------------------------------------------------------
label="[1/6] secrets (gitleaks)"
if [ "$revisions" -eq 0 ]; then
  result "$label" "ok (no history)"
else
  if [ -f "$root/.gitleaks.toml" ]; then
    "$gitleaks_bin" git --no-banner --redact --exit-code "$LEAK_EXIT" --config "$root/.gitleaks.toml" . > "$work/secrets" 2>&1
  else
    "$gitleaks_bin" git --no-banner --redact --exit-code "$LEAK_EXIT" . > "$work/secrets" 2>&1
  fi
  status=$?
  if [ "$status" -eq 0 ]; then
    result "$label" "ok"
  elif [ "$status" -eq "$LEAK_EXIT" ]; then
    result "$label" "FOUND" "$work/secrets"
  else
    result "$label" "ERROR (gitleaks failed, exit $status)" "$work/secrets"
    note_incomplete "secrets NOT checked: gitleaks failed"
  fi
fi

# --- 2. files that never belong ---------------------------------------------------
label="[2/6] files that never belong"
: > "$work/paths"
[ "$revisions" -gt 0 ] && all_names | grep -i -E "$FORBIDDEN_PATHS_RE" | grep -v -i -E "$ALLOWED_PATHS_RE" > "$work/paths"
if [ -s "$work/paths" ]; then
  result "$label" "FOUND" "$work/paths"
else
  result "$label" "ok"
fi

# --- 3-6. private terms -------------------------------------------------------------
terms_file=$(terms_file_path) || exit 2
patterns="$work/patterns"
skip_reason=""
if [ ! -f "$terms_file" ]; then
  skip_reason="private names NOT checked: $terms_file is missing (scripts/install-git-hooks.sh creates it)"
  note_incomplete "$skip_reason"
elif ! write_active_terms "$terms_file" "$patterns"; then
  skip_reason="private names NOT checked: the list has no active terms"
  note_incomplete "$skip_reason"
elif [ "$revisions" -eq 0 ]; then
  skip_reason="no history"
fi

labels=("[3/6] private terms in file contents" "[4/6] private terms in file names"
        "[5/6] private terms in commit and tag messages" "[6/6] private terms in branch and tag names")
if [ -n "$skip_reason" ]; then
  for label in "${labels[@]}"; do
    result "$label" "skipped ($skip_reason)"
  done
else
  # 3. contents, in batches of revisions. With -z each hit is "rev:path", NUL, line number, NUL, matching
  # text, so after turning NULs into newlines every third line is the "rev:path" to keep; the matching
  # text is dropped right there and never stored.
  : > "$work/content_hits"
  batch=(); n=0
  flush() {
    if [ "$n" -gt 0 ]; then
      git grep -z -I -n -i -F -f "$patterns" "${batch[@]}" -- . 2>/dev/null | tr '\0' '\n' | awk 'NR % 3 == 1' >> "$work/content_hits"
    fi
    batch=(); n=0
  }
  while IFS= read -r rev; do
    batch+=("$rev"); n=$((n + 1))
    [ "$n" -ge "$BATCH" ] && flush
  done < <(git rev-list --all)
  flush
  summarise < "$work/content_hits" > "$work/content_summary"
  if [ -s "$work/content_summary" ]; then
    result "${labels[0]}" "FOUND" "$work/content_summary"
  else
    result "${labels[0]}" "ok"
  fi

  # 4. names, including files deleted long ago
  all_names | grep -i -F -f "$patterns" > "$work/name_hits"
  if [ -s "$work/name_hits" ]; then
    result "${labels[1]}" "FOUND" "$work/name_hits"
  else
    result "${labels[1]}" "ok"
  fi

  # 5. messages: hash and date for a commit, the name for a tag; never the message
  grep_args=()
  while IFS= read -r term; do grep_args+=(--grep="$term"); done < "$patterns"
  git log --all -i -F --date=short --format='commit %h (%ad)' "${grep_args[@]}" > "$work/msg_hits"
  # `%(contents)` of a lightweight tag is its commit's message, already covered above.
  while read -r ref type; do
    if [ "$type" = tag ] && git for-each-ref --format='%(contents)' "$ref" | grep -q -i -F -f "$patterns"; then
      echo "tag ${ref#refs/tags/}" >> "$work/msg_hits"
    fi
  done < <(git for-each-ref --format='%(refname) %(objecttype)' refs/tags)
  if [ -s "$work/msg_hits" ]; then
    result "${labels[2]}" "FOUND" "$work/msg_hits"
  else
    result "${labels[2]}" "ok"
  fi

  # 6. branch and tag names: published by a push exactly like file names
  git for-each-ref --format='%(refname)' | sed 's#^refs/##' | grep -i -F -f "$patterns" > "$work/ref_hits"
  if [ -s "$work/ref_hits" ]; then
    result "${labels[3]}" "FOUND" "$work/ref_hits"
  else
    result "${labels[3]}" "ok"
  fi
fi

if [ "$problems" -gt 0 ]; then
  echo "audit-history: $problems check(s) found problems. Do not rewrite history on your own: revoke any real credential first (deleting it is not enough), then decide how to clean the history."
  exit 1
fi
if [ -n "$incomplete" ]; then
  echo "audit-history: incomplete, NOT clean: $incomplete. Nothing was found by the checks that did run."
  exit 2
fi
echo "audit-history: clean ($revisions revisions)"
exit 0
