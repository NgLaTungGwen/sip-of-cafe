#!/usr/bin/env bash
# Run the tests a unit's change affects, instead of the full suite: the full
# suite runs once, in synthesis.
#
# usage: affected-tests.sh <worktree> <base rev>
#
# affected.py tests decides what runs (see it for the rules): vitest's
# --changed in each touched vitest workspace, jest's --changedSince, in a bun
# workspace the changed test files and the ones beside the changed sources;
# the full suite when the change touches a path that can break any test
# (affected.py FORCE_FULL) or a workspace whose runner it doesn't know.
# Prints each command and why, runs them one after another, and exits 1 when
# any fails. Workers start it through bg.sh and heavy.sh, as the pinned facts
# in the worktree's bmad-build-auto.toml say.
set -uo pipefail
wt=${1:?usage: affected-tests.sh <worktree> <base rev>}
base=${2:?usage: affected-tests.sh <worktree> <base rev>}
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py=$(command -v python || command -v python3)
[ -n "$py" ] || { echo "affected tests: no python to decide with" >&2; exit 2; }
plan=$("$py" "$here/affected.py" tests "$wt" --base "$base" | tr -d '\r') || exit 2
rc=0
while IFS=$'\t' read -r what d cmd why; do
  case $what in
    NONE) echo "affected tests: none: $d" ;;
    RUN)
      echo "affected tests: $why"
      echo "+ cd $d && $cmd"
      (cd "$wt/$d" && bash -c "$cmd") < /dev/null || rc=1 ;;
  esac
done <<< "$plan"
exit $rc
