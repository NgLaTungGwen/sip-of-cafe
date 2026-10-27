"""The project profile: the commands this repository builds, tests and installs
with, so the skill adapts to it instead of assuming hive-factory's layout.

usage: python profile.py detect <root> [--write]   draft a profile from the files
       python profile.py check  <root>             "PROFILE ok|stale|unconfirmed|none ..."
       python profile.py show   <root>             summary for the approval table
       python profile.py confirm <root>            mark it confirmed (after approval)
       python profile.py get <root or worktree> <key>
       python profile.py set <root> <key[.field]> <JSON value>   change one field
                                                (e.g. set . suite '{"dir": ".", "cmd": "npm test"}')

The profile lives at <root>/.claude/bmad-batch-project.json:

    {"base": "main",
     "install":  [{"dir": "app", "cmd": "npm ci --no-audit --no-fund --loglevel=error"}],
     "checks":   [{"name": "app-typecheck", "dir": "app", "cmd": "npm run typecheck"}],
     "suite":    {"dir": "app", "cmd": "npm run test"},
     "targeted": {"dir": "app", "test": "npx vitest run <files> --reporter=dot",
                  "typecheck": "npm run typecheck"},
     "after_merge": [],
     "database": null,
     "notes": "", "confirmed": "2026-10-26", "fingerprint": "..."}

Every command is a plain string, so a stack detect does not know (Python, Go,
.NET...) is handled by writing its commands in by hand. The scripts use the
profile when it exists and keep their original behaviour when it does not.

`get` prints a string as is, a {dir, cmd} list as "dir<TAB>cmd" lines (checks
as "name<TAB>dir<TAB>cmd"), and an
object's fields as "key<TAB>value" lines; it exits 1 when there is no profile
or no such key, which tells a script to fall back.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import subprocess
import sys

NAME = os.path.join(".claude", "bmad-batch-project.json")
LOCKS = [("bun.lock", "bun"), ("bun.lockb", "bun"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
         ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm")]
INSTALL = {"npm": "npm ci --no-audit --no-fund --loglevel=error", "bun": "bun install --frozen-lockfile",
           "pnpm": "pnpm install --frozen-lockfile", "yarn": "yarn install --frozen-lockfile"}
SKIP = {"node_modules", ".git", ".claude", "_bmad", "_bmad-output", "dist", "build", ".venv", "vendor"}


def root_of(path):
    """The main checkout for a worktree (or the path itself)."""
    try:
        common = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        return os.path.dirname(common) if os.path.basename(common) == ".git" else path
    except (subprocess.CalledProcessError, FileNotFoundError):
        return path


def path_of(root):
    return os.path.join(root, NAME)


def load(root):
    p = path_of(root_of(root))
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def pm_of(d):
    return next((pm for f, pm in LOCKS if os.path.exists(os.path.join(d, f))), None)


def package_dirs(root):
    """Folders (relative, "." for the root) with their own lockfile, root first."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace("\\", "/")
        depth = 0 if rel == "." else rel.count("/") + 1
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")] if depth < 2 else []
        if pm_of(dirpath):
            found.append(rel)
    return sorted(found, key=lambda r: (r != ".", r))


def scripts_of(d):
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return {}, {}
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return pkg.get("scripts") or {}, deps


def workspace_members(d):
    """Member folders of an npm/yarn/bun workspace root, in declared order."""
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as fh:
            ws = json.load(fh).get("workspaces") or []
    except (OSError, ValueError):
        return []
    if isinstance(ws, dict):
        ws = ws.get("packages") or []
    out = []
    for pattern in ws:
        out += [m for m in sorted(glob.glob(os.path.join(d, pattern))) if os.path.exists(os.path.join(m, "package.json"))]
    return out


def base_branch(root):
    r = subprocess.run(["git", "-C", root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    for b in ("main", "master", "develop"):
        if subprocess.run(["git", "-C", root, "rev-parse", "--verify", "-q", f"origin/{b}"],
                          capture_output=True).returncode == 0:
            return b
    # No origin branch (no remote yet): the branch the checkout is on.
    r = subprocess.run(["git", "-C", root, "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or "main"


def node_dirs(root):
    """package_dirs, or the root alone when it has a package.json but no
    lockfile (a small project that never ran an install)."""
    dirs = package_dirs(root)
    if not dirs and os.path.exists(os.path.join(root, "package.json")):
        dirs = ["."]
    return dirs


def fingerprint(root):
    """Lockfiles and package scripts: when they change, the profile may be stale."""
    h = hashlib.sha256()
    for rel in node_dirs(root):
        d = os.path.join(root, rel)
        scripts, _ = scripts_of(d)
        h.update(f"{rel}|{pm_of(d)}|{json.dumps(scripts, sort_keys=True)}\n".encode())
    for f in ("pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml"):
        if os.path.exists(os.path.join(root, f)):
            h.update(f.encode())
    return h.hexdigest()[:16]


def detect(root):
    prof = {"base": base_branch(root), "install": [], "checks": [], "suite": None, "targeted": None,
            "after_merge": [], "database": None, "notes": "", "confirmed": None}
    dirs = node_dirs(root)
    for rel in dirs:
        d = os.path.join(root, rel)
        pm = pm_of(d) or "npm"
        scripts, deps = scripts_of(d)
        if pm_of(d):
            prof["install"].append({"dir": rel, "cmd": INSTALL[pm]})
        elif deps:  # no lockfile yet: npm ci would refuse
            prof["install"].append({"dir": rel, "cmd": "npm install --no-audit --no-fund --loglevel=error"})
        tag = "" if rel == "." else rel.replace("/", "-") + "-"
        for s in ("build", "typecheck", "lint", "test"):
            if s in scripts:
                prof["checks"].append({"name": f"{tag}{s}", "dir": rel, "cmd": f"{pm} run {s}"})
        if prof["suite"] is None and "test" in scripts:
            prof["suite"] = {"dir": rel, "cmd": f"{pm} run test"}
            # In a workspace monorepo the runner is declared by a member: the
            # targeted checks run in the first member that has it and a test script.
            if not ({"vitest", "jest"} & set(deps)):
                for m in workspace_members(d):
                    ms, md = scripts_of(m)
                    if "test" in ms and {"vitest", "jest"} & set(md):
                        d, deps, scripts = m, md, ms
                        rel = os.path.relpath(m, root).replace("\\", "/")
                        break
            if "vitest" in deps:
                test = "npx vitest run <files> --reporter=dot"
            elif "jest" in deps:
                test = "npx jest <files>"
            elif pm == "bun":
                test = "bun test <files>"
            else:  # node --test, mocha, ...: the test script with the files appended
                test = f"{pm} test -- <files>"
            tc = f"{pm} run typecheck" if "typecheck" in scripts else (
                "npx tsc --noEmit" if os.path.exists(os.path.join(d, "tsconfig.json")) else None)
            prof["targeted"] = {"dir": rel, "test": test, "typecheck": tc}
    # A few non-JavaScript stacks, at the root only; anything else is filled in by hand.
    if not dirs:
        if os.path.exists(os.path.join(root, "pyproject.toml")) or os.path.exists(os.path.join(root, "requirements.txt")):
            req = os.path.exists(os.path.join(root, "requirements.txt"))
            prof["install"].append({"dir": ".", "cmd": "pip install -r requirements.txt" if req else "pip install -e ."})
            prof["suite"] = {"dir": ".", "cmd": "pytest -q"}
            prof["targeted"] = {"dir": ".", "test": "pytest -q <files>", "typecheck": None}
        elif os.path.exists(os.path.join(root, "go.mod")):
            prof["suite"] = {"dir": ".", "cmd": "go test ./..."}
            prof["checks"] = [{"name": "vet", "dir": ".", "cmd": "go vet ./..."}]
            prof["targeted"] = {"dir": ".", "test": "go test <packages>", "typecheck": "go build ./..."}
        elif os.path.exists(os.path.join(root, "Cargo.toml")):
            prof["suite"] = {"dir": ".", "cmd": "cargo test"}
            prof["checks"] = [{"name": "check", "dir": ".", "cmd": "cargo check"}]
            prof["targeted"] = {"dir": ".", "test": "cargo test <name>", "typecheck": "cargo check"}
        elif glob.glob(os.path.join(root, "*.sln")):
            prof["install"].append({"dir": ".", "cmd": "dotnet restore"})
            prof["suite"] = {"dir": ".", "cmd": "dotnet test"}
            prof["checks"] = [{"name": "build", "dir": ".", "cmd": "dotnet build --no-restore"}]
            prof["targeted"] = {"dir": ".", "test": "dotnet test --filter <name>", "typecheck": "dotnet build --no-restore"}
        else:
            prof["notes"] = "No build files recognised: write the install, checks, suite and targeted commands in by hand."
    env = os.path.join(root, ".env")
    if os.path.exists(env) and any(line.startswith("DATABASE_URL=") for line in open(env, encoding="utf-8", errors="ignore")):
        prof["database"] = {"env": "DATABASE_URL", "per_worktree": False}
    prof["fingerprint"] = fingerprint(root)
    return prof


def save(root, prof):
    p = path_of(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(prof, fh, indent=2)
        fh.write("\n")
    return p


def show(prof):
    out = [f"base: {prof.get('base')}"]
    out += [f"install: {i['dir']}: {i['cmd']}" for i in prof.get("install") or []] or ["install: none"]
    out += [f"check {c['name']}: {c['dir']}: {c['cmd']}" for c in prof.get("checks") or []]
    s = prof.get("suite")
    out.append(f"full suite: {s['dir']}: {s['cmd']}" if s else "full suite: none")
    t = prof.get("targeted") or {}
    out.append(f"targeted: {t.get('dir')}: test `{t.get('test')}`, typecheck `{t.get('typecheck')}`" if t else "targeted: none")
    out += [f"after merge: {c['dir']}: {c['cmd']}" for c in prof.get("after_merge") or []]
    out.append(f"database: {json.dumps(prof.get('database'))}")
    if prof.get("notes"):
        out.append(f"notes: {prof['notes']}")
    return "\n".join(out)


def get(prof, key):
    v = prof
    for part in key.split("."):
        if not isinstance(v, dict) or v.get(part) is None:
            return None
        v = v[part]
    if isinstance(v, list):
        return "\n".join((f"{x['name']}\t" if "name" in x else "") + f"{x['dir']}\t{x['cmd']}"
                         if isinstance(x, dict) else str(x) for x in v)
    if isinstance(v, dict):
        return "\n".join(f"{k}\t{'' if x is None else x}" for k, x in v.items())
    return str(v)


def main(args):
    if len(args) < 2:
        print(__doc__)
        return 2
    cmd, root = args[0], os.path.abspath(args[1])
    if cmd == "detect":
        prof = detect(root)
        if "--write" in args:
            print(f"wrote {save(root, prof)}")
        print(show(prof))
        return 0
    prof = load(root)
    if cmd == "set":
        # Claude Code asks before an Edit to a file under .claude/, so the
        # orchestrator corrects the profile through here instead.
        if len(args) < 4:
            print("usage: profile.py set <root> <key[.field]> <JSON value>")
            return 2
        prof = prof if prof is not None else detect(root)
        try:
            value = json.loads(args[3])
        except ValueError:
            value = args[3]
        target, parts = prof, args[2].split(".")
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
        prof["confirmed"] = None
        save(root, prof)
        print(show(prof))
        return 0
    if cmd == "check":
        if prof is None:
            print("PROFILE none")
        elif prof.get("fingerprint") != fingerprint(root_of(root)):
            print("PROFILE stale: lockfiles or package scripts changed since it was written; re-detect and compare")
        elif not prof.get("confirmed"):
            print("PROFILE unconfirmed")
        else:
            print(f"PROFILE ok (confirmed {prof['confirmed']})")
        return 0
    if prof is None:
        return 1
    if cmd == "show":
        print(show(prof))
        return 0
    if cmd == "confirm":
        prof["confirmed"] = dt.date.today().isoformat()
        prof["fingerprint"] = fingerprint(root_of(root))
        save(root_of(root), prof)
        print(f"PROFILE confirmed {prof['confirmed']}")
        return 0
    if cmd == "get":
        v = get(prof, args[2])
        if v is None:
            return 1
        print(v)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
