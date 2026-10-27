"""The run journal: what happened in one bmad-batch-build run, failures first.

usage: python journal.py <session.jsonl> [--slug <slug>] [--root <main checkout>]

cost-report.py builds it for every run it records or collects, writes
<root>/.claude/worktrees/_runs/<slug>-journal.md and ~/.claude/bmad-batch-runs/
journals/<session>.md, and keeps the items on the run's row in runs.jsonl. The
end-of-run report shows its problems, and the enhancer agent reads it to fix
the skill.

Sources, all read by script so nothing depends on an agent remembering:
- every failed tool call in the session and its subagents, sorted into
  buckets (a failed gate, a failed command, a classifier denial, a call the
  harness blocked, a shell syntax error, ...);
- the commands heavy.sh and bg.sh ran (events.jsonl, from log-event.py), which
  catches failures an agent never saw as a failed call;
- each agent's reply: a worker or synthesis that ended without it, the
  SKILL ISSUES line, and synthesis' REPAIRS, VERIFY and NOTES;
- units dispatched more than once, agents resumed, turn budgets hit;
- the run file's own notes (SKILL ISSUE, NOT READY, resumes, stuck units).
The journal only sorts and counts; deciding whose fault an item is belongs to
the enhancer (references/enhancer.md).
"""
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERR = re.compile(r"(\w*Error\b|\bERR!|\bFAIL\b|\bfailed\b|error TS\d+|No such file|not found|denied|refused)", re.I)

# Display order: what most needs the user's eye first.
BUCKETS = [
    ("gate_failed", "Units blocked or failed the gate"),
    ("missing_reply", "Agent ended without its reply"),
    ("budget", "Turn budget hit"),
    ("skill_issue", "Skill issues reported by agents"),
    ("wrapper_failed", "Commands failed inside heavy.sh / bg.sh"),
    ("command_failed", "Commands failed"),
    ("classifier_denial", "Denied by the auto-mode classifier"),
    ("harness_block", "Blocked by the harness"),
    ("shell_syntax", "Shell syntax errors"),
    ("permission_denied", "Permission denied"),
    ("resume", "Units dispatched again or agents resumed"),
    ("synthesis", "Synthesis repairs, failed checks and notes"),
    ("run_note", "Notes in the run file"),
    ("tool_error", "Other failed tool calls"),
]
TURN_BUDGET = {"worker": 300, "haiku": 300, "synthesis": 400}


def load(path):
    out = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def blocks(lines):
    for x in lines:
        m = x.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict):
                    yield x, b


def text_of(content):
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ANSI.sub("", str(content or ""))


def error_line(text):
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    hits = [s for s in lines if ERR.search(s) and not s.startswith("Exit code")]
    return (hits[-1] if hits else (lines[-1] if lines else ""))[:240]


def norm(path):
    p = (path or "").replace("\\", "/").rstrip("/").lower()
    m = re.match(r"^/([a-z])/(.*)", p)
    return f"{m.group(1)}:/{m.group(2)}" if m else p


def classify(name, inp, text):
    """(bucket, detail) for one failed tool call."""
    cmd = (inp or {}).get("command", "") if name == "Bash" else ""
    if "auto mode classifier" in text:
        m = re.search(r"Reason: \[?([^\].]+)", text)
        return "classifier_denial", f"{name}: {m.group(1).strip() if m else 'denied'}"
    if "built-in Claude Code safety check" in text:
        return "classifier_denial", f"{name}: built-in safety check ({error_line(text)[:90]})"
    if "<tool_use_error>Blocked" in text or text.startswith("Blocked:"):
        return "harness_block", re.split(r"(?<=followed by: )", error_line(text.replace("<tool_use_error>", "")))[0][:100]
    if re.search(r"doesn't want to proceed|was rejected|Permission to use .* denied|permission denied by user", text, re.I):
        return "permission_denied", f"{name}"
    if name != "Bash":
        return "tool_error", f"{name}: {error_line(text)[:120]}"
    if re.search(r"unexpected EOF while looking for matching|syntax error near unexpected token", text):
        return "shell_syntax", "bash: " + error_line(text)[:120]
    if "check-unit.sh" in cmd:
        m = re.search(r"NOT READY:.*", text)
        return "gate_failed", m.group(0)[:200] if m else error_line(text)
    return "command_failed", error_line(text)


def short_cmd(cmd):
    c = re.sub(r"\s+", " ", cmd or "").strip()
    c = re.sub(r"[A-Za-z]:[/\\][^\s\"']*[/\\]", ".../", c)
    return c[:140]


def role(meta, first):
    agent = meta.get("agentType", "")
    if agent == "haiku-implementer":
        return "haiku"
    if agent == "Explore":
        return "explore"
    if "synthesis" in agent or "ynthesis" in meta.get("description", ""):
        return "synthesis"
    if "worker" in agent or "Build Auto worker" in first:
        return "worker"
    if agent == "bmad-batch-enhancer":
        return "enhancer"
    return "reviewer" if meta.get("spawnDepth", 1) > 1 else "other"


def reply_text(lines):
    """The agent's final reply: its last text blocks, or a handback message."""
    out = []
    for x, b in blocks(lines[-12:]):
        if x.get("type") == "assistant" and b.get("type") == "text":
            out.append(b.get("text", ""))
        if b.get("type") == "tool_use" and b.get("name") == "SubagentHandback":
            out.append((b.get("input") or {}).get("message", ""))
    return "\n".join(out)


def field(text, name):
    m = re.findall(rf"^\s*{name}: *(.+)$", text, re.M)
    return m[-1].strip() if m else None


def main_span(lines):
    """The run's part of the main session: from the skill invocation to the
    `cost-report.py --record` call, or, for a run that never got there, to the
    last synthesis result. Work done in the session afterwards (a follow-up
    investigation, a clean-up) is not the run's."""
    marks = ("<command-name>/bmad-batch-build</command-name>", '"skill": "bmad-batch-build"',
             "<command-name>/bmad-parallel-build</command-name>", '"skill": "bmad-parallel-build"')
    start = next((i for i, x in enumerate(lines) if x.get("type") in ("user", "assistant")
                  and any(k in json.dumps(x.get("message", "")) for k in marks)), 0)
    end, synth = None, set()
    for i in range(start, len(lines)):
        for _, b in blocks([lines[i]]):
            inp = b.get("input") or {}
            if b.get("type") == "tool_use" and b.get("name") == "Bash" and "cost-report.py" in inp.get("command", "") \
                    and "--record" in inp.get("command", ""):
                return lines[start:i + 1]
            if b.get("type") == "tool_use" and b.get("name") == "Agent" and "ynthesis" in json.dumps(inp)[:300]:
                synth.add(b["id"])
            if b.get("type") == "tool_result" and b.get("tool_use_id") in synth:
                end = i + 1
    return lines[start:end]


def build(session, slug=None, root=None, store=None):
    """Returns the journal as a dict: items, counts, timeline, outcome."""
    base = session[:-6]
    store = store or os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")
    main = main_span(load(session))
    logs = [("orchestrator", main, {}, None)]
    cutoff = next((x["timestamp"] for x in reversed(main) if x.get("timestamp")), None)
    for f in sorted(glob.glob(os.path.join(base, "subagents", "*.jsonl"))):
        mp = f[:-6] + ".meta.json"
        meta = json.load(open(mp, encoding="utf-8")) if os.path.exists(mp) else {}
        lines = load(f)
        first = next((x["timestamp"] for x in lines if x.get("timestamp")), None)
        # Agents started after the run ended belong to later work in the
        # session; the enhancer, which runs after the report, is kept.
        if cutoff and first and first > cutoff and meta.get("agentType") != "bmad-batch-enhancer":
            continue
        logs.append((None, lines, meta, f))

    # Label each agent by its role; a worker by its unit, and an agent a worker
    # launched (Haiku, reviewers) by that worker, e.g. "haiku (worker u2)".
    parent_of = {}  # Agent tool_use id -> the log that launched it (None = main)
    for _, lines, _, f in logs:
        for _, b in blocks(lines):
            if b.get("type") == "tool_use" and b.get("name") == "Agent":
                parent_of[b["id"]] = f
    labels = {}

    def label(f, depth=0):
        if f is None:
            return "orchestrator"
        if f not in labels:
            lines, meta = next((ln, m) for _, ln, m, g in logs if g == f)
            first = json.dumps(lines[0]) if lines else ""
            r = role(meta, first)
            u = re.search(r"Unit: (\d+)", first)
            parent = parent_of.get(meta.get("toolUseId"))
            if r == "worker":
                # Older prompts had no Unit line: number workers by dispatch order.
                n = u.group(1) if u else f"#{sum(1 for v in labels.values() if v.startswith('worker')) + 1}"
                labels[f] = f"worker u{n}" if u else f"worker {n}"
            elif parent is not None and depth < 5:
                labels[f] = f"{r} ({label(parent, depth + 1).split(' (')[0]})"
            else:
                labels[f] = r
        return labels[f]

    # Workers first, in dispatch order, so their numbers follow the run.
    for _, lines, _, g in sorted((p for p in logs if p[3]), key=lambda p: (p[1] or [{}])[0].get("timestamp") or ""):
        label(g)
    labels_by_id = [g for g in labels]
    items = collections.OrderedDict()

    def add(bucket, who, detail, cmd="", when=None):
        key = (bucket, who.split(" (")[0], detail)
        it = items.get(key)
        if it:
            it["count"] += 1
            it["last"] = when or it.get("last")
        else:
            items[key] = {"bucket": bucket, "who": who, "detail": detail, "cmd": short_cmd(cmd),
                          "count": 1, "first": when, "last": when}

    timeline = []
    start = end = None
    outcome = {}
    per_unit = collections.Counter()
    for lab, lines, meta, f in logs:
        who = label(f)
        uses = {}
        turns = set()
        stamps = [x["timestamp"] for x in lines if x.get("timestamp")]
        for x, b in blocks(lines):
            if x.get("type") == "assistant":
                turns.add((x.get("message") or {}).get("id"))
            if b.get("type") == "tool_use":
                uses[b["id"]] = (b.get("name"), b.get("input") or {})
                if f is None and b.get("name") == "SendMessage":
                    to = str((b.get("input") or {}).get("to", ""))
                    g = next((g for g in labels_by_id if to and to in g), None)
                    add("resume", who, f"resumed {label(g) if g else to[:40]}", when=x.get("timestamp"))
            elif b.get("type") == "tool_result" and b.get("is_error"):
                name, inp = uses.get(b.get("tool_use_id"), ("?", {}))
                bucket, detail = classify(name, inp, text_of(b.get("content")))
                add(bucket, who, detail, inp.get("command", "") if isinstance(inp, dict) else "", x.get("timestamp"))
        if stamps:
            start = min(start or stamps[0], stamps[0])
            end = max(end or stamps[-1], stamps[-1])
        if f is None:
            continue
        r = role(meta, json.dumps(lines[0]) if lines else "")
        reply = reply_text(lines)
        result = None
        if r == "worker":
            per_unit[who] += 1
            status = field(reply, "STATUS")
            result = f"STATUS {status}" if status else None
            if not field(reply, "SPEC"):
                add("missing_reply", who, "worker ended without its SPEC/STATUS reply (out of turns, or handed back)")
            blocked = field(reply, "BLOCKED")
            if blocked and blocked.lower() != "none":
                add("gate_failed", who, f"BLOCKED: {blocked[:200]}")
        elif r == "synthesis":
            res = field(reply, "RESULT")
            result = f"RESULT {res}" if res else None
            if not res:
                add("missing_reply", who, "synthesis ended without its RESULT reply")
            for k in ("PR", "MERGED"):
                if field(reply, k):
                    outcome[k.lower()] = field(reply, k)
            for k in ("REPAIRS", "NOTES"):
                v = field(reply, k)
                if v and v.lower().strip(". ") != "none":
                    add("synthesis", who, f"{k}: {v[:240]}")
            v = field(reply, "VERIFY")
            if v and re.search(r"fail|red|skip|not run", v, re.I):
                add("synthesis", who, f"VERIFY: {v[:240]}")
        issues = field(reply, "SKILL ISSUES")
        if issues and issues.lower().strip(". ") != "none":
            # Issues are separated by " | " (older replies used ";", which
            # agents also write inside an issue, so those stay whole).
            for part in issues.split(" | "):
                if part.strip():
                    add("skill_issue", who, part.strip()[:300])
        if len(turns) >= TURN_BUDGET.get(r, 10 ** 9):
            add("budget", who, f"{len(turns)} turns, budget {TURN_BUDGET[r]}")
        if r in ("worker", "synthesis", "explore", "enhancer") and stamps:
            mins = round((ts(stamps[-1]) - ts(stamps[0])).total_seconds() / 60)
            timeline.append({"who": who, "start": stamps[0][:16].replace("T", " "), "min": mins,
                             "turns": len(turns), "result": result or "-"})
    for who, n in per_unit.items():
        if n > 1 and who != "worker":
            add("resume", who, f"dispatched {n} times")

    # Commands heavy.sh and bg.sh ran during this run, in its worktrees.
    ev_path = os.path.join(store, "events.jsonl")
    if start and os.path.exists(ev_path):
        lo, hi = ts(start), ts(end) + dt.timedelta(minutes=10)
        rt = norm(root) if root else None
        for line in open(ev_path, encoding="utf-8", errors="ignore"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("rc", 0) == 0 or not (lo <= ts(e["end"]) <= hi):
                continue
            cwd = norm(e.get("cwd"))
            if rt and not (cwd == rt or cwd.startswith(rt + "/")):
                continue
            # Other runs in the same project share the root; keep this run's
            # worktrees (<slug> and <slug>-<n>) and commands at the root itself.
            if slug and "/worktrees/" in cwd and not re.search(
                    rf"/worktrees/{re.escape(slug.lower())}(-\d+)?(/|$)", cwd):
                continue
            wt = norm(e.get("cwd")).split("/worktrees/")[-1].split("/")[0] if "/worktrees/" in norm(e.get("cwd")) else "root"
            add("wrapper_failed", f"{e.get('tool')} in {wt}", f"rc {e['rc']}: {e.get('error') or '?'}", e.get("cmd", ""),
                e.get("end"))

    # The run file's own notes.
    if root and slug:
        rf = os.path.join(root, ".claude", "worktrees", "_runs", f"{slug}.md")
        if os.path.exists(rf):
            for line in open(rf, encoding="utf-8", errors="ignore"):
                s = line.strip()
                m = re.match(r"^-?\s*SKILL ISSUE: (.+)$", s)
                if m:
                    add("skill_issue", "run file", m.group(1)[:300])
                    continue
                for pat, label in ((r"NOT READY[^.;]*", "gate"), (r"[Ee]nv note:[^.]*", "env"),
                                   (r"[^.]*\b(resume[ds]?|handed back)\b[^.]*", "resume"),
                                   (r"[^.]*\b(stuck|left out|abandoned|cancelled)\b[^.]*", "stuck")):
                    for hit in re.finditer(pat, s):
                        add("run_note", f"run file ({label})", hit.group(0).strip()[:240])

    order = {b: i for i, (b, _) in enumerate(BUCKETS)}
    out = sorted(items.values(), key=lambda it: (order.get(it["bucket"], 99), -it["count"]))
    counts = collections.Counter()
    for it in out:
        counts[it["bucket"]] += it["count"]
    return {"session": os.path.basename(session)[:-6], "slug": slug, "start": start, "end": end,
            "items": out, "counts": dict(counts), "timeline": sorted(timeline, key=lambda t: t["start"]), "outcome": outcome}


def headline(j):
    names = dict(BUCKETS)
    parts = [f"{names[b].lower()} {j['counts'][b]}" for b, _ in BUCKETS if j["counts"].get(b)]
    return "journal: " + ("; ".join(parts) if parts else "no failures or issues recorded")


def markdown(j, row=None):
    names = dict(BUCKETS)
    out = [f"# Run journal: {j.get('slug') or j['session'][:8]}", ""]
    meta = [f"session {j['session'][:8]}", f"{(j.get('start') or '')[:16].replace('T', ' ')} UTC"]
    if row:
        meta += [row.get("project") or "", f"skill {row.get('skill_version') or '?'}",
                 f"{row.get('min_wall', 0) - row.get('min_user_wait', 0)} min excl. user wait", f"${row.get('usd', 0):.1f}"]
    out += [" · ".join(m for m in meta if m), ""]
    if j["outcome"]:
        out += [f"Outcome: {'; '.join(f'{k} {v}' for k, v in j['outcome'].items())}", ""]
    out += [headline(j), ""]
    if j["timeline"]:
        out += ["## Timeline", "", "| agent | start (UTC) | min | turns | result |", "|---|---|---|---|---|"]
        out += [f"| {t['who']} | {t['start']} | {t['min']} | {t['turns']} | {t['result']} |" for t in j["timeline"]]
        out.append("")
    for b, title in BUCKETS:
        its = [it for it in j["items"] if it["bucket"] == b]
        if not its:
            continue
        out += [f"## {title} ({sum(it['count'] for it in its)})", ""]
        for it in its[:12]:
            n = f" ×{it['count']}" if it["count"] > 1 else ""
            c = f" — `{it['cmd']}`" if it["cmd"] else ""
            out.append(f"- **{it['who']}**{n}: {it['detail']}{c}")
        if len(its) > 12:
            out.append(f"- … {len(its) - 12} more")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    slug = a[a.index("--slug") + 1] if "--slug" in a else None
    root = a[a.index("--root") + 1] if "--root" in a else None
    j = build(a[0], slug, root)
    sys.stdout.write(markdown(j))
