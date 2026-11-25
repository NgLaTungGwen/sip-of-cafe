"""Token and cost profile of bmad-batch-build runs (formerly bmad-parallel-build).

Usage: python cost-report.py <session.jsonl> [<session.jsonl> ...]
       python cost-report.py --record <slug> [--root <main checkout>]
       python cost-report.py --collect

--record is what the orchestrator runs at the end of every run. It finds the
session that ran <slug> (the newest session log under ~/.claude/projects that
mentions the run file <slug>.md), writes the report to <slug>-cost.txt in
the run folder (run_dir.py: ~/.claude/bmad-batch-runs/runs/<project>/<slug>/),
and appends one JSON row to
~/.claude/bmad-batch-runs/runs.jsonl (BMAD_RUNS_DIR overrides the folder), one
row per run across every project, replacing an earlier row of the same run.
Rows also carry the machine's CPUs and RAM. scripts/runs-summary.py reads that file.

--collect records every session on this machine that ran the skill and has no
current row: runs whose --record call was missed, crashed runs, and runs from
before recording existed. Reports go to ~/.claude/bmad-batch-runs/reports/.
--record runs it too, so each finished run sweeps up any that were missed.

Both also build the run journal (journal.py): what failed and what the agents
reported, written to <store>/journals/<session>.md (and, for --record, to
<slug>-journal.md in the run folder) and kept on the row under "journal".

A row is one run, keyed by (session, slug): one session can hold several
runs (a second batch asked for after the first), and each gets its own row.
A run's part of the main session runs from its own start (its skill
invocation, or the creation of its run file when it had none) to its own
last synthesis result (else its --record call, else the next run's start),
and only the subagents launched for it count (split_session). --collect
records every run of a session. Without a slug (the plain report), the main
session counts from the first invocation to the last synthesis result.
"""
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import journal  # noqa: E402  (the run journal, built alongside every row)
import run_dir  # noqa: E402  (where a run's own files live)

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
    marks = tuple(m for name in SKILL_NAMES
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


def blocks_of(x):
    m = x.get("message")
    if isinstance(m, dict) and isinstance(m.get("content"), list):
        return [b for b in m["content"] if isinstance(b, dict)]
    return []


def run_marks(lines):
    """{slug: index} of the first line where the orchestrator made or wrote
    each run's own file: `run_dir.py <slug>`, or a Write or Edit of
    <slug>.md. A run file that is only read or listed (an old run looked up)
    is not a run of this session."""
    marks = {}
    for i, x in enumerate(lines):
        if x.get("type") != "assistant":
            continue
        for b in blocks_of(x):
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            slug = None
            if b.get("name") in ("Write", "Edit"):
                slug = run_dir.slug_in(json.dumps(inp.get("file_path") or ""))
            elif b.get("name") in ("Bash", "PowerShell"):
                m = re.search(r"run_dir\.py[\"']?\s+([\w.-]+)", inp.get("command") or "")
                slug = m.group(1) if m and not m.group(1).startswith("-") else None
            if slug and slug not in marks:
                marks[slug] = i
    return marks


def slug_pattern(slug):
    # ti-port matches ti-port, ti-port-3 and ti-port/..., not ti-port-x.
    return re.compile(r"(?<![\w.-])" + re.escape(slug) + r"(?![\w.]|-[A-Za-z])")


def runs_in(lines):
    """The runs one session holds, in the order they started:
    [{"slug", "start", "next"}], where start is the line of the run's own
    skill invocation, or of its run file's creation when the run had none (a
    second run asked for in plain words), and next is the start of the run
    after it (or the end of the log). Empty when no run file was made."""
    marks = run_marks(lines)
    inv = [i for i, x in enumerate(lines) if x.get("type") in ("user", "assistant") and invokes(json.dumps(x))]
    runs, prev = [], -1
    for slug in sorted(marks, key=marks.get):
        own = [i for i in inv if prev < i <= marks[slug]]
        runs.append({"slug": slug, "start": own[-1] if own else marks[slug]})
        prev = marks[slug]
    for k, r in enumerate(runs):
        r["next"] = runs[k + 1]["start"] if k + 1 < len(runs) else len(lines)
    return runs


def owner_run(runs, at, text):
    """The run an agent belongs to: the run its dispatch text names (a run
    file, a worktree <slug>-<n>), else the run that was going when it was
    launched (the latest one started at `at`, a line index)."""
    hits = [k for k, r in enumerate(runs) if slug_pattern(r["slug"]).search(text or "")]
    cands = hits or list(range(len(runs)))
    started = [k for k in cands if runs[k]["start"] <= at]
    return max(started, key=lambda k: runs[k]["start"]) if started else cands[0]


def split_session(lines, subs):
    """Splits one session into its runs. Returns (runs, owner): each run gets
    its own "end" (its last synthesis result, else its `--record` call, else
    the start of the next run), and owner maps each subagent log to its run
    index. A subagent belongs to the run of the Agent call that launched it
    (by that call's dispatch text, then its line); one a worker launched goes
    with that worker; one with no known launcher goes by its dispatch text
    and launch time."""
    runs = runs_in(lines)
    if not runs:
        return runs, {}
    launched = {}  # Agent tool_use id -> ("main", line, text) or (log, None, text)
    synth = collections.defaultdict(set)
    for i, x in enumerate(lines):
        for b in blocks_of(x):
            if b.get("type") == "tool_use" and b.get("name") == "Agent":
                text = json.dumps(b.get("input") or {})
                k = owner_run(runs, i, text)
                launched[b["id"]] = ("main", k)
                if "ynthesis" in text[:300]:
                    synth[k].add(b["id"])
    for f, (meta, sub) in subs.items():
        for b in tool_uses(sub):
            if b.get("name") == "Agent":
                launched.setdefault(b["id"], (f, None))
    stamps = []
    for r in runs:
        stamps.append(next((x["timestamp"] for x in lines[r["start"]:] if x.get("timestamp")), "~"))
    owner = {}

    def resolve(f, depth=0):
        if f in owner:
            return owner[f]
        meta, sub = subs[f]
        via = launched.get(meta.get("toolUseId"))
        if via and via[0] == "main":
            k = via[1]
        elif via and via[0] in subs and via[0] != f and depth < 8:
            k = resolve(via[0], depth + 1)
        else:
            first = next((x["timestamp"] for x in sub if x.get("timestamp")), "")
            at = max([i for i, t in enumerate(stamps) if t <= first] or [0])
            k = owner_run(runs, runs[at]["start"], json.dumps(sub[0]) if sub else "")
        owner[f] = k
        return k

    for f in subs:
        resolve(f)
    for k, r in enumerate(runs):
        end = None
        for i in range(r["start"], len(lines)):
            for b in blocks_of(lines[i]):
                if b.get("type") == "tool_result" and b.get("tool_use_id") in synth[k]:
                    end = i + 1
        if end is None:
            pat = slug_pattern(r["slug"])
            for i in range(r["start"], len(lines)):
                cmds = [(b.get("input") or {}).get("command") or "" for b in blocks_of(lines[i])
                        if b.get("type") == "tool_use" and b.get("name") in ("Bash", "PowerShell")]
                m = next((re.search(r"--record\s+(\S+)", c) for c in cmds if "--record" in c), None)
                # A literal slug must be this run's; a "$s" in a loop records every open run.
                if m and (pat.search(m.group(1)) or "$" in m.group(1)):
                    end = i + 1
                    break
        r["end"] = end or r["next"]
    return runs, owner


def role(meta, first_line):
    agent = meta.get("agentType")
    if agent == "haiku-implementer":
        return "haiku"
    if agent == "Explore":
        return "explore"
    if agent == "bmad-batch-enhancer":
        return "enhancer"
    if agent == "bmad-batch-planner" or "bmad-batch-planner.md and follow it" in first_line:
        return "planner"
    if agent in ("bmad-batch-synthesis", "bmad-pb-synthesis") or "ynthesis" in meta.get("description", ""):
        return "synthesis"
    if agent in ("bmad-batch-worker", "bmad-pb-worker") or "Build Auto worker" in first_line:
        return "worker"
    if meta.get("spawnDepth", 1) <= 1:
        return "other"
    # A worker's coder: the Haiku one launched as general-purpose (fallback
    # agents), or a general-purpose one (bmad-build-auto's handoff).
    if "haiku-implementer.md and follow it" in first_line:
        return "haiku"
    return "implementer" if "fully and implement it" in first_line else "reviewer"


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


def profile(lines, lab, acc, edits, waits, spans=None, user_events=None, base=0):
    """Adds token and cost totals to acc[lab], and tool waits to waits.

    spans, when given, collects each tool call's (start, end) per (lab, kind),
    so parallel calls can be counted once (see union()).
    user_events, when given, gets one (line, timestamp, "ask", seconds,
    questions) per wait on the user that this adds to waits: an
    AskUserQuestion answer or a permission prompt. line is the index in the
    session log (base + the index in lines), so the intake gate can split
    them (intake_facts()).
    Returns (first, last) timestamps of the agent, or None.
    """
    calls = {}
    pending = {}
    asked = {}  # AskUserQuestion tool_use id -> (line, timestamp, questions)
    stamps = [x["timestamp"] for x in lines if x.get("timestamp")]
    for i, x in enumerate(lines):
        m = x.get("message")
        if x.get("type") == "user" and isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in pending and x.get("timestamp"):
                    t0, kind = pending.pop(b["tool_use_id"])
                    sec = (ts(x["timestamp"]) - ts(t0)).total_seconds()
                    waits[(lab, kind)][0] += 1
                    waits[(lab, kind)][1] += sec
                    if user_events is not None and kind == "AskUserQuestion":
                        n = asked.pop(b["tool_use_id"], (0, 0, 0))[2]
                        user_events.append((base + i, x["timestamp"], "ask", sec, n))
                    # The user answered a permission prompt for this call: the
                    # whole call counts as waiting on them (an upper bound).
                    by = str((x.get("permissionDecision") or {}).get("source") or "")
                    if by.startswith("user") and kind != "AskUserQuestion":
                        waits[(lab, "user permission")][0] += 1
                        waits[(lab, "user permission")][1] += sec
                        if user_events is not None:
                            user_events.append((base + i, x["timestamp"], "ask", sec, 0))
                    if spans is not None:
                        spans[(lab, kind)].append((ts(t0), ts(x["timestamp"])))
            continue
        if x.get("type") != "assistant" or not isinstance(m, dict) or m.get("model") == "<synthetic>":
            continue
        for b in m.get("content", []) if isinstance(m.get("content"), list) else []:
            if b.get("type") == "tool_use" and x.get("timestamp"):
                pending[b["id"]] = (x["timestamp"], wait_kind(b))
                if b.get("name") == "AskUserQuestion":
                    asked[b["id"]] = (base + i, x["timestamp"],
                                      len((b.get("input") or {}).get("questions") or [None]))
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
    if user_events is not None:
        # A question never answered (the log ends first) still counts, at its call.
        for line, t0, n in asked.values():
            user_events.append((line, t0, "ask", 0.0, n))
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


# (session path, slug) -> ((start, end), subagent logs) of each run run() split out.
RUN_PARTS = {}
# (session path, slug) -> the main session's waits on the user, with their
# times and log lines, and the line of the intake freeze (see run()).
USER_EVENTS = {}


def load_subagents(base):
    subs = {}
    for f in sorted(glob.glob(base + "/subagents/*.jsonl")):
        mp = f[:-6] + ".meta.json"
        subs[f] = (json.load(open(mp)) if os.path.exists(mp) else {}, load(f))
    return subs


def run(session_path, slug=None):
    """Returns (acc, edits, start, waits, spans, quality, ivals).

    With slug, only that run of the session counts: its own span of the main
    log and its own subagents (split_session). Without one, or when the
    session has no run file of that slug, the whole session from the first
    invocation to the last synthesis result.
    spans maps role -> list of seconds; ivals maps (role, kind) -> tool-call intervals.
    """
    base = session_path[:-6]
    acc = collections.defaultdict(collections.Counter)
    edits = collections.Counter()
    waits = collections.defaultdict(lambda: [0, 0.0])
    spans = collections.defaultdict(list)
    quality = {"workers": [], "fix_merge": 0, "max_turns": collections.Counter(), "worker_spans": [],
               "busy_iv": []}
    ivals = collections.defaultdict(list)
    lines = load(session_path)
    subs = load_subagents(base)
    runs, owner = split_session(lines, subs)
    quality["session_runs"] = [r["slug"] for r in runs]
    k = next((i for i, r in enumerate(runs) if r["slug"] == slug), None) if slug else None
    if k is None:
        s, e = main_span(lines)
    else:
        s, e = runs[k]["start"], runs[k]["end"]
        subs = {f: v for f, v in subs.items() if owner.get(f) == k}
        # The journal of this run reads the same part of the session.
        RUN_PARTS[(session_path, slug)] = ((s, e), set(subs))
    user_events = []
    main_ts = profile(lines[s:e], "main", acc, edits, waits, ivals, user_events, base=s)
    ends = {}
    acc["main"]["agents"] += 1
    for f, (meta, sub) in subs.items():
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
            if lab in ("worker", "synthesis"):
                quality["busy_iv"].append((ts(span[0]), ts(span[1])))
            if lab == "worker":
                # The worktree the worker ran in names its unit (<slug>-<n>), or
                # the shared lane worktree (<slug>); see unit_minutes().
                m = re.search(r"Project root: (\S+)", json.dumps(sub[0]) if sub else "")
                name = re.split(r"[\\/]+", m.group(1).rstrip("\\/"))[-1] if m else None
                u = re.search(r"Unit: (\d+)", json.dumps(sub[0]) if sub else "")
                quality["worker_spans"].append({"wt": name, "unit": int(u.group(1)) if u else None,
                                                "start": span[0],
                                                "min": round((ts(span[1]) - ts(span[0])).total_seconds() / 60)})
        acc[lab]["agents"] += 1
    # Synthesis usually runs in the background, so its Agent result returns at
    # launch; the run ends at whichever is later, main or synthesis.
    # Time the run spent waiting on the user, in two parts. ask: answering an
    # AskUserQuestion, or a permission prompt in the main session (a tool call
    # the user approved or refused by hand). typed: the gap before every other
    # typed user message (an approval asked in plain text, a nudge).
    ask = waits.get(("main", "AskUserQuestion"), [0, 0.0])[1] + waits.get(("main", "user permission"), [0, 0.0])[1]
    typed_wait = 0.0
    prev = None
    freeze_line = None
    for i, x in enumerate(lines[s:e], s):
        for b in blocks_of(x):
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                acc["main"]["questions"] += len((b.get("input") or {}).get("questions") or [None])
            if b.get("type") == "tool_use" and b.get("name") in ("Bash", "PowerShell") and re.search(
                    r"intake\.py[\"']?\s+freeze\b", (b.get("input") or {}).get("command") or ""):
                # Where the intake was approved, by the order of the log (intake_facts()).
                freeze_line = i if freeze_line is None else freeze_line
        t = x.get("timestamp")
        if not t:
            continue
        c = (x.get("message") or {}).get("content") if isinstance(x.get("message"), dict) else x.get("content")
        typed = x.get("type") in ("user", "queue-operation") and not (
            isinstance(c, list) and any(b.get("type") == "tool_result" for b in c if isinstance(b, dict))
        ) and "<task-notification>" not in json.dumps(c or "") and c
        if typed and prev and x.get("type") == "user" and not x.get("isMeta"):
            # Clock skew between lines can make a gap negative; it is no wait.
            gap = max((ts(t) - ts(prev)).total_seconds(), 0)
            typed_wait += gap
            user_events.append((i, t, "typed", gap, 0))
        if x.get("type") != "queue-operation":
            prev = t
    # What intake_facts() splits at the intake approval, once the run file is known.
    USER_EVENTS[(session_path, slug)] = {"events": user_events, "freeze_line": freeze_line,
                                         "first": main_ts[0] if main_ts else None,
                                         "last": main_ts[1] if main_ts else None}
    acc["main"]["user_ask"] += ask
    acc["main"]["user_typed"] += typed_wait
    acc["main"]["user_wait"] += ask + typed_wait
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
         "high": None, "medium": None, "passes": 0, "loops": 0, "layers": {}}
    if spec and os.path.exists(spec[-1]):
        t = open(spec[-1], encoding="utf-8").read()
        log = t.split("## Review Triage Log", 1)[1].split("\n## ", 1)[0] if "## Review Triage Log" in t else ""
        q["layers"] = layer_findings(log)
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


# The review layer tag every triage row carries (references/review.md), and
# the triage actions that mean the finding was not real.
LAYERS = ("blind-hunter", "edge-case", "verification-gap", "intent-alignment")
REJECTED = {"reject", "rejected", "dismiss", "dismissed", "false", "false-positive"}


def layer_findings(log):
    """{layer: {"total", "rejected"}} over the triage rows of one spec's
    Review Triage Log that carry a layer tag (`[intent-alignment]`, with or
    without backticks). A row is rejected when its action (`reject`,
    `[reject]`, `dismissed`, `false`, ...) says so; a deferred one is not.
    Rows with the older (intent) / (blind) form carry no tag and are not
    counted, so runs before the tags have no rates."""
    out = {}
    for line in log.splitlines():
        if not re.match(r"\s*-\s", line):
            continue
        tokens = [t.lower() for t in re.findall(r"`\[?([\w-]+)\]?`|\[([\w-]+)\]", line) for t in t if t]
        layer = next((t for t in tokens if t in LAYERS), None)
        if not layer:
            continue
        c = out.setdefault(layer, {"total": 0, "rejected": 0})
        c["total"] += 1
        c["rejected"] += any(t in REJECTED for t in tokens)
    return out


def mins(sec):
    return f"{sec / 60:.0f}m" if sec else "-"


def report(paths, slug=None):
    """Prints the report for these sessions; returns one data row per session.
    With slug, each row covers only that run of its session (see run())."""
    rows = []
    grand = collections.defaultdict(collections.Counter)
    idle = collections.Counter()
    edits = collections.Counter()
    all_waits = collections.defaultdict(lambda: [0, 0.0])
    all_union = collections.Counter()
    timing = []
    units = []
    fix_merge = []
    print(f"{'session':10} {'date':10} {'workers':>7} {'$total':>7} {'$main':>6} {'$work':>6} {'$impl':>5} {'$rev':>5} {'$haiku':>6} {'$synth':>6} {'$idle≤':>6} {'Mtok in':>8} {'Ktok out':>8}")
    for p in paths:
        acc, ed, start, waits, spans, quality, ivals = run(p, slug)
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
              f"{usd.get('main', 0):6.1f} {usd.get('worker', 0):6.1f} {usd.get('implementer', 0):5.1f} {usd.get('reviewer', 0):5.1f} "
              f"{usd.get('haiku', 0):6.2f} {usd.get('synthesis', 0):6.1f} {sum(v['usd_idle'] for v in acc.values()):6.1f} "
              f"{tok_in / 1e6:8.1f} {tok_out / 1e3:8.0f}")
        ask = acc["main"]["user_wait"]
        wall, slowest, synth = spans["main"][0], max(spans["worker"], default=0), sum(spans["synthesis"])
        # Time no worker or synthesis was running: the split, the gates, waits
        # between waves or lane units, and stalls. Correct for every mode.
        rest_s = max(wall - ask - union(quality["busy_iv"]), 0)
        q = quality["workers"]
        scored_q = [u for u in q if u["high"] is not None or u["medium"] is not None]
        layers = {}
        for u in q:
            for lay, c in (u.get("layers") or {}).items():
                tot = layers.setdefault(lay, {"total": 0, "rejected": 0})
                tot["total"] += c["total"]
                tot["rejected"] += c["rejected"]
        intent = layers.get("intent-alignment")
        rows.append({
            "session": os.path.basename(p)[:-6], "start": start,
            "usd": round(sum(usd.values()), 2),
            "usd_by_role": {k: round(v, 2) for k, v in usd.items()},
            "usd_idle_max": round(sum(v["usd_idle"] for v in acc.values()), 2),
            "mtok_in": round(tok_in / 1e6, 2), "ktok_out": round(tok_out / 1e3, 1),
            "mtok_w1h_subagents": round(sum(v["tok_w1h"] for k, v in acc.items() if k != "main") / 1e6, 2),
            # min_user_wait = min_ask + min_typed (rounded apart), kept for older readers.
            "min_wall": round(wall / 60), "min_user_wait": round(ask / 60),
            "min_ask": round(acc["main"]["user_ask"] / 60), "min_typed": round(acc["main"]["user_typed"] / 60),
            "questions": acc["main"]["questions"],
            "session_runs": quality.get("session_runs") or [],
            "min_slowest_worker": round(slowest / 60), "min_synthesis": round(synth / 60),
            "min_rest": round(rest_s / 60),
            "workers": acc["worker"]["agents"],
            "units_reviewed": sum(u["reviewed"] for u in q), "units_haiku": sum(u["haiku"] for u in q),
            "first_review_high_medium": round(sum((u["high"] or 0) + (u["medium"] or 0) for u in scored_q) / len(scored_q), 1) if scored_q else None,
            "units_with_high": sum(1 for u in scored_q if u["high"]),
            "fix_merge_commits": quality["fix_merge"],
            # Triage rows per review layer tag over the run's unit specs, and
            # the share of the intent auditor's findings that were rejected.
            "layer_findings": layers or None,
            "intent_auditor_reject_rate": round(intent["rejected"] / intent["total"], 3) if intent else None,
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
                       sum(spans["synthesis"]), tok_in + tok_out, sum(usd.values()), rest_s))
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
    for name, wall, ask, worker, synth, tk, usd, rest in timing:
        print(f"{name:10} {mins(wall):>6} {mins(ask):>9} {mins(wall - ask):>9} {mins(worker):>11} {mins(synth):>9} "
              f"{mins(rest):>9}{' !' if rest > 3600 else '  '}{tk / 1e6:6.1f} {tk / 1e6 / usd if usd else 0:7.1f}")
    if timing:
        for label, f in (("average", statistics.mean), ("median", statistics.median)):
            v = [f([t[i] for t in timing]) for i in range(1, 5)]
            nu = f([t[1] - t[2] for t in timing])
            rest = f([t[7] for t in timing])
            print(f"{label:10} {mins(v[0]):>6} {mins(v[1]):>9} {mins(nu):>9} {mins(v[2]):>11} {mins(v[3]):>9} {mins(rest):>9}")
    print("rest = wall - user wait - time any worker or synthesis ran: the split and Explore, preflight, tickets, gates,\n"
          "waits between units and waves, and any stall (a permission prompt nobody answered, a session left idle).\n"
          "'!' marks rest over an hour; look at the main session's gaps for that run.")

    print("\nTool wait time (tool_use -> tool_result). 'total' sums every call; 'union' counts time\n"
          "covered by parallel calls once, so it is what the role actually spent waiting on that kind.")
    print(f"{'role':11} {'kind':22} {'calls':>5} {'total':>7} {'union':>7} {'avg':>7} {'union/run':>9}")
    for (lab, kind), (cnt, sec) in sorted(all_waits.items(), key=lambda kv: -all_union[kv[0]]):
        if sec < 600:
            continue
        u = all_union[(lab, kind)]
        print(f"{lab:11} {kind:22} {cnt:5} {sec / 3600:6.1f}h {u / 3600:6.1f}h {sec / cnt / 60:6.1f}m {u / n / 60:8.0f}m")
    print()
    print("$in is uncached input; $idle≤ is an upper bound (a whole write after a long gap is counted).")
    print(f"{'role':11} {'agents':>6} {'turns':>6} {'Mtok in':>8} {'Mtok out':>8} {'$write':>7} {'$read':>6} {'$in':>5} {'$out':>6} {'$total':>7} {'$idle≤':>7}")
    for k, v in sorted(grand.items(), key=lambda kv: -cost(kv[1])):
        print(f"{k:11} {v['agents']:6} {v['turns']:6} {v['tok_in']/1e6:8.1f} {v['tok_out']/1e6:8.2f} {v['usd_write']:7.1f} "
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
    logs = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    for path in sorted(logs, key=os.path.getmtime, reverse=True)[:40]:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
        if any(run_dir.mentions(line, slug) for line in lines) and any(invokes(line) for line in lines):
            return path
    return None


SKILL_NAMES = ("bmad-batch-build", "bmad-batch-build-split", "bmad-parallel-build")


def is_run_skill(name):
    """The batch-build skill itself, also as a plugin skill (bmad-batch-build:bmad-batch-build),
    but not a sibling such as bmad-batch-build:bmad-batch-dashboard."""
    return str(name or "").split(":")[-1] in SKILL_NAMES


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
        return any(is_run_skill(n) for t in texts for n in re.findall(r"<command-name>/([\w:.-]+)</command-name>", t))
    if x.get("type") == "assistant" and isinstance(c, list):
        return any(b.get("type") == "tool_use" and b.get("name") == "Skill"
                   and is_run_skill((b.get("input") or {}).get("skill")) for b in c)
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


def row_key(r):
    """A row is one run: (session, slug). One session can hold several runs.
    A row written before rows were keyed this way has a slug too; one without
    any slug keys as (session, None) and is kept as it is."""
    return r.get("session"), r.get("slug")


def save_row(row):
    """Adds the row, replacing any earlier row of the same run (session and slug)."""
    rows = [r for r in load_rows() if row_key(r) != row_key(row)] + [row]
    rows.sort(key=lambda r: r.get("start") or "")
    os.makedirs(store_dir(), exist_ok=True)
    path = os.path.join(store_dir(), "runs.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
    return path


def drop_row(row):
    rows = [r for r in load_rows() if row_key(r) != row_key(row)]
    with open(os.path.join(store_dir(), "runs.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


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
        row = report([session], slug)[0]
    row.update({
        "slug": slug, "project": project, "mode": mode,
        "skill_version": version_for(row["start"]),
        "turn_budget_hit": sorted(k for k, v in row["max_turns"].items() if v >= TURN_BUDGET.get(k, 10 ** 9)),
        "hardware": hardware(),
        "session_mtime": os.path.getmtime(session),
        "recorded": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    })
    return row, buf.getvalue()


def add_journal(row, session, slug, root, run_copy=False):
    """Builds the run journal (journal.py): keeps its items on the row, writes
    <store>/journals/<session>.md, and with run_copy also <slug>-journal.md
    in the run folder. Returns its headline."""
    # A session with several runs: only this run's part of it (see run()).
    span, keep = RUN_PARTS.get((session, slug)) or (None, None)
    multi = len(row.get("session_runs") or []) > 1
    j = journal.build(session, slug, root, store_dir(), span=span if multi else None,
                      keep=keep if multi else None)
    row["journal"] = {"counts": j["counts"], "items": j["items"][:60], "outcome": j["outcome"]}
    text = journal.markdown(j, row)
    # A session that held several runs gets one journal per run.
    name = row["session"] if len(row.get("session_runs") or []) < 2 else f"{row['session']}-{slug}"
    paths = [os.path.join(store_dir(), "journals", f"{name}.md")]
    if run_copy and root:
        paths.append(os.path.join(run_dir.run_dir(root, slug), f"{slug}-journal.md"))
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return journal.headline(j), paths[-1]


def find_run_file(root, slug, suffix):
    """The run's file <slug><suffix>: in the run folder of root's project (or
    the legacy _runs folder), else the one file of that name in any project's
    run folder. --collect passes the session's folder as root, which is not
    always the checkout the run was planned in (review-merged-history: started
    in the home folder, planned under hive-factory), so its files were lost."""
    path = run_dir.find(root, slug, suffix) if root else None
    if not path:
        found = glob.glob(os.path.join(glob.escape(store_dir()), "runs", "*", glob.escape(slug),
                                       f"{glob.escape(slug)}{suffix}"))
        path = found[0] if len(found) == 1 else None
    return path


INTAKE_FIELDS = ("intake_min", "questions_at_gate", "questions_after_gate", "wait_min_gate", "wait_min_after_gate")


def marker_time(text, name):
    """The time of the first `INTAKE <name> ... <ISO>` marker in the run file,
    as an aware datetime (a time without a zone is UTC), or None."""
    m = re.search(rf"INTAKE {name}\b[^\n]*?(\d{{4}}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:?\d\d)?)", text or "")
    if not m:
        return None
    try:
        t = ts(m.group(1))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def gate_split(events, approved, freeze_line, first, last):
    """(questions at, after, wait seconds at, after) the intake approval. A
    wait is at the gate when it ended (its answer, or the typed message) at
    or before `approved`, so the approval itself counts at the gate. When
    that time is off (outside the run's own span of the main log), the order
    of the log decides: at or before the line that ran `intake.py freeze`.
    None when neither is known."""
    span_ok = approved and first and last and ts(first) - dt.timedelta(minutes=1) <= approved <= ts(last) + dt.timedelta(minutes=1)
    if span_ok:
        before = [ts(e[1]) <= approved for e in events]
    elif freeze_line is not None:
        before = [e[0] <= freeze_line for e in events]
    else:
        return None
    at = [e for e, b in zip(events, before) if b]
    after = [e for e, b in zip(events, before) if not b]
    return (sum(e[4] for e in at), sum(e[4] for e in after), sum(e[3] for e in at), sum(e[3] for e in after))


def intake_facts(row, session, slug, root):
    """Adds the intake stage's fields to the row (references/metrics.md).

    intake: whether the run had <slug>-intake.md. Without it, the gate fields
    are null. intake_min: from the run file's `INTAKE start` to `INTAKE
    approved`. The questions and the user wait of the main session split at
    the approval (gate_split()). The scenario fields come from
    <slug>-scenarios.json, null without it."""
    intake = find_run_file(root, slug, "-intake.md") if slug else None
    row["intake"] = bool(intake)
    for k in INTAKE_FIELDS:
        row[k] = None
    if intake:
        path = find_run_file(root, slug, ".md")
        text = open(path, encoding="utf-8", errors="ignore").read() if path else ""
        start, approved = marker_time(text, "start"), marker_time(text, "approved")
        if start and approved and approved >= start:
            row["intake_min"] = round((approved - start).total_seconds() / 60)
        # An approval before the start is a clock that is off: the log order decides.
        ok = approved if approved and (not start or approved >= start) else None
        info = USER_EVENTS.get((session, slug)) or {}
        split = gate_split(info.get("events") or [], ok, info.get("freeze_line"), info.get("first"), info.get("last"))
        if split:
            row["questions_at_gate"], row["questions_after_gate"] = split[0], split[1]
            row["wait_min_gate"], row["wait_min_after_gate"] = round(split[2] / 60), round(split[3] / 60)
    row["scenario_pass_rate"] = row["assert_edits"] = row["escape_count"] = None
    spath = find_run_file(root, slug, "-scenarios.json") if slug else None
    if spath:
        try:
            sc = json.load(open(spath, encoding="utf-8"))
        except ValueError:
            sc = None
        if isinstance(sc, dict):
            row["scenario_pass_rate"] = sc.get("pass_rate")
            row["assert_edits"] = sc.get("assert_edits")
            row["escape_count"] = len(sc.get("escapes") or [])
    return row


def unit_minutes(row, slug, root):
    """Each unit's size and worker minutes, from the run's plan file, so
    plan-waves.py can calibrate its model from real runs
    (items and its own estimate ride along, for the accuracy check). A worker is matched to
    its unit by the `Unit: <n>` line of its prompt, or else by its worktree
    name, <slug>-<n>; a lane worker without the line can't be matched. When a
    unit was dispatched twice, the last worker counts. Returns None without a
    plan file."""
    path = find_run_file(root, slug, "-plan.json")
    if not path:
        return None
    try:
        plan = json.load(open(path, encoding="utf-8"))
    except ValueError:
        return None
    size = {u["id"]: u.get("size") for u in plan.get("units", [])}
    items = {u["id"]: u.get("items") for u in plan.get("units", [])}
    est = plan.get("unit_estimates") or {}
    by_unit = {}
    for s in sorted(row.get("worker_spans", []), key=lambda s: s["start"]):
        unit = s.get("unit")
        if unit is None:
            m = re.fullmatch(re.escape(slug) + r"-(\d+)", s.get("wt") or "")
            unit = int(m.group(1)) if m else None
        if unit in size:
            by_unit[unit] = {"id": unit, "size": size[unit], "min": s["min"]}
            # What plan-waves.py expected, so --accuracy can compare it.
            if items.get(unit):
                by_unit[unit]["items"] = items[unit]
            if (est.get(str(unit)) or {}).get("p50"):
                by_unit[unit]["est"] = est[str(unit)]["p50"]
    return [by_unit[k] for k in sorted(by_unit)] or None


def run_file_facts(root, slug):
    """(mode, system map verdict, skill issues) from the run file, or Nones."""
    run_file = run_dir.find(root, slug, ".md") if root else None
    if not run_file:
        return None, None, []
    run_text = open(run_file, encoding="utf-8", errors="ignore").read()
    m = re.search(r"^.*Mode:.*$", run_text, re.M)
    mode = m.group(0).strip() if m else None
    m = re.search(r"\bMAP (none|ok|stale)\b", run_text)
    issues = [s.strip() for s in re.findall(r"^\s*-?\s*SKILL ISSUE: (.+)$", run_text, re.M)]
    return mode, (m.group(1) if m else None), issues


def record(slug, root):
    session = find_session(slug)
    if not session:
        print(f"record: no session log mentions the run file {slug}.md")
        return 1
    runs = run_dir.run_dir(root, slug)
    mode, system_map, issues = run_file_facts(root, slug)
    row, text = build_row(session, slug, os.path.basename(os.path.normpath(root)), mode)
    # Whether the split had a system map (check-map.py's verdict, from the run file).
    row["system_map"] = system_map
    # Workarounds the agents needed for the skill's own scripts and settings.
    row["skill_issues"] = issues
    row["unit_minutes"] = unit_minutes(row, slug, root)
    intake_facts(row, session, slug, root)
    # The version that actually ran: this script's own skill copy, at the end
    # of the run. version_for() only guesses from dates, for collected runs.
    vfile = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
    if os.path.exists(vfile):
        row["skill_version"] = open(vfile).read().split()[0]
        row["version_exact"] = True
    os.makedirs(runs, exist_ok=True)
    head, jpath = add_journal(row, session, slug, root, run_copy=True)
    with open(os.path.join(runs, f"{slug}-cost.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    path = save_row(row)
    # The headline for the orchestrator's final report.
    print(f"recorded {slug}: ${row['usd']:.1f}, wall {row['min_wall']}m (user wait {row['min_user_wait']}m: "
          f"questions {row['min_ask']}m over {row['questions']}, typed {row['min_typed']}m; "
          f"rest {row['min_rest']}m), {row['workers']} workers, reviewed {row['units_reviewed']}, "
          f"first-review high+medium {row['first_review_high_medium']}, fix(merge) {row['fix_merge_commits']}, "
          f"1h-TTL writes {row['mtok_w1h_subagents']} Mtok, turn budget hit: {', '.join(row['turn_budget_hit']) or 'none'}, "
          f"skill issues: {len(issues)}")
    if row["intake"]:
        print(f"intake: {row['intake_min'] if row['intake_min'] is not None else '-'}m to approval; "
              f"questions {row['questions_at_gate']} at the gate, {row['questions_after_gate']} after; "
              f"wait {row['wait_min_gate']}m at the gate, {row['wait_min_after_gate']}m after; "
              f"intent auditor rejected {row['intent_auditor_reject_rate']}; "
              f"scenarios pass {row['scenario_pass_rate']}, assert edits {row['assert_edits']}, "
              f"escapes {row['escape_count']}")
    print(head)
    print(f"report: {os.path.join(runs, slug + '-cost.txt')}; journal: {jpath}; data: {path}")
    return 0


def session_info(path):
    """(invoked, slugs, project, cwd) for one session log. slugs lists every
    run the session made (runs_in), in order; for an older log without a run
    file made in it, the first run file it mentions; or []."""
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
            if slug is None and "runs" in line:
                slug = run_dir.slug_in(line)
    slugs = [r["slug"] for r in runs_in(load(path))] if invoked else []
    project = os.path.basename(os.path.normpath(cwd)) if cwd else os.path.basename(os.path.dirname(path))
    return invoked, slugs or ([slug] if slug else []), project, cwd


def is_current(old_rows, mtime, migrate=True):
    """Whether a session's rows are up to date: one of them was built after
    the log last grew, and every run it saw in the session has a row. A row
    from before rows were keyed per run has no session_runs; with migrate
    (an explicit --collect) its session is recorded again once, splitting any
    runs it had merged. Without (the sweep after --record, which must stay
    quick) it counts as current, as before."""
    seen = {r.get("slug") for r in old_rows}
    for r in old_rows:
        if r.get("session_mtime", 0) < mtime - 1:
            continue
        if "session_runs" in r:
            if set(r["session_runs"]) <= seen:
                return True
        elif not migrate:
            return True
    return False


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


def prune_events(days=45):
    """Drops events.jsonl lines older than the logs they can be matched to;
    the journals already built keep what they found."""
    path = os.path.join(store_dir(), "events.jsonl")
    if not os.path.exists(path):
        return
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    lines = open(path, encoding="utf-8", errors="ignore").readlines()
    # "end" is the first field, so the line itself sorts by time.
    keep = [s for s in lines if s[:40] >= f'{{"end": "{cut[:19]}']
    if len(keep) < len(lines):
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(keep)


def collect(quiet=False, refresh=False):
    """Records every session on this machine that ran the skill and has no
    current row: missed --record calls, crashed runs and older runs alike, and
    archives their logs. --refresh rebuilds every row (after a metric change).

    A session written to in the last 15 minutes may still be running, so it
    waits for the next collect. A session that grew after its row was written
    (a resumed run) is recorded again.
    """
    rows = collections.defaultdict(list)
    for r in load_rows():
        rows[r.get("session")].append(r)
    prune_events()
    reports = os.path.join(store_dir(), "reports")
    os.makedirs(reports, exist_ok=True)
    now = dt.datetime.now().timestamp()
    added = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        session = os.path.basename(path)[:-6]
        mtime = os.path.getmtime(path)
        if now - mtime < 900:
            continue
        olds = rows.get(session) or []
        if olds and is_current(olds, mtime, migrate=not quiet) and not refresh:
            continue
        invoked, slugs, project, cwd = session_info(path)
        if not invoked:
            continue
        archive(path, project)
        if not slugs:
            # No run file: the session is one run, named as before.
            slugs = [(olds[0].get("slug") if len(olds) == 1 else None) or session[:8]]
        for slug in slugs:
            old = next((r for r in olds if r.get("slug") == slug), None)
            if old is None and len(slugs) == 1 and len(olds) == 1:
                old = olds[0]  # the session's one earlier row, whatever it was named
            try:
                mode, system_map, issues = run_file_facts(cwd, slug)
                row, text = build_row(path, slug, project, (old or {}).get("mode") or mode)
                row["unit_minutes"] = unit_minutes(row, slug, cwd)
                intake_facts(row, path, slug, cwd)
                row["system_map"] = (old or {}).get("system_map") or system_map
                row["skill_issues"] = (old or {}).get("skill_issues") or issues
                if (old or {}).get("version_exact"):
                    row["skill_version"], row["version_exact"] = old["skill_version"], True
                add_journal(row, path, slug, cwd)
            except Exception as exc:  # one unreadable log must not stop the rest
                print(f"collect: skipped {session[:8]} {slug}: {exc}")
                continue
            name = session if len(slugs) < 2 else f"{session}-{slug}"
            with open(os.path.join(reports, f"{name}.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
            if old is not None and row_key(old) != row_key(row):
                drop_row(old)
            save_row(row)
            added.append(f"{row['start'][:10]} {project} {row['slug']}")
        # A row named after the session (no run file found back then) is
        # replaced by the runs found now.
        for r in olds:
            if r.get("slug") == session[:8] and session[:8] not in slugs:
                drop_row(r)
    if not quiet or added:
        print(f"collect: {len(added)} session(s) recorded" + "".join(f"\n  {a}" for a in added))
    return 0


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '≤' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args[:1] == ["--record"]:
        root = args[args.index("--root") + 1] if "--root" in args else os.getcwd()
        code = record(args[1], root)
        collect(quiet=True)
        sys.exit(code)
    if args[:1] == ["--collect"]:
        sys.exit(collect(refresh="--refresh" in args))
    report(args)
