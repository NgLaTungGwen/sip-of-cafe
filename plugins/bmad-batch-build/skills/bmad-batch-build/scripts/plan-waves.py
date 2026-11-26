"""Schedule a run's units by their dependencies, and estimate each mode.

usage: python plan-waves.py <plan.json> [--busy] [--max-parallel N] [--no-tests]
                            [--root <repo>] [--project <name>] [--long <min>]
       python plan-waves.py --accuracy [--project <name>]

plan.json is written by the orchestrator at the split, next to the run file
(<slug>-plan.json in the run folder, run_dir.py):

    {"units": [
      {"id": 1, "title": "chart kit", "size": "M", "depends_on": [], "reason": ""},
      {"id": 3, "title": "cost chart", "size": "M", "depends_on": [1, 2],
       "reason": "imports the chart kit; uses the new contract fields"}
    ]}

With an intake, `intake.py to-plan` writes this file from the intake's Units:
ids as strings ("1"), depends_on as [{"id": "1", "reason": "..."}], size S to
XL, plus "risk", "acs", "files", "items" and the intake's path. Both forms are
accepted; the ids are written back as numbers, the reasons joined into
"reason", and "risk" and "acs" pass through. An XL unit is scheduled as L and
gets a Split line. When <slug>-intake.md is beside the plan (or the plan names
it), a **Set mode:** line follows the Mode line: the intake.py set-mode
command, ready to paste, with the reason for the mode.

A unit may also carry "items", the number of separate things it must fix or
build (findings, work items, acceptance criteria), and "files", the source
files those items are in, relative to --root. They feed the time estimate, the
split check and the same-file check below.

Only compile- or test-time dependencies belong in depends_on: unit 3 cannot
build or test without unit 1's code. A dependency that an agreed interface
can cover (B codes against A's route or type as specified) is not one.

Units are scheduled in rounds. A round holds up to --max-parallel units whose
dependencies are all in earlier rounds, taking first the unit with the longest
chain of work still behind it (the critical path), then the longest unit, so
units of similar length share a round and a short unit does not wait behind a
long one, nor a long unit hold a round of short ones. (A wave's agent calls
return together, so a unit cannot join a round that is already running.) Each
round then runs the cheaper way for itself, by the same rule as a whole run:

    lane step     = unit minutes, minus 10 for every unit after the run's first
                    (installed worktree, warm context)
    parallel wave = max(unit minutes) x queue factor + 50 (merge into the
                    integration branch and verify it)
    run total     = sum of wave times + 30 (preparation and the final verify)
    lane total    = the lane steps + 30 + synthesis (merge, verify, PR: the
                    median min_synthesis of recorded runs with workers, 60
                    until there are three)

A wave of 2 to 4 units runs in parallel when that is more than the margin
faster (20 min, 40 when --busy), a tie going to lane. A wave of 5 or more is
parallel. With no dependencies this is exactly the old lane/parallel choice;
with a single chain it is lane.

The queue factor models heavy.sh: a parallel wave's suites and installs queue
for the machine. With k workers and this many other active runs, about
(k + 2 x others) suites of max(2, CPUs / 4) Vitest workers want the CPUs; above
the CPU count the heavy part of each unit (its share of the time, 35% unless
recorded suite times in events.jsonl say otherwise) stretches by that ratio.

No parallel wave has more than --max-parallel workers (default 4; 3 when one
other run is active on the machine, 2 when two or more are). Each worker
brings a coder and up to four reviewers, and Claude Code allows 20 subagents at
once per session, so 5 workers already got launches refused; and on 2026-10-26
runs of 12, 9 and 5 parallel workers at the same time stalled the machine-wide
build queue for hours. Other active runs are sessions that ran the skill and
wrote their log in the last 15 minutes. The cap also fits the RAM: at most
(MemAvailable - 2048 MB) / the RAM one unit peaks at (3000 MB, or the 90th
percentile of heavy.sh's measured test-suite peaks, or BMAD_UNIT_PEAK_MB),
and at least 1; the plan's "parallel cap" line says which limit applied.

Unit minutes. With "items", a unit takes 40 min plus 2.2 min per item, plus 30
min per 1000 lines of every file over 2500 lines (sonar-zero-5, 2026-11-07:
units of 21 to 46 findings took 85 to 140 min; one with 64 findings and a
6500-line file took 315). Without items, its size: S 50, M 100, L 160. Every
number is blended with recorded runs (~/.claude/bmad-batch-runs/runs.jsonl,
each row's unit_minutes) from the first measurement on: (n x measured median +
3 x prior) / (n + 3), first over all projects, then over this project's own
runs (--project, else the folder name of --root).

A single number hides the tail, so every estimate also comes with a p80. Once
5 recorded runs of the chosen mode had workers, it is the estimate times the
ceil((n+1) x 0.8)-th smallest of their active minutes (wall minus user wait)
over estimate, at most 5x; the units' p80s use the same factor. Until then,
unit times scaled by the measured p80/p50 ratio of recorded units (1.6 until
there are 5), run through the same schedule. sonar-zero-5 was estimated at 6 h
and took 13.4 h. Each unit gets a budget, twice its estimate, at least 60 min
over it and 30 min past its p80; straggler.py compares a running unit with it.

A batch run pays a fixed cost per unit (worktree, install, spec, plan, coder,
reviewers, gate) and per parallel wave (merge and verify), which only pays off
when units really run side by side. So the script also says when one session
building the work directly is the better choice, and why:

    chain    nothing runs in parallel (every wave is a lane) and there is more
             than one unit
    narrow   at most 2 units at once over 3 or more waves
    no tests --no-tests (the profile has no test suite, or the repo bans
             tests) and at most 3 units at once: the review-and-verify loop
             has little to check
    long     the estimate is --long minutes (default 240) or more, and the
             units one after another in a single session would take about as
             long or less: 65% of their summed minutes, an assumption that the
             other 35% is per-unit batch overhead (not yet measured)

and a **Long run** line when the p80 is 6 h or more: ship the first waves as a
run of their own instead of holding everything for the last one.

On 2026-10-27 a 7-unit POC in 3 waves of up to 3, in a repo that bans tests,
was estimated at 618 min; one session built it directly in far less.

A unit that is too big for one worker is split before the run, not during it.
The script prints a **Split** line for every unit with more than 50 items, or
that owns a file of more than 2500 lines among other files, and writes them to
plan.json ("split"). Parts that edit the same file cannot run side by side, so
a **Chained** line lists every pair of units that share a file in "files" with
no dependency between them: the later one gets a depends_on on the earlier
(saved in plan.json), and they run in turn.

Every unit gets a planner model in "unit_estimates" ("planner"): opus, unless
the project profile under --root sets "sonnet_planner": true (off by default)
and the unit is size S with risk low, which then gets sonnet and is named on
a **Planner** line. A unit with no risk (no intake) keeps opus. Sonnet
workers that also planned skipped the review in 31 of 47 units (2026-10-21/22),
so this is a trial: compare the "planner_sonnet" role in the dashboard first.

Prints the waves, every estimate, the critical path, units that dominate the
run (**Straggler risk**), the chosen mode, the Mode line for the approval table
and, when one applies, an Advice line. Writes the schedule back into plan.json
("waves", "mode", "estimate", "estimate_p80", "unit_estimates", ...).

--accuracy compares recorded units with their estimate (the one the plan gave
when recorded, else today's model) and says whether the estimates run low,
then whole runs per mode: active minutes over the estimate, for runs that had
workers.
"""
import json
import math
import os
import re
import statistics
import sys

DEFAULT = {"S": 50, "M": 100, "L": 160}
WARM = 10        # saved per lane unit after the first
MERGE = 50       # a parallel wave's merge and verify
RUN = 30         # preparation and the final verify, once per run
MAX_PARALLEL = 4     # workers in one parallel wave (Claude Code's 20-subagent limit)
RESERVE_MB = 2048    # RAM the machine keeps for itself when capping workers by memory
UNIT_PEAK_MB = 3000  # RAM one worker's unit peaks at (its suite, dev server, DB), unless measured
SYNTH = 60           # minutes synthesis takes to merge and verify a lane, unless recorded (median 56, 36 runs)
SPREAD_CAP = 5.0     # the most a p80 factor may be (lane runs measured 4.75x on 2026-11-15)
MIN_RATIOS = 5       # recorded runs of a mode before its own p80 replaces the factor
K = 3                # recorded runs count against this many prior ones
BASE_ITEMS = 40      # minutes before the first item (worktree, install, spec, plan)
PER_ITEM = 2.2      # minutes per item (sonar-zero-5: 21 -> 85 min, 46 -> 140 min)
BIG_FILE = 2500      # lines
BIG_FILE_MIN = 30    # minutes per 1000 lines over BIG_FILE (6500 lines -> 315 min at 64 items)
SPREAD = 1.6         # p80 / p50 until there are measured units
HEAVY_SHARE = 0.35   # of a unit's time spent in heavy commands, unless suite times say
BUDGET = 2.0         # a unit's budget, times its estimate
LONG = 240           # minutes: from here on, compare with building directly
LONG_P80 = 360       # minutes: from here on, ship the first waves on their own
DIRECT_SHARE = 0.65  # of a unit's minutes that one session building it directly still needs


def store():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def read_jsonl(name):
    rows, path = [], os.path.join(store(), name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def samples_of(rows):
    """One dict per measured unit of the recorded runs."""
    out = []
    for r in rows:
        for u in r.get("unit_minutes") or []:
            if u.get("size") in DEFAULT and u.get("min"):
                out.append({"project": r.get("project"), "size": u["size"], "min": u["min"],
                            "items": u.get("items") or 0, "est": u.get("est")})
    return out


def blend(prior, got):
    """Recorded values move the prior from the first one on, a median at a time."""
    if not got:
        return prior
    return (len(got) * statistics.median(got) + K * prior) / (len(got) + K)


def quantile(values, q):
    s = sorted(values)
    return s[max(math.ceil(q * len(s)) - 1, 0)]


def calibration(project=None, rows=None):
    """The model of unit minutes: size minutes, minutes per item, p80/p50."""
    samples = samples_of(read_jsonl("runs.jsonl") if rows is None else rows)
    mine = [s for s in samples if project and s["project"] == project]
    size, source = {}, {}
    for name, default in DEFAULT.items():
        g = [s["min"] for s in samples if s["size"] == name]
        p = [s["min"] for s in mine if s["size"] == name]
        size[name] = round(blend(blend(default, g), p))
        source[name] = "default" if not g else f"measured n={len(g)}" + (f", {len(p)} in {project}" if p else "")

    def rates(group):
        return [(s["min"] - BASE_ITEMS) / s["items"] for s in group if s["items"] and s["min"] > BASE_ITEMS + 10]
    g, p = rates(samples), rates(mine)
    per_item = min(max(blend(blend(PER_ITEM, g), p), 0.5), 8.0)
    ratios = []
    for name in DEFAULT:
        got = [s["min"] for s in samples if s["size"] == name]
        if len(got) >= 3:
            m = statistics.median(got)
            ratios += [x / m for x in got]
    spread = SPREAD
    if len(ratios) >= 5:
        q = min(max(quantile(ratios, 0.8), 1.2), SPREAD_CAP)
        spread = (len(ratios) * q + K * SPREAD) / (len(ratios) + K)
    return {"size": size, "source": source, "per_item": per_item,
            "per_item_source": f"measured n={len(g)}" if g else "default", "spread": round(spread, 2),
            "units": len(samples)}


def file_lines(root, path):
    try:
        with open(os.path.join(root, path), "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def unit_minutes(u, model, root=None):
    """A unit's expected minutes: from its items (and big files) when it has
    them, else from its size."""
    items = u.get("items") or 0
    if items <= 0:
        return model["size"][size_key(u)]
    t = BASE_ITEMS + model["per_item"] * items
    if root:
        for f in u.get("files") or []:
            n = file_lines(root, f)
            if n > BIG_FILE:
                t += BIG_FILE_MIN * (n - BIG_FILE) / 1000
    return max(30, round(t))


def budget(minutes, p80=None):
    """Twice the estimate and at least an hour over it, and never less than
    half an hour past the p80: straggler.py says Slow past the p80 and
    Straggler past the budget, so a budget under the p80 skips Slow."""
    return max(round(BUDGET * minutes), minutes + 60, round(p80) + 30 if p80 else 0)


def order_stat(values, q=0.8):
    """The ceil((n+1) q)-th smallest value: a p80 that n samples cover with
    80% confidence for the next one (conformal quantile). For n up to 8 that
    is the largest value."""
    s = sorted(values)
    return s[min(math.ceil((len(s) + 1) * q), len(s)) - 1]


def run_ratios(rows, project=None):
    """Per mode, each recorded run's active minutes over its estimate. Active
    is wall minus the time the run waited for the user (a negative wait counts
    as none); only runs that had workers count, so a run built directly or
    stopped at the split does not."""
    out = {}
    for r in rows:
        m = re.search(r"Mode: (\w+) \(≈(\d+) min", r.get("mode") or "")
        if not m or not r.get("workers") or not r.get("min_wall") or int(m.group(2)) <= 0:
            continue
        if project and r.get("project") != project:
            continue
        active = r["min_wall"] - max(r.get("min_user_wait") or 0, 0)
        if active > 0:
            out.setdefault(m.group(1), []).append(active / int(m.group(2)))
    return out


def mode_spread(mode, rows, fallback):
    """The p80 factor for a run of this mode: the order statistic of its
    recorded actual/estimate ratios once there are MIN_RATIOS of them, else
    the unit spread. Returns (factor, source)."""
    got = run_ratios(rows).get(mode, [])
    if len(got) < MIN_RATIOS:
        return fallback, f"unit spread ({len(got)} {mode} run(s) recorded, {MIN_RATIOS} needed)"
    q = min(max(order_stat(got), 1.0), SPREAD_CAP)
    return round(q, 2), f"order statistic of {len(got)} {mode} run(s)"


def synthesis_minutes(rows):
    """Median synthesis minutes of the recorded runs that had workers, once
    there are three; else SYNTH."""
    got = [r["min_synthesis"] for r in rows if r.get("workers") and (r.get("min_synthesis") or 0) > 0]
    return round(statistics.median(got)) if len(got) >= 3 else SYNTH


def suite_minutes(root):
    """Median minutes of this repository's successful test commands, from the
    heavy-command log, once there are three."""
    if not root:
        return None
    base = os.path.normpath(root).replace("\\", "/").lower()
    got = []
    for e in read_jsonl("events.jsonl"):
        cwd = (e.get("cwd") or "").replace("\\", "/").lower()
        if e.get("rc") == 0 and e.get("sec") and cwd.startswith(base) and \
                re.search(r"vitest|bun test|npm (run )?test|jest|pytest|--coverage", e.get("cmd") or ""):
            got.append(e["sec"] / 60)
    return statistics.median(got) if len(got) >= 3 else None


def heavy_share(units, t, suite):
    """The part of the units' time that waits for heavy.sh: fix rounds times the
    suite time when that is known, else a fixed share."""
    if not suite:
        return HEAVY_SHARE
    heavy = total = 0
    for u in units:
        rounds = 2 + math.ceil((u.get("items") or 0) / 8)
        total += t[u["id"]]
        heavy += min(rounds * suite, 0.7 * t[u["id"]])
    return min(max(heavy / total, 0.1), 0.7) if total else HEAVY_SHARE


def contention(others, ncpu=None, slots=None):
    """How far the heavy part of a parallel wave of k workers stretches: the
    suites of all active workers want vt CPUs each, and heavy.sh admits no
    more than the CPU count."""
    ncpu = ncpu or os.cpu_count() or 8
    cpus = int(slots or os.environ.get("BMAD_HEAVY_SLOTS") or ncpu)
    vt = max(ncpu // 4, 2)
    return lambda k: max(1.0, (k + 2 * others) * vt / cpus)


def waves_of(units):
    ids = {u["id"] for u in units}
    for u in units:
        missing = [d for d in u.get("depends_on", []) if d not in ids]
        if missing:
            raise SystemExit(f"unit {u['id']} depends on unknown unit(s) {missing}")
    placed, waves = {}, []
    remaining = list(units)
    while remaining:
        ready = [u for u in remaining if all(d in placed for d in u.get("depends_on", []))]
        if not ready:
            raise SystemExit("dependency cycle among units " + ", ".join(str(u["id"]) for u in remaining))
        for u in ready:
            placed[u["id"]] = len(waves)
        waves.append(ready)
        remaining = [u for u in remaining if u["id"] not in placed]
    return waves


def tails(units, t):
    """Per unit, the minutes of the longest chain that starts with it."""
    after = {u["id"]: [] for u in units}
    for u in units:
        for d in u.get("depends_on", []):
            after[d].append(u["id"])
    memo = {}

    def tail(i):
        if i not in memo:
            memo[i] = t[i] + max((tail(j) for j in after[i]), default=0)
        return memo[i]
    return {u["id"]: tail(u["id"]) for u in units}


def critical_path(units, t):
    """The longest dependency chain (ids, minutes): the least the run can take
    however many workers it has."""
    by_id = {u["id"]: u for u in units}
    tail = tails(units, t)
    heads = [i for i, u in by_id.items() if not u.get("depends_on")]
    if not heads:
        return [], 0
    i = max(heads, key=lambda x: (tail[x], -x))
    path = [i]
    while True:
        nxt = [u["id"] for u in units if i in u.get("depends_on", [])]
        if not nxt:
            break
        i = max(nxt, key=lambda x: (tail[x], -x))
        path.append(i)
    return path, tail[path[0]]


def rounds_of(units, t, max_par):
    """Waves of up to max_par ready units, longest remaining chain first, then
    longest unit, so similar units share a wave. Raises on a cycle."""
    waves_of(units)  # validates dependencies and cycles
    tail = tails(units, t)
    placed, rounds, remaining = set(), [], list(units)
    while remaining:
        ready = [u for u in remaining if all(d in placed for d in u.get("depends_on", []))]
        ready.sort(key=lambda u: (-tail[u["id"]], -t[u["id"]], u["id"]))
        take = ready[:max_par]
        # Keep the plan's own order inside a wave: it is the order of the table.
        take.sort(key=lambda u: units.index(u))
        rounds.append(take)
        placed |= {u["id"] for u in take}
        remaining = [u for u in remaining if u["id"] not in placed]
    return rounds


def other_active_runs(minutes=15):
    """Sessions on this machine that ran the skill and wrote their log in the
    last <minutes> minutes, not counting the newest one (this run's own)."""
    import glob
    import time
    marks = ("<command-name>/bmad-batch-build", '"skill":"bmad-batch-build', '"skill": "bmad-batch-build')
    active = []
    for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        m = os.path.getmtime(f)
        if time.time() - m > minutes * 60:
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if any(k in line for k in marks) and invokes(line):
                    active.append(m)
                    break
    return max(len(active) - 1, 0)


def invokes(line):
    try:
        x = json.loads(line)
    except ValueError:
        return False
    m = x.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if x.get("type") == "user":
        texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if isinstance(b, dict)]
        return any("<command-name>/bmad-batch-build" in s for s in texts)
    return x.get("type") == "assistant" and isinstance(c, list) and any(
        b.get("type") == "tool_use" and b.get("name") == "Skill"
        and str((b.get("input") or {}).get("skill", "")).startswith("bmad-batch-build") for b in c)


def cap_for(other_runs, avail_mb=None, peak_mb=UNIT_PEAK_MB):
    """Workers per wave: the CPU-side cap (fewer with other active runs), and
    with avail_mb also no more than (avail_mb - RESERVE_MB) / peak_mb, at least 1."""
    cpu = MAX_PARALLEL if other_runs == 0 else (3 if other_runs == 1 else 2)
    if avail_mb is None:
        return cpu
    return max(1, min(cpu, (avail_mb - RESERVE_MB) // max(peak_mb, 1)))


def mem_available_mb():
    """RAM the machine can still give, in MB, or None when it can't be read.
    BMAD_MEM_AVAILABLE_MB overrides it."""
    v = os.environ.get("BMAD_MEM_AVAILABLE_MB")
    if v:
        return int(v)
    try:
        with open("/proc/meminfo") as fh:
            info = dict(line.split(":", 1) for line in fh if ":" in line)
        kb = info.get("MemAvailable") or info.get("MemFree")  # Cygwin/MSYS have only MemFree
        return int(kb.split()[0]) // 1024 if kb else None
    except (OSError, ValueError):
        pass
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        s = Status()
        s.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
            return int(s.ullAvailPhys // 1048576)
    return None


def unit_peak_mb():
    """What one unit's worker peaks at: BMAD_UNIT_PEAK_MB, else the 90th
    percentile of heavy.sh's recorded test-suite peaks (peaks.test, measured
    only where systemd scopes work), once there are three, else UNIT_PEAK_MB.
    Returns (MB, source)."""
    v = os.environ.get("BMAD_UNIT_PEAK_MB")
    if v:
        return int(v), "BMAD_UNIT_PEAK_MB"
    path = os.path.join(os.environ.get("BMAD_HEAVY_DIR") or os.path.expanduser("~/.cache/bmad-heavy"), "peaks.test")
    try:
        with open(path) as fh:
            got = [int(x) for x in fh.read().split() if x.isdigit()]
    except OSError:
        got = []
    if len(got) >= 3:
        return max(quantile(got, 0.9), 1000), f"measured, {len(got)} suites"
    return UNIT_PEAK_MB, "default"


def parallel_cap(others):
    """(cap, reason) for this machine now: the CPU-side cap, lowered by RAM."""
    cpu = cap_for(others)
    avail = mem_available_mb()
    reason = f"{others} other active run(s)"
    if avail is None:
        return cpu, reason + "; RAM not measured"
    peak, source = unit_peak_mb()
    cap = cap_for(others, avail, peak)
    ram = f"RAM {avail} MB available - {RESERVE_MB} reserve, ~{peak} MB per unit ({source})"
    if cap < cpu:
        return cap, f"capped by RAM: {ram}; {reason} allow {cpu}"
    return cap, f"{reason}; {ram} allows {max(1, (avail - RESERVE_MB) // peak)}"


def plan(units, busy=False, minutes=None, max_par=MAX_PARALLEL, times=None, queue=None, heavy=HEAVY_SHARE,
         synth=SYNTH):
    minutes = minutes or DEFAULT
    t = times or {u["id"]: minutes[size_key(u)] for u in units}
    queue = queue or (lambda k: 1.0)
    margin = 40 if busy else 20
    levels = waves_of(units)
    waves = rounds_of(units, t, max_par)
    schedule, total, first = [], RUN, True
    for wave in waves:
        k = len(wave)
        ts = [t[u["id"]] for u in wave]
        lane = sum(ts) - WARM * (k - (1 if first else 0))
        par = round(max(ts) * (1 + heavy * (queue(k) - 1))) + MERGE
        if (k >= 5 and k <= max_par) or (k >= 2 and par + margin < lane):
            how, wt = "parallel", par
        else:
            how, wt = "lane", lane
        schedule.append({"units": [u["id"] for u in wave], "run": how, "minutes": wt})
        total += wt
        first = False
    order = [u for w in waves for u in w]
    # A lane still ends in synthesis (merge the lane branch, verify, open the
    # PR), which the lane total left out. A parallel wave already carries it as
    # MERGE. The per-wave lane-or-parallel choice above is left as it was.
    lane_total = sum(t[u["id"]] for u in order) - WARM * (len(order) - 1) + RUN + synth
    independent = len(levels) == 1 and len(waves) == 1
    parallel_total = (round(max(t.values()) * (1 + heavy * (queue(len(units)) - 1))) + MERGE + RUN) if independent else None
    kinds = {w["run"] for w in schedule}
    if kinds == {"lane"}:
        mode, chosen = "lane", lane_total
    elif independent:
        mode, chosen = "parallel", parallel_total
    else:
        mode, chosen = "waves", total
    capped = max_par if len(levels) == 1 and len(waves) > 1 else None
    return {"waves": schedule, "mode": mode, "estimate": chosen,
            "lane": lane_total, "parallel": parallel_total, "waves_total": total, "capped": capped}


def ancestors(units):
    deps = {u["id"]: set(u.get("depends_on", [])) for u in units}
    out = {}

    def up(i):
        if i not in out:
            out[i] = set()
            for d in deps[i]:
                out[i] |= {d} | up(d)
        return out[i]
    return {i: up(i) for i in deps}


def chain_same_file(units):
    """Units that share a file and do not depend on each other would conflict at
    the merge: the later one now depends on the earlier. Returns
    [(later id, earlier id, file)]."""
    by_id = {u["id"]: u for u in units}
    added = []
    ids = sorted(by_id)
    for n, a in enumerate(ids):
        for b in ids[n + 1:]:
            shared = ({f.replace("\\", "/") for f in by_id[a].get("files") or []}
                      & {f.replace("\\", "/") for f in by_id[b].get("files") or []})
            if not shared:
                continue
            anc = ancestors(units)
            if a in anc[b] or b in anc[a]:
                continue
            f = sorted(shared)[0]
            by_id[b].setdefault("depends_on", []).append(a)
            why = by_id[b].get("reason") or ""
            by_id[b]["reason"] = (why + "; " if why else "") + f"edits {f}, as unit {a} does"
            added.append((b, a, f))
    return added


def stragglers(units, t):
    """Units that would dominate the run: far longer than the typical unit."""
    if len(units) < 3:
        return []
    median = statistics.median(t.values())
    total = sum(t.values())
    out = []
    for u in units:
        m = t[u["id"]]
        if m >= 120 and (m > 2 * median or m > 0.4 * total):
            out.append(f"unit {u['id']} ({u.get('title', '')}) is about {m} min, {m / median:.1f}x the median unit and "
                       f"{100 * m // total}% of all unit time: split it, or expect the run to end when it does")
    return out


def advise(units, result, no_tests=False, t=None, long_min=LONG):
    """Why one session building the work directly beats a batch run; [] when it doesn't."""
    widest = max(len(w["units"]) if w["run"] == "parallel" else 1 for w in result["waves"])
    reasons = []
    if len(units) > 1 and result["mode"] == "lane":
        reasons.append("chain: nothing runs in parallel, so every unit pays the batch overhead for no gain")
    elif widest <= 2 and len(result["waves"]) >= 3:
        reasons.append(f"narrow: at most {widest} units at once over {len(result['waves'])} waves in a row")
    if no_tests and widest <= 3:
        reasons.append("no tests: the review-and-verify loop has little to check")
    if t and result["estimate"] >= long_min:
        direct = round(DIRECT_SHARE * sum(t.values()))
        if direct <= result["estimate"]:
            reasons.append(f"long: the batch takes about {result['estimate']} min and one session building the units in "
                           f"turn about {direct} min (assuming {round(100 * (1 - DIRECT_SHARE))}% of each unit is batch overhead)")
    return reasons


def long_run(result, p80):
    if p80 and p80 >= LONG_P80 and len(result["waves"]) > 1:
        first = result["waves"][0]["units"]
        return (f"p80 is {p80} min: ship the first wave (unit {', '.join(map(str, first))}) as a run of its own, "
                "so a first PR lands early and the rest can be stopped or re-planned")
    return None


MAX_ITEMS = 50
MAX_LINES = 2500


def too_big(units, root=None):
    """One line per unit a single worker should not get, with how to split it."""
    out = []
    for u in units:
        why = []
        items = u.get("items") or 0
        files = u.get("files") or []
        if items > MAX_ITEMS:
            parts = -(-items // 40)
            how = f"split into {parts} units of at most 40"
            if len(set(files)) == 1:
                how += (f"; they all edit {files[0]}, so the parts run in turn, each with a depends_on on the one before, "
                        "because only one worker can change that file at a time")
            why.append(f"{items} items, over {MAX_ITEMS}: {how}")
        if root and len(files) > 1:
            for f in files:
                n = file_lines(root, f)
                if n > MAX_LINES:
                    why.append(f"{f} has {n} lines, over {MAX_LINES}: give it a unit of its own")
        if u.get("size") == "XL":
            why.append("size XL is more than one worker's session: split it into L or smaller units")
        if why:
            out.append(f"unit {u['id']} ({u.get('title', '')}): " + "; ".join(why))
    return out


def mode_line(units, result, p80=None):
    deps = [f"{u['id']}←{','.join(map(str, u['depends_on']))} ({u.get('reason') or 'no reason given'})"
            for u in units if u.get("depends_on")]
    shape = " → ".join(
        ("{" + ",".join(map(str, w["units"])) + "}" if w["run"] == "parallel" else ",".join(map(str, w["units"])))
        for w in result["waves"])
    par = (f"parallel ≈{result['parallel']} min" if result["parallel"]
           else f"all in parallel exceeds the cap of {result['capped']} workers" if result.get("capped")
           else "parallel not possible")
    spread = f", p80 ≈{p80} min" if p80 else ""
    line = (f"Mode: {result['mode']} (≈{result['estimate']} min{spread}; lane ≈{result['lane']} min, {par}, "
            f"waves ≈{result['waves_total']} min). Order: {shape}.")
    if deps:
        line += " Dependencies: " + "; ".join(deps) + "."
    return line


def accuracy(project=None, rows=None):
    """How the estimates compared with the recorded unit minutes."""
    samples = samples_of(read_jsonl("runs.jsonl") if rows is None else rows)
    if project:
        samples = [s for s in samples if s["project"] == project] or samples
    if not samples:
        return ["Accuracy: no measured units yet, so every estimate is still a default."]
    model = calibration(rows=[])  # today's defaults: what an unmeasured plan would have said
    ratios, recorded = [], 0
    by_size = {}
    for s in samples:
        est = s["est"]
        if est:
            recorded += 1
        else:
            est = unit_minutes({"size": s["size"], "items": s["items"]}, model)
        r = s["min"] / est
        ratios.append(r)
        by_size.setdefault(s["size"], []).append(r)
    n = len(ratios)
    med = statistics.median(ratios)
    over = sum(r > 1 for r in ratios)
    way_over = sum(r > BUDGET for r in ratios)
    mape = statistics.mean(abs(r - 1) for r in ratios)
    lines = [f"Accuracy over {n} unit(s): actual/estimate median {med:.2f}x ({'estimates run low' if med > 1.1 else 'estimates run high' if med < 0.9 else 'estimates are close'}), "
             f"{round(100 * over / n)}% of units over their estimate, {round(100 * way_over / n)}% over {BUDGET:g}x (the budget), "
             f"mean error {round(100 * mape)}%.",
             f"  {recorded} had the estimate recorded in their plan; the other {n - recorded} are compared with today's defaults."]
    for size in DEFAULT:
        if size in by_size:
            lines.append(f"  {size}: n={len(by_size[size])}, median {statistics.median(by_size[size]):.2f}x")
    by_mode = run_ratios(read_jsonl("runs.jsonl") if rows is None else rows, project)
    if by_mode:
        n_runs = sum(len(v) for v in by_mode.values())
        lines.append(f"  whole runs: {n_runs} with workers and an estimate, active minutes (wall - user wait)/estimate:")
        for mode in sorted(by_mode):
            got = by_mode[mode]
            p80 = f", p80 {order_stat(got):.2f}x" if len(got) >= MIN_RATIOS else f", p80 needs {MIN_RATIOS} runs"
            lines.append(f"    {mode}: n={len(got)}, median {statistics.median(got):.2f}x{p80}")
    return lines


def normalize(units):
    """Accepts the plan intake.py to-plan writes as well as the older one, in
    place: ids "1" (or "U1") become 1, depends_on [{"id": "1", "reason": ".."}]
    becomes [1] with the reasons joined into "reason", and an XL unit is
    scheduled as L (it gets a Split line). Other keys, such as "risk" and
    "acs", pass through untouched."""
    def as_id(v):
        if isinstance(v, dict):
            v = v.get("id")
        s = str(v).strip()
        m = re.fullmatch(r"[Uu]?(\d+)", s)
        return int(m.group(1)) if m else v
    for u in units:
        u["id"] = as_id(u["id"])
        deps, why = [], []
        for d in u.get("depends_on") or []:
            deps.append(as_id(d))
            if isinstance(d, dict) and d.get("reason"):
                why.append(d["reason"])
        u["depends_on"] = deps
        if why and not u.get("reason"):
            u["reason"] = "; ".join(why)
        size = str(u.get("size") or "M").upper()
        u["size"] = size
    return units


def size_key(u):
    """The size whose minutes the model has: XL is scheduled as L."""
    return "L" if u.get("size") == "XL" else u.get("size")


def set_mode_line(plan_path, line, result, cap_why, busy, hint=None):
    """The ready-to-paste intake.py set-mode command for this plan's intake
    (<slug>-intake.md beside <slug>-plan.json, else the plan's "intake"), or
    None when the plan has no intake."""
    import shlex
    folder, name = os.path.split(os.path.abspath(plan_path))
    intake = os.path.join(folder, name[:-len("-plan.json")] + "-intake.md") if name.endswith("-plan.json") else None
    if not (intake and os.path.exists(intake)):
        intake = hint if hint and os.path.exists(hint) else None
    if not intake:
        return None
    how = {"parallel": "no unit depends on another, and parallel beats a lane by more than the margin",
           "lane": "a lane is as fast or faster here (one unit, small units or a chain), and costs less",
           "waves": "some units need others' code, so they run in dependency waves"}[result["mode"]]
    reason = (f"plan-waves.py chose {result['mode']}: {how}; parallel cap {cap_why}"
              + ("; busy machine (40 min margin)" if busy else ""))
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intake.py").replace("\\", "/")
    return (f"python {shlex.quote(script)} set-mode {shlex.quote(intake.replace(chr(92), '/'))} "
            f"{shlex.quote(line)} --reason {shlex.quote(reason)}")


def sonnet_planner_on(root):
    """True when the project profile at <root> opts into Sonnet planners.
    Fails closed: no root, no profile or an unreadable one keeps Opus."""
    if not root:
        return False
    try:
        with open(os.path.join(root, ".claude", "bmad-batch-project.json"), encoding="utf-8") as fh:
            return json.load(fh).get("sonnet_planner") is True
    except (OSError, ValueError, AttributeError):
        return False


def planner_of(unit, sonnet_on):
    """The planner model for one unit: sonnet only for an S unit of low risk."""
    small = unit.get("size") == "S" and str(unit.get("risk") or "").lower() == "low"
    return "sonnet" if sonnet_on and small else "opus"


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args else default


def main(args):
    if not args:
        print(__doc__)
        return 2
    if "--accuracy" in args:
        for line in accuracy(opt(args, "--project")):
            print(line)
        return 0
    path = args[0]
    data = json.load(open(path, encoding="utf-8"))
    units = normalize(data["units"])
    root = opt(args, "--root")
    project = opt(args, "--project") or (os.path.basename(os.path.normpath(root)) if root else None)
    rows = read_jsonl("runs.jsonl")
    model = calibration(project, rows)
    others = other_active_runs()
    if "--max-parallel" in args:
        max_par, cap_why = int(opt(args, "--max-parallel")), "--max-parallel"
    else:
        max_par, cap_why = parallel_cap(others)
    for later, earlier, f in chain_same_file(units):
        print(f"Chained: unit {later} now depends on unit {earlier}: both edit {f}, so they run in turn.")
    t = {u["id"]: unit_minutes(u, model, root) for u in units}
    queue = contention(others)
    heavy = heavy_share(units, t, suite_minutes(root))
    busy = "--busy" in args or others > 0
    synth = synthesis_minutes(rows)
    result = plan(units, busy=busy, max_par=max_par, times=t, queue=queue, heavy=heavy, synth=synth)
    # The p80 of a mode with enough recorded runs is their order statistic;
    # until then the unit spread, run through the same schedule.
    spread, spread_src = mode_spread(result["mode"], rows, model["spread"])
    t80 = {i: round(m * spread) for i, m in t.items()}
    if spread_src.startswith("order"):
        p80 = round(result["estimate"] * spread)
    else:
        p80 = plan(units, busy=busy, max_par=max_par, times=t80, queue=queue, heavy=heavy, synth=synth)["estimate"]
    print(f"parallel cap: {max_par} workers per wave ({cap_why})")
    for i, w in enumerate(result["waves"], 1):
        print(f"wave {i}: units {', '.join(map(str, w['units']))} — {w['run']}, ≈{w['minutes']} min")
    print("unit minutes: " + ", ".join(f"{s} {model['size'][s]} ({model['source'][s]})" for s in DEFAULT)
          + f"; per item {model['per_item']:.1f} ({model['per_item_source']}); p80 factor {spread} ({spread_src})"
          + f"; synthesis of a lane ≈{synth} min")
    print("estimates: " + ", ".join(f"unit {u['id']} ≈{t[u['id']]} min (budget {budget(t[u['id']], t80[u['id']])})"
                                    for u in units))
    path_ids, path_min = critical_path(units, t)
    print(f"Critical path: {' → '.join(map(str, path_ids))} ≈{path_min} min, the least the run can take.")
    risks = stragglers(units, t)
    for s in risks:
        print("Straggler risk: " + s + ".")
    line = mode_line(units, result, p80)
    print(line)
    set_mode = set_mode_line(path, line, result, cap_why, busy, data.get("intake"))
    if set_mode:
        print("Set mode: " + set_mode)
    split = too_big(units, root)
    for s in split:
        print("Split: " + s + ".")
    advice = advise(units, result, no_tests="--no-tests" in args, t=t, long_min=int(opt(args, "--long", LONG)))
    if advice:
        print("Advice: build directly in one session; " + "; ".join(advice) + ".")
    long_line = long_run(result, p80)
    if long_line:
        print("Long run: " + long_line + ".")
    sonnet_on = sonnet_planner_on(root)
    planners = {u["id"]: planner_of(u, sonnet_on) for u in units}
    on_sonnet = [str(i) for i, m in planners.items() if m == "sonnet"]
    if on_sonnet:
        print(f"Planner: sonnet for unit {', '.join(on_sonnet)} (S, low risk; profile sonnet_planner); "
              "opus for the rest.")
    data.update({"waves": result["waves"], "mode": result["mode"], "estimate": result["estimate"],
                 "estimate_p80": p80, "unit_minutes_used": model["size"], "advice": advice, "split": split,
                 "critical_path": {"units": path_ids, "minutes": path_min}, "stragglers": risks,
                 "unit_estimates": {str(u["id"]): {"p50": t[u["id"]], "p80": t80[u["id"]],
                                                   "budget": budget(t[u["id"]], t80[u["id"]]),
                                                   "planner": planners[u["id"]]}
                                    for u in units}})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return 0


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '≤' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
