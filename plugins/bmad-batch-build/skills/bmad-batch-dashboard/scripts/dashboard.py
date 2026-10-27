"""Builds the bmad-batch-build metrics dashboard: one HTML page with d3 charts.

usage: python dashboard.py [--refresh] [--import <bundle.json> ...] [--out <file>] [--open]

Reads the dataset metrics.py keeps in <store>/metrics/ (runs.jsonl and
events.jsonl; <store> is ~/.claude/bmad-batch-runs, BMAD_RUNS_DIR overrides
it) and writes <store>/dashboard.html, with the data inside the page.

--refresh first records runs that ended since the last record
(cost-report.py --collect, which reads every session log and can take
minutes), then refreshes this machine's rows (metrics.py import).
--import merges metrics files other machines exported (metrics.py export).
Both need the bmad-batch-build scripts, 0.12.2 or later; without them the page
is built from the dataset as it is, and says so.
--open opens the page in the default browser.
"""
import datetime as dt
import glob
import json
import os
import statistics
import subprocess
import sys
import webbrowser

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(SKILL, "assets", "dashboard.html")
EVENT_FIELDS = ("installation_id", "end", "tool", "kind", "rc", "sec")


def store_dir():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def find_scripts():
    """The bmad-batch-build scripts folder that has metrics.py: the sibling
    skill (plugin or `npx skills` install), this project's copy, the user's
    copy, the maintainer's clone, then any plugin cache."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(os.path.dirname(SKILL), "bmad-batch-build", "scripts"),
        os.path.join(os.getcwd(), ".claude", "skills", "bmad-batch-build", "scripts"),
        os.path.join(home, ".claude", "skills", "bmad-batch-build", "scripts"),
        os.path.join(store_dir(), "src", "sip-of-cafe", "plugins", "bmad-batch-build", "skills", "bmad-batch-build", "scripts"),
    ]
    candidates += sorted(glob.glob(os.path.join(home, ".claude", "plugins", "**", "skills", "bmad-batch-build", "scripts"),
                                   recursive=True), key=os.path.getmtime, reverse=True)
    for c in candidates:
        if os.path.isfile(os.path.join(c, "metrics.py")) and os.path.isfile(os.path.join(c, "cost-report.py")):
            return c
    return None


def run(script, args):
    r = subprocess.run([sys.executable, script] + args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (r.stdout or "").strip().splitlines()
    if r.returncode != 0:
        print(f"dashboard: {os.path.basename(script)} {' '.join(args[:1])} failed: {(r.stderr or '').strip()[-300:]}")
    return r.returncode == 0, lines


def read_jsonl(path):
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="ignore"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def own_installation():
    path = os.path.join(store_dir(), "installation-id")
    return open(path, encoding="utf-8").read().strip() if os.path.exists(path) else None


def build(out, note=""):
    mdir = os.path.join(store_dir(), "metrics")
    runs = read_jsonl(os.path.join(mdir, "runs.jsonl"))
    events = [{k: e.get(k) for k in EVENT_FIELDS} for e in read_jsonl(os.path.join(mdir, "events.jsonl"))]
    data = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "own_installation": own_installation(), "note": note, "runs": runs, "events": events}
    # "<" never occurs outside JSON strings, so escaping it keeps </script> and <!-- out of the page.
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    html = open(TEMPLATE, encoding="utf-8").read().replace("/*__DATA__*/", payload, 1)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    return runs, events


def summary(runs, events, out):
    def med(xs):
        xs = [x for x in xs if x is not None]
        return statistics.median(xs) if xs else None

    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    recent = [r for r in runs if (r.get("start") or "") >= cut]
    prs = [r for r in runs if r.get("pr_opened") is not None]
    lines = [f"dashboard: {out}",
             f"runs: {len(runs)} from {len({r.get('installation_id') for r in runs})} machine(s), "
             f"{len({r.get('project') for r in runs})} project(s); {len(events)} heavy commands",
             f"spend: ${sum(r.get('usd') or 0 for r in runs):,.0f} total; last 30 days ${sum(r.get('usd') or 0 for r in recent):,.0f} "
             f"over {len(recent)} run(s)"]
    if runs:
        lines.append(f"median per run: ${med([r.get('usd') for r in runs]):,.1f}, "
                     f"{med([r.get('min_wall') for r in runs]) or 0:.0f} min wall; "
                     f"PR opened {sum(1 for r in prs if r.get('pr_opened'))} of {len(prs)}")
    print("\n".join(lines))


def main(args):
    out = args[args.index("--out") + 1] if "--out" in args else os.path.join(store_dir(), "dashboard.html")
    bundles = []
    if "--import" in args:
        for a in args[args.index("--import") + 1:]:
            if a.startswith("--"):
                break
            bundles.append(os.path.abspath(a))
    scripts = find_scripts()
    note = ""
    if scripts:
        print(f"dashboard: using {scripts}")
        if "--refresh" in args:
            ok, lines = run(os.path.join(scripts, "cost-report.py"), ["--collect"])
            print("\n".join(lines[:1]) if ok else "dashboard: collect failed; showing the runs already recorded")
        if "--refresh" in args or bundles or not os.path.exists(os.path.join(store_dir(), "metrics", "runs.jsonl")):
            ok, lines = run(os.path.join(scripts, "metrics.py"), ["import"] + bundles)
            if lines:
                print(lines[0])
    else:
        note = "Not refreshed: no bmad-batch-build 0.12.2+ scripts were found"
        print(f"dashboard: {note}; building from the dataset as it is")
    runs, events = build(out, note)
    summary(runs, events, out)
    if not runs:
        print("dashboard: no runs recorded yet; the page is empty until a batch build has run")
    if "--open" in args:
        webbrowser.open("file:///" + os.path.abspath(out).replace("\\", "/").lstrip("/"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
