#!/usr/bin/env bash
# Install a worktree's dependencies through the machine-wide queue.
#
# usage: install-deps.sh <worktree> [--apps]
#
# npm ci, the contracts build (api's typecheck resolves @hive/contracts through
# its gitignored dist/) and, with --apps, bun install in apps/. Each step is
# retried once, because parallel installs hit EBUSY on Windows. Workers start
# it through `bg.sh run <wt> install ...` right after checkout, so the install
# runs while the worker plans instead of before.
set -uo pipefail

wt=${1:?usage: install-deps.sh <worktree> [--apps]}
apps=0
[ "${2:-}" = --apps ] && apps=1
heavy="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/heavy.sh"

step() {
  bash "$heavy" "$1" && return 0
  echo "install-deps: retrying: $1" >&2
  bash "$heavy" "$1"
}

step "cd '$wt' && npm ci --no-audit --no-fund --loglevel=error" || exit 1
step "cd '$wt' && npm --workspace @hive/contracts run build" || exit 1
if [ "$apps" = 1 ]; then
  step "cd '$wt/apps' && bun install --frozen-lockfile" || exit 1
fi
echo "install-deps: done"
