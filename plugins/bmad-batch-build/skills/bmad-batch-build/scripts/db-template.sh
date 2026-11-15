#!/usr/bin/env bash
# One migrated template test database per run. prepare-worktree.sh
# --db-template clones it for every worktree (CREATE DATABASE ... TEMPLATE), so
# no worktree shares a test database and none migrates its own from nothing.
#
# usage: db-template.sh <main-root> <run slug>          create and migrate it
#        db-template.sh <main-root> <run slug> --drop   drop it at the end of the run
#
# Prints `DB-TEMPLATE <name>`, or `DB-TEMPLATE none: <why>`, and exits 0 either
# way: without a template each worktree falls back to what it did before (the
# hive-factory suite creates and migrates its own database; a profiled project
# shares the one in .env). A project with no database is "none" at once, with
# no docker call.
#
# The migration runs in the main checkout, which has node_modules, with
# DATABASE_URL and MIGRATION_DATABASE_URL pointed at the template. So the main
# checkout must be at or behind the run's base with no uncommitted changes:
# a migration it has and the base does not would reach every unit's database.
set -uo pipefail

root=${1//\\//} run=${2:?usage: db-template.sh <main-root> <run slug> [--drop]}
here=$(dirname "${BASH_SOURCE[0]}")
. "$here/lib-profile.sh"
. "$here/lib-db.sh"
mode=$(db_mode "$root")
tpl="hive_pb_$(db_ident "$run")_template"

if [ "${3:-}" = --drop ]; then
  [ "$mode" = none ] && exit 0
  if db_drop "$root" "$tpl" 2>/dev/null; then echo "dropped $tpl"; else echo "could not drop $tpl" >&2; fi
  exit 0
fi

none() { echo "DB-TEMPLATE none: $*"; exit 0; }
[ "$mode" = none ] && none "no per-worktree test database for this project"

mdir="" mcmd=""
if m=$(profile_get "$root" database.migrate); then
  mdir=$(printf '%s\n' "$m" | awk -F'\t' '$1=="dir"{print $2}')
  mcmd=$(printf '%s\n' "$m" | awk -F'\t' '$1=="cmd"{print $2}')
elif [ "$mode" = hive ] && grep -q '"db:migrate"' "$root/package.json" 2>/dev/null; then
  mcmd="npm run db:migrate"
fi
[ -n "$mcmd" ] || none "no migrate command (set the profile's database.migrate)"

base=$(base_ref "$root")
git -C "$root" merge-base --is-ancestor HEAD "$base" 2>/dev/null ||
  none "the main checkout is not at or behind $base, so its migrations may not be the run's"
git -C "$root" diff --quiet HEAD -- 2>/dev/null || none "the main checkout has uncommitted changes"

db_drop "$root" "$tpl" > /dev/null 2>&1 || true
db_psql "$root" postgres "CREATE DATABASE $tpl" > /dev/null 2>&1 || none "could not create $tpl (is Postgres up?)"
failed() { db_drop "$root" "$tpl" > /dev/null 2>&1; none "$*"; }
sql=$(db_sql "$root" "$mode" clone_sql)
[ -z "$sql" ] || db_psql "$root" postgres "${sql//\{db\}/$tpl}" > /dev/null 2>&1 || failed "clone_sql failed on $tpl"
sql=$(db_sql "$root" "$mode" template_sql)
[ -z "$sql" ] || db_psql "$root" "$tpl" "$sql" > /dev/null 2>&1 || failed "template_sql failed on $tpl"

rewrite=$(db_url "$mode" "$tpl")
url_of() { grep "^$1=" "$root/.env" | head -1 | tr -d '\r' | sed -E "$rewrite" | cut -d= -f2-; }
url=$(url_of DATABASE_URL) mig=$(url_of MIGRATION_DATABASE_URL)
log=$(mktemp)
if ! (cd "$root/${mdir:-.}" && DATABASE_URL="$url" MIGRATION_DATABASE_URL="${mig:-$url}" bash -c "$mcmd") > "$log" 2>&1; then
  tail -n 20 "$log" | sed -E 's#//[^/@[:space:]]*@#//***@#g' >&2
  rm -f "$log"
  failed "the migration failed on $tpl (its last lines are above)"
fi
rm -f "$log"
echo "DB-TEMPLATE $tpl"
