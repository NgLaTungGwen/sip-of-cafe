"""Says which running units have passed their budget.

usage: python straggler.py <plan.json> <unit>=<minutes running> [<unit>=<minutes> ...]

plan-waves.py writes a p50, a p80 and a budget (twice the estimate, and at
least 60 min over it) for every unit under "unit_estimates" in the plan file.
A unit past its p80 is **Slow**; one past its budget is a **Straggler**, and
the script exits 1. sonar-zero-5 (2026-11-07) was estimated at 6 h and ran
13.4 h, one unit 315 min, with nothing comparing a running unit with its
estimate.

A straggler is not stopped by this script: the orchestrator decides (SKILL.md,
section 5, "Stragglers"). Never fails the caller on bad input: it says what it
could not read and exits 0.
"""
import json
import sys


def check(plan, running):
    """[(level, line)] for the units past their p80; level is 'straggler' or 'slow'."""
    est = plan.get("unit_estimates") or {}
    out = []
    for unit, minutes in sorted(running.items()):
        e = est.get(str(unit))
        if not e:
            continue
        if minutes > e["budget"]:
            out.append(("straggler", f"Straggler: unit {unit} has run {minutes} min, over its budget of {e['budget']} "
                                     f"(estimate {e['p50']}, p80 {e['p80']}). Stop it after its current round, gate what "
                                     "it committed, and give the items still open to a fresh worker."))
        elif minutes > e["p80"]:
            out.append(("slow", f"Slow: unit {unit} has run {minutes} min, past its p80 of {e['p80']} "
                                f"(estimate {e['p50']}, budget {e['budget']}). Check its fix rounds at the next return."))
    return out


def main(args):
    try:
        plan = json.load(open(args[0], encoding="utf-8"))
        running = {}
        for a in args[1:]:
            unit, minutes = a.split("=")
            running[int(unit)] = float(minutes)
    except (IndexError, OSError, ValueError) as e:
        print(f"straggler.py: could not read its input ({e}); usage: straggler.py <plan.json> <unit>=<minutes> ...")
        return 0
    found = check(plan, running)
    for _, line in found:
        print(line)
    if not found:
        print("No unit is past its p80.")
    return 1 if any(level == "straggler" for level, _ in found) else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
