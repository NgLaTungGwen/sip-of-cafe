---
name: ado-weekly-report
description: "Write the user's weekly status report from their Azure DevOps board. Reads every work item they changed that week (Monday to Sunday), summarises it by user story for leadership (every story, task and PR kept with its status), splits delivered from in-flight work, infers the plan for next week from what was implemented, lists risks and blockers, and puts the report on the clipboard for Teams. Uses the ado-eod-report skill's script and sign-in. Use when the user types /ado-weekly-report, or asks for their weekly report, weekly status, week summary or weekly update from ADO."
---

# ADO weekly report

Builds the user's weekly report from Azure DevOps, for leadership. They read
it in two minutes, so it summarises the week by user story, yet still names
every story, task and PR with its status, and ends with a plan for next week
inferred from what was implemented.

`<eod>` below is the `ado-eod-report` skill's folder, the sibling of this
one (`../ado-eod-report` from this skill's folder: both ship in the
`ado-eod-report` plugin, and a copied install puts them side by side in
`~/.claude/skills/`). This skill has no script or rules of its own to share:
it runs `<eod>/scripts/ado_eod.py` and follows the files in
`<eod>/references/`, the same ones the EOD report uses. Each step below says
which file to read. Read it at that step; don't work from memory. Run Python
as `python3` (`python` on Windows).

## Arguments

- A week: `this` (default), `last`, or any `YYYY-MM-DD` inside the week. On a
  Monday, with no week given, report `last` week: this week has barely
  started.
- Free-text notes, such as "also ran the product demo for the client". These are
  work ADO doesn't show. Put each note in the part it belongs to.

## 1. Check access

Read `<eod>/references/access.md` and follow it, including "Who the report
is for": on `PROFILE NONE`, ask the user who they are before collecting.

```bash
python3 "<eod>/scripts/ado_eod.py" check
```

## 2. Collect

Save the JSON to a file in the scratchpad, since a week is large, and read it
from there:

```bash
python3 "<eod>/scripts/ado_eod.py" collect --week <week> > "<scratchpad>/ado-week.json"
```

`<eod>/references/data.md` describes the JSON; read it. For a week, `date`
is the Monday and `to` the Sunday; each event has a `date`; `merged_today`
means merged during the week.

If `items` is empty, say there is no ADO activity for that week, and ask
whether they meant another week or want a report from their notes only. Don't
write an empty report.

## 3. Write the report

Read `<eod>/references/report-rules.md` before writing, and apply it inside
this layout. Keep the top headings even when empty; inside Delivered and In
Progress, leave out a section with nothing in it.

```
# Weekly Report – <DD Mon> to <DD Mon YYYY>
**<profile name>** · <profile role>

## Summary
- <3-5 bullets for a reader who reads nothing else: the headline outcomes,
  the delivery count, what carries over, and any risk.>
- Delivered: <n> user stories, <n> bugs, <n> tasks; <n> PRs merged.

## Delivered This Week

### <section name, from `sections`>
- **[User Story #100] <short story name>:** <what it delivers, as an outcome> -> **Done, PR #45 merged**
  - <what this task did, in plain words> (#101, PR #45) **Done**
  - <what this task did, in plain words> (#102, PR #48) **Done**

### <next section>
- ...

## In Progress (carrying over)

### <section name>
- **[User Story #120] <short story name>:** <where it stands> -> **In progress (2/4 tasks done)**
  - <what this task did, in plain words> (#121) **Done**
  - <what this task is doing, in plain words> (#122, PR #50) **In review**

## Plan for Next Week
- ...

## Risks & Blockers
- None
```

### Weekly differences

On top of `report-rules.md`:

- **Status is the end of the week**: `state_end` and the PRs' current status.
- **Delivered This Week** holds the items whose status is Done, Done with a
  merged PR, or Resolved. A story goes here when its own status is done, or
  when it is still open but `children` (or `parent.children`) shows every
  task done (status **Tasks done, story open**).
- **In Progress (carrying over)** holds everything else: in review, in
  progress, created and scoped, blocked. A story with any open task in
  `children.open` goes here, once, with all its worked tasks as its bullets
  and its status as **In progress (done/total tasks done)**, even when
  every task worked this week is done.
- Every story, task and PR from the week appears exactly once.
- **Outcome lines** describe what the week delivered, from the titles, all the
  week's comments and changes. Don't narrate day by day, and don't invent
  work the data doesn't show.
- **Order**: inside each section, the biggest outcome first (stories that
  closed, then bugs, then the rest).

### Summary

Write it last, from the lines below it. 3-5 bullets, business words, no task
IDs; at most one story ID per bullet for a headline item. The delivery-count
bullet is required, and its numbers must match the Delivered lines: count
user stories (and other parents) and bugs from their own lines, tasks from
the task bullets (a merged bullet counts each ID), and PRs merged from
`merged_prs`.

### Plan for Next Week

Write 4 to 8 lines, inferred from what was implemented this week and what is
left. Each line names its item(s) with an ID. Most important first:

1. Blocked items: what has to happen to unblock them, and who.
2. Carry-over in progress: "Complete <work, in plain words> (#id): <next concrete step>",
   from the comments, the story's open tasks, or "finish and raise PR".
3. In review: "Get PR #n reviewed and merged".
4. Resolved: "Support QA verification of #id".
5. Delivered stories whose story is still open: "Close out #id after QA
   sign-off". Delivered work with an obvious next step in the comments, such
   as "deploy to UAT", gets that step.
6. Stories with open tasks nobody worked on this week (in `children.open`
   or `parent.children.open`, and not in `items`): "Start <task> (#id) on
   <story>".
7. Fill any free lines from `open_assigned`, highest priority first: "Start
   <title> (#id)".

Group lines on the same story into one. Don't plan work for something that is
finished with nothing left.

### Risks & Blockers

Follow the Blockers rule in `report-rules.md`. Also add a risk line for an
item that stayed in progress all week without a state change, or a PR in
review for more than three days, when the data shows it.

## 4. Print

Read `<eod>/references/clipboard.md` and follow it. The HTML headings are:
`<p><b>Weekly Report – <dates></b></p>`; `<p><b>Summary:</b></p>` with a
`<ul>`; `<p><b>Delivered This Week:</b></p>` and
`<p><b>In Progress (carrying over):</b></p>`, both sectioned; then
`<p><b>Plan for Next Week:</b></p>` and `<p><b>Risks &amp; Blockers:</b></p>`,
each with a `<ul>`.
