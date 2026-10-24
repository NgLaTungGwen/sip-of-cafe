#!/usr/bin/env bash
# Gate one worker's unit before synthesis.
#
# usage: check-unit.sh <worktree> <branch> <spec file>
#
# Prints `READY <commits>` or `NOT READY: <reason>; <reason>` on one line and
# exits 0 or 1. Besides the tree and branch checks it rejects a spec that shows
# the bmad-build-auto workflow did not run: no baseline_revision (step 3 sets it
# before any change), no <intent-contract> (step 2 writes it), or no
# "### <date> — Review pass" entry (step 4 appends one on every pass, even with
# zero findings; the template's HTML comment alone does not count). A worker
# that coded first and wrote the spec afterwards produces exactly that shape.
set -uo pipefail

wt=$1 branch=$2 spec=$3
reasons=()

if [ ! -f "$spec" ]; then
  reasons+=("spec not found: $spec")
else
  front=$(awk 'NR==1 && /^---/ {f=1; next} f && /^---/ {exit} f' "$spec")
  status=$(printf '%s\n' "$front" | sed -nE "s/^status: *'?([a-z-]+)'?.*/\1/p")
  case $status in
    done|built|in-review) ;;
    *) reasons+=("status is '${status:-missing}'") ;;
  esac
  baseline=$(printf '%s\n' "$front" | sed -nE "s/^baseline_revision: *'?([^']*)'?.*/\1/p")
  if [ -z "$baseline" ] || [ "$baseline" = NO_VCS ]; then
    reasons+=("no baseline_revision: implementation did not start from the workflow")
  fi
  grep -q '<intent-contract>' "$spec" || reasons+=("no <intent-contract>: plan step skipped")
  passes=$(awk '/^## Review Triage Log/ {f=1; next} f && /^## / {exit} f && /^### .*Review pass/' "$spec" | wc -l)
  [ "$passes" -gt 0 ] || reasons+=("no review pass in Review Triage Log: review step skipped")
fi

[ -z "$(git -C "$wt" status --porcelain)" ] || reasons+=("dirty tree")
current=$(git -C "$wt" branch --show-current)
[ "$current" = "$branch" ] || reasons+=("on branch '$current', expected '$branch'")
commits=$(git -C "$wt" rev-list --count "origin/main..$branch" 2>/dev/null || echo 0)
[ "$commits" -gt 0 ] || reasons+=("no commits ahead of origin/main")

if [ ${#reasons[@]} -eq 0 ]; then
  echo "READY $commits"
  exit 0
fi
(IFS=';'; echo "NOT READY: ${reasons[*]}")
exit 1
