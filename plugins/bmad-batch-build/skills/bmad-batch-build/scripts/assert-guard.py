"""Find assertions the branch removed or changed in test files.

usage: python assert-guard.py <worktree> <base ref>

Reads `git diff <base>...HEAD` of the test files (a path with a test, tests,
spec, specs, __tests__ or e2e folder, or a name like *.test.*, *.spec.*,
*_test.*, test_*.py) and prints, for each removed or modified line that holds
an assertion (expect(, assert, should, toBe, toEqual, t.Equal, require.X(...):

    ASSERT-EDIT <file>:<old line> <removed text>

then `assert-guard: N edits`. A line removed and added back with the same text
(moved, re-indented, or moved to another test file) does not count. Exits 1
when N > 0, 2 when git fails, 0 otherwise.

Synthesis runs it before the PR: a changed assertion does not block the PR,
but each one is listed under "Changed assertions" with why it changed
(references/synthesis.md).
"""
import collections
import re
import subprocess
import sys

TEST_PATH = re.compile(
    r"(^|/)(tests?|specs?|__tests__|e2e)/"
    r"|\.(test|spec)\.[^/]+$"
    r"|_(test|spec)\.[^/]+$"
    r"|(^|/)test_[^/]+\.py$"
)
ASSERTION = re.compile(
    r"\bexpect\s*\("
    r"|\bassert"
    r"|\bshould\b"
    r"|\.to(Be|Equal|StrictEqual|Match|Throw|Contain|Have|Resolve|Reject)\w*"
    r"|\bt\.(Equal|NotEqual|Error|Errorf|Fatal|Fatalf|True|False|Nil|NotNil|Assert)\b"
    r"|\b(require|assert)\.\w+\s*\("
    r"|\bpytest\.raises\b"
    r"|\bself\.fail\b"
)
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def is_test(path):
    return bool(path) and bool(TEST_PATH.search(path.replace("\\", "/")))


def file_diff(chunk):
    """(file, [(old line, removed text)], [added text]) for one file's diff."""
    old_path = new_path = None
    removed, added, old = [], [], 0
    in_hunks = False
    for raw in chunk.splitlines():
        line = raw.rstrip("\r")
        if not in_hunks:
            if line.startswith("--- "):
                p = line[4:]
                old_path = p[2:] if p.startswith("a/") else None
            elif line.startswith("+++ "):
                p = line[4:]
                new_path = p[2:] if p.startswith("b/") else None
            elif line.startswith("rename to "):
                new_path = line[len("rename to "):]
        m = HUNK.match(line)
        if m:
            in_hunks, old = True, int(m.group(1))
            continue
        if not in_hunks:
            continue
        if line.startswith("-"):
            removed.append((old, line[1:]))
            old += 1
        elif line.startswith("+"):
            added.append(line[1:])
    return new_path or old_path, removed, added


def edits(diff):
    removed, added = [], collections.Counter()
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        if not chunk.startswith("diff --git "):
            continue
        path, rem, add = file_diff(chunk)
        if not is_test(path):
            continue
        removed += [(path, n, t) for n, t in rem]
        added.update(t.strip() for t in add)
    hits = []
    for path, line, text in removed:
        if not ASSERTION.search(text):
            continue
        key = text.strip()
        if added[key] > 0:
            added[key] -= 1  # moved, not changed
            continue
        hits.append((path, line, key))
    return hits


def main(argv):
    if len(argv) != 2:
        print("usage: assert-guard.py <worktree> <base ref>", file=sys.stderr)
        return 2
    wt, base = argv
    r = subprocess.run(
        ["git", "-C", wt, "-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "-M",
         "-U0", "--src-prefix=a/", "--dst-prefix=b/", f"{base}...HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"assert-guard: git diff failed: {r.stderr.strip()}", file=sys.stderr)
        return 2
    hits = edits(r.stdout)
    for path, line, text in hits:
        print(f"ASSERT-EDIT {path}:{line} {text}")
    print(f"assert-guard: {len(hits)} edits")
    return 1 if hits else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
