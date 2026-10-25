"""Check that every path named in the project's system map still exists.

usage: python check-map.py <root> [--map docs/architecture/system-map.md] [--strict]

The map is the split's first stop: business terms and modules mapped to code
paths, so Explore starts from the right folders instead of searching. A map
that names paths which moved sends it the wrong way, so the orchestrator runs
this before reading it. Paths are the backticked tokens that contain a `/` or
end in a file extension; a `*` is matched as a glob.

Prints `MAP none` (no map), `MAP ok <n> paths`, or `MAP stale` with the missing
paths. Exits 0 unless --strict is given and a path is missing (for CI).
"""
import glob
import os
import re
import sys

PATH = re.compile(r"`([A-Za-z0-9_.@*\-/\[\]]+)`")


def paths(text):
    seen = []
    for token in PATH.findall(text):
        token = token.rstrip("/")
        if token.startswith(("http", "-", ".")) and not token.startswith(("./", ".claude/", ".github/")):
            continue
        if "/" not in token and not re.search(r"\.[a-z]{1,5}$", token):
            continue
        if token not in seen:
            seen.append(token)
    return seen


def main(args):
    root = args[0] if args else "."
    rel = args[args.index("--map") + 1] if "--map" in args else "docs/architecture/system-map.md"
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        print("MAP none")
        return 0
    found = paths(open(path, encoding="utf-8").read())
    missing = [p for p in found
               if not (glob.glob(os.path.join(root, p)) if "*" in p else os.path.exists(os.path.join(root, p)))]
    if not missing:
        print(f"MAP ok {len(found)} paths")
        return 0
    print(f"MAP stale: {len(missing)} of {len(found)} paths in {rel} no longer exist:")
    for p in missing:
        print(f"  {p}")
    return 1 if "--strict" in args else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
