#!/usr/bin/env bash
# Check one unit in the running app: start the app the project profile's
# "app" names, run the worker's drive script against it, stop the app.
#
# usage: app-check.sh <worktree> <drive script>
#
# The profile's "app" is {"dir": ".", "start": "npm run dev -- --port {port}",
# "url": "http://127.0.0.1:{port}/", "timeout": 180}. {port} is the
# worktree's APP_PORT, which prepare-worktree.sh writes to <wt>/.env (one free
# port per worktree, so parallel units don't collide); the start command also
# gets it as $APP_PORT and $PORT. The script waits until <url> answers at all
# (any HTTP status), up to <timeout> seconds.
#
# The drive script (<wt>-relative, run by its extension: .sh with bash, .mjs
# or .js with node, .py with python) runs from <wt> with APP_URL and APP_PORT
# set. It exercises the unit's acceptance criteria (curl for an API, a
# Playwright script for a page, a CLI call), prints what it saw for each, and
# exits non-zero when one fails.
#
# Run it through bg.sh as `app-<spec file name without .md>`, so the log and
# rc land in the worktree's git dir, where check-unit.sh reads them. The first
# line printed is `HEAD <sha>`: the gate rejects the result when code changed
# after it. Exits with the drive script's code, 2 without a profile "app", or
# 3 when the app never answered.
set -uo pipefail

wt=${1:?usage: app-check.sh <worktree> <drive script>} drive=${2:?drive script}
. "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
app=$(profile_get "$wt" app) || { echo "app-check: the project profile has no app"; exit 2; }
field() { printf '%s\n' "$app" | awk -F'\t' -v k="$1" '$1==k{print $2}'; }
dir=$(field dir) start=$(field start) url=$(field url) limit=$(field timeout)
[ -n "$start" ] || { echo "app-check: the profile's app has no start command"; exit 2; }
port=$(sed -nE 's/^APP_PORT=([0-9]+).*/\1/p' "$wt/.env" 2>/dev/null | tail -1)
[ -n "$port" ] || port=$("$PROFILE_PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
url=${url:-"http://127.0.0.1:{port}/"}
start=${start//\{port\}/$port} url=${url//\{port\}/$port}

echo "HEAD $(git -C "$wt" rev-parse HEAD)"
echo "APP $start (port $port, $url)"
gd=$(git -C "$wt" rev-parse --absolute-git-dir) || exit 2
mkdir -p "$gd/bmad"
server_log="$gd/bmad/app-server.log"
(cd "$wt/${dir:-.}" && APP_PORT=$port PORT=$port exec bash -c "$start") > "$server_log" 2>&1 &
pid=$!

# Stop the whole process tree: a dev server is usually npm -> node, and on
# Windows those are native processes that a Git Bash kill does not reach.
kill_tree() {
  local c
  for c in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$c"; done
  kill "$1" 2>/dev/null
}
stop() {
  if command -v taskkill >/dev/null 2>&1 && [ -r "/proc/$pid/winpid" ]; then
    taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1
  fi
  kill_tree "$pid"
  wait "$pid" 2>/dev/null
}
trap stop EXIT

waited=0
until [ "$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)" != 000 ]; do
  if ! kill -0 "$pid" 2>/dev/null || [ "$waited" -ge "${limit:-180}" ]; then
    echo "APP not answering at $url after ${waited}s; server log:"
    tail -n 30 "$server_log"
    exit 3
  fi
  sleep 2
  waited=$((waited + 2))
done
echo "APP up after ${waited}s"

case $drive in
  *.mjs|*.js) run=(node "$drive") ;;
  *.py) run=("$PROFILE_PY" "$drive") ;;
  *) run=(bash "$drive") ;;
esac
echo "DRIVE $drive"
(cd "$wt" && APP_URL=$url APP_PORT=$port "${run[@]}")
rc=$?
echo "DRIVE rc $rc"
exit "$rc"
