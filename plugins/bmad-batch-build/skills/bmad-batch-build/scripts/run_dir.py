"""Where a run's own files live: one folder per run, all projects in one place.

usage: python run_dir.py <slug> [--root <main checkout>]

Creates and prints <store>/runs/<project>/<slug>/, where <store> is
~/.claude/bmad-batch-runs (BMAD_RUNS_DIR overrides it) and <project> is the
main checkout's folder name. The orchestrator runs it once at the start of a
run and uses the printed absolute path for the run file (<slug>.md), the plan
(<slug>-plan.json), the tickets, the synthesis state, the cost report and the
journal; the names keep the slug so a path alone says which run it is.

Older runs kept these files in <root>/.claude/worktrees/_runs/. Creating
the folder moves that run's own files over (adopt()), so a run resumed after
an update finds them; readers also fall back to the old folder (find()).
"""
import os
import re
import sys


def store_dir():
    return os.environ.get("BMAD_RUNS_DIR") or os.path.expanduser("~/.claude/bmad-batch-runs")


def project_name(root):
    return os.path.basename(os.path.normpath(root)) if root else "unknown"


# The files a run keeps; adopt() moves exactly these, never <slug>-<other>.
SUFFIXES = (".md", "-plan.json", "-tickets.json", "-synthesis.md", "-contracts.md", "-cost.txt", "-journal.md")


def run_dir(root, slug, create=False):
    path = os.path.normpath(os.path.join(store_dir(), "runs", project_name(root), slug))
    if create:
        os.makedirs(path, exist_ok=True)
        adopt(root, slug, path)
    return path


def adopt(root, slug, path):
    """Moves this run's files from the legacy _runs folder into its run folder,
    so a run started before the move resumes from where SKILL.md now looks.
    A file already in the run folder wins. Returns the names moved."""
    import shutil
    legacy = legacy_dir(root) if root else None
    moved = []
    for suffix in SUFFIXES:
        src = os.path.join(legacy, slug + suffix) if legacy else None
        if src and os.path.isfile(src) and not os.path.exists(os.path.join(path, slug + suffix)):
            shutil.move(src, os.path.join(path, slug + suffix))
            moved.append(slug + suffix)
    return moved


def legacy_dir(root):
    return os.path.join(root or "", ".claude", "worktrees", "_runs")


def find(root, slug, suffix=""):
    """The existing file <slug><suffix> of this run (suffix ".md", "-plan.json",
    ...), in the run folder or else the legacy _runs folder; None if neither has it."""
    name = f"{slug}{suffix}"
    for folder in (run_dir(root, slug), legacy_dir(root) if root else None):
        if folder and os.path.exists(os.path.join(folder, name)):
            return os.path.join(folder, name)
    return None


# The run file as a session log mentions it, in either layout:
# runs/<project>/<slug>/<slug>.md, or the legacy _runs/<slug>.md. Separators
# may be /, \ or a JSON-escaped \\.
_SEP = r"[/\\]+"
RUN_FILE_RE = re.compile(
    rf"(?:{_SEP}runs{_SEP}[^/\\\"]+{_SEP}(?P<a>[\w.-]+){_SEP}(?P=a)|_runs{_SEP}(?P<b>[\w.-]+?))\.md")
NOT_RUN_FILE = ("-cost", "-synthesis", "-contracts", "-spec", "-journal")


def slug_in(line):
    """The slug of the first run file this line mentions, or None."""
    for m in RUN_FILE_RE.finditer(line):
        slug = m.group("a") or m.group("b")
        if not slug.endswith(NOT_RUN_FILE):
            return slug
    return None


def mentions(line, slug):
    return any((m.group("a") or m.group("b")) == slug for m in RUN_FILE_RE.finditer(line))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        sys.exit(__doc__.strip().splitlines()[2])
    root = args[args.index("--root") + 1] if "--root" in args else os.getcwd()
    print(run_dir(root, args[0], create=True))
