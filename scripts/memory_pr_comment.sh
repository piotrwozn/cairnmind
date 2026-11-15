#!/usr/bin/env bash
# Posts or updates the one pull request comment that summarizes published team memory (#172).
#
# Usage: memory_pr_comment.sh COMMENT_FILE
# Environment: GH_TOKEN (the workflow's default token), REPOSITORY (owner/name), NUMBER (the pull request).
#
# COMMENT_FILE is the output of `cairnmind memory diff BASE HEAD --markdown`. Its first line is the
# marker that finds the comment again; its second line says whether anything changed. The comment of
# the workflow's own account that starts with the marker is updated. Without one, a comment is posted
# only when the memory changed or would not import, so a pull request without memory gets no comment.
set -euo pipefail

body=${1:?usage: memory_pr_comment.sh COMMENT_FILE}
marker='<!-- cairnmind-memory-diff -->'
unchanged='<!-- cairnmind-memory-diff-state: unchanged -->'

[[ "${REPOSITORY:-}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "REPOSITORY must be owner/name" >&2; exit 2; }
[[ "${NUMBER:-}" =~ ^[0-9]+$ ]] || { echo "NUMBER must be a pull request number" >&2; exit 2; }
[ "$(sed -n 1p "$body")" = "$marker" ] || { echo "$body is not a memory summary" >&2; exit 2; }
state=$(sed -n 2p "$body")

existing=$(gh api --paginate "repos/$REPOSITORY/issues/$NUMBER/comments" \
  --jq ".[] | select(.user.login == \"github-actions[bot]\" and (.body | startswith(\"$marker\"))) | .id" \
  | sed -n 1p)

if [ -n "$existing" ]; then
  [[ "$existing" =~ ^[0-9]+$ ]] || { echo "unexpected comment ID" >&2; exit 2; }
  gh api --method PATCH "repos/$REPOSITORY/issues/comments/$existing" -F "body=@$body" > /dev/null
  echo "Updated the memory summary comment $existing."
elif [ "$state" != "$unchanged" ]; then
  gh api --method POST "repos/$REPOSITORY/issues/$NUMBER/comments" -F "body=@$body" > /dev/null
  echo "Posted the memory summary comment."
else
  echo "No published memory changed, and there is no earlier comment to update."
fi
