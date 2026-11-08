"""PreToolUse hook: block destructive actions by the agents of a batch build.

The plugin registers it in hooks/hooks.json, so it sees every Bash,
PowerShell, Write, Edit and NotebookEdit call in every session. It acts only on:

- the run's own agents (agent_type bmad-batch-worker, bmad-batch-synthesis,
  haiku-implementer, bmad-batch-enhancer, with or without the plugin prefix);
- any other subagent working inside a prepared batch worktree (one with
  _bmad/custom/bmad-build-auto.toml), such as a worker's reviewers or the
  general-purpose agents of fallback mode.

Everything else, your own session included, is left alone. A blocked call
gets a "deny" with the reason, which the agent reads and works around. The
rules are a safety net under the agents' instructions, not a sandbox: a
command inside a script the agent runs is not inspected.

Blocked:
- git that changes the main checkout (the repository the worktrees belong
  to): checkout, switch, reset, merge, rebase, commit, pull, stash, clean,
  restore, cherry-pick, revert, rm, mv, add;
- Write, Edit or NotebookEdit of a file in the main checkout outside
  .claude/worktrees/;
- force pushes, pushes to a protected branch (main, master, develop, trunk,
  release/*, the profile's base), deleting remote branches, --no-verify;
- completing or merging a PR (gh pr merge, az repos pr update --status
  completed or --auto-complete, --bypass-policy);
- starting or approving pipelines and deploys (az pipelines run / release /
  build queue, gh workflow run, approval APIs, az devops invoke or gh api
  with a write method). Re-queuing a PR's validation (az repos pr policy
  queue, gh run rerun) is allowed;
- infrastructure and cluster changes (terraform apply/destroy, pulumi
  up/destroy, kubectl apply/delete/..., helm install/upgrade/uninstall,
  az/aws/gcloud delete or create commands outside az boards/repos);
- dropping a database that is not a test database;
- rm -r of /, ~, .., or an absolute path outside the worktrees and the temp
  folders;
- the enhancer pushing anything (it only drafts on a branch);
- running a script of another copy of this skill (an older plugin version in
  the cache, a backup, another project's copy) when its version differs from
  this one's: sonar-zero-5 implementers ran heavy.sh from 0.12.12 and from a
  .claude/backups copy on a 0.15.1 run;
- a shell redirect (`> file`, `>> file`, `tee file`) to a file outside the
  worktrees, the temp folders and the run store: one implementer wrote a log
  two levels above its worktree, which nobody could clean up.
"""
import json
import os
import re
import subprocess
import sys

AGENTS = {"bmad-batch-worker", "bmad-batch-synthesis", "haiku-implementer", "bmad-batch-enhancer"}
PROTECTED = {"main", "master", "develop", "trunk"}
GIT_CHANGES = {"checkout", "switch", "reset", "merge", "rebase", "commit", "pull", "stash", "clean", "restore",
               "cherry-pick", "revert", "rm", "mv", "add", "am", "apply"}
INFRA = [
    (r"\bterraform\s+(apply|destroy|import|state\s+(rm|mv|push))\b", "terraform changes infrastructure"),
    (r"\bpulumi\s+(up|destroy|refresh|import|state\s+delete|stack\s+rm)\b", "pulumi changes infrastructure"),
    (r"\bkubectl\s+(apply|delete|create|replace|patch|scale|rollout|drain|cordon|edit|set)\b", "kubectl changes a cluster"),
    (r"\bhelm\s+(install|upgrade|uninstall|delete|rollback)\b", "helm changes a cluster"),
    (r"\baz\s+(?!boards\b|repos\b|account\b|devops\s+configure\b|pipelines\s+runs\s+(list|show)\b)"
     r"[\w-]+(\s+[\w-]+)*\s+(delete|purge|create|update|set|start|stop|restart|deploy|swap)\b",
     "az changes Azure resources"),
    (r"\baws\s+\S+\s+(delete|terminate|remove|put|create|update)[\w-]*\b", "aws changes cloud resources"),
    (r"\bgcloud\s+.*\b(delete|create|deploy|update)\b", "gcloud changes cloud resources"),
    (r"\b(docker|podman)\s+(system\s+prune|volume\s+(rm|prune))\b", "this deletes container data"),
    (r"\bdocker\s+compose\s+down\b.*\s-v\b", "this deletes container volumes"),
]
PIPELINES = [
    (r"\baz\s+pipelines\s+(run\b|build\s+queue\b|release\s+(create|deployment)\b)", "starting a pipeline or a deploy is for the user"),
    (r"\baz\s+pipelines\s+runs?\s+.*\b(approve|update)\b", "approving a pipeline is for the user"),
    (r"\bgh\s+workflow\s+(run|enable|disable)\b", "starting a workflow or a deploy is for the user"),
    (r"\bgh\s+run\s+(cancel|delete)\b", "cancelling or deleting runs is for the user"),
    (r"\bgh\s+pr\s+merge\b", "merging the PR is for the user"),
    (r"\baz\s+repos\s+pr\s+update\b.*--(status\s+completed|auto-complete|bypass-policy)", "completing the PR is for the user"),
    (r"--bypass-policy\b", "bypassing branch policies is for the user"),
    (r"\baz\s+devops\s+invoke\b.*--http-method\s+(?!get\b)", "az devops invoke may only read"),
    (r"\bgh\s+api\b.*(-X|--method)\s*(?!get\b)\w+", "gh api may only read"),
    (r"\bgh\s+api\b.*(pending_deployments|/approve|/dispatches)", "approving or dispatching deploys is for the user"),
    (r"\bgh\s+(repo\s+delete|release\s+(delete|create))\b", "this changes the repository on GitHub"),
]


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"bmad-batch-build guard: {reason}. If the task really needs it, stop and "
                                    "report it as a blocking condition for the user instead."}}))
    return 0


def norm(p):
    return os.path.normcase(os.path.abspath(os.path.expanduser(p))).replace("\\", "/").rstrip("/")


def main_root(cwd):
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return norm(os.path.dirname(common)) if common.endswith(".git") else None


def batch_worktree(path):
    """The prepared batch worktree that contains <path>, or None."""
    m = re.match(r"^(.*?/\.claude/worktrees/[^/]+)", norm(path) + "/")
    if m and os.path.isfile(os.path.join(m.group(1), "_bmad", "custom", "bmad-build-auto.toml")):
        return m.group(1)
    return None


def in_scope(event):
    kind = (event.get("agent_type") or "").split(":")[-1]
    if kind in AGENTS:
        return kind
    if event.get("agent_id") and batch_worktree(event.get("cwd") or ""):
        return "subagent"
    return None


def protected(root):
    names = set(PROTECTED)
    try:
        with open(os.path.join(root, ".claude", "bmad-batch-project.json"), encoding="utf-8") as fh:
            names.add(json.load(fh).get("base") or "main")
    except (OSError, ValueError, TypeError):
        pass
    return names


def segments(cmd):
    """Shell commands split on ; && || | and newlines, each with the
    directory it runs in (following cd)."""
    return [s.strip() for s in re.split(r"&&|\|\||;|\||\n", cmd) if s.strip()]


def unquote(s):
    return s.strip().strip("'\"")


def check_git(seg, where, root, kind, bases):
    m = re.match(r"^(?:\w+=\S+\s+)*git\s+(.*)$", seg)
    if not m:
        return None
    rest = m.group(1)
    target = where
    while True:
        mc = re.match(r"^-C\s+(\"[^\"]*\"|'[^']*'|\S+)\s+(.*)$", rest)
        if mc:
            target, rest = unquote(mc.group(1)), mc.group(2)
            continue
        mo = re.match(r"^(-c\s+\S+|--\S+)\s+(.*)$", rest)
        if mo:
            rest = mo.group(2)
            continue
        break
    sub = (rest.split() or [""])[0]
    if root and target and norm(target) == root and sub in GIT_CHANGES:
        return f"git {sub} would change the main checkout {root}; work only in your worktree"
    if sub in ("commit", "push", "merge", "rebase") and "--no-verify" in rest.split():
        return "--no-verify skips the repository's hooks"
    if sub == "push":
        if kind == "bmad-batch-enhancer":
            return "the enhancer drafts on a branch and never pushes; the user ships it"
        args = rest.split()[1:]
        if any(a in ("-f", "--force", "--mirror", "--prune", "--delete", "-d") or a.startswith("--force")
               for a in args):
            return "force pushes, mirror pushes and remote branch deletions are not allowed"
        refspecs = [a for a in args if not a.startswith("-")][1:]
        if not refspecs or any(a.split(":")[-1] == "HEAD" for a in refspecs):
            try:
                current = subprocess.run(["git", "-C", target or ".", "branch", "--show-current"],
                                         capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                current = ""
            if current in bases or current.startswith("release/"):
                return f"you are on the protected branch {current}; push the run's branch and open a PR"
        for a in args:
            if a.startswith("+"):
                return "force pushes are not allowed"
            if a.startswith(":"):
                return "deleting a remote branch is not allowed"
            dest = a.split(":")[-1].replace("refs/heads/", "")
            if dest in bases or dest.startswith("release/"):
                return f"pushing to the protected branch {dest} is not allowed; push the run's branch and open a PR"
    return None


def check_rm(seg, root, where=None):
    m = re.match(r"^(?:sudo\s+)?rm\s+(.*)$", seg)
    if not m:
        return None
    words = m.group(1).split()
    if not any(w.startswith("-") and ("r" in w.lower() or w == "--recursive") for w in words):
        return None
    for w in (unquote(x) for x in words if not x.startswith("-")):
        if w.startswith("..") and where and w not in ("..", "../", "../*"):
            # A relative path that climbs out of the current folder is fine
            # while it stays inside the same batch worktree, below its top
            # (sonar-zero-5: `cd api && rm -rf ../_bmad-output/cov-u3c`).
            p = norm(os.path.join(where, w))
            wt = batch_worktree(where)
            if wt and p.startswith(norm(wt) + "/") and "*" not in w:
                continue
        if w in ("/", "/*", "~", "~/", "$HOME", "${HOME}", ".", "..", "*") or w.startswith(".."):
            return f"rm -r {w} is not allowed"
        if w.startswith(("/", "~", "$HOME")) or re.match(r"^[A-Za-z]:[\\/]", w):
            p = norm(w)
            if root and (p == root or p.startswith(root + "/")) and not p.startswith(root + "/.claude/worktrees/"):
                return f"rm -r of {w} would delete files in the main checkout"
            tmp = [norm(os.environ.get(k, "")) for k in ("TEMP", "TMP", "TMPDIR") if os.environ.get(k)]
            if "/.claude/worktrees/" in p + "/" or p.startswith("/tmp") or any(t and p.startswith(t) for t in tmp):
                continue
            return f"rm -r of {w}, outside the worktrees and the temp folders, is not allowed"
    return None


SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SKILL_SCRIPT = re.compile(r"""(?P<p>[^\s"';|&()<>]*?(?:bmad-batch-build|bmad-parallel-build)[^\s"';|&()<>]*?)[\\/]scripts[\\/](?P<n>[\w.-]+\.(?:sh|py))""")


def posix_drive(p):
    """Git Bash's /c/Users/... as C:/Users/... (abspath would make it C:/c/Users)."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    return f"{m.group(1).upper()}:/{m.group(2)}" if m and os.name == "nt" else p


def version_of(scripts_dir):
    try:
        with open(os.path.join(os.path.dirname(scripts_dir), "VERSION"), encoding="utf-8") as fh:
            return fh.read().split()[0]
    except (OSError, IndexError):
        return None


def check_skill_copy(cmd, kind=None):
    """Deny a script run from another copy of this skill with another version.
    The enhancer and the ship work on the skill's source, in worktrees under
    the run store's src/, which are allowed whatever their version."""
    if kind == "bmad-batch-enhancer":
        return None
    mine = norm(SCRIPTS)
    own = version_of(SCRIPTS)
    store_src = norm(os.environ.get("BMAD_RUNS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "bmad-batch-runs")) + "/src/"
    for m in SKILL_SCRIPT.finditer(cmd):
        d = m.group("p")
        if "$" in d or not d:
            continue
        theirs = norm(posix_drive(d.rstrip("/\\")) + "/scripts")
        if theirs.startswith(store_src):
            continue
        if theirs == mine or (own and version_of(theirs) == own):
            continue
        return (f"{m.group(0)} is another copy of this skill's scripts (version {version_of(theirs) or 'unknown'}, "
                f"this run uses {own or 'this copy'}); run {SCRIPTS.replace(chr(92), '/')}/{m.group('n')} instead")
    return None


def allowed_write(p):
    tmp = [norm(os.environ.get(k, "")) for k in ("TEMP", "TMP", "TMPDIR") if os.environ.get(k)]
    store = norm(os.environ.get("BMAD_RUNS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "bmad-batch-runs"))
    # Inside one worktree, not merely under .claude/worktrees/.
    inside = re.search(r"/\.claude/worktrees/[^/]+/.", p) is not None
    # A worktree's private git dir (<repo>/.git/worktrees/<name>/), where bg.sh
    # and synthesis-diff.sh keep their files.
    gitdir = re.search(r"/\.git/worktrees/[^/]+/.", p) is not None
    return inside or gitdir or p.startswith(store) or any(t and p.startswith(t) for t in tmp)


def check_redirect(seg, where):
    for m in re.finditer(r"(?:\d?>>?|\btee(?:\s+-a)?)\s*(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", seg):
        target = unquote(m.group(1))
        if not target or target.startswith(("&", "$")) or target in ("/dev/null", "nul", "NUL"):
            continue
        if target == "/tmp" or target.startswith(("/tmp/", "/dev/")):
            continue  # Git Bash's /tmp is the user's temp folder
        absolute = target.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", target)
        if not absolute and ".." not in re.split(r"[\\/]", target):
            continue
        if not absolute and not where:
            continue
        p = norm(posix_drive(target) if target.startswith("/") else os.path.join(where or "", target))
        if not allowed_write(p):
            return (f"writing {target} puts a file outside the worktrees, the temp folders and the run store; "
                    "write it inside your worktree (under _bmad-output/) or the temp folder")
    return None


def check_bash(cmd, cwd, root, kind):
    bases = protected(root) if root else set(PROTECTED)
    for pattern, why in PIPELINES + INFRA:
        if re.search(pattern, cmd, re.I):
            return why
    if re.search(r"\bdrop\s+(database|schema)\b(?![^;]*_test\b)", cmd, re.I) or \
            re.search(r"\bdropdb\b(?![^;]*_test\b)", cmd):
        return "dropping a database that is not a test database is not allowed"
    why = check_skill_copy(cmd, kind)
    if why:
        return why
    where = cwd
    for seg in segments(cmd):
        mc = re.match(r"^cd\s+(\"[^\"]*\"|'[^']*'|\S+)$", seg)
        if mc:
            where = os.path.join(where, unquote(mc.group(1))) if where else unquote(mc.group(1))
            continue
        why = check_git(seg, where, root, kind, bases) or check_rm(seg, root, where) or check_redirect(seg, where)
        if why:
            return why
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    kind = in_scope(event)
    if not kind:
        return 0
    tool, inp, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd") or os.getcwd()
    root = main_root(cwd)
    if tool in ("Bash", "PowerShell"):
        # Through PowerShell the agents run bash -c '<command>' (SKILL.md); the
        # rules read the inner command, and a plain PowerShell line the same way.
        cmd = inp.get("command") or ""
        m = re.match(r"^\s*bash(?:\.exe)?\s+-c\s+'(.*)'\s*$", cmd, re.S)
        cmd = m.group(1).replace("''", "'") if m else cmd
        why = check_bash(cmd, cwd, root, kind)
        return deny(why) if why else 0
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if root and path:
            p = norm(path)
            if (p == root or p.startswith(root + "/")) and not p.startswith(root + "/.claude/worktrees/"):
                return deny(f"{path} is in the main checkout; edit only inside your worktree under "
                            f"{root}/.claude/worktrees/")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # a guard bug must never break a tool call
        sys.exit(0)
