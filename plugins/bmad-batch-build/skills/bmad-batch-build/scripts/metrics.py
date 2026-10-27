"""Run metrics in one stable format, for sharing and for a dashboard.

usage: python metrics.py export [--out <file>] [--since YYYY-MM-DD] [--include-text]
       python metrics.py import <bundle.json> [<bundle.json> ...]
       python metrics.py id

cost-report.py records every run on this machine in <store>/runs.jsonl, and
heavy.sh and bg.sh log each heavy command in <store>/events.jsonl (<store> is
~/.claude/bmad-batch-runs; BMAD_RUNS_DIR overrides it). Those files are the
skill's internal format and change with it, and they hold paths, branch names
and command lines. This script is the outward-facing format:

export writes one bundle (schema SCHEMA) of this machine's runs and heavy
commands, keyed by a random installation id (<store>/installation-id), never
the host name. It keeps the numbers: cost, time, tokens, workers, review and
journal counts, skill version, CPUs and RAM. It drops paths, commands, branch
names and error text, and hashes the project name. --include-text adds back
the agents' skill-issue reports and journal details, which are what the
enhancer learns from; only the person exporting decides that. Nothing is sent
anywhere: the person shares the file.

import merges bundles into <store>/metrics/runs.jsonl and events.jsonl, one
row per (installation, session) and per heavy command, newest bundle winning.
It also refreshes this machine's own rows there, with project names and text,
so a dashboard reads one dataset in one format. Nothing runs import on its
own: run it (after cost-report.py --collect) before reading the dataset, and
at least every 45 days, or heavy commands older than cost-report's prune of
events.jsonl are lost.
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid

SCHEMA = 1

# Numeric and categorical fields copied from a runs.jsonl row as they are.
KEEP = ["start", "usd", "usd_by_role", "usd_idle_max", "mtok_in", "ktok_out", "mtok_w1h_subagents",
        "min_wall", "min_user_wait", "min_slowest_worker", "min_synthesis", "min_rest", "workers",
        "units_reviewed", "units_haiku", "first_review_high_medium", "units_with_high",
        "fix_merge_commits", "max_turns", "by_role", "turn_budget_hit", "system_map",
        "skill_version", "version_exact"]


def store_dir():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def installation_id():
    path = os.path.join(store_dir(), "installation-id")
    if os.path.exists(path):
        value = open(path, encoding="utf-8").read().strip()
        if value:
            return value
    value = str(uuid.uuid4())
    os.makedirs(store_dir(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    return value


def read_jsonl(path):
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8", errors="ignore"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def mode_kind(mode):
    """'lane', 'parallel' or 'waves' from the run file's Mode line."""
    m = re.search(r"Mode:\s*(lane|parallel|waves)", mode or "")
    return m.group(1) if m else None


def project_ref(name, inst, clear):
    if clear or not name:
        return name
    return "p-" + hashlib.sha256(f"{inst}:{name}".encode()).hexdigest()[:10]


def scrub(text, names=()):
    """Text for --include-text: URLs, absolute paths, branch names and this
    installation's project names and run slugs replaced."""
    text = re.sub(r"https?://\S+", "<url>", text or "")
    text = re.sub(r"(?:[A-Za-z]:)?[\\/][\w.@~ -]+(?:[\\/][\w.@~ -]+)+", "<path>", text)
    text = re.sub(r"\b[\w.-]+/(?:feat|fix|docs|chore|refactor|test|perf|ci|build|plan|enhance)\b[\w./-]*",
                  "<branch>", text)
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), "<name>", text, flags=re.I)
    return text[:400]


def run_row(r, inst, text, clear, names=()):
    out = {k: r.get(k) for k in KEEP}
    hw = r.get("hardware") or {}
    j = r.get("journal") or {}
    out.update({
        "installation_id": inst,
        "session": r.get("session"),
        "project": project_ref(r.get("project"), inst, clear),
        "mode": mode_kind(r.get("mode")),
        "hardware": {"os": hw.get("os"), "cpus": hw.get("cpus"), "ram_gb": hw.get("ram_gb")},
        "units": [{"size": u.get("size"), "min": u.get("min")} for u in r.get("unit_minutes") or []],
        "journal_counts": j.get("counts") or {},
        # The synthesis outcome names the PR and the merged units; only whether a PR opened leaves.
        "pr_opened": bool(re.match(r"https?://", (j.get("outcome") or {}).get("pr") or "")),
        "skill_issue_count": len(r.get("skill_issues") or []),
    })
    if clear:
        out["slug"] = r.get("slug")
    if text:
        out["skill_issues"] = [scrub(s, names) for s in r.get("skill_issues") or []]
        out["journal_items"] = [{"bucket": i.get("bucket"), "who": i.get("who"), "count": i.get("count"),
                                 "detail": scrub(i.get("detail"), names)} for i in j.get("items") or []]
    return out


KINDS = [("install", r"npm ci|(?:npm|bun|pnpm|yarn) install|install-deps"),
         ("verify", r"verify\.sh|check-test-env|check-unit"),
         ("typecheck", r"typecheck|\btsc\b"),
         ("lint", r"\blint\b|eslint|knip"),
         ("test", r"vitest|jest|pytest|\btest\b"),
         ("build", r"\bbuild\b")]


def command_kind(cmd):
    """What a heavy command was, without the command line: the order follows
    heavy.sh's memory classes (an install before a test that follows it)."""
    for kind, pattern in KINDS:
        if re.search(pattern, cmd or ""):
            return kind
    return "other"


def event_row(e, inst):
    """A heavy command: its label (heavy, bg:suite, ...), kind, result and seconds."""
    return {"installation_id": inst, "end": e.get("end"), "tool": e.get("tool"),
            "kind": command_kind(e.get("cmd")), "rc": e.get("rc"), "sec": e.get("sec")}


def bundle(since=None, text=False, clear=False):
    inst = installation_id()
    since = since or ""
    rows = read_jsonl(os.path.join(store_dir(), "runs.jsonl"))
    # Project names and run slugs also turn up in the agents' prose.
    names = () if clear else {n for r in rows for n in (r.get("project"), r.get("slug")) if n}
    runs = [run_row(r, inst, text, clear, names) for r in rows if (r.get("start") or "") >= since]
    events = [event_row(e, inst) for e in read_jsonl(os.path.join(store_dir(), "events.jsonl"))
              if (e.get("end") or "") >= since]
    return {"schema": SCHEMA, "installation_id": inst,
            "exported": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "includes_text": text, "runs": runs, "events": events}


def export(args):
    out = args[args.index("--out") + 1] if "--out" in args else None
    since = args[args.index("--since") + 1] if "--since" in args else None
    b = bundle(since, text="--include-text" in args)
    out = out or os.path.join(store_dir(), f"metrics-{b['installation_id'][:8]}-{b['exported'][:10]}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(b, fh, indent=1)
    kept = "with the agents' issue text (paths removed)" if b["includes_text"] else "numbers only, no text"
    print(f"export: {len(b['runs'])} run(s), {len(b['events'])} heavy command(s), {kept}; "
          f"no host name, paths, commands or branch names; project names hashed.\n"
          f"wrote {out}\nRead it before sharing it.")
    return 0


def merge(rows, new, key):
    by = {key(r): r for r in rows}
    for r in new:
        by[key(r)] = r
    return sorted(by.values(), key=lambda r: r.get("start") or r.get("end") or "")


def import_bundles(paths):
    mdir = os.path.join(store_dir(), "metrics")
    runs_path, events_path = os.path.join(mdir, "runs.jsonl"), os.path.join(mdir, "events.jsonl")
    runs, events = read_jsonl(runs_path), read_jsonl(events_path)
    # This machine's own rows, in full, so the dataset covers it too.
    bundles = [bundle(text=True, clear=True)]
    for p in paths:
        b = json.load(open(p, encoding="utf-8"))
        if b.get("schema") != SCHEMA:
            print(f"import: skipped {p}: schema {b.get('schema')}, this script reads {SCHEMA}")
            continue
        bundles.append(b)
    for b in bundles:
        runs = merge(runs, b["runs"], lambda r: (r["installation_id"], r["session"]))
        events = merge(events, b["events"], lambda e: (e["installation_id"], e["end"], e["tool"], e["sec"]))
    write_jsonl(runs_path, runs)
    write_jsonl(events_path, events)
    installs = sorted({r["installation_id"] for r in runs})
    print(f"import: {len(runs)} run(s) from {len(installs)} installation(s), {len(events)} heavy command(s)\n"
          f"dataset: {runs_path}\n         {events_path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["export"]:
        sys.exit(export(args[1:]))
    if args[:1] == ["import"]:
        sys.exit(import_bundles(args[1:]))
    if args[:1] == ["id"]:
        print(installation_id())
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
