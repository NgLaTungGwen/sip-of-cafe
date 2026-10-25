"""Schedule a run's units by their dependencies, and estimate each mode.

usage: python plan-waves.py <plan.json> [--busy]

plan.json is written by the orchestrator at the split, next to the run file
(_runs/<slug>-plan.json):

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

Unit minutes come from recorded runs (~/.claude/bmad-batch-runs/runs.jsonl,
each row's unit_minutes) once a size has 5 measurements, and otherwise from
the first calibration: S 50, M 100, L 160.

Prints the waves, every estimate, the chosen mode and the Mode line for the
approval table. Writes the schedule back into plan.json ("waves", "mode").
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


def plan(units, busy=False, minutes=None):
    minutes = minutes or DEFAULT
    t = {u["id"]: minutes[u["size"]] for u in units}
    margin = 40 if busy else 20
    waves = waves_of(units)
    schedule, total, first = [], RUN, True
    for wave in waves:
        k = len(wave)
        times = [t[u["id"]] for u in wave]
        lane = sum(times) - WARM * (k - (1 if first else 0))
        par = max(times) + MERGE
        if k >= 5 or (k >= 2 and par + margin < lane):
            how, wt = "parallel", par
        else:
            how, wt = "lane", lane
        schedule.append({"units": [u["id"] for u in wave], "run": how, "minutes": wt})
        total += wt
        first = False
    order = [u for w in waves for u in w]
    lane_total = sum(t[u["id"]] for u in order) - WARM * (len(order) - 1) + RUN
    independent = len(waves) == 1
    parallel_total = max(t.values()) + MERGE + RUN if independent else None
    kinds = {w["run"] for w in schedule}
    if kinds == {"lane"}:
        mode, chosen = "lane", lane_total
    elif independent:
        mode, chosen = "parallel", parallel_total
    else:
        mode, chosen = "waves", total
    return {"waves": schedule, "mode": mode, "estimate": chosen,
            "lane": lane_total, "parallel": parallel_total, "waves_total": total}


def mode_line(units, result):
    deps = [f"{u['id']}←{','.join(map(str, u['depends_on']))} ({u.get('reason') or 'no reason given'})"
            for u in units if u.get("depends_on")]
    shape = " → ".join(
        ("{" + ",".join(map(str, w["units"])) + "}" if w["run"] == "parallel" else ",".join(map(str, w["units"])))
        for w in result["waves"])
    par = f"parallel ≈{result['parallel']} min" if result["parallel"] else "parallel not possible"
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
    result = plan(units, busy="--busy" in args, minutes=minutes)
    for i, w in enumerate(result["waves"], 1):
        print(f"wave {i}: units {', '.join(map(str, w['units']))} — {w['run']}, ≈{w['minutes']} min")
    print("unit minutes: " + ", ".join(f"{s} {minutes[s]} ({source[s]})" for s in DEFAULT))
    print(mode_line(units, result))
    data.update({"waves": result["waves"], "mode": result["mode"], "estimate": result["estimate"],
                 "unit_minutes_used": minutes})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
