#!/usr/bin/env bash
# Full verify of an integration worktree: build + typecheck, the test suite,
# and, when asked, the apps/ typecheck and test suite and the infra/
# typecheck, all at the same time.
#
# usage: verify.sh <worktree> [--apps] [--infra] [--no-test]
#
# --no-test skips the test suites (and, with a project profile, every check
# whose name ends in "test"): for a run that changed nothing the suite covers.
#
# It also runs the count ratchets the PR pipeline enforces (ci-ratchets.py:
# `--max-warnings $(API_MAX_WARNINGS)` and the like), because a local lint
# without the cap passes while the server fails (sonar-zero-5).
#
# Logs go to a temp folder outside the worktree, so the tree stays clean. The
# last lines print the folder and one "<check>.rc:<exit code>" line per check.
# It exits 1 when any check's rc is not 0, so `bg.sh wait` reports a failure
# instead of "ok" (sonar-zero-5: "verify: ok" with apps.rc:2).
# Run it through heavy.sh with --weight 3, because it starts several heavy
# commands at once.
set -uo pipefail

wt=$1; shift
apps=0 infra=0 notest=0
for a in "$@"; do
  case $a in
    --apps) apps=1 ;;
    --infra) infra=1 ;;
    --no-test) notest=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

v=$(mktemp -d)
# Named first as well as last, so whoever waits can tail <check>.log there
# while the checks run (verify.log stayed empty until the end before).
echo "logs: $v"
cd "$wt" || exit 2
here="$(dirname "${BASH_SOURCE[0]}")"
py=$(command -v python || command -v python3)
. "$here/lib-packages.sh"
. "$here/lib-profile.sh"

# The pipeline's ratchets, one "<name>	<dir>	<cmd>" line each.
ratchets=""
[ -n "$py" ] && ratchets=$("$py" "$here/ci-ratchets.py" "$wt" 2>/dev/null)
# run_ratchets <dir filter: root|apps|infra|all>: runs those ratchets one after
# another (they are heavy and share the install), each into <name>.log/.rc.
run_ratchets() {
  local want=$1 name d cmd
  while IFS=$'	' read -r name d cmd; do
    [ -n "$cmd" ] || continue
    case $want in
      root) [[ $d == apps* || $d == infra* ]] && continue ;;
      apps) [[ $d == apps* ]] || continue ;;
      infra) [[ $d == infra* ]] || continue ;;
    esac
    (cd "$wt/$d" && bash -c "$cmd" > "$v/$name.log" 2>&1; echo $? > "$v/$name.rc")
  done <<< "$ratchets"
}
has_ratchet() { echo "$ratchets" | awk -F'	' -v w="$1" '$2 ~ "^"w {f=1} END {exit !f}'; }

# finish: print the folder and the rc lines, and exit 1 when any is not 0.
finish() {
  echo "logs: $v"
  if ! ls "$v"/*.rc > /dev/null 2>&1; then
    echo "${1:-no checks}"
    exit 0
  fi
  grep -H . "$v"/*.rc | sed 's#.*/##'
  if grep -qvx 0 "$v"/*.rc; then exit 1; fi
  exit 0
}

# The project profile's checks, all at once, when the project has a profile;
# with none listed, its full suite. --apps and --infra don't apply.
if profile_get "$wt" base > /dev/null; then
  checks=$(profile_get "$wt" checks)
  [ -n "$checks" ] || checks=$(profile_get "$wt" suite | awk -F'\t' '{a[$1]=$2} END {if (a["cmd"] != "") print "suite\t" a["dir"] "\t" a["cmd"]}')
  [ -n "$checks" ] || { echo "no checks: the project profile lists none"; exit 0; }
  while IFS=$'\t' read -r name d cmd; do
    [ -n "$cmd" ] || continue
    [ "$notest" = 1 ] && [[ $name == *test || $name == suite ]] && continue
    (cd "$wt/$d" && bash -c "$cmd" > "$v/$name.log" 2>&1; echo $? > "$v/$name.rc") &
  done <<< "$checks"
  wait
  # The pipeline's ratchets, unless the profile already carries a capped check.
  echo "$checks" | grep -q -- '--max-' || run_ratchets all
  finish
fi

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
  run_ratchets all
  finish "no checks: no package folder defines build, typecheck, lint or test"
fi

# The root ratchets need the build (type-aware lint reads the built packages).
(npm run build > "$v/build.log" 2>&1 && npm run typecheck > "$v/typecheck.log" 2>&1; echo $? > "$v/build-typecheck.rc"
  run_ratchets root) &
[ "$notest" = 1 ] || (npm test > "$v/test.log" 2>&1; echo $? > "$v/test.rc") &
if [ "$apps" = 1 ]; then
  # bun, never npm, inside apps/. The root npm test never reaches apps/, so
  # its own suite runs here (apps-test.rc), after its typecheck to keep the
  # peak load down; --no-test skips it like the root suite.
  # A worktree has no apps/node_modules until someone installs it there
  # (sonar-zero-5: the typecheck failed on it, rc 2); install from the lockfile
  # first. Lint runs with the pipeline's cap when it has one.
  (cd apps && {
    if [ ! -d node_modules ]; then
      bun install --frozen-lockfile > "$v/apps-install.log" 2>&1 || { echo $? > "$v/apps-install.rc"; exit; }
    fi
    bun run typecheck > "$v/apps.log" 2>&1; echo $? > "$v/apps.rc"
    if has_ratchet apps; then run_ratchets apps
    elif has_script . lint; then bun run lint > "$v/apps-lint.log" 2>&1; echo $? > "$v/apps-lint.rc"; fi
    [ "$notest" = 1 ] || { bun test > "$v/apps-test.log" 2>&1; echo $? > "$v/apps-test.rc"; }; }) &
fi
if [ "$infra" = 1 ]; then
  (cd infra && npm ci --no-audit --no-fund --loglevel=error > "$v/infra.log" 2>&1 && npm run typecheck >> "$v/infra.log" 2>&1; echo $? > "$v/infra.rc"
    run_ratchets infra) &
fi
wait
finish
