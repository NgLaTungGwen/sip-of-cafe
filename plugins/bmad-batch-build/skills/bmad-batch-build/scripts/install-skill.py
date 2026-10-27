"""Installs this copy of the skill into every project listed in
~/.claude/bmad-batch-runs/install/installs.json (BMAD_RUNS_DIR overrides the
folder).

usage: python install-skill.py [--dry-run] [--force]

installs.json lists the projects on this machine that use the skill:

    {"targets": [
      {"root": "D:/Stuff/Quy/hive-factory", "patch": "patch-hive-factory.py",
       "drop": ["scripts/ensure-bmad.sh"]},
      {"root": "D:/Stuff/Quy/HealthPro"}
    ]}

For each target it replaces <root>/.claude/skills/bmad-batch-build with this
copy, removes the files in "drop", runs the optional local patch (a Python
script in the install folder, given the installed skill directory), and copies
the agents into <root>/.claude/agents. Prints one line per target. A project
whose Claude Code session wrote in the last 15 minutes is skipped (a run there
may be using the scripts); --force installs anyway. The dashboard skill
beside this one, when present, goes once to ~/.claude/skills.
"""
import json
import os
import re
import shutil
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = ("SKILL.md", "VERSION", "references", "scripts", "agents", "tests")


def store():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def invokes(line):
    """A typed /bmad-batch-build command or a Skill call that loads it; text
    that merely quotes the command (an edit, a tool result) doesn't count."""
    try:
        x = json.loads(line)
    except ValueError:
        return False
    m = x.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if x.get("type") == "user":
        texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or [] if isinstance(b, dict)]
        return any(is_run_skill(n) for s in texts for n in re.findall(r"<command-name>/([\w:.-]+)</command-name>", s))
    return x.get("type") == "assistant" and isinstance(c, list) and any(
        b.get("type") == "tool_use" and b.get("name") == "Skill"
        and is_run_skill((b.get("input") or {}).get("skill")) for b in c)


def is_run_skill(name):
    """bmad-batch-build itself, also as a plugin skill (bmad-batch-build:bmad-batch-build);
    viewing the dashboard (bmad-batch-build:bmad-batch-dashboard) is not a run."""
    return str(name or "").split(":")[-1] == "bmad-batch-build"


def active_minutes(root):
    """Minutes since a Claude Code session in this project that ran the skill
    last wrote its log (looking back an hour), or None. A run's scripts must not be replaced under it: bash reads a script
    while running it."""
    import glob
    import time
    key = "".join(c if c.isalnum() else "-" for c in os.path.normpath(root)).lower()
    # Only sessions that ran the skill count (typed command or Skill call,
    # copied or plugin install), so a session merely working there doesn't.
    marks = ("<command-name>/bmad-batch-build", '"skill":"bmad-batch-build', '"skill": "bmad-batch-build')
    newest = None
    for d in glob.glob(os.path.expanduser("~/.claude/projects/*")):
        if os.path.basename(d).lower() != key:
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            m = os.path.getmtime(f)
            if newest is not None and m <= newest or time.time() - m > 3600:
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                if any(invokes(line) for line in fh if any(k in line for k in marks)):
                    newest = m
    return None if newest is None else (time.time() - newest) / 60


def install(target, dry, force=False):
    root = target["root"]
    dest = os.path.join(root, ".claude", "skills", "bmad-batch-build")
    if not os.path.isdir(root):
        return f"{root}: skipped, not found"
    idle = active_minutes(root)
    if not force and idle is not None and idle < 15:
        return (f"{root}: SKIPPED, a session there wrote {idle:.0f} min ago and may be running the skill; "
                f"run install-skill.py again when it is idle (or with --force)")
    if dry:
        return f"{root}: would install into {dest}"
    os.makedirs(dest, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for part in PARTS:
        src, dst = os.path.join(SKILL, part), os.path.join(dest, part)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.remove(dst)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=ignore)
        elif os.path.exists(src):
            shutil.copy2(src, dst)
    for rel in target.get("drop", []):
        p = os.path.join(dest, rel)
        if os.path.exists(p):
            os.remove(p)
    note = ""
    if target.get("patch"):
        patch = os.path.join(store(), "install", target["patch"])
        r = subprocess.run([sys.executable, "-I", patch, dest], capture_output=True, text=True)
        if r.returncode != 0:
            return f"{root}: INSTALLED BUT PATCH FAILED ({target['patch']}): {(r.stderr or r.stdout).strip()[-300:]}"
        note = f", patched with {target['patch']}"
    agents = os.path.join(root, ".claude", "agents")
    os.makedirs(agents, exist_ok=True)
    changed = []
    for f in sorted(os.listdir(os.path.join(SKILL, "agents"))):
        src, dst = os.path.join(SKILL, "agents", f), os.path.join(agents, f)
        if not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read():
            shutil.copy2(src, dst)
            changed.append(f[:-3])
    version = open(os.path.join(dest, "VERSION")).read().split()[0]
    return f"{root}: installed {version}{note}; agents updated: {', '.join(changed) or 'none'}"


def install_dashboard(dry):
    """The dashboard skill (a sibling of this one) goes to the user's skills,
    ~/.claude/skills/bmad-batch-dashboard, so /bmad-batch-dashboard works in
    every project. Viewing it never runs the build scripts, so it needs no
    active-run check."""
    src = os.path.join(os.path.dirname(SKILL), "bmad-batch-dashboard")
    if not os.path.isdir(src):
        return None
    dest = os.path.join(os.path.expanduser("~"), ".claude", "skills", "bmad-batch-dashboard")
    if dry:
        return f"dashboard: would install into {dest}"
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return f"dashboard: installed into {dest} (/bmad-batch-dashboard)"


def main(args):
    line = install_dashboard("--dry-run" in args)
    if line:
        print(line)
    path = os.path.join(store(), "install", "installs.json")
    if not os.path.exists(path):
        print(f"install-skill: no {path}; create it (see this script's docstring) to install automatically")
        return 1
    targets = json.load(open(path, encoding="utf-8")).get("targets", [])
    failed = False
    for t in targets:
        line = install(t, "--dry-run" in args, "--force" in args)
        failed |= "FAILED" in line
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
