"""Schedule a run's units by their dependencies, and estimate each mode.

usage: python plan-waves.py <plan.json> [--busy] [--max-parallel N] [--no-tests]

plan.json is written by the orchestrator at the split, next to the run file
(<slug>-plan.json in the run folder, run_dir.py):

    {"units": [
      {"id": 1, "title": "chart kit", "size": "M", "depends_on": [], "reason": ""},
      {"id": 3, "title": "cost chart", "size": "M", "depends_on": [1, 2],
       "reason": "imports the chart kit; uses the new contract fields"}
    ]}

Only compile- or test-time dependencies belong in depends_on: unit 3 cannot
build or test without unit 1's code. A dependency that an agreed interface
can cover (B codes against A's route or type as specified) is not one.

Units are sorted into waves: wave k holds the units whose dependencies are all
in earlier waves. Each wave then runs the cheaper way for itself, by the same
rule as a whole run:

    lane step     = unit minutes, minus 10 for every unit after the run's first
                    (installed worktree, warm context)
    parallel wave = max(unit minutes) + 50 (merge into the integration branch
                    and verify it)
    run total     = sum of wave times + 30 (preparation and the final verify)

A wave of 2 to 4 units runs in parallel when that is more than the margin
faster (20 min, 40 when --busy), a tie going to lane. A wave of 5 or more is
parallel. With no dependencies this is exactly the old lane/parallel choice;
with a single chain it is lane.

No parallel wave has more than --max-parallel workers (default 4; 3 when one
other run is active on the machine, 2 when two or more are). A larger wave is
split into several in a row. Each worker brings a coder and up to four
reviewers, and Claude Code allows 20 subagents at once per session, so 5
workers already got launches refused; and on 2026-10-26 runs of 12, 9 and 5
parallel workers at the same time stalled the machine-wide build queue for
hours. Other active runs are sessions that ran the skill and wrote their log
in the last 15 minutes.

Unit minutes come from recorded runs (~/.claude/bmad-batch-runs/runs.jsonl,
each row's unit_minutes) once a size has 5 measurements, and otherwise from
the first calibration: S 50, M 100, L 160.

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

On 2026-10-27 a 7-unit POC in 3 waves of up to 3, in a repo that bans tests,
was estimated at 618 min; one session built it directly in far less.

Prints the waves, every estimate, the chosen mode, the Mode line for the
approval table and, when one applies, an Advice line. Writes the schedule back
into plan.json ("waves", "mode", "advice").
"""
import json
import os
import statistics
import sys

DEFAULT = {"S": 50, "M": 100, "L": 160}
WARM = 10        # saved per lane unit after the first
MERGE = 50       # a parallel wave's merge and verify
RUN = 30         # preparation and the final verify, once per run
MIN_SAMPLES = 5
MAX_PARALLEL = 4     # workers in one parallel wave (Claude Code's 20-subagent limit)


def calibration():
    store = os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")
    samples = {s: [] for s in DEFAULT}
    path = os.path.join(store, "runs.jsonl")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            for u in row.get("unit_minutes") or []:
                if u.get("size") in samples and u.get("min"):
                    samples[u["size"]].append(u["min"])
    minutes, source = {}, {}
    for size, default in DEFAULT.items():
        got = samples[size]
        if len(got) >= MIN_SAMPLES:
            minutes[size], source[size] = round(statistics.median(got)), f"measured, n={len(got)}"
        else:
            minutes[size], source[size] = default, f"default ({len(got)} measured)"
    return minutes, source


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


def split_waves(waves, max_par):
    """A dependency level with more units than max_par becomes several waves."""
    out = []
    for wave in waves:
        if len(wave) <= max_par:
            out.append(wave)
        else:
            out += [wave[i:i + max_par] for i in range(0, len(wave), max_par)]
    return out


def plan(units, busy=False, minutes=None, max_par=MAX_PARALLEL):
    minutes = minutes or DEFAULT
    t = {u["id"]: minutes[u["size"]] for u in units}
    margin = 40 if busy else 20
    levels = waves_of(units)
    waves = split_waves(levels, max_par)
    schedule, total, first = [], RUN, True
    for wave in waves:
        k = len(wave)
        times = [t[u["id"]] for u in wave]
        lane = sum(times) - WARM * (k - (1 if first else 0))
        par = max(times) + MERGE
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
    parallel_total = max(t.values()) + MERGE + RUN if independent else None
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


def advise(units, result, no_tests=False):
    """Why one session building the work directly beats a batch run; [] when it doesn't."""
    widest = max(len(w["units"]) if w["run"] == "parallel" else 1 for w in result["waves"])
    reasons = []
    if len(units) > 1 and result["mode"] == "lane":
        reasons.append("chain: nothing runs in parallel, so every unit pays the batch overhead for no gain")
    elif widest <= 2 and len(result["waves"]) >= 3:
        reasons.append(f"narrow: at most {widest} units at once over {len(result['waves'])} waves in a row")
    if no_tests and widest <= 3:
        reasons.append("no tests: the review-and-verify loop has little to check")
    return reasons


def mode_line(units, result):
    deps = [f"{u['id']}←{','.join(map(str, u['depends_on']))} ({u.get('reason') or 'no reason given'})"
            for u in units if u.get("depends_on")]
    shape = " → ".join(
        ("{" + ",".join(map(str, w["units"])) + "}" if w["run"] == "parallel" else ",".join(map(str, w["units"])))
        for w in result["waves"])
    par = (f"parallel ≈{result['parallel']} min" if result["parallel"]
           else f"all in parallel exceeds the cap of {result['capped']} workers" if result.get("capped")
           else "parallel not possible")
    line = (f"Mode: {result['mode']} (≈{result['estimate']} min; lane ≈{result['lane']} min, {par}, "
            f"waves ≈{result['waves_total']} min). Order: {shape}.")
    if deps:
        line += " Dependencies: " + "; ".join(deps) + "."
    return line


def main(args):
    if not args:
        print(__doc__)
        return 2
    path = args[0]
    data = json.load(open(path, encoding="utf-8"))
    units = data["units"]
    minutes, source = calibration()
    others = other_active_runs()
    max_par = int(args[args.index("--max-parallel") + 1]) if "--max-parallel" in args else cap_for(others)
    result = plan(units, busy="--busy" in args or others > 0, minutes=minutes, max_par=max_par)
    print(f"parallel cap: {max_par} workers per wave ({others} other active run(s))")
    for i, w in enumerate(result["waves"], 1):
        print(f"wave {i}: units {', '.join(map(str, w['units']))} — {w['run']}, ≈{w['minutes']} min")
    print("unit minutes: " + ", ".join(f"{s} {minutes[s]} ({source[s]})" for s in DEFAULT))
    print(mode_line(units, result))
    advice = advise(units, result, no_tests="--no-tests" in args)
    if advice:
        print("Advice: build directly in one session; " + "; ".join(advice) + ".")
    data.update({"waves": result["waves"], "mode": result["mode"], "estimate": result["estimate"],
                 "unit_minutes_used": minutes, "advice": advice})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
