"""Summary of every recorded bmad-batch-build run, grouped by skill version.

usage: python runs-summary.py [--since YYYY-MM-DD] [--out <file>]

Reads ~/.claude/bmad-batch-runs/runs.jsonl (BMAD_RUNS_DIR overrides the
folder), which `cost-report.py --record` appends to at the end of every run.
Medians, not means: one stuck run can cost as much as ten normal ones.
"""
import collections
import json
import os
import statistics
import sys


def med(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def machine(r):
    hw = r.get("hardware") or {}
    return f" [{hw.get('cpus', '?')} CPU, {hw.get('ram_gb', '?')} GB]" if hw else ""


def fmt(v, unit=""):
    if v is None:
        return "-"
    return f"{v:.1f}{unit}" if isinstance(v, float) and not v.is_integer() else f"{v:.0f}{unit}"


def main(args):
    since = args[args.index("--since") + 1] if "--since" in args else ""
    out = args[args.index("--out") + 1] if "--out" in args else None
    store = os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")
    path = os.path.join(store, "runs.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("start") or "") >= since:
                rows.append(row)
    lines = [f"bmad-batch-build runs{' since ' + since if since else ''}: {len(rows)} (data: {path})", ""]
    if not rows:
        lines.append("No runs recorded yet.")
    by_version = collections.defaultdict(list)
    for row in rows:
        by_version[row.get("skill_version") or "?"].append(row)
    head = (f"{'version':14} {'runs':>4} {'$/run':>6} {'wall-user':>9} {'slowest wkr':>11} {'synth':>6} "
            f"{'rest':>5} {'reviewed':>8} {'hi+med':>6} {'fix(merge)':>10} {'1h writes':>9} {'budget hit':>10}")
    if rows:
        lines += ["Medians per run (minutes exclude user wait)", head]
    for version, rs in sorted(by_version.items()):
        units = sum(r["workers"] for r in rs)
        reviewed = sum(r["units_reviewed"] for r in rs)
        lines.append(
            f"{version:14} {len(rs):4} {fmt(med(r['usd'] for r in rs), ''):>6} "
            f"{fmt(med(r['min_wall'] - r['min_user_wait'] for r in rs), 'm'):>9} "
            f"{fmt(med(r['min_slowest_worker'] for r in rs), 'm'):>11} {fmt(med(r['min_synthesis'] for r in rs), 'm'):>6} "
            f"{fmt(med(r['min_rest'] for r in rs), 'm'):>5} {f'{reviewed}/{units}':>8} "
            f"{fmt(med(r['first_review_high_medium'] for r in rs)):>6} {fmt(med(r['fix_merge_commits'] for r in rs)):>10} "
            f"{sum(1 for r in rs if r['mtok_w1h_subagents'] > 0)}/{len(rs):<7} "
            f"{sum(1 for r in rs if r['turn_budget_hit']):>10}")
    # Code discovery: lookups and context size per role, medians per run.
    roles = ("explore", "worker", "haiku", "synthesis")
    with_roles = [r for r in rows if r.get("by_role")]
    if with_roles:
        lines += ["", "Code discovery, medians per run: turns, context per turn (k tokens), Read + Grep/Glob + small Bash calls"]
        lines.append(f"{'version':14} " + " ".join(f"{role:>24}" for role in roles))
        for version, rs in sorted(by_version.items()):
            rs = [r for r in rs if r.get("by_role")]
            if not rs:
                continue
            cells = []
            for role in roles:
                got = [r["by_role"].get(role) for r in rs if r["by_role"].get(role)]
                if not got:
                    cells.append(f"{'-':>24}")
                    continue
                turns = med(g["turns"] for g in got)
                ctx = med(g["ktok_ctx_per_turn"] for g in got)
                look = med(g["read"] + g["grep_glob"] + g["bash_other"] for g in got)
                cells.append(f"{fmt(turns) + 't ' + fmt(ctx) + 'k ' + fmt(look) + ' look':>24}")
            lines.append(f"{version:14} " + " ".join(cells))
    if rows:
        lines += ["", "Per run"]
        for r in sorted(rows, key=lambda r: r["start"]):
            lines.append(
                f"  {r['start'][:10]} {r.get('project', '?'):14} {r['slug'][:28]:28} {r.get('skill_version') or '?':14} "
                f"${r['usd']:6.1f} {r['min_wall'] - r['min_user_wait']:4}m {r['workers']:2} units, reviewed {r['units_reviewed']}"
                f"{', BUDGET HIT ' + '/'.join(r['turn_budget_hit']) if r['turn_budget_hit'] else ''}"
                f"{', no 1h-TTL writes' if r['mtok_w1h_subagents'] == 0 else ''}"
                f"{machine(r)}")
        lines += ["", "Checks: '1h writes' should be every run (otherwise the agents' cacheTtl did not apply);",
                  "'budget hit' should be 0 (otherwise raise maxTurns or look at that agent)."]
        issues = [(r, i) for r in sorted(rows, key=lambda r: r["start"]) for i in r.get("skill_issues") or []]
        lines += ["", "Skill issues (workarounds the agents reported for the skill's own scripts)"]
        lines += [f"  {r['start'][:10]} {r['slug'][:28]:28} {r.get('skill_version') or '?':14} {i}" for r, i in issues] or ["  none"]
    text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv[1:])
