#!/usr/bin/env bash
# Run the Linux-only tests a branch changed, in a Linux container, from a
# Windows host.
#
# usage: linux-tests.sh <worktree> [<base revision>]
#
# <base> defaults to the merge base with the run's base branch (origin/<profile
# base>, or origin/main).
#
# Tests guarded by `skipIf(process.platform === "win32")` and the like never
# run on a Windows verify host, so their failures first show on CI
# (sonar-zero-5: two verifier tests, one CI fix round). This finds the test
# files changed since <base> that mention win32 or process.platform, copies the
# worktree into a node container (without node_modules, .git, .env, secrets or
# coverage), installs from the lockfile with scripts off, and runs those files
# with vitest as the image's non-root `node` user, in the folder of the
# package that holds them. No database: DB suites skip as they do without one.
#
# Prints one of, as its last line, and exits:
#   0  "linux-tests: ok (<n> files)"  or  "linux-tests: none (<why>)"
#   1  "linux-tests: failed; log <file>", after the end of the vitest output
#   3  "linux-tests: skipped (<why>)": not Windows, no docker, no npm lockfile
# The log stays in the worktree's private git dir (<git dir>/bmad/linux-tests.log).
set -uo pipefail

wt=${1:?usage: linux-tests.sh <worktree> [<base>]}
. "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
base=${2:-$(git -C "$wt" merge-base HEAD "$(base_ref "$wt")" 2>/dev/null)}
[ -n "$base" ] || { echo "linux-tests: skipped (no base revision to compare with)"; exit 3; }
gd=$(git -C "$wt" rev-parse --absolute-git-dir) || exit 3
out="$gd/bmad"; mkdir -p "$out"
log="$out/linux-tests.log"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) ;;
  *) [ -n "${LINUX_TESTS_FORCE:-}" ] || { echo "linux-tests: skipped (not a Windows host; the suite already runs these)"; exit 3; } ;;
esac

# Changed test files that only run on some platforms.
files=$(git -C "$wt" diff --name-only --diff-filter=AMR "$base" -- '*.spec.ts' '*.test.ts' '*.spec.tsx' '*.test.tsx' '*.spec.js' '*.test.js' 2>/dev/null |
  while read -r f; do [ -f "$wt/$f" ] && grep -qE 'win32|process\.platform' "$wt/$f" && echo "$f"; done)
[ -n "$files" ] || { echo "linux-tests: none (no changed test file is platform-specific)"; exit 0; }
[ -f "$wt/package-lock.json" ] || { echo "linux-tests: skipped (no package-lock.json at the root)"; exit 3; }
command -v docker > /dev/null && docker info > /dev/null 2>&1 || { echo "linux-tests: skipped (docker is not running)"; exit 3; }

# Group the files by the package folder that holds them (the nearest package.json).
declare -A groups
while read -r f; do
  d=$(dirname "$f")
  while [ "$d" != "." ] && [ ! -f "$wt/$d/package.json" ]; do d=$(dirname "$d"); done
  groups[$d]+="${f#"$d"/} "
done <<< "$files"

node_major=22
if [ -f "$wt/.nvmrc" ]; then
  v=$(tr -dc '0-9.' < "$wt/.nvmrc" | cut -d. -f1); [ -n "$v" ] && node_major=$v
fi

runs="" skipped=""
for d in "${!groups[@]}"; do
  # A bun package (hive-factory's apps/) runs bun test, not vitest; its
  # platform checks are not covered here.
  if [ -f "$wt/$d/bun.lock" ] || [ -f "$wt/$d/bun.lockb" ]; then skipped+="$d "; continue; fi
  runs+="cd /w/$d && su node -c 'npx vitest run ${groups[$d]}--reporter=dot' || rc=1; "
done
[ -n "$skipped" ] && echo "linux-tests: not run in bun package(s) ${skipped% }"
[ -n "$runs" ] || { echo "linux-tests: none (only bun packages changed platform-specific tests)"; exit 0; }

src=$(cygpath -w "$wt" 2>/dev/null || echo "$wt")
dst=$(cygpath -w "$out" 2>/dev/null || echo "$out")
MSYS_NO_PATHCONV=1 docker run --rm -v "$src:/src:ro" -v "$dst:/out" "node:$node_major" bash -c "
set -u; rc=0
mkdir -p /w && cd /src && tar --exclude=node_modules --exclude=.git --exclude=.env --exclude=secrets --exclude='coverage*' -cf - . | tar -C /w -xf -
cd /w && npm ci --ignore-scripts --no-audit --no-fund > /tmp/npm-ci.log 2>&1 || { tail -n 40 /tmp/npm-ci.log; exit 2; }
printf '[user]\n email = t@t\n name = t\n[init]\n defaultBranch = main\n' > /home/node/.gitconfig
chown -R node:node /w /home/node/.gitconfig
$runs
exit \$rc" > "$log" 2>&1
rc=$?
n=$(echo "$files" | wc -l | tr -d ' ')
if [ "$rc" -eq 0 ]; then
  echo "linux-tests: ok ($n files)"
  exit 0
fi
tail -n 40 "$log"
echo "linux-tests: failed; log $log"
exit 1
