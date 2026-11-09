---
name: ado-eod-report
description: "Write the user's end-of-day (EOD) report from their Azure DevOps board. Reads every work item they changed that day, groups the work into sections (one per Azure DevOps project, or the user's own configured sections), summarises it by user story for leadership (every story, task and PR kept with its status), infers the upcoming tasks, lists blockers, and puts the report on the clipboard for Teams. Signs in with an Azure DevOps PAT, or with the Azure CLI sign-in (az login) when there is no PAT. Use when the user types /ado-eod-report, or asks for their EOD report, end-of-day report, daily report or daily status from ADO."
---

# ADO EOD report

Builds the user's end-of-day report from Azure DevOps, plus their GitHub
issues and PRs when the `gh` CLI is signed in. The script reads the
board with the user's PAT, or with their `az login` session when there is no
PAT or the PAT is rejected. You turn what it returns into a short,
summarised report for leadership that reads in under a minute. It summarises
the day by user story, yet still names every story, task and PR with its
status.

For the weekly report, use the `ado-weekly-report` skill; it runs this
skill's script and follows the same reference files.

`<eod>` below is this skill's folder. Run Python as `python3` (`python` on
Windows).

The parts both reports share live in `<eod>/references/`, one file per step:

| File | Covers |
|---|---|
| `access.md` | `check`, and what to do on each `ERROR` |
| `data.md` | the `collect` JSON fields, and how sections are matched |
| `report-rules.md` | grouping by story, status words, noise, blockers, style |
| `clipboard.md` | the HTML fragment, `copy_html.ps1`, and the text fallback |

Each step below says which file to read. Read it at that step; don't work
from memory.

## Arguments

- A date: `today` (default), `yesterday`, or `YYYY-MM-DD`. On a Monday,
  "last Friday" means the date of that Friday.
- Free-text notes, such as "also paired with a teammate on the data import".
  These are work ADO doesn't show. Put each note in the section it belongs to.

## 1. Check access

Read `<eod>/references/access.md` and follow it, including "Who the report
is for": on `PROFILE NONE`, ask the user who they are before collecting.

```bash
python3 "<eod>/scripts/ado_eod.py" check
```

## 2. Collect

```bash
python3 "<eod>/scripts/ado_eod.py" collect --date <date>
```

`<eod>/references/data.md` describes the JSON; read it.

If `items` is empty, say there is no ADO activity for that date. Ask whether
they meant another date, or want a report from their notes only. Don't write
an empty report.

## 3. Write the report

Read `<eod>/references/report-rules.md` before writing, and apply it inside
this layout. Use exactly this layout; keep every heading, even when it's
empty.

```
# EOD Report – <Day DD Mon YYYY>
**<profile name>** · <profile role>

## Summary
<2-3 sentences in business terms: what was delivered, what is in review,
any risk. Lead with the outcome that matters most.>

## Today Accomplished

### <section name, from `sections`>
- **[User Story #100] <short story name>:** <what it delivers, as an outcome> -> **<status>**
  - <what this task did, in plain words> (#101, PR #45) **Done**
  - <what this task is doing, in plain words> (#102, PR #46) **In review**
- **[Bug #210] <short name>:** <what was fixed, for the user> -> **<status>**

### <next section>
- ...

## Upcoming Task(s)
- ...

## Blocker(s)
- None
```

EOD-specific rules, on top of `report-rules.md`:

- **The Summary** is written last, from the lines below it, for a reader who
  reads nothing else: 2-3 sentences, no task IDs, at most one story ID for the
  headline item. Add counts when they help, such as "2 stories completed,
  3 PRs merged"; they must match the lines below.
- **Order** each section by importance: Done first, then In review, then In
  progress.
- **Sections**: one `###` per entry in the JSON's `sections`, in that order.
  A configured section with no work gets `- No updates`.
- **Blocker(s)** follows the Blockers rule in `report-rules.md`.

### Upcoming Task(s)

Write 2 to 5 lines, inferred from today's work. Most important first:

1. Blocked items: what has to happen to unblock them.
2. In progress: "Continue <work, in plain words> (#id): <next concrete step>". Take the next
   step from the comments, the remaining tasks under the same parent, or
   "finish and raise PR".
3. In review: "Address review comments on PR #n" or "Merge PR #n once
   approved".
4. Resolved: "Support QA verification of #id".
5. Done items whose story still has open tasks (in `children.open` or
   `parent.children.open`): "Start <task> (#id)" for the next one.
6. Fill any free lines from `open_assigned`, highest priority first: "Start
   <title> (#id)".

## 4. Print

Read `<eod>/references/clipboard.md` and follow it. The HTML headings are:
`<p><b>EOD Report – <date></b></p>`; `<p><b>Summary:</b></p>` with the
summary in a `<p>`; `<p><b>Today Accomplished:</b></p>` (sectioned); then
`<p><b>Upcoming task(s):</b></p>` and `<p><b>Blocker(s):</b></p>`, each with a
`<ul>`.
