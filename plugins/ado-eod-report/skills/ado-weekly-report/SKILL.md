---
name: ado-weekly-report
description: "Write the user's weekly status report from their Azure DevOps board (and GitHub), in the team's weekly template: a two-column table, Planned and Accomplished then Plan for Next Week, each with People Manager's comments left empty. Reads every work item they changed that week (Monday to Sunday), writes the project work per project and theme as plain past-tense activity lines, asks for their personal learning and other notes, infers next week's plan, and puts the table on the clipboard for Excel, Teams or Outlook. Uses the ado-eod-report skill's script and sign-in. Use when the user types /ado-weekly-report, or asks for their weekly report, weekly status, week summary or weekly update from ADO."
---

# ADO weekly report

Builds the user's weekly report in the team's template: a two-column table
whose left column is the report and whose right column, **People Manager's
comments**, stays empty for the manager to fill in. The project work comes
from Azure DevOps (and GitHub, when signed in); the personal learning and
other notes come from the user.

`<eod>` below is the `ado-eod-report` skill's folder, the sibling of this
one (`../ado-eod-report` from this skill's folder: both ship in the
`ado-eod-report` plugin, and a copied install puts them side by side in
`~/.claude/skills/`). This skill runs `<eod>/scripts/ado_eod.py` and follows
the files in `<eod>/references/`, the same ones the EOD report uses. Each
step below says which file to read. Read it at that step; don't work from
memory. Run Python as `python3` (`python` on Windows).

## Arguments

- A week: `this` (default), `last`, or any `YYYY-MM-DD` inside the week. On a
  Monday, with no week given, report `last` week: this week has barely
  started.
- Free-text notes: work ADO doesn't show (a client meeting, a demo), personal
  learning (certificates, English, courses), concerns or ideas, and plans for
  next week. Put each note in the row it belongs to.

## 1. Check access

Read `<eod>/references/access.md` and follow it, including "Who the report
is for": on `PROFILE NONE`, ask the user who they are before collecting.

```bash
python3 "<eod>/scripts/ado_eod.py" check
```

## 2. Collect, and ask for what the board can't know

Save the JSON to a file in the scratchpad, since a week is large, and read it
from there:

```bash
python3 "<eod>/scripts/ado_eod.py" collect --week <week> > "<scratchpad>/ado-week.json"
```

`<eod>/references/data.md` describes the JSON; read it. For a week, `date`
is the Monday and `to` the Sunday; each event has a `date`; `merged_today`
means merged during the week.

The board has no personal learning, concerns or learning plans. Unless the
notes already cover them, ask the user once, in one message (AskUserQuestion
when you have it, with "None" as an option for each):

1. Personal learning and development this week (certificates, English,
   courses, self-study).
2. Others: concerns, improvement suggestions, innovation ideas.
3. Personal learning planned for next week.
4. Anything else they worked on that the board doesn't show (meetings,
   reviews, demos, client calls).

If `items` and `github.items` are both empty, say there is no tracked
activity for that week, and ask whether they meant another week or want a
report from their notes only.

## 3. Write the report

Read `<eod>/references/report-rules.md` first: its grouping by story, noise,
status and "what to write" rules decide what each line says. This template
then replaces its layout and format: no ticket IDs, no bold status words, no
Tasks or PRs sub-lines.

Use exactly this layout, as plain lines (each `-` line has no space after the
dash, as in the team's template):

```
Weekly Report – <DD Mon> to <DD Mon YYYY>
<profile name> · <profile role>

Planned and Accomplished | People Manager's comments
Project Development Activities
(Summary of project tasks, issues, and obstacles)

<Section>:
<Theme>:
-<Past-tense activity line>
-<...>
<Theme>:
-<...>

<Next section>:
...

*) Personal Learning and Development Activities (Certificates, English...)
- <from the user>

*) Others (Concerns, Improvement Suggestions, Innovation Ideas...)
- <from the user>

Plan for Next Week | People Manager's comments
Project Development Activities
- <next step>

*) Personal Learning and Development Activities
- <from the user>
```

### Project Development Activities

- **Sections**: one `<Section>:` per entry in the JSON's `sections` that has
  work, in that order; leave out sections with none.
- **Themes**: inside a section, group the stories into 1-4 themes and give
  each a short heading ending in a colon, named for the kind of work, such as
  `Requirements & Scoping:`, `Dashboard & Analytics:`,
  `Delivery Pipeline & Infrastructure:`, `Code Quality:`. A section with only
  2-3 lines can skip the theme heading.
- **Activity lines**: one line per user story, issue story or standalone
  item, sometimes two for a large story. Each line starts with a past-tense
  verb (Delivered, Built, Added, Fixed, Reviewed, Defined, Presented,
  Aligned, Migrated) and says what was done and what it gives the product or
  the client, in 12-30 plain words. Fold the story's tasks into the sentence
  ("...with cost, token and budget-burn charts on the admin and engagement
  screens"); don't list them. No IDs, PR numbers, branch, file or field
  names.
- **State in words**: work that isn't finished says so in the line: "started",
  "in review", "awaiting QA", "blocked on <what>". Finished work needs no
  status word.
- **Merged PRs with no work item** fold into the theme they belong to, one line
  per theme of related fixes ("Fixed build and validation failures so the
  main pipeline stays green").
- **Obstacles**: a blocker or risk from the Blockers rule in
  `report-rules.md` goes as a line in its section: `-Obstacle: <what>, waiting
  on <whom>, <impact>`.
- **Notes**: work from the user's notes goes in the section and theme it
  belongs to, written the same way.
- **Order**: inside each section, the biggest outcome first.

### Personal Learning and Others

Write the user's answers as `- ` lines, in their words, lightly tidied. When
they said none, write `- None`. Never invent learning or concerns.

### Plan for Next Week

**Project Development Activities**: 2-6 lines, inferred from what was done
and what is left, most important first, with no IDs:

1. Blocked work: what has to happen to unblock it, and who.
2. Carry-over: "Continue <work>: <next concrete step>", from the comments, the
   story's open tasks, or "finish and raise PR".
3. In review: "Get <work> reviewed and merged".
4. Resolved or delivered stories awaiting QA: "Support QA verification of
   <work>".
5. Open tasks nobody worked on this week (`children.open`,
   `parent.children.open`) and `open_assigned`, highest priority first:
   "Start <work>".

Group lines on the same work. Don't plan work that is finished with nothing
left.

**Personal Learning and Development Activities**: the user's answer to
question 3, as `- ` lines, or `- None`.

## 4. Print

Put the report on the clipboard as a table, so it pastes into the team's
Excel sheet, Teams or Outlook in the same shape:

1. Write an HTML fragment to a file in the scratchpad. Escape `&`, `<`, `>`.
   Inside a cell, end each line with
   `<br style="mso-data-placement:same-cell">` so Excel keeps the cell's lines
   in one cell. Bold the section and theme headings, and the two
   `Project Development Activities` lines.

   ```html
   <p><b>Weekly Report – <dates></b><br><b><name></b> · <role></p>
   <table border="1" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-family:Arial;font-size:10pt;width:720px">
   <tr style="background:#1F3864;color:#FFFFFF;font-weight:bold"><td width="60%">Planned and Accomplished</td><td>People Manager's comments</td></tr>
   <tr><td valign="top">project development lines</td><td></td></tr>
   <tr><td valign="top">personal learning lines</td><td></td></tr>
   <tr><td valign="top">others lines</td><td></td></tr>
   <tr style="background:#1F3864;color:#FFFFFF;font-weight:bold"><td>Plan for Next Week</td><td>People Manager's comments</td></tr>
   <tr><td valign="top">next week project lines</td><td></td></tr>
   <tr><td valign="top">next week learning lines</td><td></td></tr>
   </table>
   ```
2. On Windows, copy it:
   `powershell -NoProfile -STA -File "<eod>/scripts/copy_html.ps1" <file.html>`.
   Elsewhere, skip this step.

Then print the report in one fenced `text` block as a fallback, in the
layout of step 3. Below it, say first that the table is on the clipboard and
pastes into the weekly sheet with Ctrl+V. Then at most three short lines: how
many work items and PRs were read and how many activity lines they became,
anything left out on purpose, and any `warnings`.

Don't write to ADO. The report is only printed; the user copies it.
