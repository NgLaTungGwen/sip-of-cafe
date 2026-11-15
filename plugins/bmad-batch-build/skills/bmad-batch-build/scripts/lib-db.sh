# Sourced, after lib-profile.sh, by prepare-worktree.sh, remove-worktree.sh and
# db-template.sh: the private test database every worktree gets.
#
# Two modes. hive-factory has no profile ("hive"): Postgres runs in the
# hive-postgres container at 127.0.0.1:55432, the suite appends _test to the
# database in .env and creates, migrates and seeds it itself
# (api/test/test-database.ts), and the grants are createDatabase's. A project
# with a profile ("profile") names its own in "database" (see profile.py).
# Anything else, a project with no postgres DATABASE_URL among them, is "none":
# nothing here runs and no docker call is made.

# db_ident <text>: a database name part, lower case [a-z0-9_].
db_ident() { printf '%s' "$1" | tr -c 'a-zA-Z0-9\n' '_' | tr 'A-Z' 'a-z'; }

# db_mode <root>: hive, profile or none.
db_mode() {
  grep -q '^DATABASE_URL=postgres' "$1/.env" 2>/dev/null || { echo none; return; }
  if ! profile_get "$1" base > /dev/null; then
    echo hive
  elif profile_get "$1" database > /dev/null && [ "$(profile_get "$1" database.per_worktree)" != False ]; then
    echo profile
  else
    echo none
  fi
}

# db_suffix <root> <mode>: what the suite appends to the database in .env.
db_suffix() {
  if [ "$2" = hive ]; then echo _test; else profile_get "$1" database.test_suffix || true; fi
}

# db_sql <root> <mode> <template_sql|clone_sql>: the SQL to run, {db} unfilled.
db_sql() {
  if [ "$2" = profile ]; then profile_get "$1" "database.$3" || true; return; fi
  case $3 in
    template_sql) echo 'GRANT CREATE, USAGE ON SCHEMA public TO hive_owner; GRANT USAGE ON SCHEMA public TO hive_app; ALTER DEFAULT PRIVILEGES FOR ROLE hive_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hive_app; ALTER DEFAULT PRIVILEGES FOR ROLE hive_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO hive_app' ;;
    # CREATE DATABASE ... TEMPLATE copies schema grants, not the database's own.
    clone_sql) echo 'GRANT CONNECT ON DATABASE {db} TO hive_owner, hive_app' ;;
  esac
}

# db_psql <root> <database> <sql>: run SQL as the postgres superuser.
db_psql() {
  local c
  c=$(profile_get "$1" database.container) || c=hive-postgres
  docker exec "$c" psql -U postgres -d "$2" -v ON_ERROR_STOP=1 -qAtc "$3"
}

# db_url <mode> <database>: a sed script that points the DATABASE_URL and
# MIGRATION_DATABASE_URL lines of a .env at <database>. hive also moves the host
# to 127.0.0.1:55432 (the localhost relay on that machine wedges); a profiled
# project keeps its own host.
db_url() {
  if [ "$1" = hive ]; then
    echo "s#^(DATABASE_URL|MIGRATION_DATABASE_URL)=(postgres://[^@]*@)[^/]*/.*#\\1=\\2127.0.0.1:55432/$2#"
  else
    echo "s#^(DATABASE_URL|MIGRATION_DATABASE_URL)=(postgres(ql)?://[^/]*)/[^?]*#\\1=\\2/$2#"
  fi
}

# db_clone <root> <mode> <template> <target>: make <target> a fresh copy of
# <template>; returns 1 when it cannot, for example while something is
# connected to the template, and leaves no half-made <target> behind.
db_clone() {
  local i sql
  db_psql "$1" postgres "DROP DATABASE IF EXISTS $4" > /dev/null 2>&1 || true
  for i in 1 2 3; do
    if db_psql "$1" postgres "CREATE DATABASE $4 TEMPLATE $3" > /dev/null 2>&1; then
      sql=$(db_sql "$1" "$2" clone_sql)
      if [ -n "$sql" ] && ! db_psql "$1" postgres "${sql//\{db\}/$4}" > /dev/null 2>&1; then
        db_drop "$1" "$4"
        return 1
      fi
      return 0
    fi
    [ "$i" = 3 ] || sleep "${BMAD_DB_RETRY_PAUSE:-2}"
  done
  return 1
}

# db_drop <root> <database>: drop it, closing any connection still open on it.
db_drop() {
  db_psql "$1" postgres "DROP DATABASE IF EXISTS $2 WITH (FORCE)" > /dev/null 2>&1 ||
    db_psql "$1" postgres "DROP DATABASE IF EXISTS $2" > /dev/null
}
