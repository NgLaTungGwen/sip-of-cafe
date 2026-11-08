"""List the count ratchets a project's PR pipeline enforces, as local checks.

usage: ci-ratchets.py <worktree>

A ratchet is a one-line pipeline step that caps a count with a variable:
`eslint --max-warnings $(API_MAX_WARNINGS)`, `knip --max-issues $(KNIP_MAX_ISSUES)`.
The cap lives only in the pipeline, so a local `npm run lint` passes while
the server fails (sonar-zero-5: 1548 warnings against a cap of 1546, found
only on CI). verify.sh runs what this prints.

Reads Azure Pipelines files (pipeline/*.yml, azure-pipelines*.yml, .azure*/**)
and GitHub workflows (.github/workflows/*.yml). Prints one line per ratchet:
`<name>\\t<dir>\\t<command>`, with the variable replaced by its value and <dir>
relative to the worktree ("." for the root). A ratchet whose variable has no
literal value in the file, or whose directory does not exist, is skipped.
"""
import glob
import os
import re
import sys

FLAG = re.compile(r"--max-(?:warnings|issues)[ =]+(\$\((\w+)\)|\$\{\{\s*(?:env|vars)\.(\w+)\s*\}\}|\$\{?(\w+)\}?)")
STEP = re.compile(r"^(\s*)-?\s*(script|run|bash):\s*(.+?)\s*$")
WDIR = re.compile(r"^\s*(workingDirectory|working-directory):\s*(.+?)\s*$")
ROOT_VARS = ("$(Build.SourcesDirectory)", "$(System.DefaultWorkingDirectory)",
             "${{ github.workspace }}", "$(Pipeline.Workspace)/s", "$GITHUB_WORKSPACE")


def pipeline_files(wt):
    pats = ["pipeline/*.yml", "pipeline/*.yaml", "pipelines/*.yml", "azure-pipelines*.yml",
            ".azure*/**/*.yml", ".github/workflows/*.yml", ".github/workflows/*.yaml"]
    seen = []
    for p in pats:
        for f in sorted(glob.glob(os.path.join(wt, p), recursive=True)):
            if f not in seen:
                seen.append(f)
    return seen


def variables(text):
    """name -> literal value, from ADO `- name: X` / `value: N` pairs and `X: N` maps."""
    out = {}
    for m in re.finditer(r"-\s*name:\s*['\"]?(\w+)['\"]?\s*\n\s*value:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", text, re.M):
        out[m.group(1)] = m.group(2).strip()
    for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]*):\s*['\"]?(\d+)['\"]?\s*$", text, re.M):
        out.setdefault(m.group(1), m.group(2))
    return out


def unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def ratchets(wt):
    found = []
    for f in pipeline_files(wt):
        text = open(f, encoding="utf-8", errors="replace").read()
        vals = variables(text)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = STEP.match(line)
            if not m or m.group(3) in ("|", ">", "|-", ">-"):
                continue
            cmd = unquote(m.group(3))
            fm = FLAG.search(cmd)
            if not fm:
                continue
            var = fm.group(2) or fm.group(3) or fm.group(4)
            if var not in vals:
                continue
            cmd = cmd.replace(fm.group(1), vals[var])
            # The step's own keys follow it at a deeper indent.
            d = "."
            ind = len(m.group(1)) + (0 if line.lstrip().startswith("-") else -2)
            for nxt in lines[i + 1:i + 12]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= ind:
                    break
                w = WDIR.match(nxt)
                if w:
                    d = unquote(w.group(2))
                    for r in ROOT_VARS:
                        d = d.replace(r, "")
                    d = d.strip("/") or "."
            if d == "." and not line.lstrip().startswith("-"):
                # GitHub: `working-directory:` may come before `run:` in the same step.
                for prev in reversed(lines[max(0, i - 12):i]):
                    w = WDIR.match(prev)
                    if w:
                        d = unquote(w.group(2))
                        for r in ROOT_VARS:
                            d = d.replace(r, "")
                        d = d.strip("/") or "."
                    if prev.lstrip().startswith("-"):
                        break
            if not os.path.isdir(os.path.join(wt, d)):
                continue
            name = "ratchet-" + var.lower().replace("_", "-")
            if name not in [n for n, _, _ in found]:
                found.append((name, d, cmd))
    return found


if __name__ == "__main__":
    for name, d, cmd in ratchets(sys.argv[1]):
        print(f"{name}\t{d}\t{cmd}")
