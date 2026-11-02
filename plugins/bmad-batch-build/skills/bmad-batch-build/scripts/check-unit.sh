#!/usr/bin/env bash
# Gate one worker's unit before synthesis.
#
# usage: check-unit.sh [--bug] <worktree> <branch> <spec file> [<base rev>]
#
# <base rev> defaults to origin/<the project profile's base>, else origin/main. In lane mode the units share one branch,
# so pass the HEAD recorded before the unit started.
#
# Prints `READY <commits>` or `NOT READY: <reason>; <reason>` on one line and
# exits 0 or 1. Besides the tree and branch checks it rejects a spec that shows
# the bmad-build-auto workflow did not run: no baseline_revision (step 3 sets it
# before any change), no <intent-contract> (step 2 writes it), or no
# "### <date> — Review pass" entry (step 4 appends one on every pass, even with
# zero findings; the template's HTML comment alone does not count). A worker
# that coded first and wrote the spec afterwards produces exactly that shape.
#
# --bug (a unit whose intent says `Kind: bug`): the spec's `## Reproduction`
# must hold a `RED:` line (the reproduction failing before the fix) and a
# `GREEN:` line (passing after it).
#
# When the project profile has an "app", the unit must also have passed
# app-check.sh, run through bg.sh as app-<spec file name without .md>: its rc
# file is 0, and no code outside _bmad-output changed after the HEAD it
# checked.
set -uo pipefail

bug= args=()
for a in "$@"; do
  case $a in
    --bug) bug=1 ;;
    *) args+=("$a") ;;
  esac
done
set -- "${args[@]}"
wt=$1 branch=$2 spec=$3
. "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
base=${4:-$(base_ref "$wt")}
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
  if [ -n "$bug" ]; then
    repro=$(awk '/^## Reproduction/ {f=1; next} f && /^## / {exit} f' "$spec")
    printf '%s\n' "$repro" | grep -qE '^[-* ]*RED:' || reasons+=("bug unit with no RED: line under ## Reproduction")
    printf '%s\n' "$repro" | grep -qE '^[-* ]*GREEN:' || reasons+=("bug unit with no GREEN: line under ## Reproduction")
  fi
fi

if profile_get "$wt" app.start > /dev/null; then
  name="app-$(basename "$spec" .md)"
  dir="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)/bmad"
  if [ ! -f "$dir/$name.rc" ]; then
    reasons+=("no app check ($name)")
  elif [ "$(cat "$dir/$name.rc")" != 0 ]; then
    reasons+=("app check failed (rc $(cat "$dir/$name.rc"), log $dir/$name.log)")
  else
    checked=$(sed -nE 's/^HEAD ([0-9a-f]+).*/\1/p' "$dir/$name.log" | head -1)
    if [ -z "$checked" ] || ! git -C "$wt" diff --quiet "$checked" "$branch" -- . ':(exclude)_bmad-output' 2>/dev/null; then
      reasons+=("code changed after the app check ($name): run it again")
    fi
  fi
fi

[ -z "$(git -C "$wt" status --porcelain)" ] || reasons+=("dirty tree")
current=$(git -C "$wt" branch --show-current)
[ "$current" = "$branch" ] || reasons+=("on branch '$current', expected '$branch'")
commits=$(git -C "$wt" rev-list --count "$base..$branch" 2>/dev/null || echo 0)
[ "$commits" -gt 0 ] || reasons+=("no commits ahead of $base")

if [ ${#reasons[@]} -eq 0 ]; then
  echo "READY $commits"
  exit 0
fi
(IFS=';'; echo "NOT READY: ${reasons[*]}")
exit 1
