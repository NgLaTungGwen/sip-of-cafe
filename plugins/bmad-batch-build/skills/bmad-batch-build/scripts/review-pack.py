"""The scored review of a whole branch, cut small for its reviewers.

usage: python review-pack.py pack <worktree> <since> --out <dir> [--split <split.json>]
                                  [--max-lines 1200] [--exclude <glob>]...
       python review-pack.py rules <worktree>
       python review-pack.py score <findings> --owners <owners.json> [--units 1,2,3]
       python review-pack.py table <findings> --owners <owners.json> --unit <id>

`pack` writes one diff per reviewable file that changed in <since>..HEAD into
<dir>/files/, and groups the files for reviewers: one group per unit when a
split file is given (each file goes to the last unit, in dependency order,
that touched it: its PR holds the final content), otherwise per top-level
folder. Small units share a group and a big one is cut by file, so a group stays
under --max-lines changed lines. A reviewer gets one group file and reads each
diff from disk. An --out folder named review-<k> is round k: pack refuses
k above 2, so the review never runs a third round. It never gets one big patch: in review-workspace-pr-size,
reviewers ran `cat` and `head -c 60000` on 30 KB patches, some of them twice.
Lock files, `_bmad-output/`, snapshots, minified files, source maps, binaries
and every --exclude glob are left out and listed in <dir>/index.md. Prints:

    PACK <n> files, <lines> lines, <k> groups, <x> excluded: <dir>/index.md
    GROUP 1 U1 U2: 6 files, 840 lines: <dir>/group-1.md

and writes <dir>/owners.json ({path: unit id or null}) for `score`. A second
round packs only the fixes: <since> is the head the first round reviewed.

`rules` prints the review method and project rules that the xt-dev-tools
review-pr skill would use, when it is installed: `METHOD <path>` (its
references/review-method.md, newest version) and `RULES <path>` (the user's
~/.xt-skills/review-pr/projects/<repo-id>.md for the origin remote), or
`METHOD none` / `RULES none`.

`score` reads the findings file the review rounds keep, one finding per line:

    <id> <open|fixed|rejected> <critical|major|minor|suggestion> <location> | <issue> | <fix>

<location> runs up to the first ` | ` and is `<path>:<line>`, `pr:<unit id>` (that unit's PR description)
or `run` (the whole run). Lines starting with `#` are ignored. A finding
counts against the unit that owns its file (owners.json) while it is open.
The score starts at 100: critical -30, major -12, minor -4, suggestion -1
(at most -5 in all), the review-pr scale. Prints `SCORE all <n> (...)`, then
`SCORE U<id> <n> (...)` for each unit (--units adds units with no finding).
A finding whose file has no owner, and a `run` finding, count only in `all`.

`table` prints the unit's findings as a markdown table, for its PR
description, or `none`.

Exit codes: 0 done, 2 bad arguments or files.
"""
import argparse
import fnmatch
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

NOISE = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
         "Cargo.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "go.sum", "composer.lock", "Gemfile.lock",
         "packages.lock.json")
NOISE_GLOBS = ("_bmad-output/*", "*/_bmad-output/*", "*.snap", "*.min.js", "*.min.css", "*.map")
WEIGHT = {"critical": 30, "major": 12, "minor": 4, "suggestion": 1}
SUGGESTION_CAP = 5
MAX_ROUNDS = 2  # the scored review runs at most two rounds; a third is refused
ROUND_DIR = re.compile(r"^review-(\d+)$")
LARGE_DIFF = 600  # diff lines above which a reviewer reads the file in parts
# The location runs up to the first " | ", so a path may hold spaces.
FINDING = re.compile(r"^(\S+)\s+(open|fixed|rejected)\s+(critical|major|minor|suggestion)\s+(.+?)\s+\|\s*(.*)$")


def git(wt, *args):
    r = subprocess.run(["git", "-c", "core.quotePath=false", "-C", wt, *args], capture_output=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def numstat(wt, since, head="HEAD"):
    """[(path, added, deleted)] of the files changed since..head; added is None for a binary."""
    out, rows = git(wt, "diff", "--numstat", "-z", "--no-renames", since, head), []
    for rec in out.split("\0"):
        if not rec.strip():
            continue
        added, deleted, path = rec.split("\t", 2)
        rows.append((path, None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return rows


def noise(path, extra):
    base = os.path.basename(path)
    return base in NOISE or any(fnmatch.fnmatch(path, g) for g in (*NOISE_GLOBS, *extra))


def split_owners(wt, split_path):
    """{path: unit id}: the last unit in dependency order that touched the path."""
    spec = importlib.util.spec_from_file_location("split_prs", os.path.join(os.path.dirname(__file__), "split-prs.py"))
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    with open(split_path, encoding="utf-8") as f:
        split = json.load(f)
    left_out = {int(x) for x in split.get("left_out") or []}
    units = {int(u["id"]): u for u in split["units"] if int(u["id"]) not in left_out and u.get("from") and u.get("to")}
    order = sp.topo({u: [int(d) for d in units[u].get("depends_on") or [] if int(d) in units] for u in units})
    owners = {}
    for uid in order:
        for p in sp.lines(git(wt, "diff", "--name-only", "--no-renames", units[uid]["from"], units[uid]["to"])):
            owners[p] = uid
    return owners


def groups_of(files, max_lines):
    """files: [(path, lines, owner key)], in owner order. Groups of whole owners
    up to max_lines; an owner above it is cut by file."""
    buckets = {}
    for f in files:
        buckets.setdefault(f[2], []).append(f)
    out, cur, size = [], [], 0
    for key in buckets:
        b, n = buckets[key], sum(x[1] for x in buckets[key])
        if n > max_lines:
            if cur:
                out.append(cur)
                cur, size = [], 0
            part, psize = [], 0
            for x in b:
                if part and psize + x[1] > max_lines:
                    out.append(part)
                    part, psize = [], 0
                part.append(x)
                psize += x[1]
            out.append(part)
            continue
        if cur and size + n > max_lines:
            out.append(cur)
            cur, size = [], 0
        cur += b
        size += n
    if cur:
        out.append(cur)
    return out


def label(key):
    return f"U{key}" if isinstance(key, int) else str(key)


def cmd_pack(a):
    m = ROUND_DIR.match(os.path.basename(os.path.normpath(a.out)))
    if m and int(m.group(1)) > MAX_ROUNDS:
        raise ValueError(f"round {m.group(1)} refused: the scored review stops after {MAX_ROUNDS} rounds; "
                         "put the findings still open under ## Open review findings")
    since = git(a.worktree, "rev-parse", a.since).strip()
    head = git(a.worktree, "rev-parse", "HEAD").strip()
    owners = split_owners(a.worktree, a.split) if a.split else {}
    keep, dropped = [], []
    for path, added, deleted in numstat(a.worktree, since, head):
        if added is None or noise(path, a.exclude):
            dropped.append(path)
            continue
        if a.split:
            key = owners.get(path, "synthesis")
        else:
            key = path.split("/", 1)[0] if "/" in path else "(root)"
        keep.append((path, added + deleted, key))
    order = sorted({k for _, _, k in keep}, key=lambda k: (not isinstance(k, int), k if isinstance(k, int) else 0, str(k)))
    keep.sort(key=lambda f: (order.index(f[2]), f[0]))
    os.makedirs(os.path.join(a.out, "files"), exist_ok=True)
    # A pack into a used folder must not leave an earlier round's groups or diffs behind.
    for old in glob.glob(os.path.join(a.out, "group-*.md")) + glob.glob(os.path.join(a.out, "files", "*.diff")):
        os.remove(old)
    diff_of = {}
    for i, (path, n, key) in enumerate(keep, 1):
        dp = os.path.join(a.out, "files", f"{i:03d}.diff")
        text = git(a.worktree, "diff", "-U5", "--no-renames", since, head, "--", path)
        with open(dp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        diff_of[path] = (dp, text.count("\n"))
    groups = groups_of(keep, a.max_lines)
    with open(os.path.join(a.out, "owners.json"), "w", encoding="utf-8") as f:
        json.dump({p: (owners.get(p) if a.split else None) for p, _, _ in keep}, f, indent=1, ensure_ascii=False)
    index = [f"# Review pack {since[:8]}..{head[:8]}", "",
             f"{len(keep)} files, {sum(f[1] for f in keep)} changed lines, {len(groups)} groups.", ""]
    for g, files in enumerate(groups, 1):
        keys = " ".join(dict.fromkeys(label(f[2]) for f in files))
        lines = [f"# Group {g}: {keys}", "",
                 f"Range {since}..{head}. Read every diff below in full with the Read tool, one file at a time;",
                 "open a source file only where a diff needs its context.", ""]
        for path, n, key in files:
            dp, dl = diff_of[path]
            big = f"; {dl} lines, read it in parts with offset/limit" if dl > LARGE_DIFF else ""
            lines.append(f"- `{path}` ({n} changed, {label(key)}{big}): {dp}")
        gp = os.path.join(a.out, f"group-{g}.md")
        with open(gp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        index.append(f"- Group {g} ({keys}): {len(files)} files, {sum(f[1] for f in files)} lines: {gp}")
    if dropped:
        index += ["", "Left out (lock, generated, binary or excluded):"] + [f"- `{p}`" for p in dropped]
    ip = os.path.join(a.out, "index.md")
    with open(ip, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(index) + "\n")
    print(f"PACK {len(keep)} files, {sum(f[1] for f in keep)} lines, {len(groups)} groups, "
          f"{len(dropped)} excluded: {ip}")
    for g, files in enumerate(groups, 1):
        keys = " ".join(dict.fromkeys(label(f[2]) for f in files))
        print(f"GROUP {g} {keys}: {len(files)} files, {sum(f[1] for f in files)} lines: "
              f"{os.path.join(a.out, f'group-{g}.md')}")
    return 0


def repo_id(url):
    """review-pr's repo-id for a remote URL: host__org__project__repo (Azure DevOps) or host__owner__repo."""
    url = re.sub(r"\.git$", "", url.strip())
    m = re.match(r"(?:https?://)?(?:[^@/]+@)?(dev\.azure\.com)/([^/]+)/([^/]+)/_git/([^/]+)", url)
    if m:
        return "__".join(m.groups()).lower()
    m = re.match(r"(?:https?://)?(?:[^@/]+@)?([^/:]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/([^/]+)", url)
    if m:
        return "__".join(("dev.azure.com",) + m.groups()).lower()
    m = re.match(r"(?:ssh://)?git@ssh\.dev\.azure\.com:(?:v3/)?([^/]+)/([^/]+)/([^/]+)", url)
    if m:
        return "__".join(("dev.azure.com",) + m.groups()).lower()
    m = re.match(r"(?:https?://|ssh://)?(?:[^@/]+@)?([^/:]+)[:/]([^/]+)/([^/]+)$", url)
    if m:
        return "__".join(m.groups()).lower()
    return None


def version_key(path):
    m = re.search(r"/(\d+(?:\.\d+)*)/skills/", path.replace("\\", "/"))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def cmd_rules(a):
    home = os.path.expanduser("~")
    methods = glob.glob(os.path.join(home, ".claude", "plugins", "cache", "*", "*", "*", "skills", "review-pr",
                                     "references", "review-method.md"))
    print(f"METHOD {max(methods, key=version_key) if methods else 'none'}")
    try:
        url = git(a.worktree, "remote", "get-url", "origin")
    except RuntimeError:
        url = ""
    rid = repo_id(url) if url else None
    path = os.path.join(home, ".xt-skills", "review-pr", "projects", f"{rid}.md") if rid else ""
    print(f"RULES {path if path and os.path.isfile(path) else 'none'}")
    return 0


def read_findings(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = FINDING.match(line)
            if not m:
                raise ValueError(f"{path}:{n}: not a finding line: {line[:80]}")
            fid, status, sev, loc, rest = m.groups()
            parts = [p.strip() for p in rest.split("|")]
            out.append({"id": fid, "status": status, "severity": sev, "location": loc,
                        "issue": parts[0] if parts else "", "fix": parts[1] if len(parts) > 1 else ""})
    return out


def unit_of(finding, owners):
    loc = finding["location"]
    m = re.fullmatch(r"pr:[Uu]?(\d+)", loc)
    if m:
        return int(m.group(1))
    if loc == "run":
        return None
    path = re.sub(r":\d+(?:-\d+)?$", "", loc)
    owner = owners.get(path)
    return int(owner) if owner is not None else None


def score(findings):
    open_ = [f for f in findings if f["status"] == "open"]
    count = {s: sum(1 for f in open_ if f["severity"] == s) for s in WEIGHT}
    lost = sum(WEIGHT[s] * count[s] for s in ("critical", "major", "minor")) + min(SUGGESTION_CAP, count["suggestion"])
    return max(0, 100 - lost), count


def summary(count, fixed):
    open_ = ", ".join(f"{count[s]} {s}" for s in WEIGHT if count[s])
    return f"open: {open_ or 'none'}; fixed {fixed}"


def load_owners(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cmd_score(a):
    findings, owners = read_findings(a.findings), load_owners(a.owners)
    s, c = score(findings)
    print(f"SCORE all {s} ({summary(c, sum(f['status'] == 'fixed' for f in findings))})")
    per = {}
    for f in findings:
        u = unit_of(f, owners)
        if u is not None:
            per.setdefault(u, []).append(f)
    for u in (int(x.strip().lstrip("Uu")) for x in a.units.split(",") if x.strip()):
        per.setdefault(u, [])
    for u in sorted(per):
        s, c = score(per[u])
        print(f"SCORE U{u} {s} ({summary(c, sum(f['status'] == 'fixed' for f in per[u]))})")
    return 0


def table_lines(findings, owners, uid):
    """The unit's open and fixed findings as markdown table lines; [] when it has none."""
    mine = [f for f in findings if unit_of(f, owners) == uid and f["status"] != "rejected"]
    if not mine:
        return []
    out = ["| # | Severity | Location | Issue | Fix | Status |", "|---|----------|----------|-------|-----|--------|"]
    for f in mine:
        cells = [f["id"], f["severity"], f"`{f['location']}`", f["issue"], f["fix"], f["status"]]
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return out


def cmd_table(a):
    rows = table_lines(read_findings(a.findings), load_owners(a.owners), int(re.sub(r"^[Uu]", "", a.unit)))
    print("\n".join(rows) if rows else "none")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="review-pack.py", description="See the module docstring.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pack")
    s.add_argument("worktree")
    s.add_argument("since")
    s.add_argument("--out", required=True)
    s.add_argument("--split")
    s.add_argument("--max-lines", type=int, default=1200)
    s.add_argument("--exclude", action="append", default=[])
    s.set_defaults(fn=cmd_pack)
    s = sub.add_parser("rules")
    s.add_argument("worktree")
    s.set_defaults(fn=cmd_rules)
    for name, fn in (("score", cmd_score), ("table", cmd_table)):
        s = sub.add_parser(name)
        s.add_argument("findings")
        s.add_argument("--owners", required=True)
        if name == "score":
            s.add_argument("--units", default="")
        else:
            s.add_argument("--unit", required=True)
        s.set_defaults(fn=fn)
    return p


def main(argv):
    a = parser().parse_args(argv)
    if getattr(a, "max_lines", 1) < 1:
        print("--max-lines must be at least 1", file=sys.stderr)
        return 2
    try:
        return a.fn(a)
    except (RuntimeError, ValueError, OSError, KeyError) as e:
        print(f"review-pack: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
