"""What an lcov file leaves uncovered, per source file.

usage: python lcov-gaps.py <lcov.info> [<path part> ...]

Prints one line per source file whose path contains any <path part> (all
files when none is given):

    <path>  lines <hit>/<found>  branches <hit>/<found>  functions <hit>/<found>
      lines: 12, 40-44
      branches: 57 (block 0, branch 1), ...
      functions: name1, name2            (or "3 unnamed" when the lcov has no FN names)

then "total: N files, M with gaps". Exits 0 when nothing is uncovered, 1 when
something is, and 2 when the lcov has no SF record at all (an include pattern
that matched nothing; vitest with coverage.allowExternal needs
`--coverage.include=**/<dir>/src/...`, not `src/...`).

It reads the lcov files that tripped every coder in sonar-zero-5: CRLF line
endings (Windows), backslash or relative SF paths, and bun's lcov, which has
no BRDA lines and no FN names, only the FNF/FNH counts.
"""
import sys


def parse(text):
    files, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            cur = {"path": line[3:].replace("\\", "/"), "da": {}, "br": [], "fn": {}, "fnda": {}, "fnf": 0, "fnh": 0}
        elif cur is None:
            continue
        elif line.startswith("DA:"):
            n, h = line[3:].split(",")[:2]
            cur["da"][int(n)] = cur["da"].get(int(n), 0) + int(h)
        elif line.startswith("BRDA:"):
            ln, block, branch, taken = line[5:].split(",")[:4]
            cur["br"].append((int(ln), block, branch, 0 if taken in ("-", "") else int(taken)))
        elif line.startswith("FN:"):
            parts = line[3:].split(",")
            cur["fn"][parts[-1]] = int(parts[0]) if parts[0].isdigit() else 0
        elif line.startswith("FNDA:"):
            h, name = line[5:].split(",", 1)
            cur["fnda"][name] = cur["fnda"].get(name, 0) + int(h)
        elif line.startswith("FNF:"):
            cur["fnf"] = int(line[4:])
        elif line.startswith("FNH:"):
            cur["fnh"] = int(line[4:])
        elif line == "end_of_record":
            files.append(cur)
            cur = None
    return files


def ranges(nums):
    out, start, prev = [], None, None
    for n in sorted(nums):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def report(f):
    missed_lines = [n for n, h in f["da"].items() if h == 0]
    missed_br = [b for b in f["br"] if b[3] == 0]
    if f["fn"]:
        fn_found = len(f["fn"])
        missed_fn = sorted((name for name in f["fn"] if f["fnda"].get(name, 0) == 0), key=lambda n: f["fn"][n])
        fn_hit = fn_found - len(missed_fn)
    else:
        fn_found, fn_hit, missed_fn = f["fnf"], f["fnh"], None
    head = (f"{f['path']}  lines {len(f['da']) - len(missed_lines)}/{len(f['da'])}"
            f"  branches {len(f['br']) - len(missed_br)}/{len(f['br'])}"
            f"  functions {fn_hit}/{fn_found}")
    if not f["br"]:
        head += " (no branch data in this lcov)"
    body = []
    if missed_lines:
        body.append("  lines: " + ranges(missed_lines))
    if missed_br:
        body.append("  branches: " + ", ".join(f"{b[0]} (block {b[1]}, branch {b[2]})" for b in missed_br))
    if fn_hit < fn_found:
        body.append("  functions: " + (", ".join(f"{n} (line {f['fn'][n]})" for n in missed_fn) if missed_fn
                                        else f"{fn_found - fn_hit} unnamed (this lcov has no FN names; to find them, reformat the file "
                                             "with prettier --print-width 20 so each function has lines of its own, run "
                                             "its tests, read the uncovered lines, then git checkout the file)"))
    return head, body


def main(args):
    if not args:
        print(__doc__)
        return 2
    text = open(args[0], encoding="utf-8", errors="replace").read()
    files = parse(text)
    if not files:
        print(f"{args[0]}: no SF record: the coverage include matched nothing, or the run wrote no coverage")
        return 2
    parts = [p.replace("\\", "/") for p in args[1:]]
    shown = gaps = 0
    for f in files:
        if parts and not any(p in f["path"] for p in parts):
            continue
        shown += 1
        head, body = report(f)
        print(head)
        for line in body:
            print(line)
        gaps += bool(body)
    if parts and not shown:
        print(f"no file in {args[0]} matches {', '.join(parts)}")
        return 2
    print(f"total: {shown} files, {gaps} with gaps")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
