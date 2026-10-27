#!/usr/bin/env bash
# Run one heavy command (install, build, full test suite, typecheck) under a
# machine-wide admission check shared by every session and every run.
#
# usage: heavy.sh [--weight N] [--mem MB] <command string>
#
# The goal is that every parallel run finishes without crashing, not that the
# machine stays idle: CPU and RAM may run high. A command starts when, counting
# the commands already running across all sessions,
#   - the running weight plus its own stays within BMAD_HEAVY_SLOTS (default:
#     the CPU count), and
#   - free RAM, minus what commands started in the last 90 s will still take,
#     covers its --mem estimate plus a BMAD_HEAVY_FLOOR_MB reserve (default 2048).
# The first command always starts, so nothing waits forever, and after
# BMAD_HEAVY_MAX_WAIT seconds (default 1500) a command starts anyway with a
# warning. Vitest is capped at VITEST_MAX_FORKS / VITEST_MAX_THREADS workers
# (default a quarter of the CPUs, at least 2; its own default is one per CPU,
# which is what crashed workers when several suites ran at once). The minimum is
# set to 1 with it: Vitest 2 defaults the minimum to CPUs - 1 and refuses to
# start when that is above the maximum.
#
# Waiting commands start in arrival order. State lives in BMAD_HEAVY_DIR
# (default ~/.cache/bmad-heavy): a wait.* ticket per waiting command and a
# run.* entry per running one. Entries of dead processes, or older than 4 h,
# are dropped.
set -uo pipefail

weight=1 mem=""
while [ $# -gt 1 ]; do
  case $1 in
    --weight) weight=$2; shift 2 ;;
    --mem) mem=$2; shift 2 ;;
    *) break ;;
  esac
done
cmd=${1:?usage: heavy.sh [--weight N] [--mem MB] <command string>}
# Memory to reserve, by kind of command, unless --mem says: a typecheck or lint
# needs far less than a test suite, and reserving 2.5 GB for each held
# typechecks behind installs for over 20 minutes on 2026-10-26.
if [ -z "$mem" ]; then
  case $cmd in
    *typecheck*|*"tsc "*|*"tsc -"*|*" lint"*|*eslint*) mem=1000 ;;
    *"npm ci"*|*"npm install"*|*"bun install"*|*"pnpm install"*|*"yarn install"*) mem=1500 ;;
    *) mem=2500 ;;
  esac
  mem=$((mem * weight))
fi

dir="${BMAD_HEAVY_DIR:-$HOME/.cache/bmad-heavy}"
slots="${BMAD_HEAVY_SLOTS:-$(nproc 2>/dev/null || echo 8)}"
floor="${BMAD_HEAVY_FLOOR_MB:-2048}"
max_wait="${BMAD_HEAVY_MAX_WAIT:-1500}"
mkdir -p "$dir"
entry="$dir/run.$$"

free_mb() {
  # Without /proc/meminfo (macOS) only the slot count applies.
  [ -r /proc/meminfo ] || { echo 999999; return; }
  # Cygwin/MSYS report available physical memory as MemFree.
  awk '/^MemAvailable:/ {a=$2} /^MemFree:/ {f=$2} END {print int((a ? a : f) / 1024)}' /proc/meminfo
}

lock() {
  local n=0
  until mkdir "$dir/.lock" 2>/dev/null; do
    # A holder that died leaves the lock behind; it is never held for long.
    if [ -n "$(find "$dir/.lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
      rmdir "$dir/.lock" 2>/dev/null
    fi
    n=$((n + 1)); [ $n -gt 600 ] && return 1
    sleep 0.1
  done
}
unlock() { rmdir "$dir/.lock" 2>/dev/null; }

# Prints "<running weight> <MB still to be claimed by recent starts>".
survey() {
  local now used=0 recent=0 f pid w m start
  now=$(date +%s)
  for f in "$dir"/run.*; do
    [ -e "$f" ] || continue
    read -r pid w m start < "$f" || { rm -f "$f"; continue; }
    if ! kill -0 "$pid" 2>/dev/null || [ $((now - start)) -gt 14400 ]; then
      rm -f "$f"; continue
    fi
    used=$((used + w))
    [ $((now - start)) -lt 90 ] && recent=$((recent + m))
  done
  echo "$used $recent"
}

# First come, first served: every command takes a ticket named by its arrival
# time, and only the oldest live ticket may start. A later, smaller command
# never overtakes an earlier one, even when it would fit.
ticket="$dir/wait.$(date +%s%N).$$"
echo "$$" > "$ticket"
trap 'rm -f "$entry" "$ticket"' EXIT

# Prints how many live tickets are older than this one, dropping dead ones.
ahead() {
  local f n=0
  for f in "$dir"/wait.*; do
    [ -e "$f" ] || continue
    [ "$f" = "$ticket" ] && break
    if kill -0 "$(cat "$f" 2>/dev/null)" 2>/dev/null; then n=$((n + 1)); else rm -f "$f"; fi
  done
  echo "$n"
}

start() {
  echo "$$ $weight $mem $(date +%s)" > "$entry"
  rm -f "$ticket"
}

waited=0 used=0 avail=0 queue=0 t_queue=$SECONDS t_said=$((SECONDS - 60))
while :; do
  if lock; then
    queue=$(ahead)
    read -r used recent < <(survey)
    avail=$(( $(free_mb) - recent ))
    if [ "$queue" -eq 0 ] && { [ "$used" -eq 0 ] || { [ $((used + weight)) -le "$slots" ] && [ $((avail - mem)) -ge "$floor" ]; }; }; then
      start
      unlock
      break
    fi
    unlock
  fi
  # Real elapsed time: a loop also spends time on the lock and the survey, and
  # counting only the sleeps once let waiters sit for hours past max_wait.
  waited=$((SECONDS - t_queue))
  if [ "$waited" -ge "$max_wait" ]; then
    echo "heavy.sh: waited ${waited}s; starting anyway" >&2
    start
    break
  fi
  if [ $((SECONDS - t_said)) -ge 60 ]; then
    echo "heavy.sh: waiting (${queue} ahead in the queue, running weight $used of $slots, ${avail} MB free)" >&2
    t_said=$SECONDS
  fi
  # Ten to fourteen seconds, so dozens of waiters don't fight over the lock
  # (on 2026-10-26, 28 waiters ended in "fork: Resource temporarily unavailable").
  sleep $((10 + RANDOM % 5))
done

# A quarter of the CPUs per suite, at least 2 (3 on a 12-CPU machine).
vt=$(( $(nproc 2>/dev/null || echo 8) / 4 )); [ "$vt" -lt 2 ] && vt=2
export VITEST_MAX_FORKS="${VITEST_MAX_FORKS:-$vt}" VITEST_MAX_THREADS="${VITEST_MAX_THREADS:-$vt}"
export VITEST_MIN_FORKS="${VITEST_MIN_FORKS:-1}" VITEST_MIN_THREADS="${VITEST_MIN_THREADS:-1}"

# Every command goes into the run journal's event log (log-event.py), unless
# bg.sh runs this one and logs it itself.
# Each command has a time limit (BMAD_HEAVY_TIMEOUT, default 45 min): seven
# npm ci runs that hung on 2026-10-26 held their slots for over an hour and
# stopped every build on the machine.
limit="${BMAD_HEAVY_TIMEOUT:-2700}"
run() {
  timeout -k 30 "$limit" bash -c "$cmd"
  local rc=$?
  [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] && echo "heavy.sh: stopped after ${limit}s (BMAD_HEAVY_TIMEOUT): $cmd" >&2
  return "$rc"
}
if [ -n "${BMAD_EVENT_LOGGED:-}" ]; then
  run
  exit $?
fi
out=$(mktemp)
t0=$SECONDS
run 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
py=$(command -v python || command -v python3)
[ -n "$py" ] && "$py" "$(dirname "${BASH_SOURCE[0]}")/log-event.py" heavy "$PWD" "$rc" "$((SECONDS - t0))" "$cmd" "$out"
rm -f "$out"
exit "$rc"
