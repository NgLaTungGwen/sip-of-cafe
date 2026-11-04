"""The review findings the units fixed, for spotting mistakes that recur.

usage: python review-findings.py <spec>...

Reads each spec's `## Review Triage Log` (bmad-build-auto step 04), where a
finding is a line like

    - `[medium]` `patch` (blind) Fake answers an explicit "main" ... — fixed: ...

and prints the ones that were fixed (`patch`), one per line:

    <spec name>\t<severity>\t<review layer>\t<finding>

Rejected and deferred findings are left out: only a mistake that was really
made and fixed says what a lint rule or a type could have caught. Final
synthesis groups these lines into kinds of mistake (references/synthesis.md).

Workers also write the action in brackets (`[patch]` in backticks), as the
severity is; both forms are read.
"""
import os
import re
import sys

FINDING = re.compile(r"^\s*-\s*`\[([\w-]+)\]`\s*`\[?([\w-]+)\]?`\s*(?:\(([\w-]+)\))?\s*(.*)$")


def findings(path):
    out, inside = [], False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("## "):
                inside = line.strip() == "## Review Triage Log"
                continue
            m = inside and FINDING.match(line)
            if m and m.group(2) == "patch":
                out.append((m.group(1), m.group(3) or "-", m.group(4).strip()[:300]))
    return out


def main(paths):
    for p in paths:
        if not os.path.isfile(p):
            print(f"missing\t-\t-\t{p}")
            continue
        name = os.path.splitext(os.path.basename(p))[0]
        for sev, layer, text in findings(p):
            print(f"{name}\t{sev}\t{layer}\t{text}")
    return 0


if __name__ == "__main__":
    # A Windows console is cp1252, which has no '≈' or '≤' (UnicodeEncodeError).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
