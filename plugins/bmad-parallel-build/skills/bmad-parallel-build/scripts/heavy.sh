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
# (default 3; its own default is one per CPU, which is what crashed workers
# when several suites ran at once).
#
# State lives in BMAD_HEAVY_DIR (default ~/.cache/bmad-heavy), one file per
# running command. Entries of dead processes, or older than 4 h, are dropped.
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

waited=0 used=0 avail=0
while :; do
  if lock; then
    read -r used recent < <(survey)
    avail=$(( $(free_mb) - recent ))
    if [ "$used" -eq 0 ] || { [ $((used + weight)) -le "$slots" ] && [ $((avail - mem)) -ge "$floor" ]; }; then
      echo "$$ $weight $mem $(date +%s)" > "$entry"
      unlock
      break
    fi
    unlock
  fi
  if [ "$waited" -ge "$max_wait" ]; then
    echo "heavy.sh: waited ${waited}s for capacity; starting anyway" >&2
    echo "$$ $weight $mem $(date +%s)" > "$entry"
    break
  fi
  [ $((waited % 60)) -eq 0 ] && echo "heavy.sh: waiting for capacity (running weight $used of $slots, ${avail} MB free)" >&2
  sleep 10
  waited=$((waited + 10))
done
trap 'rm -f "$entry"' EXIT

export VITEST_MAX_FORKS="${VITEST_MAX_FORKS:-3}" VITEST_MAX_THREADS="${VITEST_MAX_THREADS:-3}"
bash -c "$cmd"
