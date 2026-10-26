#!/usr/bin/env bash
# Full verify of an integration worktree: build + typecheck, the test suite,
# and the apps/ and infra/ typechecks when asked, all at the same time.
#
# usage: verify.sh <worktree> [--apps] [--infra]
#
# Logs go to a temp folder outside the worktree, so the tree stays clean. The
# last lines print the folder and one "<check>.rc:<exit code>" line per check.
# Run it through heavy.sh with --weight 3, because it starts several heavy
# commands at once.
set -uo pipefail

wt=$1; shift
apps=0 infra=0
for a in "$@"; do
  case $a in
    --apps) apps=1 ;;
    --infra) infra=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

v=$(mktemp -d)
cd "$wt" || exit 2
. "$(dirname "${BASH_SOURCE[0]}")/lib-packages.sh"

# No package.json at the root (packages in app/ and the like): run, in every
# package folder, each of build, typecheck, lint and test that it defines.
if [ ! -f package.json ]; then
  for d in $(package_dirs "$wt"); do
    pm=$(pm_of "$d"); tag=$(echo "$d" | tr '/' '-')
    for s in build typecheck lint test; do
      has_script "$d" "$s" || continue
      (cd "$d" && $(run_cmd "$pm" "$s") > "$v/$tag-$s.log" 2>&1; echo $? > "$v/$tag-$s.rc") &
    done
  done
  wait
  echo "logs: $v"
  grep . "$v"/*.rc 2>/dev/null | sed 's#.*/##' || echo "no checks: no package folder defines build, typecheck, lint or test"
  exit 0
fi

(npm run build > "$v/build.log" 2>&1 && npm run typecheck > "$v/typecheck.log" 2>&1; echo $? > "$v/build-typecheck.rc") &
(npm test > "$v/test.log" 2>&1; echo $? > "$v/test.rc") &
if [ "$apps" = 1 ]; then
  # bun, never npm, inside apps/
  (cd apps && bun run typecheck > "$v/apps.log" 2>&1; echo $? > "$v/apps.rc") &
fi
if [ "$infra" = 1 ]; then
  (cd infra && npm ci --no-audit --no-fund --loglevel=error > "$v/infra.log" 2>&1 && npm run typecheck >> "$v/infra.log" 2>&1; echo $? > "$v/infra.rc") &
fi
wait
echo "logs: $v"
grep . "$v"/*.rc | sed 's#.*/##'
