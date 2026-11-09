#!/usr/bin/env bash
# Run one heavy command (install, build, full test suite, typecheck) under a
# machine-wide admission check shared by every session and every run.
#
# usage: heavy.sh [--weight N] [--mem MB] <command string>
#        (separate words, as in heavy.sh npm ci --no-audit, are joined into one)
#        heavy.sh --status           what runs, what waits, free RAM and pressure
#        heavy.sh --count            how many commands run now
#        heavy.sh --explain <cmd>    the kind, weight and memory it would get
#
# The goal is that every parallel run finishes without crashing, not that the
# machine stays idle: CPU and RAM may run high. A command starts when, counting
# the commands already running across all sessions,
#   - the running weight plus its own stays within BMAD_HEAVY_SLOTS (default:
#     the CPU count). A test suite weighs as many slots as the Vitest workers
#     it gets, other commands 1, unless --weight says;
#   - free RAM, minus what commands started in the last 90 s will still take,
#     covers its memory estimate plus a BMAD_HEAVY_FLOOR_MB reserve (default
#     2048). The estimate is --mem, else what the same kind of command peaked
#     at in earlier runs (when the machine can measure it), else a default per
#     kind (typecheck and lint 1000, install 1500, test 2500, e2e 3500);
#   - the machine is not already stalling: /proc/pressure memory and CPU
#     "some avg10" below BMAD_HEAVY_PSI_MEM (20) and BMAD_HEAVY_PSI_CPU (90),
#     and swap not above BMAD_HEAVY_SWAP_PCT (60) while RAM is short.
# The first command always starts, so nothing waits forever. After
# BMAD_HEAVY_MAX_WAIT seconds (default 1500) a command starts anyway with a
# warning, but only if at least BMAD_HEAVY_HARD_MB (default 512) is free; with
# less it keeps waiting, and after twice that time it exits 75 ("overloaded,
# try again") instead of starting into an out-of-memory crash.
#
# Vitest is capped at VITEST_MAX_FORKS / VITEST_MAX_THREADS workers (default a
# quarter of the CPUs, at least 2; its own default is one per CPU, which is
# what crashed workers when several suites ran at once). The minimum is set to
# 1 with it: Vitest 2 defaults the minimum to CPUs - 1 and refuses to start
# when that is above the maximum.
#
# The command runs in its own session. When heavy.sh is killed, or dies
# without cleaning up, the next heavy.sh call ends that session, so no
# orphaned build keeps eating RAM unseen. Where systemd-run --user works, it
# also runs in a scope with a generous memory cap (the larger of 4x its
# estimate and 8 GB, at most 90% of RAM; BMAD_HEAVY_CGROUP=0 turns it off), so
# an estimate that was far too low kills that command, not the machine, and
# the scope reports the peak memory used for the next estimate. The peak counts
# page cache, so a learned estimate stays within half to double the default.
#
# Own kinds: BMAD_HEAVY_CLASSES (default <state dir>/classes.conf) holds lines
# "<kind> <MB> <glob>", for example "e2e 4000 *playwright*"; the first glob
# that matches the command wins over the built-in kinds.
#
# Waiting commands start in arrival order. State lives in BMAD_HEAVY_DIR
# (default ~/.cache/bmad-heavy): a wait.* ticket per waiting command, a run.*
# entry per running one, pg.* and peak.* beside them, peaks.<kind> with the
# last ten peaks. Entries of dead processes, or older than 4 h, are dropped.
set -uo pipefail

dir="${BMAD_HEAVY_DIR:-$HOME/.cache/bmad-heavy}"
ncpu=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 8)
slots="${BMAD_HEAVY_SLOTS:-$ncpu}"
floor="${BMAD_HEAVY_FLOOR_MB:-2048}"
max_wait="${BMAD_HEAVY_MAX_WAIT:-1500}"
hard="${BMAD_HEAVY_HARD_MB:-512}"
psi_mem="${BMAD_HEAVY_PSI_MEM:-20}"
psi_cpu="${BMAD_HEAVY_PSI_CPU:-90}"
swap_max="${BMAD_HEAVY_SWAP_PCT:-60}"
limit="${BMAD_HEAVY_TIMEOUT:-2700}"
mkdir -p "$dir"
entry="$dir/run.$$"
ticket="" wt_lock="" bg="" pmsg=""

# Memory the machine can still give, in MB.
free_mb() {
  if [ -r /proc/meminfo ]; then
    # Cygwin/MSYS report available physical memory as MemFree.
    awk '/^MemAvailable:/ {a=$2} /^MemFree:/ {f=$2} END {print int((a ? a : f) / 1024)}' /proc/meminfo
  elif command -v vm_stat >/dev/null 2>&1; then
    vm_stat | awk '/page size of/ {ps=$8} /^Pages free/ {f=$3} /^Pages inactive/ {i=$3} /^Pages speculative/ {s=$3} END {print int((f+i+s)*ps/1048576)}'
  else
    echo 999999   # nothing to measure: only the slot count applies
  fi
}

swap_pct() {
  [ -r /proc/meminfo ] || { echo 0; return; }
  awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print (t > 0 ? int((t - f) * 100 / t) : 0)}' /proc/meminfo
}

# Share of the last 10 s some task stalled on $1 (memory, cpu); 0 without PSI.
psi() {
  [ -r "/proc/pressure/$1" ] || { echo 0; return; }
  awk '/^some/ {split($2, a, "="); print int(a[2])}' "/proc/pressure/$1" 2>/dev/null
}

# True (and pmsg says why) when the machine is already stalling.
pressure() {
  local m c s
  m=$(psi memory); c=$(psi cpu)
  if [ "${m:-0}" -ge "$psi_mem" ]; then pmsg="memory stalls ${m}%"; return 0; fi
  if [ "${c:-0}" -ge "$psi_cpu" ]; then pmsg="cpu stalls ${c}%"; return 0; fi
  s=$(swap_pct)
  if [ "${s:-0}" -ge "$swap_max" ] && [ "$avail" -lt $((floor * 2)) ]; then
    pmsg="swap ${s}% used"; return 0
  fi
  return 1
}

# flock frees the lock when its holder dies; without flock (macOS, Git Bash) a
# mkdir lock, which a dead holder leaves behind for a minute.
have_flock=0
if command -v flock >/dev/null 2>&1; then
  { exec 9>"$dir/.flock"; } 2>/dev/null && have_flock=1
fi
lock() {
  if [ "$have_flock" = 1 ]; then flock -w 60 9; return; fi
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
unlock() {
  if [ "$have_flock" = 1 ]; then flock -u 9; else rmdir "$dir/.lock" 2>/dev/null; fi
}

# End a command's whole session: the build and everything it started.
kill_session() {
  command -v pkill >/dev/null 2>&1 || return 0
  pkill -TERM -s "$1" 2>/dev/null
  ( sleep 20; pkill -KILL -s "$1" 2>/dev/null ) >/dev/null 2>&1 &
}
reap() {
  local pg
  pg=$(cat "$dir/pg.$1" 2>/dev/null)
  rm -f "$dir/pg.$1" "$dir/peak.$1"
  [ -n "$pg" ] && kill_session "$pg"
}

# Prints "<running weight> <MB still to be claimed by recent starts> <running>".
survey() {
  local now used=0 recent=0 n=0 f pid w m start kind
  now=$(date +%s)
  for f in "$dir"/run.*; do
    [ -e "$f" ] || continue
    read -r pid w m start kind < "$f" || { rm -f "$f"; continue; }
    if ! kill -0 "$pid" 2>/dev/null || [ $((now - start)) -gt 14400 ]; then
      reap "$pid"; rm -f "$f"; continue
    fi
    used=$((used + w)); n=$((n + 1))
    [ $((now - start)) -lt 90 ] && recent=$((recent + m))
  done
  echo "$used $recent $n"
}

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

status() {
  local used recent n f pid w m start kind now waiting=0 locked=0
  lock && locked=1
  read -r used recent n < <(survey)
  for f in "$dir"/wait.*; do
    [ -e "$f" ] && kill -0 "$(cat "$f" 2>/dev/null)" 2>/dev/null && waiting=$((waiting + 1))
  done
  [ "$locked" = 1 ] && unlock
  now=$(date +%s)
  echo "heavy.sh: running weight $used of $slots slots ($n running, $waiting waiting)"
  for f in "$dir"/run.*; do
    [ -e "$f" ] || continue
    read -r pid w m start kind < "$f" || continue
    echo "  pid $pid  ${kind:-?}  weight $w  reserves $m MB  running $((now - start))s"
  done
  echo "free RAM $(free_mb) MB (floor $floor), swap $(swap_pct)% used, stalls: memory $(psi memory)%, cpu $(psi cpu)%"
}

# One test run per worktree at a time. Parallel coders in one worktree share
# its coverage folder (apps/bunfig.toml fixes coverageDir, so --coverage-dir
# is ignored) and its private test database, so their runs overwrote each
# other's lcov and failed on each other's rows; four of them also queued four
# 2.5 GB suites at once on a machine with 4 GB free, and the queue held them
# for 10 minutes (sonar-zero-5). A test command takes the worktree's lock
# first, before it queues for the machine, and waits for the run ahead of it.
is_test() {
  case $1 in
    *vitest*|*"bun test"*|*"npm test"*|*"npm run test"*|*"npm t "*|*jest*|*pytest*|*--coverage*|*"go test"*|*"cargo test"*) return 0 ;;
  esac
  return 1
}

# Sets kind and dmem (default MB) from the command. A typecheck or lint needs
# far less than a test suite, and reserving 2.5 GB for each held typechecks
# behind installs for over 20 minutes on 2026-10-26.
classify() {
  local k m g conf="${BMAD_HEAVY_CLASSES:-$dir/classes.conf}"
  kind=other dmem=2500
  if [ -r "$conf" ]; then
    while read -r k m g; do
      case $k in ""|"#"*) continue ;; esac
      # shellcheck disable=SC2254
      case $1 in $g) kind=$k dmem=$m; return ;; esac
    done < "$conf"
  fi
  case $1 in
    *playwright*|*cypress*|*e2e*) kind=e2e dmem=3500 ;;
    *typecheck*|*"tsc "*|*"tsc -"*|*" lint"*|*eslint*) kind=typecheck dmem=1000 ;;
    *"npm ci"*|*"npm install"*|*"bun install"*|*"pnpm install"*|*"yarn install"*) kind=install dmem=1500 ;;
    *"docker build"*|*"docker buildx"*|*"docker compose build"*|*"next build"*|*webpack*|*"vite build"*) kind=build dmem=3000 ;;
    *) if is_test "$1"; then kind=test dmem=2500; elif [[ $1 == *" build"* ]]; then kind=build; fi ;;
  esac
}

# What the same kind of command peaked at before: the 90th percentile of the
# last ten successful runs, once there are three.
learned() {
  [ -r "$dir/peaks.$1" ] || return 1
  sort -n "$dir/peaks.$1" | awk '/^[0-9]+$/ {a[++n]=$1} END {if (n < 3) exit 1; i=int(n * 0.9 + 0.999); if (i > n) i=n; print a[i]}'
}

cleanup() {
  rm -f "$entry" "${ticket:-}" "$dir/pg.$$" "$dir/peak.$$"
  [ -n "$wt_lock" ] && rm -rf "$wt_lock"
}
trap cleanup EXIT
on_signal() {
  local pg
  pg=$(cat "$dir/pg.$$" 2>/dev/null)
  [ -n "$pg" ] && kill_session "$pg"
  [ -n "$bg" ] && kill "$bg" 2>/dev/null
  exit 143
}
trap on_signal TERM INT HUP

explain=0
case ${1:-} in
  --status) status; exit 0 ;;
  --count) lock && { read -r _ _ n < <(survey); unlock; echo "${n:-0}"; exit 0; }; echo 0; exit 0 ;;
  --explain) explain=1; shift ;;
esac

weight="" mem=""
while [ $# -gt 1 ]; do
  case $1 in
    --weight) weight=$2; shift 2 ;;
    --mem) mem=$2; shift 2 ;;
    *) break ;;
  esac
done
: "${1:?usage: heavy.sh [--weight N] [--mem MB] <command string>}"
# One string runs as given (pipes, && and globs inside it keep working). Separate
# words are quoted and joined: taking only the first one ran a bare `npm` for
# `heavy.sh npm ci --no-audit --no-fund` (batch-parallelism, 2026-11-02).
if [ $# -eq 1 ]; then
  cmd=$1
else
  printf -v cmd '%q ' "$@"
  cmd=${cmd% }
fi

# A quarter of the CPUs per suite, at least 2 (3 on a 12-CPU machine).
vt=$((ncpu / 4)); [ "$vt" -lt 2 ] && vt=2
classify "$cmd"
if [ -z "$mem" ]; then
  mem=$dmem
  if l=$(learned "$kind") && [ -n "$l" ]; then
    l=$((l * 115 / 100))
    [ "$l" -lt $((dmem / 2)) ] && l=$((dmem / 2))
    [ "$l" -gt $((dmem * 2)) ] && l=$((dmem * 2))
    mem=$l
  fi
  [ -n "$weight" ] && mem=$((mem * weight))
fi
# A suite runs as many workers as it gets, so it takes as many slots; with one
# slot each, a 12-CPU machine let twelve suites of three workers start at once.
if [ -z "$weight" ]; then
  weight=1
  [ "$kind" = test ] && weight=$vt
fi
[ "$weight" -gt "$slots" ] && weight=$slots
[ "$weight" -lt 1 ] && weight=1
if [ "$explain" = 1 ]; then
  echo "kind=$kind weight=$weight mem=$mem"
  exit 0
fi

if [ "$kind" = test ] || is_test "$cmd"; then
  # The worktree: a leading `cd <path>` in the command, else the current folder.
  where=$PWD
  if [[ $cmd =~ ^[[:space:]]*cd[[:space:]]+(\"[^\"]+\"|\'[^\']+\'|[^[:space:]\;\&]+) ]]; then
    where=${BASH_REMATCH[1]}; where=${where//\"/}; where=${where//\'/}
  fi
  gd=$(git -C "$where" rev-parse --absolute-git-dir 2>/dev/null)
  if [ -n "$gd" ]; then
    mkdir -p "$gd/bmad"
    wt_lock="$gd/bmad/test.lock"
    t_lock=$SECONDS t_said=$((SECONDS - 60))
    until mkdir "$wt_lock" 2>/dev/null; do
      holder=$(cat "$wt_lock/pid" 2>/dev/null)
      # A holder that died, or that never wrote its pid within a minute, leaves it behind.
      if { [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; } ||
         { [ -z "$holder" ] && [ -n "$(find "$wt_lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; }; then
        rm -rf "$wt_lock"; continue
      fi
      if [ $((SECONDS - t_lock)) -ge "$max_wait" ]; then
        echo "heavy.sh: waited $((SECONDS - t_lock))s for the test run ahead in this worktree; starting anyway" >&2
        wt_lock=""; break
      fi
      if [ $((SECONDS - t_said)) -ge 60 ]; then
        echo "heavy.sh: waiting for another test run in this worktree (pid ${holder:-?}) to end" >&2
        t_said=$SECONDS
      fi
      sleep 5
    done
    [ -n "$wt_lock" ] && echo $$ > "$wt_lock/pid"
  fi
fi

# First come, first served: every command takes a ticket named by its arrival
# time, and only the oldest live ticket may start. A later, smaller command
# never overtakes an earlier one, even when it would fit.
ticket="$dir/wait.$(date +%s%N).$$"
echo "$$" > "$ticket"

start() {
  echo "$$ $weight $mem $(date +%s) $kind" > "$entry"
  rm -f "$ticket"
}

# Ten to fourteen seconds without flock, so dozens of waiters don't fight over
# the mkdir lock (on 2026-10-26, 28 waiters ended in "fork: Resource
# temporarily unavailable"); flock waiters can poll faster.
poll="${BMAD_HEAVY_POLL:-$([ "$have_flock" = 1 ] && echo 5 || echo 10)}"

waited=0 used=0 avail=$(free_mb) queue=0 t_queue=$SECONDS t_said=$((SECONDS - 60)) why=""
while :; do
  if lock; then
    queue=$(ahead)
    read -r used recent _ < <(survey)
    avail=$(( $(free_mb) - recent ))
    why=""
    if [ "$queue" -gt 0 ]; then why="${queue} ahead in the queue"
    elif [ "$used" -eq 0 ]; then why=""
    elif [ $((used + weight)) -gt "$slots" ]; then why="running weight $used of $slots"
    elif [ $((avail - mem)) -lt "$floor" ]; then why="${avail} MB free, needs $((mem + floor))"
    elif pressure; then why="$pmsg"
    fi
    if [ -z "$why" ]; then
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
    if [ "$avail" -ge "$hard" ]; then
      echo "heavy.sh: waited ${waited}s; starting anyway" >&2
      start
      break
    elif [ "$waited" -ge $((max_wait * 2)) ]; then
      echo "heavy.sh: machine overloaded: ${avail} MB free after ${waited}s, not starting (try again): $cmd" >&2
      exit 75
    fi
  fi
  if [ $((SECONDS - t_said)) -ge 60 ]; then
    echo "heavy.sh: waiting (${why:-the lock}; ${avail} MB free)" >&2
    t_said=$SECONDS
  fi
  sleep $((poll + RANDOM % (poll / 2 + 1)))
done

export VITEST_MAX_FORKS="${VITEST_MAX_FORKS:-$vt}" VITEST_MAX_THREADS="${VITEST_MAX_THREADS:-$vt}"
export VITEST_MIN_FORKS="${VITEST_MIN_FORKS:-1}" VITEST_MIN_THREADS="${VITEST_MIN_THREADS:-1}"

# In a systemd user scope with a memory cap, when that works here.
use_cg=0 cap=0
if [ "${BMAD_HEAVY_CGROUP:-1}" != 0 ] && [ -r /proc/meminfo ] && command -v systemd-run >/dev/null 2>&1 &&
   timeout 5 systemd-run --user --scope -q --collect true >/dev/null 2>&1; then
  total=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
  cap=$((mem * 4)); [ "$cap" -lt 8192 ] && cap=8192
  [ "$cap" -gt $((total * 9 / 10)) ] && cap=$((total * 9 / 10))
  [ "$cap" -gt 0 ] && use_cg=1
fi
# Inside the scope: run the command, then write the cgroup's peak memory.
peak_script='bash -c "$1"; rc=$?; f=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)/memory.peak; [ -r "$f" ] && echo $(( $(cat "$f") / 1048576 )) > "$2"; exit $rc'

# Each command has a time limit (BMAD_HEAVY_TIMEOUT, default 45 min): seven
# npm ci runs that hung on 2026-10-26 held their slots for over an hour and
# stopped every build on the machine.
run() {
  local cp rc t=$SECONDS
  local -a pre=() inner
  command -v setsid >/dev/null 2>&1 && command -v pkill >/dev/null 2>&1 && pre=(setsid)
  inner=(bash -c "$cmd")
  [ "$use_cg" = 1 ] && inner=(systemd-run --user --scope -q --collect -p "MemoryMax=${cap}M" -- bash -c "$peak_script" _ "$cmd" "$dir/peak.$$")
  ${pre[@]+"${pre[@]}"} timeout -k 30 "$limit" "${inner[@]}" <&0 9>&- &
  cp=$!
  [ "${#pre[@]}" -gt 0 ] && echo "$cp" > "$dir/pg.$$"
  wait "$cp"; rc=$?
  if [ "$rc" -eq 124 ] || { [ "$rc" -eq 137 ] && [ $((SECONDS - t)) -ge "$limit" ]; }; then
    echo "heavy.sh: stopped after ${limit}s (BMAD_HEAVY_TIMEOUT): $cmd" >&2
  elif [ "$rc" -eq 137 ] && [ "$use_cg" = 1 ]; then
    echo "heavy.sh: killed (exit 137), probably at the ${cap} MB memory cap: $cmd" >&2
  fi
  return "$rc"
}

# Every command goes into the run journal's event log (log-event.py), unless
# bg.sh runs this one and logs it itself. The command runs in the background so
# that a signal to heavy.sh interrupts the wait and ends the command's session.
logged="${BMAD_EVENT_LOGGED:-}" out=""
t0=$SECONDS
if [ -n "$logged" ]; then
  run <&0 &
else
  out=$(mktemp)
  { run 2>&1 | tee "$out"; exit "${PIPESTATUS[0]}"; } <&0 &
fi
bg=$!
wait "$bg"; rc=$?
bg=""

peak=""
if [ -s "$dir/peak.$$" ]; then
  peak=$(cat "$dir/peak.$$" 2>/dev/null)
  if [ "$rc" -eq 0 ] && [[ $peak =~ ^[0-9]+$ ]]; then
    echo "$peak" >> "$dir/peaks.$kind"
    tail -n 10 "$dir/peaks.$kind" > "$dir/peaks.$kind.$$" && mv -f "$dir/peaks.$kind.$$" "$dir/peaks.$kind"
  fi
fi
rm -f "$dir/pg.$$" "$dir/peak.$$"

if [ -z "$logged" ]; then
  py=$(command -v python || command -v python3)
  [ -n "$py" ] && "$py" "$(dirname "${BASH_SOURCE[0]}")/log-event.py" heavy "$PWD" "$rc" "$((SECONDS - t0))" "$cmd" "$out" "$peak"
  rm -f "$out"
fi
exit "$rc"
