# Sourced by install-deps.sh, merge-units.sh and verify.sh: where a worktree's
# JavaScript packages are, and how each one installs and runs scripts.
#
# A repository with a lockfile at its root keeps the original behaviour (one
# install at the root; hive-factory also gets its contracts build and apps/).
# One without, such as a repository whose packages live in app/, installs in
# every folder up to two levels down that has a lockfile.

# The package manager a folder's lockfile belongs to, or nothing.
pm_of() {
  local d=$1
  if [ -f "$d/bun.lock" ] || [ -f "$d/bun.lockb" ]; then echo bun
  elif [ -f "$d/pnpm-lock.yaml" ]; then echo pnpm
  elif [ -f "$d/yarn.lock" ]; then echo yarn
  elif [ -f "$d/package-lock.json" ] || [ -f "$d/npm-shrinkwrap.json" ]; then echo npm
  fi
}

install_cmd() {
  case $1 in
    npm) echo "npm ci --no-audit --no-fund --loglevel=error" ;;
    bun) echo "bun install --frozen-lockfile" ;;
    pnpm) echo "pnpm install --frozen-lockfile" ;;
    yarn) echo "yarn install --frozen-lockfile" ;;
  esac
}

# "npm run <script>", "bun run <script>", ...
run_cmd() { echo "$1 run $2"; }

# Folders (relative to the worktree, "." for the root) that hold their own
# lockfile, when the root has none. Prints nothing when the root has one.
package_dirs() {
  local wt=$1
  [ -n "$(pm_of "$wt")" ] && return 0
  (cd "$wt" && find . -mindepth 2 -maxdepth 3 \
      \( -name node_modules -o -name .git -o -name .claude -o -name _bmad -o -name _bmad-output \) -prune -o \
      \( -name package-lock.json -o -name npm-shrinkwrap.json -o -name bun.lock -o -name bun.lockb \
         -o -name pnpm-lock.yaml -o -name yarn.lock \) -print 2>/dev/null) |
    sed 's#^\./##; s#/[^/]*$##' | sort -u
}

# Whether a package.json in that folder defines the script.
has_script() {
  [ -f "$1/package.json" ] && node -e "process.exit(require(process.argv[1]).scripts?.[process.argv[2]] ? 0 : 1)" \
    "$(cd "$1" && pwd -W 2>/dev/null || pwd)/package.json" "$2" 2>/dev/null
}
