"""Token and cost profile of bmad-batch-build runs (formerly bmad-parallel-build).

Usage: python cost-report.py <session.jsonl> [<session.jsonl> ...]
       python cost-report.py --record <slug> [--root <main checkout>]
       python cost-report.py --collect

--record is what the orchestrator runs at the end of every run. It finds the
session that ran <slug> (the newest session log under ~/.claude/projects that
mentions the run file _runs/<slug>.md), writes the report to
<root>/.claude/worktrees/_runs/<slug>-cost.txt, and appends one JSON row to
~/.claude/bmad-batch-runs/runs.jsonl (BMAD_RUNS_DIR overrides the folder), one
row per run across every project, replacing an earlier row of the same run.
Rows also carry the machine's CPUs and RAM. scripts/runs-summary.py reads that file.

--collect records every session on this machine that ran the skill and has no
current row: runs whose --record call was missed, crashed runs, and runs from
before recording existed. Reports go to ~/.claude/bmad-batch-runs/reports/.
--record runs it too, so each finished run sweeps up any that were missed.

The main session is counted only from the skill invocation to the last
synthesis Agent result, so unrelated work in the same session is excluded.
"""
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

# $/MTok: input, output, cache read. Cache writes: 1.25x input (5m), 2x (1h).
PRICES = {"opus": (4, 20, 0.20), "sonnet": (2, 10, 0.20), "haiku": (0.10, 0.50, 0.01), "fable": (10, 50, 1.0)}


def price(model):
    return next((v for k, v in PRICES.items() if k in (model or "")), PRICES["opus"])


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main_span(lines):
    """Index range from the skill invocation to the last synthesis result."""
    # The invocation is the slash command or a Skill tool call, not any mention.
    marks = tuple(m for name in ("bmad-batch-build", "bmad-parallel-build")
                  for m in (f"<command-name>/{name}</command-name>", f'"skill": "{name}"'))
    start = next((i for i, x in enumerate(lines) if x.get("type") in ("user", "assistant")
                  and any(k in json.dumps(x.get("message", "")) for k in marks)), 0)
    synth_ids = set()
    end = len(lines)
    for i, x in enumerate(lines):
        m = x.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_use" and b["name"] == "Agent" and "ynthesis" in json.dumps(b["input"])[:300]:
                synth_ids.add(b["id"])
            if b.get("type") == "tool_result" and b.get("tool_use_id") in synth_ids and i >= start:
                end = i + 1
    return start, end


def role(meta, first_line):
    agent = meta.get("agentType")
    if agent == "haiku-implementer":
        return "haiku"
    if agent == "Explore":
        return "explore"
    if agent in ("bmad-batch-synthesis", "bmad-pb-synthesis") or "ynthesis" in meta.get("description", ""):
        return "synthesis"
    if agent in ("bmad-batch-worker", "bmad-pb-worker") or "Build Auto worker" in first_line:
        return "worker"
    return "reviewer" if meta.get("spawnDepth", 1) > 1 else "other"


def wait_kind(block):
    if block["name"] == "Bash":
        c = block["input"].get("command", "")
        # Order matters: a poll loop that greps a test log is a wait, not a test.
        for k, pat in (("poll background", r"\b(until|while)\b.*\bsleep\b|for i in .*\bsleep\b|bg\.sh\S* wait"),
                       ("npm ci", r"npm ci|install-deps"), ("bun install", r"bun install"),
                       ("prepare-worktree", r"prepare-worktree"), ("render skill", r"render_skill\.py"),
                       ("test", r"vitest|\b(npm|bun|pnpm) (run )?test\b|npm run test:"),
                       ("build", r"run build"), ("typecheck/lint", r"typecheck|\btsc\b|lint")):
            if re.search(pat, c, re.S):
                return "Bash " + k
        return "Bash other"
    return block["name"]


def profile(lines, lab, acc, edits, waits, spans=None):
    """Adds token and cost totals to acc[lab], and tool waits to waits.

    spans, when given, collects each tool call's (start, end) per (lab, kind),
    so parallel calls can be counted once (see union()).
    Returns (first, last) timestamps of the agent, or None.
    """
    calls = {}
    pending = {}
    stamps = [x["timestamp"] for x in lines if x.get("timestamp")]
    for x in lines:
        m = x.get("message")
        if x.get("type") == "user" and isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in pending and x.get("timestamp"):
                    t0, kind = pending.pop(b["tool_use_id"])
                    waits[(lab, kind)][0] += 1
                    waits[(lab, kind)][1] += (ts(x["timestamp"]) - ts(t0)).total_seconds()
                    if spans is not None:
                        spans[(lab, kind)].append((ts(t0), ts(x["timestamp"])))
            continue
        if x.get("type") != "assistant" or not isinstance(m, dict) or m.get("model") == "<synthetic>":
            continue
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use" and x.get("timestamp"):
                pending[b["id"]] = (x["timestamp"], wait_kind(b))
        # One message is logged as one line per content block, each carrying
        # usage; only the last line has the final output count, so keep the
        # first timestamp and the last usage.
        if "usage" in m:
            if m["id"] in calls:
                calls[m["id"]][2] = m["usage"]
            else:
                calls[m["id"]] = [x["timestamp"], m["model"], m["usage"], set()]
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use":
                if m.get("id") in calls:
                    calls[m["id"]][3].add(wait_kind(b))
                if b["name"] in ("Edit", "Write") and lab == "worker":
                    p = b["input"].get("file_path", "").replace("\\", "/")
                    edits["spec/output" if re.search(r"_bmad|\.md$", p) else "source"] += 1
    prev = None
    c = acc[lab]
    for t, model, u, tools in sorted(calls.values(), key=lambda c: c[0]):
        tool = " + ".join(sorted(tools)) or "-"
        pin, pout, pcr = price(model)
        cw = u.get("cache_creation_input_tokens", 0)
        w1 = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
        cr = u.get("cache_read_input_tokens", 0)
        out = u.get("output_tokens", 0)
        cw_usd = ((cw - w1) * 1.25 + w1 * 2) * pin / 1e6
        c["tok_in"] += u.get("input_tokens", 0) + cw + cr
        c["tok_w1h"] += w1
        c["tok_out"] += out
        c["usd_write"] += cw_usd
        c["usd_read"] += cr * pcr / 1e6
        c["usd_out"] += out * pout / 1e6
        c["usd_in"] += u.get("input_tokens", 0) * pin / 1e6
        c["turns"] += 1
        # The cache expired if the gap beat the TTL the write used: 5 min, or
        # 1 h for agents with cacheTtl 1h (they re-write as 1h after a gap).
        # An upper bound: a write after a long gap is counted whole, even when
        # part of it would have been written anyway (new tool output).
        gap = (ts(t) - ts(prev[0])).total_seconds() if prev else 0
        if gap > (3600 if w1 else 300) and cw > 2000:
            c["usd_idle"] += cw_usd
            acc["_idle"][(lab, prev[1])] += cw_usd
        prev = (t, tool)
    return (stamps[0], stamps[-1]) if stamps else None


def cost(v):
    return v["usd_write"] + v["usd_read"] + v["usd_out"] + v["usd_in"]


def union(intervals):
    """Seconds covered by a list of (start, end), counting overlaps once."""
    total, cur = 0.0, None
    for a, b in sorted(intervals):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
            continue
        if cur:
            total += (cur[1] - cur[0]).total_seconds()
        cur = [a, b]
    return total + ((cur[1] - cur[0]).total_seconds() if cur else 0)


def run(session_path):
    """Returns (acc, edits, start, waits, spans, quality, ivals).

    spans maps role -> list of seconds; ivals maps (role, kind) -> tool-call intervals.
    """
    base = session_path[:-6]
    acc = collections.defaultdict(collections.Counter)
    edits = collections.Counter()
    waits = collections.defaultdict(lambda: [0, 0.0])
    spans = collections.defaultdict(list)
    quality = {"workers": [], "fix_merge": 0, "max_turns": collections.Counter(), "worker_spans": []}
    ivals = collections.defaultdict(list)
    lines = load(session_path)
    s, e = main_span(lines)
    main_ts = profile(lines[s:e], "main", acc, edits, waits, ivals)
    ends = {}
    acc["main"]["agents"] += 1
    for f in glob.glob(base + "/subagents/*.jsonl"):
        mp = f[:-6] + ".meta.json"
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        sub = load(f)
        lab = role(meta, json.dumps(sub[0]) if sub else "")
        if lab == "worker":
            quality["workers"].append(unit_quality(sub, children(base, sub)))
        if lab == "synthesis":
            quality["fix_merge"] += sum(1 for b in tool_uses(sub) if b["name"] == "Bash"
                                        and "fix(merge)" in b["input"].get("command", "")
                                        and "commit" in b["input"].get("command", ""))
        before = acc[lab]["turns"]
        span = profile(sub, lab, acc, edits, waits, ivals)
        quality["max_turns"][lab] = max(quality["max_turns"][lab], acc[lab]["turns"] - before)
        if span:
            spans[lab].append((ts(span[1]) - ts(span[0])).total_seconds())
            ends.setdefault(lab, []).append(span[1])
            if lab == "worker":
                # The worktree the worker ran in names its unit (<slug>-<n>), or
                # the shared lane worktree (<slug>); see unit_minutes().
                m = re.search(r"Project root: (\S+)", json.dumps(sub[0]) if sub else "")
                name = re.split(r"[\\/]+", m.group(1).rstrip("\\/"))[-1] if m else None
                quality["worker_spans"].append({"wt": name, "start": span[0],
                                                "min": round((ts(span[1]) - ts(span[0])).total_seconds() / 60)})
        acc[lab]["agents"] += 1
    # Synthesis usually runs in the background, so its Agent result returns at
    # launch; the run ends at whichever is later, main or synthesis.
    # Time the run spent waiting on the user: the gap before every typed user
    # message (an approval asked in plain text) plus AskUserQuestion answers.
    user_wait = waits.get(("main", "AskUserQuestion"), [0, 0.0])[1]
    prev = None
    for x in lines[s:e]:
        t = x.get("timestamp")
        if not t:
            continue
        c = (x.get("message") or {}).get("content") if isinstance(x.get("message"), dict) else x.get("content")
        typed = x.get("type") in ("user", "queue-operation") and not (
            isinstance(c, list) and any(b.get("type") == "tool_result" for b in c if isinstance(b, dict))
        ) and "<task-notification>" not in json.dumps(c or "") and c
        if typed and prev and x.get("type") == "user" and not x.get("isMeta"):
            user_wait += (ts(t) - ts(prev)).total_seconds()
        if x.get("type") != "queue-operation":
            prev = t
    acc["main"]["user_wait"] += user_wait
    start = main_ts[0] if main_ts else ""
    end = max([main_ts[1]] + ends.get("synthesis", [])) if main_ts else ""
    spans["main"].append((ts(end) - ts(start)).total_seconds() if main_ts else 0)
    return acc, edits, start, waits, spans, quality, ivals


def tool_uses(lines):
    for x in lines:
        m = x.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    yield b


def children(base, lines):
    """Meta of the subagents this agent launched, linked by tool_use id."""
    ids = {b["id"] for b in tool_uses(lines) if b["name"] == "Agent"}
    out = []
    for mp in glob.glob(base + "/subagents/*.meta.json"):
        meta = json.load(open(mp))
        if meta.get("toolUseId") in ids:
            out.append(meta)
    return out


def unit_quality(lines, kids):
    """Workflow path and first-review findings of one worker's unit."""
    text = "\n".join(b["input"].get("message", "") for b in tool_uses(lines[-8:]) if b["name"] == "SubagentHandback")
    for x in lines[-8:]:
        m = x.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            text += "\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text")
    spec = re.findall(r"^SPEC: *(\S.*?\.md)\s*$", text, flags=re.M)
    q = {"haiku": any(k.get("agentType") == "haiku-implementer" for k in kids),
         "reviewed": any(re.search(r"review|hunter|audit", k.get("description", ""), re.I) for k in kids),
         "high": None, "medium": None, "passes": 0, "loops": 0}
    if spec and os.path.exists(spec[-1]):
        t = open(spec[-1], encoding="utf-8").read()
        log = t.split("## Review Triage Log", 1)[1].split("\n## ", 1)[0] if "## Review Triage Log" in t else ""
        passes = re.split(r"^### .*Review pass", log, flags=re.M)[1:]
        q["passes"] = len(passes)
        if passes:
            h = re.search(r"high (\d+)", passes[0])
            m = re.search(r"medium (\d+)", passes[0])
            q["high"] = int(h.group(1)) if h else None
            q["medium"] = int(m.group(1)) if m else None
        lp = re.search(r"review_loop_iteration: *'?(\d+)", t)
        q["loops"] = int(lp.group(1)) if lp else 0
    return q


def mins(sec):
    return f"{sec / 60:.0f}m" if sec else "-"


def report(paths):
    """Prints the report for these sessions; returns one data row per session."""
    rows = []
    grand = collections.defaultdict(collections.Counter)
    idle = collections.Counter()
    edits = collections.Counter()
    all_waits = collections.defaultdict(lambda: [0, 0.0])
    all_union = collections.Counter()
    timing = []
    units = []
    fix_merge = []
    print(f"{'session':10} {'date':10} {'workers':>7} {'$total':>7} {'$main':>6} {'$work':>6} {'$rev':>5} {'$haiku':>6} {'$synth':>6} {'$idle≤':>6} {'Mtok in':>8} {'Ktok out':>8}")
    for p in paths:
        acc, ed, start, waits, spans, quality, ivals = run(p)
        for k, iv in ivals.items():
            all_union[k] += union(iv)
        units += quality["workers"]
        fix_merge.append(quality["fix_merge"])
        edits.update(ed)
        idle.update(acc.pop("_idle", {}))
        for k, (n, sec) in waits.items():
            all_waits[k][0] += n
            all_waits[k][1] += sec
        usd = {k: cost(v) for k, v in acc.items()}
        for k, v in acc.items():
            grand[k].update(v)
        tok_in = sum(v["tok_in"] for v in acc.values())
        tok_out = sum(v["tok_out"] for v in acc.values())
        print(f"{os.path.basename(p)[:8]:10} {start[:10]:10} {acc['worker']['agents']:7} {sum(usd.values()):7.1f} "
              f"{usd.get('main', 0):6.1f} {usd.get('worker', 0):6.1f} {usd.get('reviewer', 0):5.1f} "
              f"{usd.get('haiku', 0):6.2f} {usd.get('synthesis', 0):6.1f} {sum(v['usd_idle'] for v in acc.values()):6.1f} "
              f"{tok_in / 1e6:8.1f} {tok_out / 1e3:8.0f}")
        ask = acc["main"]["user_wait"]
        wall, slowest, synth = spans["main"][0], max(spans["worker"], default=0), sum(spans["synthesis"])
        q = quality["workers"]
        scored_q = [u for u in q if u["high"] is not None or u["medium"] is not None]
        rows.append({
            "session": os.path.basename(p)[:-6], "start": start,
            "usd": round(sum(usd.values()), 2),
            "usd_by_role": {k: round(v, 2) for k, v in usd.items()},
            "usd_idle_max": round(sum(v["usd_idle"] for v in acc.values()), 2),
            "mtok_in": round(tok_in / 1e6, 2), "ktok_out": round(tok_out / 1e3, 1),
            "mtok_w1h_subagents": round(sum(v["tok_w1h"] for k, v in acc.items() if k != "main") / 1e6, 2),
            "min_wall": round(wall / 60), "min_user_wait": round(ask / 60),
            "min_slowest_worker": round(slowest / 60), "min_synthesis": round(synth / 60),
            "min_rest": round(max(wall - ask - slowest - synth, 0) / 60),
            "workers": acc["worker"]["agents"],
            "units_reviewed": sum(u["reviewed"] for u in q), "units_haiku": sum(u["haiku"] for u in q),
            "first_review_high_medium": round(sum((u["high"] or 0) + (u["medium"] or 0) for u in scored_q) / len(scored_q), 1) if scored_q else None,
            "units_with_high": sum(1 for u in scored_q if u["high"]),
            "fix_merge_commits": quality["fix_merge"],
            "max_turns": dict(quality["max_turns"]),
            "worker_spans": quality["worker_spans"],
            # Code discovery per role: how many turns, how large the context each
            # turn re-reads, and how many lookups (Read, Grep/Glob, small Bash).
            "by_role": {lab: {
                "agents": v["agents"], "turns": v["turns"],
                "ktok_ctx_per_turn": round(v["tok_in"] / v["turns"] / 1e3, 1) if v["turns"] else None,
                "read": waits.get((lab, "Read"), [0])[0],
                "grep_glob": waits.get((lab, "Grep"), [0])[0] + waits.get((lab, "Glob"), [0])[0],
                "bash_other": waits.get((lab, "Bash other"), [0])[0],
            } for lab, v in acc.items() if not lab.startswith("_") and v["turns"]},
        })
        timing.append((os.path.basename(p)[:8], spans["main"][0], ask, max(spans["worker"], default=0),
                       sum(spans["synthesis"]), tok_in + tok_out, sum(usd.values())))
    import statistics
    total = sum(cost(v) for v in grand.values())
    n = max(len(paths), 1)
    tok = sum(v["tok_in"] + v["tok_out"] for v in grand.values())
    print(f"\nTOTAL ${total:.0f}, {tok / 1e6:.0f} Mtok; per run mean ${total / n:.1f}, {tok / n / 1e6:.1f} Mtok")
    # Means hide outliers (one stuck run once cost $449 alone), so also give the
    # median and the mean without runs above twice the median cost or twice the
    # median time excluding user wait.
    if timing:
        med_usd = statistics.median(t[6] for t in timing)
        med_net = statistics.median(t[1] - t[2] for t in timing)
        outliers = sorted(t[0] for t in timing if t[6] > 2 * med_usd or (med_net and t[1] - t[2] > 2 * med_net))
        kept = [t[6] for t in timing if t[0] not in outliers]
        print(f"per run median ${med_usd:.1f}; mean without outliers "
              f"{'$%.1f' % statistics.mean(kept) if kept else '-'} over {len(kept)} runs; "
              f"outliers (>2x median cost or time): {', '.join(outliers) or 'none'}")
    print()

    # Wall-clock runs from the skill invocation to the synthesis reply, so it
    # includes the user's time at the split approval (the "user wait" column).
    print("Timing per run (m = minutes)")
    print(f"{'session':10} {'wall':>6} {'user wait':>9} {'wall-user':>9} {'slowest wkr':>11} {'synthesis':>9} {'rest':>9} {'Mtok/run':>8} {'Mtok/$':>7}")
    for name, wall, ask, worker, synth, tk, usd in timing:
        rest = max(wall - ask - worker - synth, 0)
        print(f"{name:10} {mins(wall):>6} {mins(ask):>9} {mins(wall - ask):>9} {mins(worker):>11} {mins(synth):>9} "
              f"{mins(rest):>9}{' !' if rest > 3600 else '  '}{tk / 1e6:6.1f} {tk / 1e6 / usd if usd else 0:7.1f}")
    if timing:
        for label, f in (("average", statistics.mean), ("median", statistics.median)):
            v = [f([t[i] for t in timing]) for i in range(1, 5)]
            nu = f([t[1] - t[2] for t in timing])
            rest = f([max(t[1] - t[2] - t[3] - t[4], 0) for t in timing])
            print(f"{label:10} {mins(v[0]):>6} {mins(v[1]):>9} {mins(nu):>9} {mins(v[2]):>11} {mins(v[3]):>9} {mins(rest):>9}")
    print("rest = wall - user wait - slowest worker - synthesis: the split and Explore, preflight, tickets, the gate,\n"
          "lane units after the first, and any stall (a permission prompt nobody answered, a session left idle).\n"
          "'!' marks rest over an hour; look at the main session's gaps for that run.")

    print("\nTool wait time (tool_use -> tool_result). 'total' sums every call; 'union' counts time\n"
          "covered by parallel calls once, so it is what the role actually spent waiting on that kind.")
    print(f"{'role':10} {'kind':22} {'calls':>5} {'total':>7} {'union':>7} {'avg':>7} {'union/run':>9}")
    for (lab, kind), (cnt, sec) in sorted(all_waits.items(), key=lambda kv: -all_union[kv[0]]):
        if sec < 600:
            continue
        u = all_union[(lab, kind)]
        print(f"{lab:10} {kind:22} {cnt:5} {sec / 3600:6.1f}h {u / 3600:6.1f}h {sec / cnt / 60:6.1f}m {u / n / 60:8.0f}m")
    print()
    print("$in is uncached input; $idle≤ is an upper bound (a whole write after a long gap is counted).")
    print(f"{'role':10} {'agents':>6} {'turns':>6} {'Mtok in':>8} {'Mtok out':>8} {'$write':>7} {'$read':>6} {'$in':>5} {'$out':>6} {'$total':>7} {'$idle≤':>7}")
    for k, v in sorted(grand.items(), key=lambda kv: -cost(kv[1])):
        print(f"{k:10} {v['agents']:6} {v['turns']:6} {v['tok_in']/1e6:8.1f} {v['tok_out']/1e6:8.2f} {v['usd_write']:7.1f} "
              f"{v['usd_read']:6.1f} {v['usd_in']:5.1f} {v['usd_out']:6.1f} {cost(v):7.1f} {v['usd_idle']:7.1f}")
    print("\nIdle cache rewrites, upper bound (gap beyond the 5 min or 1 h TTL), by what the agent was waiting on:")
    for (lab, tool), v in idle.most_common(10):
        print(f"  ${v:5.1f}  {lab:9} {tool}")
    print(f"\nWorker Edit/Write calls: {dict(edits)}")

    # Quality, read from each unit's spec. Baseline with Opus implementers
    # (2026-10-13..20): 5.6 high+medium findings per unit at the first review,
    # and 2 of 47 units with any high.
    print("\nQuality per unit")
    print(f"  workers {len(units)}: full workflow (haiku + review) "
          f"{sum(1 for u in units if u['haiku'] and u['reviewed'])}, reviewed "
          f"{sum(u['reviewed'] for u in units)}, handed to haiku {sum(u['haiku'] for u in units)}")
    scored = [u for u in units if u["high"] is not None or u["medium"] is not None]
    if scored:
        hm = [(u["high"] or 0) + (u["medium"] or 0) for u in scored]
        print(f"  first review: {sum(hm) / len(hm):.1f} high+medium per unit over {len(scored)} units; "
              f"units with any high {sum(1 for u in scored if u['high'])}/{len(scored)}")
    reviewed = [u for u in units if u["passes"]]
    if reviewed:
        print(f"  review passes per reviewed unit {sum(u['passes'] for u in reviewed) / len(reviewed):.1f}; "
              f"bad_spec loopbacks {sum(u['loops'] for u in reviewed)}")
    print(f"  synthesis fix(merge) commits per run: {fix_merge}")
    w1 = sum(v["tok_w1h"] for v in grand.values() if v is not grand["main"])
    print(f"  subagent 1h-TTL cache writes: {w1 / 1e6:.1f} Mtok (0 means the agents' cacheTtl did not take effect)")
    return rows


# maxTurns in the shipped agent files; a role at its budget ran out of turns.
TURN_BUDGET = {"worker": 300, "haiku": 300, "synthesis": 400}


def find_session(slug):
    """The newest session log that invoked the skill and wrote this run's run file.

    A later session that only mentions the run file (a clean-up, a re-read)
    has no skill invocation, so it is skipped.
    """
    needles = (f"_runs/{slug}.md", f"_runs\\\\{slug}.md")
    logs = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    for path in sorted(logs, key=os.path.getmtime, reverse=True)[:40]:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if any(n in line for line in lines for n in needles) and any(invokes(line) for line in lines):
            return path
    return None


SKILL_NAMES = ("bmad-batch-build", "bmad-parallel-build")


def invokes(line):
    """True for a typed /bmad-batch-build command or a Skill call that loads it.

    Text that merely quotes the command (an edit, a tool result) doesn't count.
    """
    if not any(n in line for n in SKILL_NAMES):
        return False
    try:
        x = json.loads(line)
    except ValueError:
        return False
    m = x.get("message")
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if x.get("type") == "user":
        texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if b.get("type") == "text"]
        return any(f"<command-name>/{n}</command-name>" in t for t in texts for n in SKILL_NAMES)
    if x.get("type") == "assistant" and isinstance(c, list):
        return any(b.get("type") == "tool_use" and b.get("name") == "Skill"
                   and (b.get("input") or {}).get("skill") in SKILL_NAMES for b in c)
    return False


def store_dir():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def load_rows():
    path = os.path.join(store_dir(), "runs.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def save_row(row):
    """Adds the row, replacing any earlier row of the same session."""
    rows = [r for r in load_rows() if r.get("session") != row["session"]] + [row]
    rows.sort(key=lambda r: r.get("start") or "")
    os.makedirs(store_dir(), exist_ok=True)
    path = os.path.join(store_dir(), "runs.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    return path


def hardware():
    """The machine the run used, so runs from different machines compare fairly."""
    import platform
    ram = None
    try:
        if os.path.exists("/proc/meminfo"):
            ram = int(open("/proc/meminfo").read().split("MemTotal:")[1].split()[0]) // 1024 // 1024
        elif sys.platform == "win32":
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            ram = round(st.ullTotalPhys / 1024 ** 3)
        elif sys.platform == "darwin":
            ram = int(os.popen("sysctl -n hw.memsize").read()) // 1024 ** 3
    except Exception:  # hardware info is a nice-to-have
        pass
    return {"host": platform.node(), "os": platform.system(), "cpus": os.cpu_count(), "ram_gb": ram}


def version_for(start):
    """VERSION holds '<version> <release date>'. A session that started on or
    after the release ran this version; an older one ran something earlier."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    if not os.path.exists(path):
        return None
    parts = open(path).read().split()
    version, released = parts[0], (parts[1] if len(parts) > 1 else "")
    return version if (start or "")[:10] >= released else f"before-{version}"


def build_row(session, slug, project, mode):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        row = report([session])[0]
    row.update({
        "slug": slug, "project": project, "mode": mode,
        "skill_version": version_for(row["start"]),
        "turn_budget_hit": sorted(k for k, v in row["max_turns"].items() if v >= TURN_BUDGET.get(k, 10 ** 9)),
        "hardware": hardware(),
        "session_mtime": os.path.getmtime(session),
        "recorded": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    return row, buf.getvalue()


def unit_minutes(row, slug, root):
    """Each unit's size and worker minutes, from the run's plan file, so
    plan-waves.py can calibrate S/M/L from real runs. A unit is matched by its
    worktree, <slug>-<n>; units that shared the lane worktree <slug> are
    matched in the order they ran. Returns None when they can't be matched
    one to one (no plan file, or a unit was dispatched twice)."""
    path = os.path.join(root or "", ".claude", "worktrees", "_runs", f"{slug}-plan.json")
    if not root or not os.path.exists(path):
        return None
    try:
        plan = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return None
    size = {u["id"]: u.get("size") for u in plan.get("units", [])}
    lane_ids = [i for w in plan.get("waves", []) if w.get("run") == "lane" for i in w["units"]]         or [u["id"] for u in plan.get("units", [])]
    out, shared = [], []
    for s in row.get("worker_spans", []):
        m = re.fullmatch(re.escape(slug) + r"-(\d+)", s.get("wt") or "")
        if m and int(m.group(1)) in size:
            out.append({"id": int(m.group(1)), "size": size[int(m.group(1))], "min": s["min"]})
        elif s.get("wt") == slug:
            shared.append(s)
    shared.sort(key=lambda s: s["start"])
    if shared:
        if len(shared) > len(lane_ids):
            return None
        out += [{"id": i, "size": size.get(i), "min": s["min"]} for i, s in zip(lane_ids, shared)]
    if len({u["id"] for u in out}) != len(out):
        return None
    return out or None


def record(slug, root):
    session = find_session(slug)
    if not session:
        print(f"record: no session log mentions _runs/{slug}.md")
        return 1
    runs = os.path.join(root, ".claude", "worktrees", "_runs")
    run_file = os.path.join(runs, f"{slug}.md")
    mode = system_map = None
    if os.path.exists(run_file):
        run_text = open(run_file, encoding="utf-8").read()
        m = re.search(r"^.*Mode:.*$", run_text, re.M)
        mode = m.group(0).strip() if m else None
        m = re.search(r"\bMAP (none|ok|stale)\b", run_text)
        system_map = m.group(1) if m else None
    row, text = build_row(session, slug, os.path.basename(os.path.normpath(root)), mode)
    # Whether the split had a system map (check-map.py's verdict, from the run file).
    row["system_map"] = system_map
    row["unit_minutes"] = unit_minutes(row, slug, root)
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(runs, f"{slug}-cost.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    path = save_row(row)
    # The headline for the orchestrator's final report.
    print(f"recorded {slug}: ${row['usd']:.1f}, wall {row['min_wall']}m (user wait {row['min_user_wait']}m, "
          f"rest {row['min_rest']}m), {row['workers']} workers, reviewed {row['units_reviewed']}, "
          f"first-review high+medium {row['first_review_high_medium']}, fix(merge) {row['fix_merge_commits']}, "
          f"1h-TTL writes {row['mtok_w1h_subagents']} Mtok, turn budget hit: {', '.join(row['turn_budget_hit']) or 'none'}")
    print(f"report: {os.path.join(runs, slug + '-cost.txt')}; data: {path}")
    return 0


def session_info(path):
    """(invoked, slug, project, cwd) for one session log."""
    invoked, slug, cwd = False, None, None
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not invoked and invokes(line):
                invoked = True
            if cwd is None and '"cwd"' in line:
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    pass
            if slug is None and "_runs" in line:
                m = re.search(r"_runs[/\\\\]+([\w.-]+?)\.md", line)
                if m and not m.group(1).endswith(("-cost", "-synthesis", "-contracts", "-spec")):
                    slug = m.group(1)
    project = os.path.basename(os.path.normpath(cwd)) if cwd else os.path.basename(os.path.dirname(path))
    return invoked, slug, project, cwd


def archive(path, project):
    """Gzipped copy of a session log and its subagent logs under
    <store>/sessions/, because Claude Code deletes transcripts after
    cleanupPeriodDays (30 by default) and old runs must stay re-measurable.
    Copies only files that are new or changed since the last archive."""
    import gzip
    import shutil
    session = os.path.basename(path)[:-6]
    dest = os.path.join(store_dir(), "sessions", project, session)
    sources = [(path, os.path.join(dest, session + ".jsonl.gz"))]
    sub = path[:-6]
    for f in glob.glob(os.path.join(sub, "subagents", "*")):
        sources.append((f, os.path.join(dest, "subagents", os.path.basename(f) + ".gz")))
    for src, dst in sources:
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fi, gzip.open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)


def collect(quiet=False, refresh=False):
    """Records every session on this machine that ran the skill and has no
    current row: missed --record calls, crashed runs and older runs alike, and
    archives their logs. --refresh rebuilds every row (after a metric change).

    A session written to in the last 15 minutes may still be running, so it
    waits for the next collect. A session that grew after its row was written
    (a resumed run) is recorded again.
    """
    rows = {r["session"]: r for r in load_rows()}
    reports = os.path.join(store_dir(), "reports")
    os.makedirs(reports, exist_ok=True)
    now = dt.datetime.now().timestamp()
    added = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        session = os.path.basename(path)[:-6]
        mtime = os.path.getmtime(path)
        if now - mtime < 900:
            continue
        old = rows.get(session)
        if old and old.get("session_mtime", 0) >= mtime - 1 and not refresh:
            continue
        invoked, slug, project, cwd = session_info(path)
        if not invoked:
            continue
        archive(path, project)
        try:
            row, text = build_row(path, (old or {}).get("slug") or slug or session[:8], project, (old or {}).get("mode"))
            row["unit_minutes"] = unit_minutes(row, row["slug"], cwd)
            row["system_map"] = (old or {}).get("system_map")
        except Exception as exc:  # one unreadable log must not stop the rest
            print(f"collect: skipped {session[:8]}: {exc}")
            continue
        with open(os.path.join(reports, f"{session}.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        save_row(row)
        added.append(f"{row['start'][:10]} {project} {row['slug']}")
    if not quiet or added:
        print(f"collect: {len(added)} session(s) recorded" + "".join(f"\n  {a}" for a in added))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--record"]:
        root = args[args.index("--root") + 1] if "--root" in args else os.getcwd()
        code = record(args[1], root)
        collect(quiet=True)
        sys.exit(code)
    if args[:1] == ["--collect"]:
        sys.exit(collect(refresh="--refresh" in args))
    report(args)
