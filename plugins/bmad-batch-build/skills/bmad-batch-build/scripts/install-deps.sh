#!/usr/bin/env bash
# Install a worktree's dependencies through the machine-wide queue.
#
# usage: install-deps.sh <worktree> [--apps]
#
# The root install (npm ci for a package-lock.json), the contracts build when
# packages/contracts exists (api's typecheck resolves @hive/contracts through
# its gitignored dist/) and, with --apps, bun install in apps/. A repository
# without a root lockfile installs every package folder instead
# (lib-packages.sh). Each step is
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

. "$(dirname "${BASH_SOURCE[0]}")/lib-packages.sh"

# No lockfile at the root: install each package folder with its own manager.
root_pm=$(pm_of "$wt")
if [ -z "$root_pm" ]; then
  dirs=$(package_dirs "$wt")
  [ -n "$dirs" ] || { echo "install-deps: no lockfile in $wt or its package folders; nothing to install"; exit 0; }
  for d in $dirs; do
    step "cd '$wt/$d' && $(install_cmd "$(pm_of "$wt/$d")")" || exit 1
  done
  echo "install-deps: done ($(echo $dirs | tr ' ' ','))"
  exit 0
fi

step "cd '$wt' && $(install_cmd "$root_pm")" || exit 1
if [ -d "$wt/packages/contracts" ]; then
  step "cd '$wt' && npm --workspace @hive/contracts run build" || exit 1
fi
if [ "$apps" = 1 ]; then
  step "cd '$wt/apps' && bun install --frozen-lockfile" || exit 1
fi
echo "install-deps: done"
