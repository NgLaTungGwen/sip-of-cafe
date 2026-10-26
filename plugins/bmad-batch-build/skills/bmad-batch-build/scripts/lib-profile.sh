# Sourced by the scripts that build, test and install: reads the project
# profile (profile.py). profile_get <root or worktree> <key> prints the value
# and returns 0, or returns 1 when there is no profile or no such key, in which
# case the script keeps its original behaviour.
PROFILE_PY=$(command -v python || command -v python3)
profile_get() {
  [ -n "$PROFILE_PY" ] || return 1
  local out
  out=$("$PROFILE_PY" "$(dirname "${BASH_SOURCE[0]}")/profile.py" get "$1" "$2" 2>/dev/null) || return 1
  # Python on Windows ends lines with \r\n.
  printf '%s\n' "$out" | tr -d '\r'
}
