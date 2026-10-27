---
name: bmad-batch-dashboard
description: Build and open a dashboard with d3 charts of every bmad-batch-build run - cost by role, wall time, problems from the run journals, skill versions compared, heavy command times, unit times and context per turn - across projects and across the machines whose metrics files were imported. Use when the user types /bmad-batch-dashboard, or asks for the batch build dashboard, run metrics or skill metrics.
---

# Batch build dashboard

Builds one HTML page from the metrics dataset of bmad-batch-build
(`~/.claude/bmad-batch-runs/metrics/`) and opens it in the browser. The page
holds its own data and works offline except for d3, which loads from cdnjs.

## Steps

1. **Files from other machines.** If the user gave metrics files (made with
   `metrics.py export` on another machine), pass them with `--import`.
2. **Build.** Run one Bash call with `run_in_background: true`, because
   `--refresh` reads every session log on the machine and can take several
   minutes:

   ```bash
   python "<this skill>/scripts/dashboard.py" --refresh --open [--import <file> ...]
   ```

   Leave out `--refresh` when the user asks for a quick view; the page is
   then built from the dataset as it was last refreshed. Wait for the
   completion notice; don't poll the output or sleep.
3. **Report.** Give the user the lines the script printed: the page's path,
   runs and machines, spend, medians and PRs opened. Then add, in at most
   three lines, what stands out on the page: the most expensive recent run
   and why (role), the most common problem, or a version that changed cost or
   time. Say "not refreshed" if the script said so.

Don't read the HTML file into the conversation; it carries all the data.
Don't publish it anywhere: the user's own rows carry project names.

## When something is missing

- **"no runs recorded yet":** no batch build has finished on this machine,
  or bmad-batch-build is older than 0.12.2. Say so.
- **"Not refreshed":** the bmad-batch-build scripts weren't found. The page
  shows the dataset as it was; installing or updating bmad-batch-build fixes
  it.
- To share this machine's numbers instead, the user runs
  `python <bmad-batch-build>/scripts/metrics.py export`.
