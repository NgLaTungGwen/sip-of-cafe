"""Token and cost profile of bmad-batch-build runs (formerly bmad-parallel-build).

Usage: python cost_report.py <session.jsonl> [<session.jsonl> ...]

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


def profile(lines, lab, acc, edits, waits):
    """Adds token and cost totals to acc[lab], and tool waits to waits.

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
            continue
        if x.get("type") != "assistant" or not isinstance(m, dict) or m.get("model") == "<synthetic>":
            continue
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use" and x.get("timestamp"):
                pending[b["id"]] = (x["timestamp"], wait_kind(b))
        if "usage" in m and m["id"] not in calls:
            calls[m["id"]] = [x["timestamp"], m["model"], m["usage"], None]
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use":
                if m.get("id") in calls:
                    calls[m["id"]][3] = wait_kind(b)
                if b["name"] in ("Edit", "Write") and lab == "worker":
                    p = b["input"].get("file_path", "").replace("\\", "/")
                    edits["spec/output" if re.search(r"_bmad|\.md$", p) else "source"] += 1
    prev = None
    c = acc[lab]
    for t, model, u, tool in sorted(calls.values()):
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
        c["usd_out"] += out * pout / 1e6 + u.get("input_tokens", 0) * pin / 1e6
        c["turns"] += 1
        # The cache expired if the gap beat the TTL the write used: 5 min, or
        # 1 h for agents with cacheTtl 1h (they re-write as 1h after a gap).
        gap = (ts(t) - ts(prev[0])).total_seconds() if prev else 0
        if gap > (3600 if w1 else 300) and cw > 2000:
            c["usd_idle"] += cw_usd
            acc["_idle"][(lab, prev[1])] += cw_usd
        prev = (t, tool)
    return (stamps[0], stamps[-1]) if stamps else None


def run(session_path):
    """Returns (acc, edits, start, waits, spans); spans maps role -> list of seconds."""
    base = session_path[:-6]
    acc = collections.defaultdict(collections.Counter)
    edits = collections.Counter()
    waits = collections.defaultdict(lambda: [0, 0.0])
    spans = collections.defaultdict(list)
    quality = {"workers": [], "fix_merge": 0}
    lines = load(session_path)
    s, e = main_span(lines)
    main_ts = profile(lines[s:e], "main", acc, edits, waits)
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
        span = profile(sub, lab, acc, edits, waits)
        if span:
            spans[lab].append((ts(span[1]) - ts(span[0])).total_seconds())
            ends.setdefault(lab, []).append(span[1])
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
    return acc, edits, start, waits, spans, quality


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


if __name__ == "__main__":
    grand = collections.defaultdict(collections.Counter)
    idle = collections.Counter()
    edits = collections.Counter()
    all_waits = collections.defaultdict(lambda: [0, 0.0])
    timing = []
    units = []
    fix_merge = []
    print(f"{'session':10} {'date':10} {'workers':>7} {'$total':>7} {'$main':>6} {'$work':>6} {'$rev':>5} {'$haiku':>6} {'$synth':>6} {'$idle':>6} {'Mtok in':>8} {'Ktok out':>8}")
    for p in sys.argv[1:]:
        acc, ed, start, waits, spans, quality = run(p)
        units += quality["workers"]
        fix_merge.append(quality["fix_merge"])
        edits.update(ed)
        idle.update(acc.pop("_idle", {}))
        for k, (n, sec) in waits.items():
            all_waits[k][0] += n
            all_waits[k][1] += sec
        usd = {k: v["usd_write"] + v["usd_read"] + v["usd_out"] for k, v in acc.items()}
        for k, v in acc.items():
            grand[k].update(v)
        tok_in = sum(v["tok_in"] for v in acc.values())
        tok_out = sum(v["tok_out"] for v in acc.values())
        print(f"{os.path.basename(p)[:8]:10} {start[:10]:10} {acc['worker']['agents']:7} {sum(usd.values()):7.1f} "
              f"{usd.get('main', 0):6.1f} {usd.get('worker', 0):6.1f} {usd.get('reviewer', 0):5.1f} "
              f"{usd.get('haiku', 0):6.2f} {usd.get('synthesis', 0):6.1f} {sum(v['usd_idle'] for v in acc.values()):6.1f} "
              f"{tok_in / 1e6:8.1f} {tok_out / 1e3:8.0f}")
        ask = acc["main"]["user_wait"]
        timing.append((os.path.basename(p)[:8], spans["main"][0], ask, max(spans["worker"], default=0),
                       sum(spans["synthesis"]), tok_in + tok_out, sum(usd.values())))
    total = sum(v["usd_write"] + v["usd_read"] + v["usd_out"] for v in grand.values())
    n = max(len(sys.argv) - 1, 1)
    tok = sum(v["tok_in"] + v["tok_out"] for v in grand.values())
    print(f"\nTOTAL ${total:.0f}, {tok / 1e6:.0f} Mtok; per run ${total / n:.1f}, {tok / n / 1e6:.1f} Mtok\n")

    # Wall-clock runs from the skill invocation to the synthesis reply, so it
    # includes the user's time at the split approval (the "approval" column).
    print("Timing per run (m = minutes)")
    print(f"{'session':10} {'wall':>6} {'user wait':>9} {'wall-user':>9} {'slowest wkr':>11} {'synthesis':>9} {'orchestr.':>9} {'Mtok/run':>8} {'Mtok/$':>7}")
    for name, wall, ask, worker, synth, tk, usd in timing:
        rest = max(wall - ask - worker - synth, 0)
        print(f"{name:10} {mins(wall):>6} {mins(ask):>9} {mins(wall - ask):>9} {mins(worker):>11} {mins(synth):>9} "
              f"{mins(rest):>9} {tk / 1e6:8.1f} {tk / 1e6 / usd if usd else 0:7.1f}")
    if timing:
        import statistics
        for label, f in (("average", statistics.mean), ("median", statistics.median)):
            v = [f([t[i] for t in timing]) for i in range(1, 5)]
            nu = f([t[1] - t[2] for t in timing])
            print(f"{label:10} {mins(v[0]):>6} {mins(v[1]):>9} {mins(nu):>9} {mins(v[2]):>11} {mins(v[3]):>9}")

    print("\nTool wait time (tool_use -> tool_result; parallel calls overlap, so sums can exceed wall-clock)")
    print(f"{'role':10} {'kind':22} {'calls':>5} {'total':>7} {'avg':>7} {'per run':>8}")
    for (lab, kind), (cnt, sec) in sorted(all_waits.items(), key=lambda kv: -kv[1][1]):
        if sec < 600:
            continue
        print(f"{lab:10} {kind:22} {cnt:5} {sec / 3600:6.1f}h {sec / cnt / 60:6.1f}m {sec / n / 60:7.0f}m")
    print()
    print(f"{'role':10} {'agents':>6} {'turns':>6} {'Mtok in':>8} {'Mtok out':>8} {'$write':>7} {'$read':>6} {'$out':>6} {'$total':>7} {'$idle-rewrite':>13}")
    for k, v in sorted(grand.items(), key=lambda kv: -(kv[1]["usd_write"] + kv[1]["usd_read"] + kv[1]["usd_out"])):
        t = v["usd_write"] + v["usd_read"] + v["usd_out"]
        print(f"{k:10} {v['agents']:6} {v['turns']:6} {v['tok_in']/1e6:8.1f} {v['tok_out']/1e6:8.2f} {v['usd_write']:7.1f} "
              f"{v['usd_read']:6.1f} {v['usd_out']:6.1f} {t:7.1f} {v['usd_idle']:13.1f}")
    print("\nIdle cache rewrites (gap beyond the 5 min or 1 h TTL), by what the agent was waiting on:")
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
