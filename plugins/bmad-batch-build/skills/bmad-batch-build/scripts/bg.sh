#!/usr/bin/env bash
# Run a long command off an agent's critical path, and wait for it later.
#
# usage: bg.sh run  <worktree> <name> <command string>
#        bg.sh wait <worktree> <name> [<seconds>]
#
# `run` is meant for a Bash call with run_in_background: true. It runs the
# command and writes <name>.log and then <name>.rc into the worktree's private
# git dir (<git dir>/bmad/), so the working tree stays clean for the gate.
#
# `wait` returns as soon as <name>.rc exists, or after <seconds> (default 240).
# Four minutes keeps every wait under the default 5-minute prompt-cache TTL,
# which the Haiku coder and the reviewers use, and which every agent falls
# back to when the 1-hour TTL does not apply. Calling again is cheap. It prints one line, and exits:
#   0  "<name>: ok"
#   1  "<name>: failed (rc N)" followed by the last 40 lines of the log
#   2  "<name>: still running", so call it again
#   3  "<name>: not started"
# A wait whose context is small costs little when the cache goes idle, which
# is why the Haiku coder, not the Opus worker, waits for the install.
set -uo pipefail

mode=${1:?usage: bg.sh run|wait <worktree> <name> ...} wt=${2:?worktree} name=${3:?name}
gd=$(git -C "$wt" rev-parse --absolute-git-dir) || exit 3
dir="$gd/bmad"
mkdir -p "$dir"
log="$dir/$name.log" rc="$dir/$name.rc"

case $mode in
  run)
    cmd=${4:?command}
    rm -f "$rc"
    echo $$ > "$dir/$name.pid"
    t0=$SECONDS
    BMAD_EVENT_LOGGED=1 bash -c "$cmd" > "$log" 2>&1
    code=$?
    echo "$code" > "$rc"
    # One line in the run journal's event log (log-event.py).
    py=$(command -v python || command -v python3)
    [ -n "$py" ] && "$py" "$(dirname "${BASH_SOURCE[0]}")/log-event.py" "bg:$name" "$wt" "$code" "$((SECONDS - t0))" "$cmd" "$log"
    rm -f "$dir/$name.pid"
    echo "$name: $([ "$code" -eq 0 ] && echo ok || echo "failed (rc $code)"); log $log"
    exit "$code"
    ;;
  wait)
    limit=${4:-240} waited=0
    until [ -f "$rc" ]; do
      # Give a run that was launched just now 30 s to write its pid.
      if [ ! -f "$dir/$name.pid" ] && [ "$waited" -ge 30 ]; then
        echo "$name: not started"
        exit 3
      fi
      if [ "$waited" -ge "$limit" ]; then
        echo "$name: still running (log $log)"
        exit 2
      fi
      sleep 5
      waited=$((waited + 5))
    done
    code=$(cat "$rc")
    if [ "$code" -eq 0 ]; then
      echo "$name: ok"
      exit 0
    fi
    echo "$name: failed (rc $code); log $log"
    tail -n 40 "$log"
    exit 1
    ;;
  *)
    echo "unknown mode: $mode" >&2
    exit 2
    ;;
esac
