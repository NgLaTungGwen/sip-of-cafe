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
# which is what crashed workers when several suites ran at once).
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
mem=${mem:-$((2500 * weight))}

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

waited=0 used=0 avail=0 queue=0
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
  if [ "$waited" -ge "$max_wait" ]; then
    echo "heavy.sh: waited ${waited}s; starting anyway" >&2
    start
    break
  fi
  [ $((waited % 60)) -eq 0 ] && echo "heavy.sh: waiting (${queue} ahead in the queue, running weight $used of $slots, ${avail} MB free)" >&2
  sleep 5
  waited=$((waited + 5))
done

# A quarter of the CPUs per suite, at least 2 (3 on a 12-CPU machine).
vt=$(( $(nproc 2>/dev/null || echo 8) / 4 )); [ "$vt" -lt 2 ] && vt=2
export VITEST_MAX_FORKS="${VITEST_MAX_FORKS:-$vt}" VITEST_MAX_THREADS="${VITEST_MAX_THREADS:-$vt}"
bash -c "$cmd"
