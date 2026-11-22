"""Cut a verified integration branch into one PR branch per unit
(bmad-batch-build-split).

usage: python split-prs.py stacks <plan.json>
       python split-prs.py cut <split.json> [--assign <path>=<unit> ...]
                               [--message "<template>"] [--footer "<line>"]
                               [--replace] [--dry-run]

`stacks` reads the run's plan file (plan-waves.py's format) and prints the PR
shape the dependencies give, for the approval table: one line per unit,
`PR <id> -> base` or `PR <id> -> on <id>`, then a `PRS:` summary line.

`cut` reads the split file, written by the orchestrator while the run goes
(<slug>-split.json in the run folder):

    {"worktree": "<iwt>", "ibranch": "feat/x", "base": "<sha of origin/main>",
     "base_branch": "main",
     "units": [
       {"id": 1, "title": "chart kit", "from": "<sha>", "to": "<sha>",
        "depends_on": [], "pr_branch": "feat/x-pr1"},
       {"id": 3, "title": "cost chart", "from": "<sha>", "to": "<sha>",
        "depends_on": [1]}
     ],
     "left_out": [2]}

`base` is the commit the run started from (a sha, so a later fetch doesn't
move it; a ref name works too), `base_branch` the branch the first PR of each
stack targets (default: `base` without `origin/`).
`from`..`to` is the unit's own work: for a lane unit the head before its
worker and the head after its gate; for a unit of a parallel wave the wave's
base and its branch tip. `pr_branch` defaults to `<ibranch>-pr<id>`.

Every file the integration branch changed since the base is given to the units
that touched it in their own range. A file only synthesis touched (a contract
sync output, a repair outside every unit) is UNOWNED until `--assign` names its
unit. Units are grouped into stacks: units joined by depends_on, or by a file
both touched, form one stack, ordered by their dependencies (ties by id). The
first PR of a stack targets the base branch, each later one the PR branch
before it, so every PR shows only its own unit. In a stack, the last unit that
touched a file takes the integration branch's content, an earlier one the
content at its own `to`; so repairs synthesis made land in the PR of the unit
that owns the file.

Each PR branch is built in the worktree, which must be clean and on the
integration branch, as one commit per unit (`--message`, default
"{type}: {title}" where type is the integration branch's prefix; `{id}` also
works; `--footer` adds a trailer line). Then it checks that nothing was lost:
every stack's tip matches the integration branch on its stack's files and the
base on every other file. The worktree goes back to the integration branch.

The result is written into the split file (`pr_branch`, `target`, `stack`,
`files`) and
printed:

    PR 1 feat/x-pr1 -> main (3 files)
    PR 3 feat/x-pr3 -> feat/x-pr1 (5 files)
    STACKS: 1 -> 3 | 4
    CHECK ok

`describe` writes the parts of each PR's description that the split decides,
after `cut`, so no reviewer finds them wrong (review-workspace-pr-size: most
first-round findings on its 23 PRs were an AC claimed as met by a PR that held
only part of it, a missing stack note, or an unexplained size):

    python split-prs.py describe <split.json> --intake <intake> --out <dir>
                       [--pr-ids 1=930,3=934] [--budget 400]
                       [--review <findings> --owners <owners.json> --reviewed-at <sha>]

For each PR it writes <dir>/pr<id>.md with `## Acceptance criteria` (every AC
of the unit, marked `here` when no other unit lists it, else `part here; rest
in U<x> (!<pr id>)`), `## Stack` (its place and the stack's merge order),
`## Size` (changed lines without lock and generated files, and a note when
over --budget) and, with --review, `## Review` (the unit's score from
review-pack.py and its findings table; a file goes to the last unit of its
stack whose `files` hold it, else to its owner in owners.json). Run it again with --pr-ids once the
PRs are open, to fill in their numbers. Prints `DESCRIBE U<id> <file>`.

Exit codes: 0 done, 2 UNOWNED files or a bad split file, 3 a PR branch already
exists (--replace deletes a local one that was never pushed; a pushed one is
never replaced, since that needs a force push), 5 REJECTED (a commit hook
refused the message; git's output follows; run again with --replace and a
--message the hook accepts), 6 CHECK failed.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

CHUNK = 80  # paths per git call, under the Windows command-line limit


def git(wt, *args, check=True):
    r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()}")
    return r


def lines(text):
    return [x for x in text.splitlines() if x.strip()]


def topo(units):
    """Unit ids in dependency order, ties by id."""
    ids = sorted(units)
    done, order = set(), []
    while len(order) < len(ids):
        ready = [u for u in ids if u not in done and all(d in done for d in units[u])]
        if not ready:
            raise ValueError("depends_on has a cycle: " + ", ".join(str(u) for u in ids if u not in done))
        order.append(ready[0])
        done.add(ready[0])
    return order


def stacks_of(order, deps, shared=()):
    """Stacks (lists in `order`) of units joined by a dependency or a shared file."""
    parent = {u: u for u in order}

    def find(u):
        while parent[u] != u:
            parent[u] = parent[parent[u]]
            u = parent[u]
        return u

    for u in order:
        for d in deps[u]:
            parent[find(u)] = find(d)
    for group in shared:
        group = list(group)
        for u in group[1:]:
            parent[find(u)] = find(group[0])
    out = {}
    for u in order:
        out.setdefault(find(u), []).append(u)
    return sorted(out.values(), key=lambda s: order.index(s[0]))


def stacks_cmd(path):
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    deps = {int(u["id"]): [int(d) for d in u.get("depends_on") or []] for u in plan["units"]}
    order = topo(deps)
    stacks = stacks_of(order, deps)
    for s in stacks:
        for i, u in enumerate(s):
            print(f"PR {u} -> " + ("base" if i == 0 else f"on {s[i - 1]}"))
    print(f"PRS: {len(order)} PRs in {len(stacks)} stack{'s' if len(stacks) != 1 else ''}: "
          + " | ".join(" -> ".join(str(u) for u in s) for s in stacks))


def exists_at(wt, rev, paths):
    """The subset of `paths` present in `rev`."""
    found = set()
    for i in range(0, len(paths), CHUNK):
        found.update(lines(git(wt, "ls-tree", "-r", "--name-only", rev, "--", *paths[i:i + CHUNK]).stdout))
    return found


def cut(path, assign, message, footer, replace, dry):
    with open(path, encoding="utf-8") as f:
        split = json.load(f)
    wt, ibranch, base = split["worktree"], split["ibranch"], split["base"]
    left_out = {int(x) for x in split.get("left_out") or []}
    units = {int(u["id"]): u for u in split["units"] if int(u["id"]) not in left_out}
    if not units:
        print("no unit to split", file=sys.stderr)
        return 2
    deps = {}
    for uid, u in units.items():
        ds = [int(d) for d in u.get("depends_on") or []]
        bad = [d for d in ds if d not in units]
        if bad:
            print(f"unit {uid} depends on {bad}, which is left out or unknown; leave it out too", file=sys.stderr)
            return 2
        deps[uid] = ds

    if lines(git(wt, "status", "--porcelain", "--untracked-files=no").stdout):
        print(f"{wt} has uncommitted changes; commit them first", file=sys.stderr)
        return 2
    head = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != ibranch:
        print(f"{wt} is on {head}, not {ibranch}", file=sys.stderr)
        return 2
    base_sha = git(wt, "merge-base", ibranch, base).stdout.strip()
    target_base = split.get("base_branch") or (base.split("/", 1)[1] if base.startswith("origin/") else base)

    touched = {uid: set(lines(git(wt, "diff", "--name-only", "--no-renames", u["from"], u["to"]).stdout))
               for uid, u in units.items()}
    changed = set(lines(git(wt, "diff", "--name-only", "--no-renames", base_sha, ibranch).stdout))
    owners = {}
    for uid, files in touched.items():
        for f in files:
            owners.setdefault(f, set()).add(uid)
    for a in assign:
        p, _, uid = a.rpartition("=")
        if not p or not uid.isdigit() or int(uid) not in units:
            print(f"bad --assign {a!r}: use <path>=<unit id of a unit in the split>", file=sys.stderr)
            return 2
        owners.setdefault(p.replace("\\", "/"), set()).add(int(uid))
    unowned = sorted(changed - set(owners))
    if unowned:
        for p in unowned:
            print(f"UNOWNED {p}")
        print("Give each one to a unit with --assign <path>=<unit>.", file=sys.stderr)
        return 2

    order = topo(deps)
    stacks = stacks_of(order, deps, [o for o in owners.values() if len(o) > 1])
    pos = {u: i for i, u in enumerate(order)}
    files_of = {u: set() for u in order}
    source = {}  # (unit, path) -> revision its content comes from
    for p, us in owners.items():
        last = max(us, key=pos.get)
        for u in us:
            files_of[u].add(p)
            source[(u, p)] = ibranch if u == last else units[u]["to"]

    plan = []
    for k, s in enumerate(stacks, 1):
        for i, u in enumerate(s):
            pb = units[u].get("pr_branch") or f"{ibranch}-pr{u}"
            units[u]["pr_branch"] = pb
            target = target_base if i == 0 else units[s[i - 1]]["pr_branch"]
            plan.append((u, pb, target, k))

    for u, pb, _, _ in plan:
        local = git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{pb}", check=False).returncode == 0
        remote = git(wt, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{pb}", check=False).returncode == 0
        if remote:
            print(f"EXISTS {pb} (pushed; never replaced, that needs a force push)")
            return 3
        if local and not replace:
            print(f"EXISTS {pb} (local; run again with --replace to rebuild it)")
            return 3

    kinds = {"feat", "fix", "chore", "refactor", "docs", "perf", "test", "build", "ci", "style"}
    # feat/<slug> or <user>/feat-<slug>
    rtype = next((c for c in (ibranch.split("/", 1)[0], ibranch.rsplit("/", 1)[-1].split("-", 1)[0]) if c in kinds),
                 "feat")
    for u, pb, target, k in plan:
        print(f"PR {u} {pb} -> {target} ({len(files_of[u])} file{'s' if len(files_of[u]) != 1 else ''})")
    print("STACKS: " + " | ".join(" -> ".join(str(u) for u in s) for s in stacks))
    if dry:
        return 0

    try:
        for u, pb, target, k in plan:
            parent = base_sha if target == target_base else target
            git(wt, "switch", "-q", "-C" if replace else "-c", pb, parent)
            by_src = {}
            for p in sorted(files_of[u]):
                by_src.setdefault(source[(u, p)], []).append(p)
            for src, paths in by_src.items():
                present = exists_at(wt, src, paths)
                keep = [p for p in paths if p in present]
                gone = [p for p in paths if p not in present]
                for i in range(0, len(keep), CHUNK):
                    git(wt, "checkout", src, "--", *keep[i:i + CHUNK])
                for i in range(0, len(gone), CHUNK):
                    git(wt, "rm", "-q", "--ignore-unmatch", "--", *gone[i:i + CHUNK])
            title = units[u].get("title", f"unit {u}")
            msg = message.format(type=rtype, title=title, id=u)
            if footer:
                msg += "\n\n" + footer
            r = git(wt, "commit", "-q", "--allow-empty", "-m", msg, check=False)
            if r.returncode != 0:
                git(wt, "reset", "-q", "--hard", check=False)
                git(wt, "switch", "-q", ibranch, check=False)
                print(f"REJECTED {pb}")
                print((r.stdout + r.stderr).strip())
                return 5
            units[u]["target"] = target
            units[u]["stack"] = k
            units[u]["files"] = sorted(files_of[u])
    finally:
        git(wt, "switch", "-q", ibranch, check=False)

    problems = []
    for s in stacks:
        tip = units[s[-1]]["pr_branch"]
        mine = set().union(*(files_of[u] for u in s))
        for p in sorted(lines(git(wt, "diff", "--name-only", "--no-renames", tip, ibranch).stdout)):
            if p in mine:
                problems.append(f"{tip}: {p} differs from {ibranch}")
        for p in sorted(lines(git(wt, "diff", "--name-only", "--no-renames", base_sha, tip).stdout)):
            if p not in mine:
                problems.append(f"{tip}: {p} is not this stack's file but differs from the base")
    covered = set().union(*files_of.values())
    problems += [f"{p}: in no PR" for p in sorted(changed - covered)]

    split["units"] = [units.get(int(u["id"]), u) for u in split["units"]]
    split["stacks"] = stacks
    with open(path, "w", encoding="utf-8") as f:
        json.dump(split, f, indent=2, ensure_ascii=False)
    if problems:
        print("CHECK failed")
        for p in problems:
            print("  " + p)
        return 6
    print("CHECK ok")
    return 0


def sibling(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(os.path.dirname(os.path.abspath(__file__)), file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def describe(argv):
    p = argparse.ArgumentParser(prog="split-prs.py describe")
    p.add_argument("split")
    p.add_argument("--intake", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--pr-ids", default="")
    p.add_argument("--budget", type=int, default=400)
    p.add_argument("--review")
    p.add_argument("--owners")
    p.add_argument("--reviewed-at", default="")
    a = p.parse_args(argv)
    if a.review and not a.owners:
        print("--review needs --owners (review-pack.py pack wrote it)", file=sys.stderr)
        return 2
    intake, pack = sibling("intake_mod", "intake.py"), sibling("review_pack", "review-pack.py")
    with open(a.split, encoding="utf-8") as f:
        split = json.load(f)
    if not split.get("stacks"):
        print("the split file has no stacks: run cut first", file=sys.stderr)
        return 2
    wt, base = split["worktree"], split["base"]
    units = {int(u["id"]): u for u in split["units"]}
    with open(a.intake, encoding="utf-8") as f:
        text = f.read()
    ac_text = intake.acs(text)
    acs_of = {int(u["id"][1:]): u["acs"] for u in intake.units(text) if u["id"][1:].isdigit()}
    pr_ids = {}
    for pair in filter(None, (x.strip() for x in a.pr_ids.split(","))):
        k, _, v = pair.partition("=")
        k, v = k.strip().lstrip("Uu"), v.strip().lstrip("!#")
        if not k.isdigit() or not v:
            print(f"bad --pr-ids entry {pair!r}: use <unit>=<pr id>", file=sys.stderr)
            return 2
        pr_ids[int(k)] = v
    in_prs = {u for s in split["stacks"] for u in s}

    def ref(u):
        return f"U{u} (!{pr_ids[u]})" if u in pr_ids else f"U{u} (`{units[u].get('pr_branch', '')}`)"

    findings = pack.read_findings(a.review) if a.review else None
    owners = pack.load_owners(a.owners) if a.review else {}
    # The cut placed every file, the ones only synthesis touched included: the
    # last unit of a stack that holds a file has its final content.
    for st in split["stacks"]:
        for u in st:
            for path in units[u].get("files") or []:
                owners[path] = u
    os.makedirs(a.out, exist_ok=True)
    for s in split["stacks"]:
        for k, u in enumerate(s):
            mine = acs_of.get(u, [])
            out = ["## Acceptance criteria"]
            for ac in mine:
                first = ac_text.get(ac, f"- {ac}: (not in the intake)").split("\n", 1)[0]
                text_of = re.sub(rf"^- {re.escape(ac)}\s*:\s*", "", first)
                others = [v for v in sorted(in_prs) if v != u and ac in acs_of.get(v, [])]
                where = "here" if not others else "part here; rest in " + ", ".join(ref(v) for v in others)
                out.append(f"- {ac} ({where}): {text_of}")
            if not mine:
                out.append("- none in the intake for this unit")
            target = units[u].get("target") or split.get("base_branch") or "base"
            out += ["", "## Stack"]
            if len(s) == 1:
                out.append(f"1 of 1: into `{target}`, nothing stacked on it.")
            else:
                after = f"merge after {ref(s[k - 1])}" if k else f"the first, into `{target}`"
                out.append(f"Part {k + 1} of {len(s)}: {after}. Merge order: "
                           + " -> ".join(ref(v) for v in s) + ".")
            parent = base if k == 0 else units[s[k - 1]]["pr_branch"]
            rows = [r for r in pack.numstat(wt, parent, units[u]["pr_branch"])
                    if r[1] is not None and not pack.noise(r[0], ())]
            n = sum(r[1] + r[2] for r in rows)
            out += ["", "## Size", f"{n} changed lines in {len(rows)} files (lock, generated and binary files left out)."]
            if n > a.budget:
                out.append(f"Over the {a.budget}-line budget: this is one unit of the split, the smallest part that "
                           f"delivers {', '.join(mine) or 'its change'} and passes its checks on its own.")
            if findings is not None:
                ours = [f for f in findings if pack.unit_of(f, owners) == u]
                sc, count = pack.score(ours)
                at = f" at `{a.reviewed_at[:8]}`" if a.reviewed_at else ""
                fixed = sum(f["status"] == "fixed" for f in ours)
                out += ["", "## Review", f"Scored review of the integration branch{at} (review-pr scale): "
                        f"this PR's files score {sc}/100 ({pack.summary(count, fixed)})."]
                table = pack.table_lines(findings, owners, u)
                if table:
                    out += [""] + table
            path = os.path.join(a.out, f"pr{u}.md")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(out) + "\n")
            print(f"DESCRIBE U{u} {path}")
    return 0


def main(argv):
    if len(argv) >= 2 and argv[0] == "stacks":
        stacks_cmd(argv[1])
        return 0
    if argv and argv[0] == "describe":
        try:
            return describe(argv[1:])
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            print(f"split-prs: {e}", file=sys.stderr)
            return 2
    if len(argv) < 2 or argv[0] != "cut":
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    path, rest = argv[1], argv[2:]
    assign, message, footer, replace, dry = [], "{type}: {title}", "", False, False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--assign":
            assign.append(rest[i + 1]); i += 1
        elif a == "--message":
            message = rest[i + 1]; i += 1
        elif a == "--footer":
            footer = rest[i + 1]; i += 1
        elif a == "--replace":
            replace = True
        elif a == "--dry-run":
            dry = True
        else:
            print(f"unknown argument {a}", file=sys.stderr)
            return 2
        i += 1
    try:
        return cut(path, assign, message, footer, replace, dry)
    except (RuntimeError, ValueError, KeyError) as e:
        print(f"split-prs: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
