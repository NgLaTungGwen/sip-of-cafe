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

# base_ref <root or worktree>: the revision a run branches from and compares
# against: origin/<base>, or plain <base> in a repository without that remote
# branch (no origin at all, or a branch never pushed).
base_ref() {
  local b
  b=$(profile_get "$1" base || echo main)
  if git -C "$1" rev-parse --verify --quiet "origin/$b" >/dev/null 2>&1; then
    echo "origin/$b"
  else
    echo "$b"
  fi
}
