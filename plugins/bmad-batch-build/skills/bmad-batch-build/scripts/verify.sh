#!/usr/bin/env bash
# Verify of an integration worktree: build + typecheck, the test suite, and
# the apps/ typecheck and test suite and the infra/ typecheck, all at the same
# time, limited to what the branch changed.
#
# usage: verify.sh <worktree> [--apps] [--infra] [--no-test] [--full] [--only <check>[,<check>]]
#
# What runs: the checks whose dir has a code change since the merge base with
# the base branch (affected.py select; the second block of output says which
# and why). A change to docs, YAML or scripts only runs no check at all. A
# path that can break any test (affected.py FORCE_FULL: packages/contracts,
# migrations, test setup, vitest/tsconfig config, lockfiles, the profile)
# runs everything, and so does --full. apps/ and infra/ are checked when they
# changed, with or without --apps and --infra; those flags matter only when
# there is no diff to read (no git or no commit), where everything runs as it
# always did. --no-test skips the test suites (with a project profile, every
# check whose name ends in "test"). --only runs just the named checks, the way
# to re-run the one that failed after a fix.
#
# A check that passed is not run twice on the same tree: its rc is kept under
# <git dir>/bmad/verify/<HEAD^{tree}>/<check>.rc, with its command, and a later
# verify of the same tree (clean, same command) reuses it and prints
# "cached: <check>". A fix commit changes the tree, so after one the checks
# the diff selects run again.
#
# When the project has a database (the profile's "database", or a postgres
# DATABASE_URL in .env) and a test suite is selected, its port must answer:
# otherwise database.rc is 1, the suites don't run, and verify fails, instead
# of the database suites skipping themselves and verify saying ok.
#
# Failures recorded as already failing on the base commit (affected.py known)
# are named with "known-on-base:" lines; they still fail the verify.
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
apps=0 infra=0 notest=0 full=0 only=""
while [ $# -gt 0 ]; do
  case $1 in
    --apps) apps=1 ;;
    --infra) infra=1 ;;
    --no-test) notest=1 ;;
    --full) full=1 ;;
    --only) only=${2:-}; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

v=$(mktemp -d)
# Named first as well as last, so whoever waits can tail <check>.log there
# while the checks run (verify.log stayed empty until the end before).
echo "logs: $v"
cd "$wt" || exit 2
wt=$(pwd)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py=$(command -v python || command -v python3)
. "$here/lib-packages.sh"
. "$here/lib-profile.sh"
base=$(base_ref "$wt")

# The cache of passed checks, for a clean tree only: HEAD^{tree} says nothing
# about uncommitted changes.
cache="" tree=""
if gd=$(git rev-parse --absolute-git-dir 2>/dev/null) && tree=$(git rev-parse --verify -q 'HEAD^{tree}' 2>/dev/null) \
    && [ -z "$(git status --porcelain 2>/dev/null)" ]; then
  cache="$gd/bmad/verify/$tree"
fi

# select <rows>: decides, from "name<TAB>dir<TAB>kind" rows, which checks run,
# prints the choice and why, and keeps the chosen ones in $v/.selected.
# Without Python every row runs.
select_checks() {
  local rows=$1 flags=() line what name kind why
  [ "$apps" = 1 ] && flags+=(--apps)
  [ "$infra" = 1 ] && flags+=(--infra)
  [ "$notest" = 1 ] && flags+=(--no-test)
  [ "$full" = 1 ] && flags+=(--full)
  [ -n "$only" ] && flags+=(--only "$only")
  : > "$v/.selected"
  if [ -z "$py" ]; then
    printf '%s\n' "$rows" | awk -F'\t' 'NF {print $1 "\t" $3}' > "$v/.selected"
    return
  fi
  while IFS=$'\t' read -r what name kind why; do
    case $what in
      mode) echo "selection: $name ($kind)" ;;
      select) printf '%s\t%s\n' "$name" "$kind" >> "$v/.selected"; echo "  run  $name: $why" ;;
      skip) echo "  skip $name: $why" ;;
    esac
  done < <(printf '%s\n' "$rows" | "$py" "$here/affected.py" select "$wt" --base "$base" "${flags[@]}" \
             --separate "${separate:-}" | tr -d '\r')
  [ -n "$cache" ] && echo "cache: tree ${tree:0:12}" || echo "cache: off (uncommitted changes, or no commit)"
  # The database, before any suite needs it.
  if awk -F'\t' '$2=="test" {f=1} END {exit !f}' "$v/.selected"; then
    local msg
    if ! msg=$("$py" "$here/affected.py" db "$wt" | tr -d '\r'); then
      echo "$msg" > "$v/database.log"; echo 1 > "$v/database.rc"
      echo "$msg"
      awk -F'\t' '$2=="test" {print "  skip " $1 ": the database is down"}' "$v/.selected"
      awk -F'\t' '$2!="test"' "$v/.selected" > "$v/.selected.tmp" && mv "$v/.selected.tmp" "$v/.selected"
    else
      echo "$msg"
    fi
  fi
}
want() { awk -F'\t' -v n="$1" '$1==n {f=1} END {exit !f}' "$v/.selected"; }

# run_check <name> <dir> <cmd>: runs one check into <name>.log/.rc, or reuses
# its rc 0 from an earlier verify of the same tree. Sets ran=1 when it ran.
run_check() {
  local name=$1 d=$2 cmd=$3 rc key
  key="$d"$'\t'"$cmd"
  if [ -n "$cache" ] && [ "$(cat "$cache/$name.rc" 2>/dev/null)" = 0 ] \
      && [ "$(cat "$cache/$name.cmd" 2>/dev/null)" = "$key" ]; then
    echo 0 > "$v/$name.rc"
    echo "passed at tree $tree in an earlier verify; not run again" > "$v/$name.log"
    echo "cached: $name (passed at tree ${tree:0:12})"
    return 0
  fi
  (cd "$wt/$d" && bash -c "$cmd") > "$v/$name.log" 2>&1; rc=$?
  echo $rc > "$v/$name.rc"
  ran=1
  if [ -n "$cache" ]; then
    mkdir -p "$cache" && printf '%s' "$key" > "$cache/$name.cmd" && echo $rc > "$cache/$name.rc"
  fi
  return 0
}

# The pipeline's ratchets, one "<name>	<dir>	<cmd>" line each.
ratchets=""
[ -n "$py" ] && ratchets=$("$py" "$here/ci-ratchets.py" "$wt" 2>/dev/null | tr -d '\r')
# ratchet_rows <dir filter>: the ratchets as selection rows.
ratchet_rows() {
  local want=$1 name d cmd
  while IFS=$'	' read -r name d cmd; do
    [ -n "$cmd" ] || continue
    case $want in
      root) [[ $d == apps* || $d == infra* ]] && continue ;;
      apps) [[ $d == apps* ]] || continue ;;
      infra) [[ $d == infra* ]] || continue ;;
    esac
    printf '%s\t%s\tratchet\n' "$name" "$d"
  done <<< "$ratchets"
}
# run_ratchets <dir filter: root|apps|infra|all>: runs the selected ratchets
# one after another (they are heavy and share the install).
run_ratchets() {
  local name d cmd
  while IFS=$'	' read -r name d _; do
    [ -n "$name" ] && want "$name" || continue
    cmd=$(printf '%s\n' "$ratchets" | awk -F'\t' -v n="$name" '$1==n {sub(/^[^\t]*\t[^\t]*\t/, ""); print; exit}')
    run_check "$name" "$d" "$cmd"
  done < <(ratchet_rows "$1")
}
any_wanted() { local r; while IFS=$'\t' read -r r _; do [ -n "$r" ] && want "$r" && return 0; done <<< "$1"; return 1; }
has_ratchet() { echo "$ratchets" | awk -F'	' -v w="$1" '$2 ~ "^"w {f=1} END {exit !f}'; }

# finish: print the folder and the rc lines, and exit 1 when any is not 0.
finish() {
  echo "logs: $v"
  if ! ls "$v"/*.rc > /dev/null 2>&1; then
    echo "${1:-no checks}"
    exit 0
  fi
  # Failures already known on the base commit, named so they are triaged once.
  if [ -n "$py" ] && grep -qvx 0 "$v"/*.rc; then
    local name sha note
    while IFS=$'\t' read -r name sha note; do
      [ -f "$v/$name.rc" ] && [ "$(cat "$v/$name.rc")" != 0 ] && echo "known-on-base: $name ($base $sha): $note"
    done < <("$py" "$here/affected.py" known "$wt" --base "$base" | tr -d '\r')
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
  rows=$(printf '%s\n' "$checks" | awk -F'\t' 'NF {print $1 "\t" $2 "\t" (($1 ~ /test$/ || $1 == "suite") ? "test" : "check")}')
  # The pipeline's ratchets, unless the profile already carries a capped check.
  capped=0; echo "$checks" | grep -q -- '--max-' && capped=1
  [ "$capped" = 1 ] || rows+=$'\n'$(ratchet_rows all)
  separate="" select_checks "$rows"
  while IFS=$'\t' read -r name d cmd; do
    [ -n "$cmd" ] && want "$name" || continue
    run_check "$name" "$d" "$cmd" &
  done <<< "$checks"
  wait
  [ "$capped" = 1 ] || run_ratchets all
  finish "no checks selected"
fi

# No package.json at the root (packages in app/ and the like): run, in every
# package folder, each of build, typecheck, lint and test that it defines.
if [ ! -f package.json ]; then
  rows=""
  for d in $(package_dirs "$wt"); do
    tag=$(echo "$d" | tr '/' '-')
    for s in build typecheck lint test; do
      has_script "$d" "$s" || continue
      rows+="$tag-$s"$'\t'"$d"$'\t'$([ "$s" = test ] && echo test || echo check)$'\n'
    done
  done
  rows+=$(ratchet_rows all)
  separate="" select_checks "$rows"
  for d in $(package_dirs "$wt"); do
    pm=$(pm_of "$d"); tag=$(echo "$d" | tr '/' '-')
    for s in build typecheck lint test; do
      want "$tag-$s" || continue
      run_check "$tag-$s" "$d" "$(run_cmd "$pm" "$s")" &
    done
  done
  wait
  run_ratchets all
  finish "no checks: no package folder defines build, typecheck, lint or test, or none was selected"
fi

# A root package.json (hive-factory): the root build, typecheck and suite, and
# apps/ and infra/ beside them. The root checks cover everything but apps/ and
# infra/, which have their own.
rows="build-typecheck"$'\t.\tcheck\n'"test"$'\t.\ttest\n'
rows+=$(ratchet_rows root)$'\n'
if [ -f apps/package.json ]; then
  rows+="apps"$'\tapps\tcheck\n'
  has_ratchet apps || ! has_script apps lint || rows+="apps-lint"$'\tapps\tcheck\n'
  rows+="apps-test"$'\tapps\ttest\n'
  rows+=$(ratchet_rows apps)$'\n'
fi
if [ -f infra/package.json ]; then
  rows+="infra"$'\tinfra\tcheck\n'
  rows+=$(ratchet_rows infra)$'\n'
fi
separate="apps,infra" select_checks "$rows"

# The root ratchets need the build (type-aware lint reads the built packages):
# when the build-typecheck check doesn't run here, the build runs first anyway.
if want build-typecheck || any_wanted "$(ratchet_rows root)"; then
  (ran=0
   want build-typecheck && run_check build-typecheck . "npm run build && npm run typecheck"
   if [ "$ran" = 0 ] && any_wanted "$(ratchet_rows root)"; then npm run build > "$v/build.log" 2>&1; fi
   run_ratchets root) &
fi
want test && run_check test . "npm test" &
if want apps || want apps-lint || want apps-test || any_wanted "$(ratchet_rows apps)"; then
  # bun, never npm, inside apps/. The root npm test never reaches apps/, so
  # its own suite runs here (apps-test.rc), after its typecheck to keep the
  # peak load down; --no-test skips it like the root suite.
  # A worktree has no apps/node_modules until someone installs it there
  # (sonar-zero-5: the typecheck failed on it, rc 2); install from the lockfile
  # first. Lint runs with the pipeline's cap when it has one.
  ({
    if [ ! -d apps/node_modules ]; then
      (cd apps && bun install --frozen-lockfile) > "$v/apps-install.log" 2>&1 || { echo $? > "$v/apps-install.rc"; exit; }
    fi
    want apps && run_check apps apps "bun run typecheck"
    if has_ratchet apps; then run_ratchets apps
    elif want apps-lint; then run_check apps-lint apps "bun run lint"; fi
    # Its own test script when it has one: bun test is bun's own runner and
    # ignores a vitest.config (threat-intel's "test": "vitest run --coverage"; ti-port).
    apps_test="bun test"; has_script apps test && apps_test="bun run test"
    want apps-test && run_check apps-test apps "$apps_test"; }) &
fi
if want infra || any_wanted "$(ratchet_rows infra)"; then
  (want infra && run_check infra infra "npm ci --no-audit --no-fund --loglevel=error && npm run typecheck"
    run_ratchets infra) &
fi
wait
finish "no checks selected"
