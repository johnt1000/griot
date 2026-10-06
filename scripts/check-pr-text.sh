#!/usr/bin/env bash
# Refuses a pull request whose title or description credits an AI assistant.
# Run by .github/workflows/pr-text.yml with the two texts in PR_TITLE and
# PR_BODY.
#
# The git hooks refuse such a line in a commit message, but a pull request's
# title and description are written on GitHub, and the squash commit GitHub
# builds from them reaches main without any hook having run. The rule is the
# hooks' own pattern, sourced from common.sh, so the two cannot drift apart.
#
# Both texts are whatever the person who opened the pull request typed: they
# are only ever read as data here, from the environment.
set -u

here=$(cd "$(dirname "$0")" && pwd) || exit 2
. "$here/git-hooks/common.sh"

# Unset means the workflow no longer hands the text over. Passing then would
# pass every pull request without reading it. (An empty description is set,
# to nothing, and is fine.)
if [ -z "${PR_TITLE+set}" ] || [ -z "${PR_BODY+set}" ]; then
  printf 'check-pr-text: PR_TITLE and PR_BODY must both be set by the workflow; nothing was checked.\n' >&2
  exit 2
fi
if [ -z "${ATTRIBUTION_PATTERN:-}" ]; then
  printf 'check-pr-text: no ATTRIBUTION_PATTERN came from common.sh; nothing was checked.\n' >&2
  exit 2
fi

work=$(mktemp -d) || exit 2
trap 'rm -rf "$work"' EXIT
# A description written on the web has Windows line endings: the pattern
# anchors only at the start of a line, so the carriage return at the end of
# each one changes nothing.
printf '%s\n' "$PR_TITLE" > "$work/title" || exit 2
printf '%s\n' "$PR_BODY" > "$work/body" || exit 2

found=0
# grep exits 1 for no match and 2 for an error; an error must not read as
# "nothing found".
title_hits=$(grep -a -i -E "$ATTRIBUTION_PATTERN" "$work/title"); status=$?
[ "$status" -le 1 ] || exit 2
if [ "$status" -eq 0 ]; then
  printf 'check-pr-text: the title credits an AI assistant: %s\n' "$title_hits" >&2
  found=1
fi
body_hits=$(grep -n -a -i -E "$ATTRIBUTION_PATTERN" "$work/body"); status=$?
[ "$status" -le 1 ] || exit 2
if [ "$status" -eq 0 ]; then
  printf '%s\n' "$body_hits" | while IFS=: read -r line text; do
    printf 'check-pr-text: the description, line %s, credits an AI assistant: %s\n' "$line" "$text" >&2
  done
  found=1
fi

if [ "$found" -eq 1 ]; then
  printf 'The author of what is merged here is the person who makes it (an assistant as co-author, a session link, "Generated with"): edit the pull request and take the line out. The check runs again on the edit.\n' >&2
  exit 1
fi
exit 0
