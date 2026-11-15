"""What a change affects: which checks verify.sh runs, which tests a unit runs,
whether the test database answers, and the base branch's known failures.

usage: python affected.py select <worktree> --base <rev> [--full] [--no-test] [--only a,b]
                                [--apps] [--infra] [--separate apps,infra]  < rows
       python affected.py tests  <worktree> --base <rev>
       python affected.py db     <worktree>
       python affected.py known  <worktree> --base <rev> [<check> <note>]

`select` reads one "name<TAB>dir<TAB>kind" row per check on stdin (kind is
test, check or ratchet) and prints a "mode<TAB>diff|full|nodiff<TAB>why" line,
then one "select|skip<TAB>name<TAB>kind<TAB>why" line per row. A check is
selected when a code file under its dir changed since the merge base with
<rev>; "." covers every dir except the --separate ones. A change to docs, YAML
or scripts only selects no check (ratchets still run when YAML changed: the
pipeline's caps live there). A FORCE_FULL path selects everything, and so does
--full. With no diff to read (no git, no commit, unknown base) everything is
selected as before this script existed, rows under apps/ or infra/ only with
--apps or --infra.

`tests` prints what a unit runs instead of the full suite, one
"RUN<TAB>dir<TAB>cmd<TAB>why" line per command, or a single "NONE<TAB>why".
Vitest workspaces get `vitest run --changed <rev>`, jest ones
`jest --changedSince=<rev>`, bun ones the changed test files and the test
files beside the changed sources. A FORCE_FULL path, or a changed workspace
with no runner this knows, runs the full suite (the profile's, or `npm test`
at the root) and every bun workspace's `bun test`.

`db` exits 1 with a message when the project has a database (the profile's
"database", or a postgres DATABASE_URL in .env) and its port does not answer.

`known` lists the checks recorded as already failing on <rev>'s commit, or
records one: a failure synthesis has traced to the base, so it is triaged once
per base commit and not again by every wave. They are kept per base SHA, in
the common git dir, so every worktree of the run sees them.
"""
import fnmatch
import importlib.util
import json
import os
import socket
import subprocess
import sys
from urllib.parse import urlparse

# The skill's profile.py, loaded by path: `import profile` can find the
# standard library's profiler instead.
_spec = importlib.util.spec_from_file_location("bmad_profile", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                             "profile.py"))
prof_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prof_mod)

# Paths whose change can break tests anywhere, so a unit or a verify that
# touches one runs everything. The one list; verify.sh and the worker's test
# run both read it through this script.
FORCE_FULL = [
    "packages/contracts/*",          # the shared API contracts every workspace imports
    "*/migrations/*", "migrations/*",
    "*.prisma",
    "vitest.config.*", "*/vitest.config.*", "vitest.workspace.*", "*/vitest.workspace.*",
    "vite.config.*", "*/vite.config.*", "jest.config.*", "*/jest.config.*", "bunfig.toml", "*/bunfig.toml",
    "tsconfig*.json", "*/tsconfig*.json",
    "*vitest.setup.*", "*jest.setup.*", "*setupTests.*",
    "*/test/*setup*", "*/tests/*setup*", "test/*setup*", "tests/*setup*",
    "*/test/global-*", "*/tests/global-*", "test/global-*", "tests/global-*",
    "package.json", "*/package.json",
    ".claude/bmad-batch-project.json",
] + [p + n for n in [x for x, _ in prof_mod.LOCKS] + ["requirements*.txt", "pyproject.toml", "go.mod", "go.sum",
                                                    "Cargo.toml", "Cargo.lock"] for p in ("", "*/")]

# Paths that no test suite covers: docs, pipeline YAML, shell scripts, specs.
INERT = ["*.md", "*.mdx", "*.txt", "*.rst", "*.yml", "*.yaml", "*.sh", "*.ps1", "*.bat", "*.cmd",
         "docs/*", "_bmad-output/*", "LICENSE*", ".gitignore", "*/.gitignore", ".gitattributes",
         ".editorconfig", "CODEOWNERS", "*/CODEOWNERS"]

TEST_MARKS = (".test.", ".spec.")


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def kind_of(path):
    """force, inert or code."""
    if matches(path, FORCE_FULL):
        return "force"
    return "inert" if matches(path, INERT) else "code"


def git(wt, *args):
    r = subprocess.run(["git", "-C", wt, *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def changed_files(wt, base):
    """Files changed since the merge base with <base>, committed or not, untracked
    included; None when there is no diff to read."""
    if git(wt, "rev-parse", "--verify", "-q", "HEAD") is None:
        return None
    mb = git(wt, "merge-base", base, "HEAD")
    if not mb:
        return None
    diff = git(wt, "diff", "--name-only", mb.strip())
    new = git(wt, "ls-files", "--others", "--exclude-standard")
    if diff is None or new is None:
        return None
    seen = []
    for line in (diff + new).splitlines():
        p = line.strip().replace("\\", "/")
        if p and p not in seen:
            seen.append(p)
    return seen


def under(path, d):
    d = d.strip("/")
    return d in ("", ".") or path == d or path.startswith(d + "/")


def summary(files):
    return files[0] + (f" (+{len(files) - 1} more)" if len(files) > 1 else "")


def select(rows, files, *, full=False, notest=False, only=(), flags=(), separate=()):
    """[(select|skip, name, kind, why)] and the (mode, why) line."""
    if files is None:
        mode = ("nodiff", "no diff against the base to read: every check runs")
    else:
        forced = [f for f in files if kind_of(f) == "force"]
        if full:
            mode = ("full", "--full")
        elif forced:
            mode = ("full", f"a path that forces the full run changed: {summary(forced)}")
        else:
            mode = ("diff", f"{len(files)} files changed since the base")
    code = [f for f in files or [] if kind_of(f) == "code"]
    yaml = [f for f in files or [] if f.endswith((".yml", ".yaml"))]
    out = []
    for name, d, kind in rows:
        d = d or "."
        gated = next((g for g in ("apps", "infra") if under(d, g) and d not in (".", "")), None)
        if only:
            pick = (name in only, "--only" if name in only else "not in --only")
        elif notest and kind == "test":
            pick = (False, "--no-test")
        elif mode[0] == "nodiff":
            pick = (True, "no diff") if not gated or gated in flags else (False, f"no --{gated}")
        elif mode[0] == "full":
            pick = (True, mode[1] if mode[1] == "--full" else "full run")
        else:
            if d in (".", ""):
                hit = [f for f in code if not any(under(f, s) for s in separate)]
            else:
                hit = [f for f in code if under(f, d)]
            if hit:
                pick = (True, f"changed: {summary(hit)}")
            elif kind == "ratchet" and yaml:
                pick = (True, f"pipeline YAML changed: {summary(yaml)}")
            elif not code:
                pick = (False, "only docs, YAML or scripts changed")
            else:
                pick = (False, f"nothing under {d}/ changed" if d not in (".", "") else "nothing outside "
                        + ", ".join(s + "/" for s in separate) + " changed")
        out.append(("select" if pick[0] else "skip", name, kind, pick[1]))
    return mode, out


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def workspace_of(wt, path):
    """The deepest folder above <path> with a package.json, "." for the root,
    or None when none has one."""
    parts = path.split("/")[:-1]
    while parts:
        d = "/".join(parts)
        if os.path.exists(os.path.join(wt, d, "package.json")):
            return d
        parts.pop()
    return "." if os.path.exists(os.path.join(wt, "package.json")) else None


def runner_of(wt, d):
    pkg = read_json(os.path.join(wt, d, "package.json"))
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    # A bun workspace (hive-factory's apps/) is tested with bun, never npx,
    # even when it lists vitest. The apps runner choice is decided here.
    if prof_mod.pm_of(os.path.join(wt, d)) == "bun":
        return "bun"
    if "vitest" in deps or any(os.path.exists(os.path.join(wt, d, f"vitest.config.{x}"))
                               for x in ("ts", "mts", "js", "mjs")):
        return "vitest"
    if "jest" in deps:
        return "jest"
    if prof_mod.pm_of(os.path.join(wt, d)) == "bun":
        return "bun"
    return None


def is_test(path):
    name = path.rsplit("/", 1)[-1]
    return any(m in name for m in TEST_MARKS) or "/__tests__/" in "/" + path


def siblings(wt, path):
    """The test files beside a source file: <stem>.test.* and <stem>.spec.* in its
    folder and in its __tests__ folder."""
    folder, name = os.path.split(path)
    stem = name.split(".", 1)[0]
    out = []
    for sub in (folder, os.path.join(folder, "__tests__")):
        try:
            entries = sorted(os.listdir(os.path.join(wt, sub)))
        except OSError:
            continue
        for e in entries:
            if e.startswith(stem + ".") and any(m in e[len(stem):] for m in TEST_MARKS):
                out.append((sub + "/" + e).lstrip("/").replace("\\", "/"))
    return out


def full_suite(wt):
    """[(dir, cmd)]: the profile's suite or `npm test` at the root, and the
    `bun test` of every bun workspace the root suite does not reach."""
    p = prof_mod.load(wt)
    suite = (p or {}).get("suite") if p is not None else {"dir": ".", "cmd": "npm test"}
    out = [(suite.get("dir") or ".", suite["cmd"])] if suite and suite.get("cmd") else []
    for d in sorted(os.listdir(wt)) if os.path.isdir(wt) else []:
        if d not in prof_mod.SKIP and not d.startswith(".") and os.path.exists(os.path.join(wt, d, "package.json")) \
                and prof_mod.pm_of(os.path.join(wt, d)) == "bun" and all(x[0] != d for x in out):
            out.append((d, "bun test"))
    return out


def tests(wt, base, files):
    """[(RUN, dir, cmd, why)] or [(NONE, why)]."""
    def full(why):
        cmds = full_suite(wt)
        return [("RUN", d, c, why) for d, c in cmds] or [("NONE", f"{why}, but the project has no full suite")]
    if files is None:
        return full("no diff against the base to read")
    forced = [f for f in files if kind_of(f) == "force"]
    if forced:
        return full(f"a path that forces the full suite changed: {summary(forced)}")
    code = [f for f in files if kind_of(f) == "code"]
    if not code:
        return [("NONE", "only docs, YAML or scripts changed: no test is affected")]
    by_ws = {}
    for f in code:
        ws = workspace_of(wt, f)
        if ws is None:
            return full(f"{f} is in no JavaScript workspace")
        by_ws.setdefault(ws, []).append(f)
    out = []
    for ws, fs in sorted(by_ws.items()):
        r = runner_of(wt, ws)
        if r is None:
            return full(f"{ws}/ has no test runner this script knows")
        if r == "vitest":
            out.append(("RUN", ws, f"npx vitest run --changed {base} --passWithNoTests --reporter=dot",
                        f"{ws}/: tests related to {summary(fs)}"))
        elif r == "jest":
            out.append(("RUN", ws, f"npx jest --changedSince={base} --passWithNoTests",
                        f"{ws}/: tests related to {summary(fs)}"))
        else:
            rel = lambda p: p if ws == "." else p[len(ws) + 1:]  # noqa: E731
            picked = []
            for f in fs:
                for t in ([f] if is_test(f) else siblings(wt, f)):
                    if os.path.exists(os.path.join(wt, t)) and t not in picked:
                        picked.append(t)
            if picked:
                out.append(("RUN", ws, "bun test " + " ".join("./" + rel(t) for t in picked),
                            f"{ws}/: the changed test files and the ones beside {summary(fs)}"))
    return out or [("NONE", f"no test file is affected by {summary(code)}")]


def env_value(wt, name):
    try:
        with open(os.path.join(wt, ".env"), encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.environ.get(name)


def db(wt):
    """(ok, message)."""
    p = prof_mod.load(wt)
    var = "DATABASE_URL"
    has = False
    if p is not None and p.get("database"):
        has = True
        if isinstance(p["database"], dict) and p["database"].get("env"):
            var = p["database"]["env"]
    url = env_value(wt, var)
    if url and url.startswith(("postgres://", "postgresql://")):
        has = True
    if not has:
        return True, "database: none"
    if not url:
        return False, f"database: the profile lists one, but {var} is not set in {wt}/.env or the environment"
    u = urlparse(url)
    if not u.hostname:  # sqlite, a file path: no server to wait for
        return True, f"database: {u.scheme or 'local'}, no server to check"
    host = u.hostname
    host = "127.0.0.1" if host == "localhost" else host
    port = u.port or 5432
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True, f"database: ok {host}:{port}"
    except OSError as exc:
        return False, (f"database: DOWN, nothing answers at {host}:{port} ({var}: {exc.__class__.__name__}). "
                       "The database test suites would skip or fail without it, so verify fails instead. "
                       "Start the database (for example `docker compose up -d`) and run verify again.")


def known_dir(wt, base):
    common = git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir")
    sha = git(wt, "rev-parse", "--verify", "-q", base + "^{commit}")
    if not common or not sha:
        return None, None
    return os.path.join(common.strip(), "bmad", "verify-base", sha.strip()), sha.strip()


def opt(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main(args):
    if len(args) < 2:
        print(__doc__)
        return 2
    cmd, wt = args[0], args[1]
    base = opt(args, "--base", "origin/main")
    if cmd == "select":
        rows = [ln.rstrip("\r\n").split("\t") for ln in sys.stdin if ln.strip()]
        rows = [(r[0], r[1] if len(r) > 1 else ".", r[2] if len(r) > 2 else "check") for r in rows]
        split = lambda s: [x for x in (s or "").split(",") if x]  # noqa: E731
        mode, out = select(rows, changed_files(wt, base), full="--full" in args, notest="--no-test" in args,
                           only=split(opt(args, "--only")), separate=split(opt(args, "--separate")),
                           flags=[f for f in ("apps", "infra") if f"--{f}" in args])
        print("mode\t" + "\t".join(mode))
        for line in out:
            print("\t".join(line))
        return 0
    if cmd == "tests":
        for line in tests(wt, base, changed_files(wt, base)):
            print("\t".join(line))
        return 0
    if cmd == "db":
        ok, msg = db(wt)
        print(msg)
        return 0 if ok else 1
    if cmd == "known":
        d, sha = known_dir(wt, base)
        if d is None:
            return 0
        rest = [a for i, a in enumerate(args[2:], 2) if a != "--base" and args[i - 1] != "--base"]
        if len(rest) >= 2:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, rest[0] + ".known"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(" ".join(rest[1:]) + "\n")
            print(f"known on {base} ({sha[:10]}): {rest[0]}")
            return 0
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".known"):
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        print(f"{f[:-6]}\t{sha[:10]}\t{fh.read().strip()}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
