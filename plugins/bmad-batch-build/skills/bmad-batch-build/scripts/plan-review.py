"""The plan the user approves, as an HTML page with C4 diagrams and comments.

usage: python plan-review.py render <intake> --out <page.html> [--plan <plan.json>] [--gate]
                                    [--advice] [--note <file>]
       python plan-review.py serve <page.html> --result <review.json> [--run <run file>]
                                   [--timeout-min 240] [--open]
       python plan-review.py show <review.json>

`render` writes one self-contained page from the intake (intake.py's format):
the goal, non-goals, acceptance criteria, shared interfaces, the units, the
coupling, the mode, the holdout scenarios, assumptions and risks. It draws
the intake's `## Architecture (C4)` mermaid blocks as C4-styled flowcharts
(C4 colours and shapes, boundaries as dashed groups, the C4 source one click
away): mermaid's own C4 renderer lays boxes out in rows and draws edge labels
over them. It adds a component view built from the units: a boundary per
top-level folder they touch, a component per unit in the folder it touches
most, an arrow per dependency. Each unit is a card: chips for size, risk,
kind, branch, ticket, dependencies, ACs and estimate, one row per field of
its indented lines (`Now:`, `Should:`, `Tests:`, `Unchanged behaviour:` ...),
and its files grouped by folder. Every section, AC and unit has a comment
box. The decision panel offers Approve, Request changes and Cancel, and Build
directly first with --advice (or when the plan has an advice); --gate adds
the gate-policy choice. --plan adds each unit's estimate and wave from
plan-waves.py's file. --note puts a "What changed since your comments" box at
the top (plain text, one point per line). The diagrams need
cdn.jsdelivr.net; without it the page shows their source.

`serve` serves the page on 127.0.0.1 at a random port, behind a random token,
and prints `URL <url>`, also writing the URL to <page.html>.url. It reads the
page on every load, so a page rendered again shows on a refresh. With --open it
opens the browser. When the user presses Send, it writes <review.json>, shows
"Sent", prints what `show` prints, appends `PLAN-REVIEW <ISO> <decision>
<n> comments` to --run, and exits 0. Nothing arrives within --timeout-min:
`TIMEOUT`, exit 4. Run it in the background; its exit is the user's answer.

<review.json>:

    {"decision": "approve" | "build-directly" | "adjust" | "cancel",
     "gate": "<gate option>" | null,
     "comments": [{"target": "U2" | "AC-3" | "section:Goal" | "general", "text": "..."}],
     "sent": "<ISO>"}

`show` prints it as lines: `DECISION <d>`, `GATE <g|none>`, then
`COMMENT <target>: <text>` per comment (newlines as ` / `).

A page opened as a file (no server) can't send: its Send button then copies the
same lines for the user to paste into the chat.

Exit codes: 0 done, 2 bad arguments or files, 4 serve timed out.
"""
import argparse
import datetime
import html
import http.server
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import webbrowser

MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
DECISIONS = ("approve", "build-directly", "adjust", "cancel")
GATES = ("Re-dispatch once, then leave it out", "Merge unverified", "Ask me")
MAX_BODY = 1_000_000
TOKEN = "__PLAN_REVIEW_TOKEN__"
MERMAID_BLOCK = re.compile(r"```mermaid[ \t]*\n(.*?)\n```", re.S)


def sibling(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(os.path.dirname(os.path.abspath(__file__)), file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- render

def inline(text):
    """Escaped text with `code` spans."""
    parts = re.split(r"(`[^`\n]+`)", text)
    return "".join(f"<code>{html.escape(p[1:-1])}</code>" if p.startswith("`") and p.endswith("`") and len(p) > 1
                   else html.escape(p) for p in parts)


def markdown(text):
    """The little markdown the intake uses: `- ` lists (indented lines join their
    item), paragraphs, code spans, and mermaid blocks left out (drawn apart)."""
    text = MERMAID_BLOCK.sub("", text or "").strip()
    out, items, para = [], [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    for line in text.split("\n"):
        if line.startswith("- "):
            if para:
                flush()
            items.append(line[2:].strip())
        elif items and line[:1] in (" ", "\t") and line.strip():
            items[-1] += " " + line.strip()
        elif not line.strip():
            flush()
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return "".join(out) or "<p class=muted>(empty)</p>"


def c4_label(text):
    """A string for a mermaid C4 argument: no double quotes, no line breaks."""
    return re.sub(r"\s+", " ", (text or "").replace('"', "'")).strip()


def folder_of(path):
    return path.split("/", 1)[0] + "/" if "/" in path else "(root)"


# C4 elements -> (style class, shape). Mermaid's own C4 renderer places boxes
# in rows and draws edge labels over them (dast-gate-fixes); a flowchart gets a
# real layout, so the intake's C4 blocks are drawn as C4-styled flowcharts.
C4_KINDS = {
    "Person": ("person", "round"), "Person_Ext": ("ext", "round"),
    "System": ("system", "box"), "System_Ext": ("ext", "box"),
    "SystemDb": ("system", "db"), "SystemDb_Ext": ("ext", "db"),
    "SystemQueue": ("system", "queue"), "SystemQueue_Ext": ("ext", "queue"),
    "Container": ("container", "box"), "Container_Ext": ("ext", "box"),
    "ContainerDb": ("container", "db"), "ContainerDb_Ext": ("ext", "db"),
    "ContainerQueue": ("container", "queue"), "ContainerQueue_Ext": ("ext", "queue"),
    "Component": ("component", "box"), "Component_Ext": ("ext", "box"),
    "ComponentDb": ("component", "db"), "ComponentDb_Ext": ("ext", "db"),
    "ComponentQueue": ("component", "queue"), "ComponentQueue_Ext": ("ext", "queue"),
    "Node": ("container", "box"), "Deployment_Node": ("container", "box"),
}
C4_LABEL = {"person": "Person", "system": "Software System", "container": "Container",
            "component": "Component", "ext": "External"}
BOUNDARIES = ("Boundary", "Enterprise_Boundary", "System_Boundary", "Container_Boundary")
RELS = ("Rel", "BiRel", "Rel_U", "Rel_Up", "Rel_D", "Rel_Down", "Rel_L", "Rel_Left", "Rel_R", "Rel_Right",
        "Rel_Back", "Rel_Neighbor")
STYLES = ("classDef person fill:#08427b,stroke:#052e56,color:#fff",
          "classDef system fill:#1168bd,stroke:#0b4884,color:#fff",
          "classDef container fill:#438dd5,stroke:#2e6295,color:#fff",
          "classDef component fill:#85bbf0,stroke:#5d82a8,color:#000",
          "classDef ext fill:#8a8a8a,stroke:#6b6b6b,color:#fff",
          "classDef boundary fill:none,stroke:#8a8a8a,stroke-dasharray:6 4,color:#6b6b6b")
BIG_VIEW = 8  # units above which the component view scrolls instead of shrinking
CALL = re.compile(r"^\s*([A-Za-z_]+)\s*\((.*)\)\s*(\{)?\s*$")


def args_of(text):
    """The arguments of a C4 call: commas outside quotes split, quotes dropped,
    `$name=value` keyword arguments left out."""
    out, cur, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [a for a in out if not a.startswith("$")]


def fc_text(text):
    """Text for a flowchart label: mermaid's entity codes for the characters
    its parser would read as syntax."""
    t = re.sub(r"\s+", " ", text or "").strip()
    for ch, code in (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"), ("|", "#124;"),
                     ("[", "#91;"), ("]", "#93;"), ("{", "#123;"), ("}", "#125;")):
        t = t.replace(ch, code)
    return t


def node_id(alias):
    return "n_" + re.sub(r"\W", "_", alias)


def node(alias, cls, shape, name, tech="", desc=""):
    kind = C4_LABEL[cls] + (f": {tech}" if tech else "")
    label = f"<b>{fc_text(name)}</b><br/><small>[{fc_text(kind)}]</small>"
    if desc:
        label += f"<br/>{fc_text(desc)}"
    o, c = {"round": ("([", "])"), "db": ("[(", ")]"), "queue": ("[[", "]]")}.get(shape, ("[", "]"))
    return f'{node_id(alias)}{o}"{label}"{c}:::{cls}'


def c4_to_flowchart(source):
    """(flowchart source, title) for a mermaid C4 diagram, or None when it holds
    no element this reads."""
    lines = source.strip().split("\n")
    if not lines or not lines[0].strip().startswith("C4"):
        return None
    title, out, depth, seen, groups = lines[0].strip(), [], 0, False, []
    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        if line.startswith("title "):
            title = line[6:].strip()
            continue
        if line == "}":
            if depth:
                out.append("  " * depth + "end")
                depth -= 1
            continue
        m = CALL.match(line)
        if not m:
            continue
        fn, args, opens = m.group(1), args_of(m.group(2)), m.group(3)
        pad = "  " * (depth + 1)
        if fn in BOUNDARIES or fn.endswith("_Boundary") or (opens and fn not in C4_KINDS):
            depth += 1
            out.append(pad + f'subgraph {node_id(args[0])}["{fc_text(args[1] if len(args) > 1 else args[0])}"]')
            groups.append(node_id(args[0]))
            continue
        if fn in C4_KINDS and args:
            cls, shape = C4_KINDS[fn]
            name = args[1] if len(args) > 1 else args[0]
            if fn.startswith("Person") or fn.startswith("System"):
                tech, desc = "", args[2] if len(args) > 2 else ""
            else:
                tech, desc = (args[2] if len(args) > 2 else ""), (args[3] if len(args) > 3 else "")
            out.append(pad + node(args[0], cls, shape, name, tech, desc))
            seen = True
            if opens:  # Deployment_Node(...) { ... }
                depth += 1
                out.append(pad + f'subgraph {node_id(args[0])}_b[" "]')
                groups.append(node_id(args[0]) + "_b")
            continue
        if fn in RELS and len(args) >= 2:
            a, b = (args[1], args[0]) if fn == "Rel_Back" else (args[0], args[1])
            label = " ".join(x for x in (args[2] if len(args) > 2 else "",
                                         f"[{args[3]}]" if len(args) > 3 and args[3] else "") if x)
            arrow = "<-->" if fn == "BiRel" else "-->"
            out.append(pad + f"{node_id(a)} {arrow}" + (f'|"{fc_text(label)}"|' if label else "") + f" {node_id(b)}")
    while depth:
        out.append("  " * depth + "end")
        depth -= 1
    if not seen:
        return None
    out += [f"  class {g} boundary" for g in groups]
    return "\n".join(["flowchart TB"] + out + ["  " + st for st in STYLES]), title


def component_view(slug, units):
    """A flowchart in C4 component style: a boundary per top-level folder, a
    component per unit in the folder it touches most, an arrow per dependency."""
    # Short boxes: the file lists are on the unit cards. Long ones shrank a
    # 21-unit view to unreadable text.
    home = {}
    for u in units:
        counts = {}
        for t in u["touches"]:
            counts[folder_of(t)] = counts.get(folder_of(t), 0) + 1
        home[u["id"]] = max(sorted(counts), key=counts.get) if counts else "(root)"
    # A big run keeps its natural size and scrolls sideways: fitted to the page,
    # 21 units shrank to unreadable text. Its arrows go unlabelled.
    big = len(units) > BIG_VIEW
    lines = (["---", "config:", "  flowchart:", "    useMaxWidth: false", "---"] if big else []) + ["flowchart TB"]
    for i, folder in enumerate(sorted(set(home.values()))):
        lines.append(f'  subgraph b{i}["{fc_text(folder)}"]')
        for u in units:
            if home[u["id"]] != folder:
                continue
            folders = sorted({folder_of(t) for t in u["touches"]}, key=lambda x: (x != folder, x))
            n = len(u["touches"])
            desc = f"{n} file{'s' if n != 1 else ''} in " + ", ".join(folders) if n else ""
            title = u["title"] if len(u["title"]) <= 48 else u["title"][:47].rstrip() + "…"
            tech = " · ".join(x for x in (u["size"], u["risk"]) if x)
            lines.append("    " + node(u["id"].lower(), "component", "box", f'{u["id"]} {title}', tech, desc))
        lines.append("  end")
        lines.append(f"  class b{i} boundary")
    for u in units:
        for d in u["deps"]:
            if d["id"] in home:
                label = "" if big else '|"depends on"|'
                lines.append(f'  {node_id(u["id"].lower())} -->{label} {node_id(d["id"].lower())}')
    return "\n".join(lines + ["  " + st for st in STYLES])


def js_string(value):
    """A JS string literal safe inside <script>: html.escape would break the JS."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def comment_box(target):
    t = html.escape(target, quote=True)
    return (f'<details class="c"><summary>Comment</summary>'
            f'<textarea data-target="{t}" rows="3" placeholder="Your comment on {t}"></textarea></details>')


def section(title, inner, target=None):
    target = target or f"section:{title}"
    return (f'<section id="{html.escape(target, quote=True)}"><h2>{html.escape(title)}</h2>{inner}'
            f"{comment_box(target)}</section>")


def diagram(source, caption, c4_source=None):
    src = (f'<details class="src"><summary>C4 source</summary><pre>{html.escape(c4_source)}</pre></details>'
           if c4_source else "")
    return (f'<figure><figcaption>{html.escape(caption)}</figcaption>'
            f'<pre class="mermaid">{html.escape(source)}</pre>{src}</figure>')


def c4_figure(source):
    converted = c4_to_flowchart(source)
    kind = source.strip().split("\n", 1)[0].strip()
    if not converted:
        return diagram(source, kind)
    flow, title = converted
    return diagram(flow, f"{title} ({kind})", source)


FIELD = re.compile(r"^([A-Z][\w /()-]{0,40}?):\s*(.*)$")
META = re.compile(r"^(kind|branch|ticket)\s+(.+)$")


def unit_fields(block):
    """(meta chips, [(field, [paragraph or list items])]) from a unit's indented lines."""
    chips, fields = [], []
    for line in block.split("\n")[1:]:
        t = line.strip()
        if not t:
            continue
        m = META.match(t)
        if m:
            chips.append((m.group(1), m.group(2)))
        elif t.startswith("--"):
            chips.append(("flag", t))
        elif t.startswith("- ") and fields:
            fields[-1][1].append(t[2:].strip())
        elif FIELD.match(t):
            k, v = FIELD.match(t).groups()
            fields.append((k, [v] if v else []))
        elif fields:
            fields[-1][1][-1:] = [(fields[-1][1][-1] + " " + t) if fields[-1][1] else t]
        else:
            fields.append(("Notes", [t]))
    return chips, fields


def field_html(name, values):
    if len(values) > 1:
        body = "<ul>" + "".join(f"<li>{inline(v)}</li>" for v in values) + "</ul>"
    elif values and name.lower().startswith("unchanged") and values[0].count(";") >= 2:
        body = "<ul>" + "".join(f"<li>{inline(v.strip())}</li>" for v in values[0].split(";") if v.strip()) + "</ul>"
    else:
        body = f"<p>{inline(values[0])}</p>" if values else ""
    if name.lower() == "source":
        return f'<details class="f"><summary>{html.escape(name)}</summary>{body}</details>'
    return f'<div class="f"><div class="k">{html.escape(name)}</div><div class="v">{body}</div></div>'


def unit_card(u, plan_row):
    chips, fields = unit_fields(u["block"])
    pills = [f'<span class="pill size">{html.escape(u["size"])}</span>' if u["size"] else "",
             f'<span class="pill risk-{html.escape(u["risk"])}">risk {html.escape(u["risk"])}</span>' if u["risk"] else ""]
    pills += [f'<span class="pill">{html.escape(v if k in ("kind", "flag") else f"{k} {v}")}</span>' for k, v in chips]
    deps = ", ".join(d["id"] + (f" ({d['reason']})" if d["reason"] else "") for d in u["deps"])
    if deps:
        pills.append(f'<span class="pill">after {inline(deps)}</span>')
    if u["acs"]:
        pills.append(f'<span class="pill">{html.escape(", ".join(u["acs"]))}</span>')
    est = ", ".join(x for x in (plan_row.get("est"), plan_row.get("wave")) if x)
    if est:
        pills.append(f'<span class="pill">{html.escape(est)}</span>')
    by_folder = {}
    for t in u["touches"]:
        by_folder.setdefault(folder_of(t), []).append(t)
    files = "".join(f'<div class="k">{html.escape(f)}</div><ul class="files">'
                    + "".join(f"<li><code>{html.escape(p)}</code></li>" for p in ps) + "</ul>"
                    for f, ps in by_folder.items())
    n = len(u["touches"])
    return (f'<article class="unit" id="{u["id"]}"><h3><span class="uid">{u["id"]}</span> {inline(u["title"])}</h3>'
            f'<div class="pills">{"".join(pills)}</div>'
            + "".join(field_html(k, v) for k, v in fields)
            + (f'<details class="f"><summary>{n} file{"s" if n != 1 else ""}</summary>{files}</details>' if n else "")
            + f"{comment_box(u['id'])}</article>")


def render(a):
    intake = sibling("intake_mod", "intake.py")
    with open(a.intake, encoding="utf-8") as f:
        text = f.read()
    slug = intake.slug_of(a.intake)
    units, acs = intake.units(text), intake.acs(text)
    plan, plan_doc = {}, {}
    if a.plan:
        with open(a.plan, encoding="utf-8") as f:
            plan_doc = json.load(f)
        # plan-waves.py's format: waves [{units, run, minutes}], unit_estimates {id: {p50, p80}}.
        for k, w in enumerate(plan_doc.get("waves") or [], 1):
            for uid in w.get("units") or []:
                plan.setdefault(intake.norm_unit(str(uid)), {})["wave"] = f"wave {k} ({w.get('run', '')})"
        for uid, e in (plan_doc.get("unit_estimates") or {}).items():
            if isinstance(e, dict) and e.get("p50"):
                plan.setdefault(intake.norm_unit(str(uid)), {})["est"] = f"≈{e['p50']} min"
    note = ""
    if a.note:
        with open(a.note, encoding="utf-8") as f:
            points = [x.strip() for x in f.read().splitlines() if x.strip()]
        note = ('<section class="note"><h2>What changed since your comments</h2><ul>'
                + "".join(f"<li>{inline(p)}</li>" for p in points) + "</ul></section>")

    ac_items = "".join(
        f'<li id="{k}"><b>{k}</b> {inline(re.sub(rf"^- {re.escape(k)}\s*:\s*", "", v).replace(chr(10), " "))}'
        f"{comment_box(k)}</li>" for k, v in acs.items())
    units_html = "".join(unit_card(u, plan.get(u["id"], {})) for u in units)

    arch = intake.body(text, "Architecture (C4)")
    figures = [c4_figure(m.group(1)) for m in MERMAID_BLOCK.finditer(arch)]
    figures.append(diagram(component_view(slug, units), "The units (component view, built from the intake)"))
    arch_rest = markdown(arch) if MERMAID_BLOCK.sub("", arch).strip() else ""

    decisions = [("adjust", "Request changes", "Claude applies your comments and shows the plan again.")]
    decisions.insert(0, ("approve", "Approve", "Freeze the plan, create the tickets and start the run. Comments that "
                         "change units, ACs or interfaces turn this into Request changes."))
    if a.advice or plan_doc.get("advice"):
        decisions.insert(0, ("build-directly", "Build directly (Recommended)",
                             "No workers: Claude builds the units in this session."))
    decisions.append(("cancel", "Cancel", "Stop here; nothing is created."))
    radios = "".join(f'<label class="opt"><input type="radio" name="decision" value="{v}"{" checked" if i == 0 else ""}>'
                     f"<b>{html.escape(t)}</b><span>{html.escape(d)}</span></label>"
                     for i, (v, t, d) in enumerate(decisions))
    gate = ""
    if a.gate:
        gate = ("<h3>If a unit nothing depends on fails its gate</h3>" + "".join(
            f'<label class="opt"><input type="radio" name="gate" value="{html.escape(g, quote=True)}"'
            f'{" checked" if i == 0 else ""}><b>{html.escape(g)}{" (Recommended)" if i == 0 else ""}</b></label>'
            for i, g in enumerate(GATES)))

    body = "".join([
        note,
        section("Goal", markdown(intake.body(text, "Goal"))),
        section("Non-goals", markdown(intake.body(text, "Non-goals"))),
        section("Architecture", "".join(figures) + arch_rest, "section:Architecture (C4)"),
        section("Units", units_html),
        section("Acceptance criteria", f"<ol class=acs>{ac_items}</ol>"),
        section("Shared interfaces", markdown(intake.body(text, "Shared interfaces"))),
        section("Coupling map", markdown(intake.body(text, "Coupling map"))),
        section("Mode decision", markdown(intake.body(text, "Mode decision"))),
        section("Verification scenarios", markdown(intake.body(text, "Verification scenarios"))),
        section("Assumptions and defaults applied", markdown(intake.body(text, "Assumptions and defaults applied"))),
        section("Open risks", markdown(intake.body(text, "Open risks"))),
    ])
    page = PAGE.format(title=html.escape(f"Plan: {slug}"), slug=html.escape(slug), body=body, radios=radios,
                       gate=gate, mermaid=MERMAID, token=TOKEN, n_units=len(units), n_acs=len(acs),
                       storage=js_string(f"plan-review:{slug}"))
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    print(f"PAGE {a.out} ({len(units)} units, {len(acs)} ACs, {len(figures)} diagrams)")
    return 0


# ---------------------------------------------------------------- serve / show

def show_lines(review):
    out = [f"DECISION {review['decision']}", f"GATE {review.get('gate') or 'none'}"]
    for c in review.get("comments", []):
        out.append(f"COMMENT {c['target']}: " + " / ".join(x.strip() for x in c["text"].strip().splitlines()))
    return out


def valid(review):
    """The review as stored, or None when the page sent something else."""
    if not isinstance(review, dict) or review.get("decision") not in DECISIONS:
        return None
    gate = review.get("gate")
    if gate is not None and gate not in GATES:
        return None
    comments = []
    for c in review.get("comments") or []:
        if not isinstance(c, dict) or not isinstance(c.get("target"), str) or not isinstance(c.get("text"), str):
            return None
        if c["text"].strip():
            comments.append({"target": c["target"][:80], "text": c["text"].strip()[:4000]})
    return {"decision": review["decision"], "gate": gate, "comments": comments, "sent": now()}


def serve(a):
    with open(a.page, encoding="utf-8"):
        pass  # fail now, not at the first request, when the page is missing
    token = secrets.token_urlsafe(18)

    def body():
        # Read on every load: a page rendered again shows on a refresh, with no new URL.
        with open(a.page, encoding="utf-8") as f:
            return f.read().replace(TOKEN, token).encode("utf-8")
    done = threading.Event()
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, data, kind="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/" and f"t={token}" in self.path:
                self.reply(200, body(), "text/html; charset=utf-8")
            else:
                self.reply(404, b"not found")

        def do_POST(self):
            if self.path != "/submit" or self.headers.get("X-Plan-Token") != token:
                return self.reply(403, b"forbidden")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self.reply(413, b"too large")
            try:
                review = valid(json.loads(self.rfile.read(n).decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                review = None
            if review is None or done.is_set():
                return self.reply(400, b"bad review")
            tmp = a.result + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(review, f, indent=2, ensure_ascii=False)
            os.replace(tmp, a.result)
            result.update(review)
            done.set()
            self.reply(200, b"ok")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    with open(a.page + ".url", "w", encoding="utf-8") as f:
        f.write(url + "\n")
    print(f"URL {url}", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if a.open:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            pass
    ok = done.wait(a.timeout_min * 60)
    server.shutdown()
    if not ok:
        print("TIMEOUT")
        return 4
    print("\n".join(show_lines(result)))
    if a.run:
        with open(a.run, "a", encoding="utf-8") as f:
            f.write(f"PLAN-REVIEW {result['sent']} {result['decision']} {len(result['comments'])} comments\n")
    return 0


def show(a):
    with open(a.review, encoding="utf-8") as f:
        review = valid(json.load(f))
    if review is None:
        print(f"{a.review} is not a plan review", file=sys.stderr)
        return 2
    print("\n".join(show_lines(review)))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="plan-review.py", description="See the module docstring.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("render")
    s.add_argument("intake")
    s.add_argument("--out", required=True)
    s.add_argument("--plan")
    s.add_argument("--gate", action="store_true")
    s.add_argument("--advice", action="store_true")
    s.add_argument("--note")
    s.set_defaults(fn=render)
    s = sub.add_parser("serve")
    s.add_argument("page")
    s.add_argument("--result", required=True)
    s.add_argument("--run")
    s.add_argument("--timeout-min", type=float, default=240)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=serve)
    s = sub.add_parser("show")
    s.add_argument("review")
    s.set_defaults(fn=show)
    return p


def main(argv):
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except (OSError, ValueError, KeyError) as e:
        print(f"plan-review: {e}", file=sys.stderr)
        return 2


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#f7f7f5; --card:#fff; --ink:#1c1c1a; --muted:#6b6b66; --line:#e2e1dc; --accent:#2f5fd0;
  --note:#fff7e0; --ok:#1f7a4d; --bad:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --card:#201f1d; --ink:#ecebe6; --muted:#a3a29b;
  --line:#34332f; --accent:#7ea2ff; --note:#2c2717; --ok:#5fd49b; --bad:#ff8a80; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
header {{ padding:20px 16px 8px; max-width:1100px; margin:0 auto; }}
header h1 {{ margin:0; font-size:22px; }}
main {{ max-width:1100px; margin:0 auto; padding:0 16px 140px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:12px 0; }}
section.note {{ background:var(--note); }}
h2 {{ font-size:16px; margin:0 0 8px; }} h3 {{ font-size:14px; margin:14px 0 6px; }}
.muted {{ color:var(--muted); }} .small {{ font-size:12.5px; }}
code {{ font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg); padding:1px 4px; border-radius:4px; word-break:break-all; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th,td {{ text-align:left; vertical-align:top; padding:8px 6px; border-top:1px solid var(--line); }}
.tablewrap {{ overflow-x:auto; }}
figure {{ margin:8px 0 16px; overflow-x:auto; }} figcaption {{ color:var(--muted); font-size:12.5px; }}
pre.mermaid {{ background:var(--card); white-space:pre-wrap; font-size:12px; }}
ol.acs li {{ margin:6px 0; }}
article.unit {{ border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin:10px 0; }}
article.unit h3 {{ margin:0 0 6px; font-size:15px; }}
.uid {{ display:inline-block; background:var(--accent); color:#fff; border-radius:5px; padding:0 6px; margin-right:4px; }}
.pills {{ display:flex; flex-wrap:wrap; gap:6px; margin:4px 0 10px; }}
.pill {{ font-size:12px; border:1px solid var(--line); border-radius:999px; padding:1px 9px; background:var(--bg); }}
.pill.risk-high {{ border-color:var(--bad); color:var(--bad); }} .pill.risk-med {{ border-color:#c98a00; color:#c98a00; }}
.f {{ display:grid; grid-template-columns:150px 1fr; gap:10px; padding:6px 0; border-top:1px dashed var(--line); }}
.f .k, .unit .k {{ color:var(--muted); font-size:12.5px; font-weight:600; }}
.f .v p {{ margin:0; }} .f .v ul {{ margin:0; padding-left:18px; }}
details.f {{ display:block; }} details.f summary {{ cursor:pointer; color:var(--muted); font-size:12.5px; font-weight:600; }}
ul.files {{ margin:2px 0 8px; padding-left:18px; columns:2 320px; font-size:12.5px; }}
@media (max-width: 640px) {{ .f {{ grid-template-columns:1fr; gap:2px; }} }}
details.src summary {{ cursor:pointer; color:var(--muted); font-size:12px; }}
details.src pre {{ font-size:12px; white-space:pre-wrap; background:var(--bg); padding:8px; border-radius:6px; }}
figure figcaption {{ font-weight:600; color:var(--ink); font-size:13.5px; margin-bottom:4px; }}
details.c summary {{ cursor:pointer; color:var(--accent); font-size:12.5px; margin-top:6px; }}
details.c[open] summary {{ font-weight:600; }}
details.c.has summary::after {{ content:" ●"; }}
textarea {{ width:100%; margin-top:6px; font:inherit; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px; }}
#panel {{ position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:10px 16px; }}
#panel .in {{ max-width:1100px; margin:0 auto; }}
#panel .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
.opt {{ display:flex; gap:6px; align-items:baseline; margin:4px 0; }} .opt span {{ color:var(--muted); font-size:12.5px; }}
button {{ font:inherit; background:var(--accent); color:#fff; border:0; border-radius:8px; padding:8px 16px; cursor:pointer; }}
button:disabled {{ opacity:.5; cursor:default; }}
#status.ok {{ color:var(--ok); }} #status.bad {{ color:var(--bad); }}
#more {{ display:none; }} #panel.open #more {{ display:block; max-height:40vh; overflow:auto; }}
</style></head><body>
<header><h1>{slug}</h1><div class="muted">{n_units} units · {n_acs} acceptance criteria · comment on anything, then choose and send.</div></header>
<main>{body}<section><h2>General comment</h2><textarea data-target="general" rows="3" placeholder="Anything else"></textarea></section></main>
<div id="panel"><div class="in">
<div class="row"><b>Decision</b><span id="count" class="muted small"></span>
<button type="button" id="toggle" style="background:transparent;color:var(--accent);padding:4px 8px">Choose ▴</button>
<button type="button" id="send">Send to Claude</button><span id="status" class="small"></span></div>
<div id="more">{radios}{gate}</div>
</div></div>
<script src="{mermaid}"></script>
<script>
(function () {{
  var TOKEN = "{token}", KEY = {storage};
  var boxes = Array.prototype.slice.call(document.querySelectorAll("textarea[data-target]"));
  function load() {{ try {{ return JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{ return {{}}; }} }}
  function save() {{ var d = {{}}; boxes.forEach(function (b) {{ if (b.value.trim()) d[b.dataset.target] = b.value; }});
    try {{ localStorage.setItem(KEY, JSON.stringify(d)); }} catch (e) {{}} mark(); }}
  function mark() {{ var n = 0; boxes.forEach(function (b) {{ var det = b.closest("details");
      if (b.value.trim()) {{ n++; if (det) det.classList.add("has"); }} else if (det) det.classList.remove("has"); }});
    document.getElementById("count").textContent = n ? n + " comment" + (n > 1 ? "s" : "") : "no comments"; }}
  var saved = load();
  boxes.forEach(function (b) {{ if (saved[b.dataset.target]) {{ b.value = saved[b.dataset.target];
      var det = b.closest("details"); if (det) det.open = true; }} b.addEventListener("input", save); }});
  mark();
  var panel = document.getElementById("panel");
  document.getElementById("toggle").onclick = function () {{ panel.classList.toggle("open"); }};
  function review() {{
    var d = document.querySelector("input[name=decision]:checked"), g = document.querySelector("input[name=gate]:checked");
    return {{ decision: d ? d.value : "adjust", gate: g ? g.value : null,
      comments: boxes.filter(function (b) {{ return b.value.trim(); }}).map(function (b) {{ return {{ target: b.dataset.target, text: b.value }}; }}) }};
  }}
  function asText(r) {{
    return ["DECISION " + r.decision, "GATE " + (r.gate || "none")].concat(r.comments.map(function (c) {{
      return "COMMENT " + c.target + ": " + c.text.trim().split(/\\n/).map(function (x) {{ return x.trim(); }}).join(" / "); }})).join("\\n");
  }}
  var send = document.getElementById("send"), status = document.getElementById("status");
  send.onclick = function () {{
    var r = review();
    if (!panel.classList.contains("open")) {{ panel.classList.add("open"); status.textContent = "Check the decision, then press Send again."; return; }}
    send.disabled = true; status.className = "small"; status.textContent = "Sending…";
    fetch("/submit", {{ method: "POST", headers: {{ "Content-Type": "application/json", "X-Plan-Token": TOKEN }}, body: JSON.stringify(r) }})
      .then(function (res) {{ if (!res.ok) throw new Error(res.status);
        status.className = "small ok"; status.textContent = "Sent. You can close this page; Claude has your answer.";
        try {{ localStorage.removeItem(KEY); }} catch (e) {{}} }})
      .catch(function () {{
        send.disabled = false; var t = asText(r);
        var done = function () {{ status.className = "small bad"; status.textContent = "Not sent (no server). Copied as text: paste it into the chat."; }};
        if (navigator.clipboard) navigator.clipboard.writeText(t).then(done, function () {{ window.prompt("Copy this into the chat:", t); }});
        else window.prompt("Copy this into the chat:", t);
      }});
  }};
  if (window.mermaid) {{
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    mermaid.initialize({{ startOnLoad: false, theme: dark ? "dark" : "default", securityLevel: "strict",
      flowchart: {{ htmlLabels: true, useMaxWidth: true, nodeSpacing: 40, rankSpacing: 60, wrappingWidth: 220 }} }});
    mermaid.run({{ querySelector: "pre.mermaid" }}).catch(function () {{}});
  }}
  document.querySelectorAll("table").forEach(function (t) {{ var w = document.createElement("div"); w.className = "tablewrap";
    t.parentNode.insertBefore(w, t); w.appendChild(t); }});
}})();
</script></body></html>
"""

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
