"""The run's intake: one file that pins what the run builds, before any worktree.

usage: python intake.py new <runs> <slug> --prompt-file <f>
       python intake.py check <intake>
       python intake.py asked <intake> --count <n>
       python intake.py defaults <intake> --question "<header>" --took "<option>"
       python intake.py set-mode <intake> "<Mode line>" --reason "<r>"
       python intake.py to-plan <intake> <plan.json>
       python intake.py freeze <intake>
       python intake.py amend <intake> --reason "<r>" --text "<t>"
       python intake.py verify <intake>
       python intake.py unit <intake> <id>
       python intake.py scenarios <intake>
       python intake.py iface add <file> --unit <id> --iface "<symbol>" --change "<old> -> <new>" --reason "<r>"
       python intake.py iface show <file>

The intake is <runs>/<slug>-intake.md, next to the run file <runs>/<slug>.md
(run_dir.py). It is never written inside a worktree. Its `##` sections, in this
order, are what the subcommands parse:

    Source prompt, Goal, Non-goals, Acceptance criteria, Shared interfaces,
    Units, Coupling map, Mode decision, Verification scenarios,
    Assumptions and defaults applied, Open risks, Changes after approval

A unit is one line, with optional indented lines under it:

    - U1 <title> | size M | risk low | depends on: - | touches: a.ts, b.ts | ACs: AC-1, AC-2 | items: 3

Sizes S/M/L/XL, risk low/med/high. `depends on:` is `-` or unit ids, each with
an optional `(reason)`. A field that still holds a `<...>` placeholder counts
as empty, so the template `new` writes is NOT READY until it is filled.

Exit codes: check 0 READY / 2 NOT READY (freeze and to-plan refuse a NOT READY
intake with 2 as well); verify 0 FROZEN ok / 1 FROZEN changed / 3 NOT FROZEN;
anything refused (an existing intake for new, a second freeze, amend before
freeze, defaults or set-mode after it, an unknown unit) exits 1.

freeze hashes the text above `## Changes after approval` (newlines read as
\\n) into <intake>.sha256 and makes the intake read-only; from then on only
amend changes it, and only below that heading. Workers see `unit` output,
never the source prompt or the verification scenarios.

iface add appends to the interface broadcast file under a lock file
(<file>.lock, created with O_EXCL and retried), with one O_APPEND write, so
parallel workers can add entries at once.

Writes INTAKE markers into the run file, which metrics read:
`INTAKE start <ISO>` (new), `INTAKE questions <n> <ISO>` (asked),
`INTAKE approved <ISO>` (freeze); defaults also writes the
`TIMEOUT: <header>: took <option>` line there.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import time

HEADINGS = ("Source prompt", "Goal", "Non-goals", "Acceptance criteria", "Shared interfaces", "Units",
            "Coupling map", "Mode decision", "Verification scenarios", "Assumptions and defaults applied",
            "Open risks", "Changes after approval")
CHANGES = "## Changes after approval"
SIZES = ("S", "M", "L", "XL")
RISKS = ("low", "med", "high")
PLACEHOLDER = re.compile(r"<[^<>\n]*>")
LOCK_WAIT = 20.0   # seconds iface add waits for the lock
LOCK_STALE = 30.0  # seconds after which a left-over lock file is broken

TEMPLATE = """# Intake: {slug}

## Source prompt
{prompt}

## Goal
<1-3 sentences: what the run delivers>

## Non-goals
- <what this run does not do>

## Acceptance criteria
- AC-1: <testable, one behaviour>

## Shared interfaces
- <symbol / endpoint / schema / contract>: <current shape> -> <new shape> | owner <unit id>

## Units
- U1 <title> | size <S/M/L/XL> | risk <low/med/high> | depends on: - | touches: <files> | ACs: <AC ids> | items: <n>

## Coupling map
<which units are independent, which must be sequential, and why>

## Mode decision
<filled by intake.py set-mode from plan-waves' Mode line>

## Verification scenarios
- SC-1: Given <...> When <...> Then <...>

## Assumptions and defaults applied
- <assumption>

## Open risks
- <risk>

## Changes after approval
(empty at approval time)
"""


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def append_line(path, line):
    """Appends one line, starting a new line first when the file doesn't end
    with one. Creates the file."""
    lead = ""
    if os.path.exists(path) and os.path.getsize(path):
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            lead = "" if fh.read(1) == b"\n" else "\n"
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(lead + line + "\n")


def run_file(intake):
    name = os.path.basename(intake)
    slug = name[:-len("-intake.md")] if name.endswith("-intake.md") else os.path.splitext(name)[0]
    return os.path.join(os.path.dirname(os.path.abspath(intake)), slug + ".md")


def slug_of(intake):
    return os.path.basename(run_file(intake))[:-3]


def hash_file(intake):
    return intake + ".sha256"


def frozen(intake):
    return os.path.exists(hash_file(intake))


# ---------------------------------------------------------------- parsing

def split_frozen(text):
    """(text above the last `## Changes after approval` line, the rest), or
    (None, text) when the heading is missing."""
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() == CHANGES:
            return "\n".join(lines[:i]) + "\n", "\n".join(lines[i:])
    return None, text


def sections(text):
    """{heading: (start line, end line)} of each section's body. The verbatim
    source prompt may hold `##` lines of its own, so the headings are found
    from the bottom up: each one is the last match above the next."""
    lines = text.split("\n")
    out, upper = {}, len(lines)
    for name in reversed(HEADINGS):
        for i in range(upper - 1, -1, -1):
            if lines[i].rstrip() == "## " + name:
                out[name] = (i + 1, upper)
                upper = i
                break
    return out, lines


def body(text, name):
    spans, lines = sections(text)
    if name not in spans:
        return ""
    a, b = spans[name]
    return "\n".join(lines[a:b]).strip("\n")


def replace_body(text, name, new_body):
    spans, lines = sections(text)
    if name not in spans:
        raise SystemExit(f"intake has no ## {name} section")
    a, b = spans[name]
    return "\n".join(lines[:a] + new_body.rstrip("\n").split("\n") + [""] + lines[b:])


def empty(value):
    """True for text that is blank or still holds a <...> placeholder."""
    v = (value or "").strip()
    return not v or bool(PLACEHOLDER.search(v)) or not re.search(r"\w", v)


def items_of(block):
    """The `- ` entries of a section, each with its indented continuation lines."""
    out = []
    for line in block.split("\n"):
        if line.startswith("- "):
            out.append([line])
        elif out and line.strip() and line[:1] in (" ", "\t"):
            out[-1].append(line)
    return out


def norm_unit(uid):
    uid = (uid or "").strip()
    m = re.fullmatch(r"[Uu]?(\d+)", uid)
    return f"U{m.group(1)}" if m else uid


def acs(text):
    """{AC id: full text with its continuation lines}, placeholders left out."""
    out = {}
    for entry in items_of(body(text, "Acceptance criteria")):
        m = re.match(r"- (AC-\d+)\s*:\s*(.*)", entry[0])
        if m and not empty(m.group(2)):
            out[m.group(1)] = "\n".join(entry)
    return out


def parse_deps(value):
    value = (value or "").strip()
    if value in ("", "-", "none"):
        return []
    deps = []
    for m in re.finditer(r"([Uu]?\d+|[^,()\s]+)\s*(?:\(([^)]*)\))?", value):
        deps.append({"id": norm_unit(m.group(1)), "reason": (m.group(2) or "").strip()})
    return deps


def units(text):
    """One dict per real unit line (a unit whose title is a placeholder is not one)."""
    out = []
    for entry in items_of(body(text, "Units")):
        head = entry[0][2:]
        parts = [p.strip() for p in head.split("|")]
        m = re.match(r"([Uu]\d+)\s*(.*)", parts[0])
        if not m or empty(m.group(2)):
            continue
        u = {"id": norm_unit(m.group(1)), "title": m.group(2).strip(), "size": "", "risk": "", "deps": [],
             "touches": [], "acs": [], "items": 0, "block": "\n".join(entry)}
        for p in parts[1:]:
            key, _, val = p.partition(":") if ":" in p else p.partition(" ")
            key, val = key.strip().lower(), val.strip()
            if empty(val) and key != "depends on":
                continue
            if key == "size":
                u["size"] = val.upper()
            elif key == "risk":
                u["risk"] = val.lower()
            elif key == "depends on":
                u["deps"] = [] if empty(val) else parse_deps(val)
            elif key == "touches":
                u["touches"] = [f.strip() for f in val.split(",") if f.strip()]
            elif key == "acs":
                u["acs"] = [a.strip() for a in val.split(",") if a.strip()]
            elif key == "items":
                try:
                    u["items"] = int(val)
                except ValueError:
                    pass
        out.append(u)
    return out


def missing(text):
    """One line per gap that keeps the intake from READY."""
    gaps = []
    if empty(body(text, "Goal")):
        gaps.append("Goal is empty")
    known_acs = acs(text)
    if not known_acs:
        gaps.append("no acceptance criterion (an AC-<n>: line under Acceptance criteria)")
    us = units(text)
    if not us:
        gaps.append("no unit (a U<n> line under Units)")
    ids = {u["id"] for u in us}
    for u in us:
        uid = u["id"]
        if not u["size"]:
            gaps.append(f"{uid} has no size")
        elif u["size"] not in SIZES:
            gaps.append(f"{uid} size {u['size']} is not one of S, M, L, XL")
        if not u["risk"]:
            gaps.append(f"{uid} has no risk")
        elif u["risk"] not in RISKS:
            gaps.append(f"{uid} risk {u['risk']} is not one of low, med, high")
        if not u["touches"]:
            gaps.append(f"{uid} has no touches (the files it owns)")
        if not u["acs"]:
            gaps.append(f"{uid} has no ACs")
        for a in u["acs"]:
            if a not in known_acs:
                gaps.append(f"{uid} names {a}, which is not under Acceptance criteria")
        for d in u["deps"]:
            if d["id"] not in ids:
                gaps.append(f"{uid} depends on {d['id']}, which is not a unit")
            elif d["id"] == uid:
                gaps.append(f"{uid} depends on itself")
    for entry in items_of(body(text, "Shared interfaces")):
        line = entry[0][2:].strip()
        if line.lower() in ("none", "-"):
            continue
        name = line.split(":", 1)[0].strip() if ":" in line else line
        if empty(name):
            gaps.append("Shared interfaces still holds the template line: list them, each with its owner unit, "
                        "or write - none")
            continue
        m = re.search(r"\|\s*owner\s+([Uu]?\d+)\s*$", line)
        if not m:
            gaps.append(f"shared interface {name} has no owner unit")
        elif norm_unit(m.group(1)) not in ids:
            gaps.append(f"shared interface {name} names owner {norm_unit(m.group(1))}, which is not a unit")
    return gaps


# ---------------------------------------------------------------- commands

def cmd_new(a):
    os.makedirs(a.runs, exist_ok=True)
    path = os.path.join(a.runs, f"{a.slug}-intake.md")
    if os.path.exists(path):
        print(f"intake exists: {path} (not overwritten)", file=sys.stderr)
        return 1
    with open(a.prompt_file, encoding="utf-8", newline="") as fh:
        prompt = fh.read().replace("\r\n", "\n").rstrip("\n")
    write(path, TEMPLATE.format(slug=a.slug, prompt=prompt))
    append_line(run_file(path), f"INTAKE start {now()}")
    print(os.path.normpath(path))
    return 0


def report_check(text):
    gaps = missing(text)
    if not gaps:
        print("READY")
        return 0
    print("NOT READY")
    for g in gaps:
        print(f"MISSING: {g}")
    return 2


def cmd_check(a):
    return report_check(read(a.intake))


def cmd_asked(a):
    append_line(run_file(a.intake), f"INTAKE questions {a.count} {now()}")
    print(f"INTAKE questions {a.count}")
    return 0


def refuse_frozen(intake, what):
    if frozen(intake):
        print(f"{what}: the intake is frozen; change it with intake.py amend", file=sys.stderr)
        return True
    return False


def add_to_section(text, name, line):
    """Appends a line to a section, dropping the template's placeholder lines."""
    kept = [ln for ln in body(text, name).split("\n") if ln.strip() and not (PLACEHOLDER.search(ln) and
                                                                               ln.lstrip("- ").startswith("<"))]
    return replace_body(text, name, "\n".join(kept + [line]))


def cmd_defaults(a):
    if refuse_frozen(a.intake, "defaults"):
        return 1
    line = f"- TIMEOUT: {a.question}: took {a.took}"
    write(a.intake, add_to_section(read(a.intake), "Assumptions and defaults applied", line))
    append_line(run_file(a.intake), f"TIMEOUT: {a.question}: took {a.took}")
    print(line)
    return 0


def cmd_set_mode(a):
    if refuse_frozen(a.intake, "set-mode"):
        return 1
    mode = a.mode.strip()
    text = replace_body(read(a.intake), "Mode decision", f"{mode}\nReason: {a.reason.strip()}")
    write(a.intake, text)
    print(f"Mode decision: {mode}")
    return 0


def cmd_to_plan(a):
    text = read(a.intake)
    if missing(text):
        return report_check(text)
    old = {}
    if os.path.exists(a.plan):
        with open(a.plan, encoding="utf-8") as fh:
            old = json.load(fh)
    before = {str(u.get("id")): u for u in old.get("units") or []}
    out = []
    for u in units(text):
        uid = u["id"][1:]
        unit = {k: v for k, v in before.get(uid, {}).items()}
        unit.pop("reason", None)
        unit.update({"id": uid, "title": u["title"], "size": u["size"], "risk": u["risk"],
                     "depends_on": [{"id": d["id"][1:] if d["id"].startswith("U") else d["id"],
                                     "reason": d["reason"]} for d in u["deps"]],
                     "files": u["touches"], "items": u["items"], "acs": u["acs"]})
        out.append(unit)
    old["units"] = out
    old["intake"] = os.path.abspath(a.intake).replace("\\", "/")
    with open(a.plan, "w", encoding="utf-8") as fh:
        json.dump(old, fh, indent=2)
    print(f"PLAN {a.plan}: {len(out)} unit(s)")
    return 0


def frozen_hash(text):
    above, _ = split_frozen(text)
    return None if above is None else hashlib.sha256(above.encode("utf-8")).hexdigest()


def writable(path, on):
    os.chmod(path, (stat.S_IREAD | stat.S_IWRITE) if on else stat.S_IREAD)


def cmd_freeze(a):
    if frozen(a.intake):
        print("already frozen: a second freeze is refused; use intake.py amend", file=sys.stderr)
        return 1
    text = read(a.intake)
    if missing(text):
        return report_check(text)
    digest = frozen_hash(text)
    if digest is None:
        print(f"no '{CHANGES}' heading: cannot freeze", file=sys.stderr)
        return 1
    write(hash_file(a.intake), digest + "\n")
    append_line(run_file(a.intake), f"INTAKE approved {now()}")
    writable(a.intake, False)
    writable(hash_file(a.intake), False)
    print(f"FROZEN {digest}")
    return 0


def cmd_amend(a):
    if not frozen(a.intake):
        print("NOT FROZEN: amend works only after freeze; edit the intake directly", file=sys.stderr)
        return 1
    line = f"- {now()} {' '.join(a.text.split())} (reason: {' '.join(a.reason.split())})"
    text = read(a.intake)
    above, rest = split_frozen(text)
    if above is None:
        print(f"no '{CHANGES}' heading", file=sys.stderr)
        return 1
    writable(a.intake, True)
    try:
        if "(empty at approval time)" in rest:
            # Only the text below the heading changes, so the hash still holds.
            rest = "\n".join(ln for ln in rest.split("\n") if ln.strip() != "(empty at approval time)")
            write(a.intake, above + rest.rstrip("\n") + "\n" + line + "\n")
        else:
            append_line(a.intake, line)
    finally:
        writable(a.intake, False)
    print(line)
    return 0


def cmd_verify(a):
    if not frozen(a.intake):
        print("NOT FROZEN")
        return 3
    with open(hash_file(a.intake), encoding="utf-8") as fh:
        want = fh.read().strip()
    if frozen_hash(read(a.intake)) == want:
        print("FROZEN ok")
        return 0
    print("FROZEN changed")
    return 1


def cmd_unit(a):
    text = read(a.intake)
    uid = norm_unit(a.id)
    found = [u for u in units(text) if u["id"] == uid]
    if not found:
        print(f"no unit {uid} in the intake", file=sys.stderr)
        return 1
    u = found[0]
    known = acs(text)
    parts = [f"# Unit {uid} of {slug_of(a.intake)}", "",
             "## Goal", body(text, "Goal"), "",
             "## Non-goals", body(text, "Non-goals"), "",
             "## Shared interfaces", body(text, "Shared interfaces"), "",
             "## Unit", u["block"], "",
             "## Acceptance criteria"]
    parts += [known.get(x, f"- {x}: (not under Acceptance criteria)") for x in u["acs"]]
    print("\n".join(parts))
    return 0


def cmd_scenarios(a):
    got = ["\n".join(e) for e in items_of(body(read(a.intake), "Verification scenarios"))
           if re.match(r"- SC-\d+\s*:", e[0]) and not empty(e[0].split(":", 1)[1])]
    print("\n".join(got) if got else "none")
    return 0


def lock(path):
    lockfile = path + ".lock"
    deadline = time.time() + LOCK_WAIT
    while True:
        try:
            os.close(os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return lockfile
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lockfile) > LOCK_STALE:
                    os.remove(lockfile)
                    continue
            except OSError:
                pass
        except PermissionError:
            pass  # Windows: the lock file is being deleted right now
        if time.time() > deadline:
            raise TimeoutError(f"lock {lockfile} held for over {LOCK_WAIT:g} s")
        time.sleep(0.02)


def cmd_iface_add(a):
    folder = os.path.dirname(os.path.abspath(a.file))
    os.makedirs(folder, exist_ok=True)
    line = (f"- {now()} {norm_unit(a.unit)} {a.iface.strip()}: {' '.join(a.change.split())} "
            f"(reason: {' '.join(a.reason.split())})")
    try:
        held = lock(a.file)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        fd = os.open(a.file, os.O_APPEND | os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0))
        try:
            lead = b""
            size = os.fstat(fd).st_size
            if size:
                with open(a.file, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    lead = b"" if fh.read(1) == b"\n" else b"\n"
            os.write(fd, lead + (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    finally:
        try:
            os.remove(held)
        except OSError:
            pass
    print(line)
    return 0


def cmd_iface_show(a):
    lines = []
    if os.path.exists(a.file):
        lines = [ln for ln in read(a.file).split("\n") if ln.strip()]
    print("\n".join(lines) if lines else "none")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="intake.py", description="The run's intake (see the module docstring).")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new")
    s.add_argument("runs")
    s.add_argument("slug")
    s.add_argument("--prompt-file", required=True)
    s.set_defaults(fn=cmd_new)
    for name, fn in (("check", cmd_check), ("freeze", cmd_freeze), ("verify", cmd_verify),
                     ("scenarios", cmd_scenarios)):
        s = sub.add_parser(name)
        s.add_argument("intake")
        s.set_defaults(fn=fn)
    s = sub.add_parser("asked")
    s.add_argument("intake")
    s.add_argument("--count", type=int, required=True)
    s.set_defaults(fn=cmd_asked)
    s = sub.add_parser("defaults")
    s.add_argument("intake")
    s.add_argument("--question", required=True)
    s.add_argument("--took", required=True)
    s.set_defaults(fn=cmd_defaults)
    s = sub.add_parser("set-mode")
    s.add_argument("intake")
    s.add_argument("mode")
    s.add_argument("--reason", required=True)
    s.set_defaults(fn=cmd_set_mode)
    s = sub.add_parser("to-plan")
    s.add_argument("intake")
    s.add_argument("plan")
    s.set_defaults(fn=cmd_to_plan)
    s = sub.add_parser("amend")
    s.add_argument("intake")
    s.add_argument("--reason", required=True)
    s.add_argument("--text", required=True)
    s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("unit")
    s.add_argument("intake")
    s.add_argument("id")
    s.set_defaults(fn=cmd_unit)
    s = sub.add_parser("iface")
    isub = s.add_subparsers(dest="icmd", required=True)
    t = isub.add_parser("add")
    t.add_argument("file")
    t.add_argument("--unit", required=True)
    t.add_argument("--iface", required=True)
    t.add_argument("--change", required=True)
    t.add_argument("--reason", required=True)
    t.set_defaults(fn=cmd_iface_add)
    t = isub.add_parser("show")
    t.add_argument("file")
    t.set_defaults(fn=cmd_iface_show)
    return p


def main(argv):
    a = parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '→' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
