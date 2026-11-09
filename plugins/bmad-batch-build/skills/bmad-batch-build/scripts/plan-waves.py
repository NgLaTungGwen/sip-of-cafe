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
wrote their log in the last 15 minutes.

Unit minutes. With "items", a unit takes 40 min plus 2.2 min per item, plus 30
min per 1000 lines of every file over 2500 lines (sonar-zero-5, 2026-11-07:
units of 21 to 46 findings took 85 to 140 min; one with 64 findings and a
6500-line file took 315). Without items, its size: S 50, M 100, L 160. Every
number is blended with recorded runs (~/.claude/bmad-batch-runs/runs.jsonl,
each row's unit_minutes) from the first measurement on: (n x measured median +
3 x prior) / (n + 3), first over all projects, then over this project's own
runs (--project, else the folder name of --root).

A single number hides the tail, so every estimate also comes with a p80: unit
times scaled by the measured p80/p50 ratio of recorded units (1.6 until there
are 5), run through the same schedule. sonar-zero-5 was estimated at 6 h and
took 13.4 h. Each unit gets a budget, twice its estimate (and at least 60 min
over it); straggler.py compares a running unit with it.

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

Prints the waves, every estimate, the critical path, units that dominate the
run (**Straggler risk**), the chosen mode, the Mode line for the approval table
and, when one applies, an Advice line. Writes the schedule back into plan.json
("waves", "mode", "estimate", "estimate_p80", "unit_estimates", ...).

--accuracy compares recorded units with their estimate (the one the plan gave
when recorded, else today's model) and says whether the estimates run low.
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
        q = min(max(quantile(ratios, 0.8), 1.2), 3.0)
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
        return model["size"][u["size"]]
    t = BASE_ITEMS + model["per_item"] * items
    if root:
        for f in u.get("files") or []:
            n = file_lines(root, f)
            if n > BIG_FILE:
                t += BIG_FILE_MIN * (n - BIG_FILE) / 1000
    return max(30, round(t))


def budget(minutes):
    return max(round(BUDGET * minutes), minutes + 60)


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


def cap_for(other_runs):
    return MAX_PARALLEL if other_runs == 0 else (3 if other_runs == 1 else 2)


def plan(units, busy=False, minutes=None, max_par=MAX_PARALLEL, times=None, queue=None, heavy=HEAVY_SHARE):
    minutes = minutes or DEFAULT
    t = times or {u["id"]: minutes[u["size"]] for u in units}
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
    lane_total = sum(t[u["id"]] for u in order) - WARM * (len(order) - 1) + RUN
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
    runs = []
    for r in read_jsonl("runs.jsonl") if rows is None else rows:
        m = re.search(r"Mode: \w+ \(≈(\d+) min", r.get("mode") or "")
        if m and r.get("min_wall") and (not project or r.get("project") == project):
            runs.append(r["min_wall"] / int(m.group(1)))
    if runs:
        lines.append(f"  whole runs: {len(runs)} with an estimate, wall minutes/estimate median {statistics.median(runs):.2f}x")
    return lines


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
    units = data["units"]
    root = opt(args, "--root")
    project = opt(args, "--project") or (os.path.basename(os.path.normpath(root)) if root else None)
    model = calibration(project)
    others = other_active_runs()
    max_par = int(opt(args, "--max-parallel", cap_for(others)))
    for later, earlier, f in chain_same_file(units):
        print(f"Chained: unit {later} now depends on unit {earlier}: both edit {f}, so they run in turn.")
    t = {u["id"]: unit_minutes(u, model, root) for u in units}
    t80 = {i: round(m * model["spread"]) for i, m in t.items()}
    queue = contention(others)
    heavy = heavy_share(units, t, suite_minutes(root))
    busy = "--busy" in args or others > 0
    result = plan(units, busy=busy, max_par=max_par, times=t, queue=queue, heavy=heavy)
    p80 = plan(units, busy=busy, max_par=max_par, times=t80, queue=queue, heavy=heavy)["estimate"]
    print(f"parallel cap: {max_par} workers per wave ({others} other active run(s))")
    for i, w in enumerate(result["waves"], 1):
        print(f"wave {i}: units {', '.join(map(str, w['units']))} — {w['run']}, ≈{w['minutes']} min")
    print("unit minutes: " + ", ".join(f"{s} {model['size'][s]} ({model['source'][s]})" for s in DEFAULT)
          + f"; per item {model['per_item']:.1f} ({model['per_item_source']}); p80/p50 {model['spread']}")
    print("estimates: " + ", ".join(f"unit {u['id']} ≈{t[u['id']]} min (budget {budget(t[u['id']])})" for u in units))
    path_ids, path_min = critical_path(units, t)
    print(f"Critical path: {' → '.join(map(str, path_ids))} ≈{path_min} min, the least the run can take.")
    risks = stragglers(units, t)
    for s in risks:
        print("Straggler risk: " + s + ".")
    print(mode_line(units, result, p80))
    split = too_big(units, root)
    for s in split:
        print("Split: " + s + ".")
    advice = advise(units, result, no_tests="--no-tests" in args, t=t, long_min=int(opt(args, "--long", LONG)))
    if advice:
        print("Advice: build directly in one session; " + "; ".join(advice) + ".")
    long_line = long_run(result, p80)
    if long_line:
        print("Long run: " + long_line + ".")
    data.update({"waves": result["waves"], "mode": result["mode"], "estimate": result["estimate"],
                 "estimate_p80": p80, "unit_minutes_used": model["size"], "advice": advice, "split": split,
                 "critical_path": {"units": path_ids, "minutes": path_min}, "stragglers": risks,
                 "unit_estimates": {str(u["id"]): {"p50": t[u["id"]], "p80": t80[u["id"]], "budget": budget(t[u["id"]])}
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
