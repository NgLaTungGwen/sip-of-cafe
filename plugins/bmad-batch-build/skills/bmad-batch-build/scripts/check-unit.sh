#!/usr/bin/env bash
# Gate one worker's unit before synthesis.
#
# usage: check-unit.sh [--bug] [--intake <intake.md> --unit <id> --iface-file <file>
#                      --reply-interfaces "<text>"] <worktree> <branch> <spec file> [<base rev>]
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
#
# --intake <intake.md> --unit <id> --iface-file <file> --reply-interfaces "<text>"
# (a unit dispatched with an intake): <text> is the worker reply's INTERFACES
# value. Every `;`-separated item marked CHANGED that names an interface listed
# under the intake's `## Shared interfaces` needs an entry for this unit and that
# interface in the interface-changes file (`- <ISO> U<id> <symbol>: ...`, from
# `intake.py iface add` after an approved exception). Without one the unit fails:
# it changed a pinned interface on its own.
set -uo pipefail

bug= intake= unit= iface_file= reply_ifaces= args=()
while [ $# -gt 0 ]; do
  case $1 in
    --bug) bug=1 ;;
    --intake) intake=$2; shift ;;
    --unit) unit=$2; shift ;;
    --iface-file) iface_file=$2; shift ;;
    --reply-interfaces) reply_ifaces=$2; shift ;;
    *) args+=("$1") ;;
  esac
  shift
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

if [ -n "$intake" ]; then
  if [ ! -f "$intake" ]; then
    reasons+=("intake not found: $intake")
  else
    u=${unit#U}
    # The symbols pinned under ## Shared interfaces: "- <symbol>: <old> -> <new> | owner U1".
    pinned=$(awk '/^## / {f = ($0 ~ /^## Shared interfaces/); next}
      f && /^- / { s = substr($0, 3); i = index(s, ": "); if (i > 0) s = substr(s, 1, i - 1);
                   gsub(/^[ \t`]+|[ \t`]+$/, "", s); if (s != "" && s != "none") print s }' "$intake" | tr -d '\r')
    if [ -n "$pinned" ]; then
      IFS=';' read -r -a items <<< "$reply_ifaces"
      while IFS= read -r sym; do
        [ -n "$sym" ] || continue
        for it in "${items[@]}"; do
          case $it in *CHANGED*) ;; *) continue ;; esac
          printf '%s' "$it" | grep -qF -- "$sym" || continue
          if [ -n "$u" ]; then want=" U$u $sym:"; else want=" $sym:"; fi
          if [ -z "$iface_file" ] || [ ! -f "$iface_file" ] || ! tr -d '\r' < "$iface_file" | grep -qF -- "$want"; then
            reasons+=("pinned interface '$sym' marked CHANGED with no approved entry for U${u:-?} in ${iface_file:-the interface-changes file} (intake.py iface add)")
          fi
          break
        done
      done <<< "$pinned"
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
