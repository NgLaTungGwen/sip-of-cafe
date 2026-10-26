"""Installs this copy of the skill into every project listed in
~/.claude/bmad-batch-runs/install/installs.json (BMAD_RUNS_DIR overrides the
folder).

usage: python install-skill.py [--dry-run]

installs.json lists the projects on this machine that use the skill:

    {"targets": [
      {"root": "D:/Stuff/Quy/hive-factory", "patch": "patch-hive-factory.py",
       "drop": ["scripts/ensure-bmad.sh"]},
      {"root": "D:/Stuff/Quy/HealthPro"}
    ]}

For each target it replaces <root>/.claude/skills/bmad-batch-build with this
copy, removes the files in "drop", runs the optional local patch (a Python
script in the install folder, given the installed skill directory), and copies
the agents into <root>/.claude/agents. Prints one line per target.
"""
import json
import os
import shutil
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = ("SKILL.md", "VERSION", "references", "scripts", "agents", "tests")


def store():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def install(target, dry):
    root = target["root"]
    dest = os.path.join(root, ".claude", "skills", "bmad-batch-build")
    if not os.path.isdir(root):
        return f"{root}: skipped, not found"
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


def main(args):
    path = os.path.join(store(), "install", "installs.json")
    if not os.path.exists(path):
        print(f"install-skill: no {path}; create it (see this script's docstring) to install automatically")
        return 1
    targets = json.load(open(path, encoding="utf-8")).get("targets", [])
    failed = False
    for t in targets:
        line = install(t, "--dry-run" in args)
        failed |= "FAILED" in line
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
