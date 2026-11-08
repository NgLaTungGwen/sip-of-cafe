"""Where the server-side pipelines of a run stand: the PR's validation checks
before the merge, and the deploy pipelines after it.

usage: python ci-status.py pr     <pr id> --root <worktree> [--wait <s>] [--ignore <name,...>]
       python ci-status.py deploy <pr id> --root <worktree> [--wait <s>] [--ignore <name,...>]

The provider comes from the origin remote of <root>: Azure DevOps (az) or
GitHub (gh).

- pr: the checks the PR runs. Azure DevOps: its Build policies (PR
  Validation); GitHub: `gh pr checks`.
- deploy: the pipelines that ran on the target branch for the PR's merge
  commit, once the PR has completed (deploys, release pipelines). The latest
  run of each pipeline whose commit contains the merge commit counts. A PR
  that is not merged yet prints `CI none`.

--wait polls every 30 s until nothing is pending, for at most <s> seconds
(default 0: look once). Keep it at 240 or less, so a wait stays under the
5-minute prompt cache, and call again while it prints pending. --ignore
names checks, stages, jobs or tasks that fail by design (the profile's
`ci.ignore`); a run whose failures are all under those names counts as
passed.

Prints one summary line, then one line per check, then the log of each
failure:

    CI green|red|pending|approval|none <summary>
    CHECK <pass|fail|pending|approval|skip> <name> <url>
    LOG <name>: <file>

`approval` means a pipeline waits for a person to approve it (a deploy to a
protected environment): stop waiting and tell the user. The log files (on
Azure DevOps the failed tests the build published, with their messages and
stacks, then the error lines and the last lines of each failed step) go to the worktree's git
dir, <git dir>/bmad/ci-<name>.log, out of the tree. Exits 0 green or none,
1 red, 2 pending, 4 approval, 3 when the provider can't be read.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

ADO = ((r"https://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/", "https://dev.azure.com/{}"),
       (r"(?:git@)?ssh\.dev\.azure\.com:v3/([^/]+)/([^/]+)/", "https://dev.azure.com/{}"),
       (r"https://(?:[^@/]+@)?([^./]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/",
        "https://{}.visualstudio.com"))
GH = r"(?:https://(?:[^@/]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?/?$"
RANK = {"fail": 0, "approval": 1, "pending": 2, "pass": 3, "skip": 4}


class CliError(Exception):
    pass


def cli(*args, root):
    """Run az or gh in <root> and return stdout; tests replace this."""
    exe = shutil.which(args[0]) or shutil.which(args[0] + ".cmd") or args[0]
    r = subprocess.run([exe, *args[1:]], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=root)
    if r.returncode != 0 and not (args[:3] == ("gh", "pr", "checks") and r.stdout.strip()):
        # gh pr checks exits 8 while checks are pending and 1 when one failed, with the JSON on stdout.
        raise CliError(f"{' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout.strip()


def provider(root):
    url = git(root, "remote", "get-url", "origin")
    for pattern, org in ADO:
        m = re.match(pattern, url)
        if m:
            return "az", {"org": org.format(m.group(1)), "project": urllib.parse.unquote(m.group(2))}
    m = re.match(GH, url)
    if m:
        return "gh", {"repo": f"{m.group(1)}/{m.group(2)}"}
    return None, {}


def ignored(name, ignore):
    return any(i and i.lower() in (name or "").lower() for i in ignore)


def log_file(root, name):
    d = os.path.join(subprocess.run(["git", "-C", root, "rev-parse", "--absolute-git-dir"],
                                    capture_output=True, text=True).stdout.strip(), "bmad")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ci-" + re.sub(r"[^\w.-]+", "-", name).strip("-")[:60] + ".log")


def excerpt(lines):
    """The error lines and the last 60 lines of a failed step's log."""
    errors = [x for x in lines if re.search(r"##\[error\]|\berror\b|FAIL|Error:", x, re.I)][-30:]
    return "\n".join(["--- error lines ---", *errors, "--- last lines ---", *lines[-60:]])


# Azure DevOps

def az_invoke(root, ctx, resource, **params):
    route = [f"{k}={v}" for k, v in {"project": ctx["project"], **params}.items()]
    out = cli("az", "devops", "invoke", "--org", ctx["org"], "--area", "build", "--resource", resource,
              "--route-parameters", *route, "--api-version", "7.1", "-o", "json", root=root)
    return json.loads(out or "{}")


def az_failed_tests(root, ctx, build_id, records, failed, fetched):
    """The failed test results a build published: file, test name, message and
    the top of the stack. A step's log shows only the end of vitest's junit
    output, which names no failing test (sonar-zero-5 synthesis fetched these by
    hand). The test run ids come from the Publish Test Results tasks' logs
    ("Test run id:1177"): listing a build's runs through `az devops invoke
    --area test --resource runs` resolves to Runs/Statistics and 404s. Only the
    publish tasks in a job that has a failed task are read, reusing the logs
    already fetched (<fetched>: log id -> lines), because each az devops invoke
    call takes about two minutes. Best effort: a failure to read them never
    hides the build's own log."""
    out = []
    jobs = {r.get("parentId") for r in failed}
    try:
        run_ids = []
        for r in records:
            if r.get("type") != "Task" or "test result" not in (r.get("name") or "").lower():
                continue
            log_id = (r.get("log") or {}).get("id")
            if not log_id or r.get("parentId") not in jobs:
                continue
            if log_id not in fetched:
                fetched[log_id] = az_invoke(root, ctx, "logs", buildId=build_id, logId=log_id).get("value", [])
            for line in fetched[log_id]:
                m = re.search(r"[Tt]est run id:\s*(\d+)|test run '(\d+)'", line)
                if m and (m.group(1) or m.group(2)) not in run_ids:
                    run_ids.append(m.group(1) or m.group(2))
        for run_id in run_ids[:5]:
            res = json.loads(cli("az", "devops", "invoke", "--org", ctx["org"], "--area", "test", "--resource", "results",
                                 "--route-parameters", f"project={ctx['project']}", f"runId={run_id}",
                                 "--query-parameters", "outcomes=Failed", "$top=30",
                                 "--api-version", "7.1", "-o", "json", root=root) or "{}").get("value") or []
            for r in res:
                name = r.get("automatedTestName") or r.get("testCaseTitle") or "?"
                where = r.get("automatedTestStorage") or ""
                msg = (r.get("errorMessage") or "").strip()[:800]
                stack = "\n".join((r.get("stackTrace") or "").strip().splitlines()[:8])
                out.append(f"FAILED {where}: {name}\n{msg}\n{stack}".rstrip())
    except Exception:  # noqa: BLE001 - best effort, see above
        pass
    return "\n\n".join(out)


def az_build(root, ctx, build_id, name, url, ignore):
    """One build as a check: its state, and the log of each failed task that
    is not under an ignored stage, job or task."""
    tl = az_invoke(root, ctx, "timeline", buildId=build_id)
    recs = {r["id"]: r for r in tl.get("records") or []}

    def path(r):
        names = []
        while r:
            names.append(r.get("name") or "")
            r = recs.get(r.get("parentId"))
        return names

    if any(r.get("type") == "Checkpoint.Approval" and r.get("state") != "completed" for r in recs.values()):
        return "approval", None
    failed = [r for r in recs.values() if r.get("type") == "Task" and r.get("result") == "failed"]
    real = [r for r in failed if not any(ignored(n, ignore) for n in path(r))]
    if failed and not real:
        return "pass", None
    if not real:
        return None, None
    parts, fetched = [], {}
    for r in real[:3]:
        log_id = (r.get("log") or {}).get("id")
        lines = az_invoke(root, ctx, "logs", buildId=build_id, logId=log_id).get("value", []) if log_id else []
        if log_id:
            fetched[log_id] = lines
        issues = [i.get("message", "") for i in r.get("issues") or []]
        parts.append(f"=== {' / '.join(reversed(path(r)))}\n" + "\n".join(issues) + "\n" + excerpt(lines))
    tests = az_failed_tests(root, ctx, build_id, recs.values(), real, fetched)
    if tests:
        parts.insert(0, "=== failed tests (published test results)\n" + tests)
    return "fail", "\n\n".join(parts)


def az_pr(root, ctx, pr, ignore):
    evals = json.loads(cli("az", "repos", "pr", "policy", "list", "--id", str(pr), "--org", ctx["org"],
                           "-o", "json", root=root) or "[]")
    checks = []
    for e in evals:
        conf = e.get("configuration") or {}
        if (conf.get("type") or {}).get("displayName") != "Build":
            continue
        c = e.get("context") or {}
        name = (conf.get("settings") or {}).get("displayName") or c.get("buildDefinitionName") or "build"
        url = f"{ctx['org']}/{urllib.parse.quote(ctx['project'])}/_build/results?buildId={c.get('buildId')}"
        status = e.get("status")
        log = None
        if ignored(name, ignore) or status == "notApplicable":
            state = "skip"
        elif status in ("queued", "running") or c.get("buildIsNotCurrent"):
            state = "pending"
        elif status == "approved":
            state = "pass"
        elif c.get("isExpired"):
            state, log = "fail", (f"expired: requeue with az repos pr policy queue --id {pr} "
                                  f"--evaluation-id {e.get('evaluationId')}")
        elif c.get("buildId"):
            state, log = az_build(root, ctx, c["buildId"], name, url, ignore)
            state = state or "fail"
        else:
            state = "fail"
        checks.append((state, name, url, log))
    return checks


def az_deploy(root, ctx, pr, ignore):
    info = json.loads(cli("az", "repos", "pr", "show", "--id", str(pr), "--org", ctx["org"], "-o", "json", root=root))
    if info.get("status") != "completed":
        return None, f"PR {pr} is {info.get('status')}, not merged yet"
    merge = (info.get("lastMergeCommit") or {}).get("commitId")
    branch = info.get("targetRefName")
    runs = json.loads(cli("az", "pipelines", "runs", "list", "--org", ctx["org"], "-p", ctx["project"],
                          "--branch", branch, "--top", "50", "-o", "json", root=root) or "[]")
    latest = {}
    for r in runs:  # newest first
        name = (r.get("definition") or {}).get("name")
        if r.get("reason") == "pullRequest" or name in latest or not contains(root, r.get("sourceVersion"), merge):
            continue
        latest[name] = r
    checks = []
    for name, r in latest.items():
        url = f"{ctx['org']}/{urllib.parse.quote(ctx['project'])}/_build/results?buildId={r['id']}"
        log = None
        if ignored(name, ignore):
            state = "skip"
        elif r.get("status") != "completed":
            state = az_build(root, ctx, r["id"], name, url, ignore)[0] == "approval" and "approval" or "pending"
        elif r.get("result") in ("succeeded", "partiallySucceeded"):
            state = "pass"
        elif r.get("result") == "canceled":
            state = "skip"
        else:
            state, log = az_build(root, ctx, r["id"], name, url, ignore)
            state = state or "fail"
        checks.append((state, name, url, log))
    return checks, f"merge commit {merge[:8] if merge else '?'} on {branch}"


def contains(root, sha, merge):
    """Whether commit <sha> includes <merge> (fetching once if unknown)."""
    if not sha or not merge:
        return False
    if sha == merge:
        return True
    r = subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", merge, sha], capture_output=True)
    if r.returncode == 128:
        subprocess.run(["git", "-C", root, "fetch", "-q", "origin"], capture_output=True)
        r = subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", merge, sha], capture_output=True)
    return r.returncode == 0


# GitHub

def gh_failed_log(root, url):
    m = re.search(r"/runs/(\d+)", url or "")
    if not m:
        return None
    try:
        return excerpt(cli("gh", "run", "view", m.group(1), "--log-failed", root=root).splitlines())
    except CliError as e:
        return str(e)


def gh_pr(root, ctx, pr, ignore):
    rows = json.loads(cli("gh", "pr", "checks", str(pr), "--repo", ctx["repo"], "--json",
                          "name,state,bucket,link,workflow", root=root) or "[]")
    checks = []
    for c in rows:
        name = " / ".join(x for x in (c.get("workflow"), c.get("name")) if x)
        st, bucket = (c.get("state") or "").upper(), c.get("bucket")
        if ignored(name, ignore) or bucket in ("skipping", "cancel"):
            state = "skip"
        elif st in ("WAITING", "ACTION_REQUIRED"):
            state = "approval"
        elif bucket == "pending":
            state = "pending"
        elif bucket == "pass":
            state = "pass"
        else:
            state = "fail"
        checks.append((state, name, c.get("link"), gh_failed_log(root, c.get("link")) if state == "fail" else None))
    return checks


def gh_deploy(root, ctx, pr, ignore):
    info = json.loads(cli("gh", "pr", "view", str(pr), "--repo", ctx["repo"], "--json",
                          "state,mergeCommit,baseRefName", root=root))
    if info.get("state") != "MERGED":
        return None, f"PR {pr} is {info.get('state')}, not merged yet"
    merge = (info.get("mergeCommit") or {}).get("oid")
    branch = info.get("baseRefName")
    runs = json.loads(cli("gh", "run", "list", "--repo", ctx["repo"], "--branch", branch, "--limit", "50", "--json",
                          "databaseId,name,status,conclusion,headSha,event,url", root=root) or "[]")
    latest = {}
    for r in runs:  # newest first
        if r.get("event") == "pull_request" or r.get("name") in latest or not contains(root, r.get("headSha"), merge):
            continue
        latest[r.get("name")] = r
    checks = []
    for name, r in latest.items():
        status, conclusion = r.get("status"), r.get("conclusion")
        if ignored(name, ignore) or conclusion in ("cancelled", "skipped", "neutral"):
            state = "skip"
        elif status == "waiting":
            state = "approval"
        elif status != "completed":
            state = "pending"
        elif conclusion == "success":
            state = "pass"
        else:
            state = "fail"
        checks.append((state, name, r.get("url"), gh_failed_log(root, r.get("url")) if state == "fail" else None))
    return checks, f"merge commit {merge[:8] if merge else '?'} on {branch}"


def look(mode, root, pr, ignore):
    kind, ctx = provider(root)
    if not kind:
        raise CliError("origin is neither Azure DevOps nor GitHub")
    if mode == "pr":
        return (az_pr if kind == "az" else gh_pr)(root, ctx, pr, ignore), f"PR {pr}"
    return (az_deploy if kind == "az" else gh_deploy)(root, ctx, pr, ignore)


def summary(checks):
    if not checks:
        return "none"
    worst = min(checks, key=lambda c: RANK[c[0]])[0]
    return "green" if worst in ("pass", "skip") else {"fail": "red"}.get(worst, worst)


def main(args):
    if len(args) < 2 or args[0] not in ("pr", "deploy") or "--root" not in args:
        print(__doc__)
        return 3
    mode, pr = args[0], args[1]
    opt = lambda k, d=None: args[args.index(k) + 1] if k in args else d
    root, wait = opt("--root"), int(opt("--wait", "0"))
    ignore = [x.strip() for x in (opt("--ignore") or "").split(",") if x.strip()]
    start = time.time()
    while True:
        try:
            checks, what = look(mode, root, pr, ignore)
        except (CliError, ValueError) as e:
            print(f"CI error: {e}")
            return 3
        state = summary(checks) if checks is not None else "none"
        if state != "pending" or time.time() - start + 30 > wait:
            break
        time.sleep(30)
    counts = {}
    for c in checks or []:
        counts[c[0]] = counts.get(c[0], 0) + 1
    print(f"CI {state} {what}: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no checks"))
    for st, name, url, log in sorted(checks or [], key=lambda c: RANK[c[0]]):
        print(f"CHECK {st} {name} {url or ''}".rstrip())
    for st, name, url, log in checks or []:
        if st == "fail" and log:
            path = log_file(root, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(log + "\n")
            print(f"LOG {name}: {path}")
    return {"green": 0, "none": 0, "red": 1, "pending": 2, "approval": 4}[state]


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '≤' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
