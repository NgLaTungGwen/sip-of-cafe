#!/usr/bin/env bash
# The mechanical part of synthesis: merge the unit branches, then check and
# repair what merges cleanly but breaks.
#
# usage: merge-units.sh <worktree> [--installed <rev>] [<branch>:<title> ...]
#
# Merges each branch in order with --no-ff. A branch already in HEAD is
# skipped, so after resolving a conflict (and committing it) the same call can
# simply be run again. On a conflict it stops, leaves the merge in progress and
# exits 3 after printing "CONFLICT <branch>" and the conflicted files.
#
# Once every branch is in, it prints one line per check:
#   MIGRATIONS  ok, or the numbers that collide (the agent renumbers them)
#   CONTRACTS   unchanged, or synced (it ran sync-contracts.mjs and committed)
#   DEPS        unchanged, or reinstalled (a lockfile differs from --installed,
#               the revision the worktree's node_modules was installed from)
# and exits 0, or 4 when a check needs the agent.
set -uo pipefail

wt=${1:?usage: merge-units.sh <worktree> [--installed <rev>] [<branch>:<title> ...]}
shift
installed=""
if [ "${1:-}" = --installed ]; then installed=$2; shift 2; fi
cd "$wt" || exit 2
scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
heavy="$scripts/heavy.sh"

for arg in "$@"; do
  br=${arg%%:*} title=${arg#*:}
  if git merge-base --is-ancestor "$br" HEAD; then
    echo "MERGED $br (already in HEAD)"
    continue
  fi
  if git merge --no-ff "$br" -m "merge: $title" > /dev/null 2>&1; then
    echo "MERGED $br"
  else
    echo "CONFLICT $br"
    git diff --name-only --diff-filter=U | sed 's/^/  /'
    exit 3
  fi
done

need=0

# Migration numbers: the numeric prefixes of every migration on HEAD must be
# unique, so a unit's new file must not reuse a number from origin/main or
# from another unit.
mig=compose/db/migrations
if [ -d "$mig" ]; then
  dups=$(ls "$mig" | sed -nE 's/^([0-9]+).*/\1/p' | sort | uniq -d)
  if [ -z "$dups" ]; then
    echo "MIGRATIONS ok"
  else
    need=1
    for n in $dups; do
      echo "MIGRATIONS collide on $n: $(ls "$mig" | grep -E "^$n([^0-9]|$)" | tr '\n' ' ')"
      echo "  added since origin/main: $(git diff --name-only --diff-filter=A origin/main -- "$mig" | grep -E "/$n([^0-9]|$)" | tr '\n' ' ')"
    done
  fi
fi

# Contracts: regenerate the apps/ copy whenever the source changed.
if git diff --quiet origin/main -- packages/contracts/src; then
  echo "CONTRACTS unchanged"
else
  if node apps/scripts/sync-contracts.mjs > /dev/null 2>&1; then
    if [ -n "$(git status --porcelain -- apps/src/contracts)" ]; then
      git add apps/src/contracts && git commit -qm "fix(merge): sync generated contracts"
      echo "CONTRACTS synced and committed"
    else
      echo "CONTRACTS in sync"
    fi
  else
    need=1
    echo "CONTRACTS sync failed: node apps/scripts/sync-contracts.mjs"
  fi
fi

# Dependencies: reinstall only what a merged lockfile changed.
if [ -n "$installed" ]; then
  deps=""
  if ! git diff --quiet "$installed" -- package-lock.json; then
    bash "$heavy" "npm ci --no-audit --no-fund --loglevel=error > /dev/null" || { need=1; deps="$deps npm-ci-failed"; }
    deps="$deps npm"
  fi
  if [ -d packages/contracts ] && { ! git diff --quiet "$installed" -- packages/contracts || [[ $deps == *npm* ]]; }; then
    bash "$heavy" "npm --workspace @hive/contracts run build > /dev/null" || { need=1; deps="$deps contracts-build-failed"; }
    deps="$deps contracts-build"
  fi
  if [ -f apps/bun.lock ] && ! git diff --quiet "$installed" -- apps/bun.lock; then
    bash "$heavy" "cd apps && bun install --frozen-lockfile > /dev/null" || { need=1; deps="$deps bun-failed"; }
    deps="$deps bun"
  fi
  # Packages without a root lockfile (lib-packages.sh): each folder whose
  # lockfile changed and that was installed.
  . "$(dirname "${BASH_SOURCE[0]}")/lib-packages.sh"
  for d in $(package_dirs .); do
    [ -d "$d/node_modules" ] || continue
    git diff --quiet "$installed" -- "$d/package-lock.json" "$d/bun.lock" "$d/bun.lockb" "$d/pnpm-lock.yaml" "$d/yarn.lock" && continue
    bash "$heavy" "cd '$d' && $(install_cmd "$(pm_of "$d")") > /dev/null" || { need=1; deps="$deps $d-failed"; }
    deps="$deps $d"
  done
  echo "DEPS ${deps:+reinstalled:}${deps:-unchanged}"
fi

exit $((need ? 4 : 0))
