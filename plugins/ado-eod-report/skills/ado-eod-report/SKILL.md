---
name: ado-eod-report
description: "Write the user's end-of-day (EOD) report from their Azure DevOps board. Reads every work item they changed that day, groups the work into PXP, SXP, Timetable and Others, summarises it by user story for leadership (every story, task and PR kept with its status), infers the upcoming tasks, lists blockers, and puts the report on the clipboard for Teams. Signs in with an Azure DevOps PAT, or with the Azure CLI sign-in (az login) when there is no PAT. Use when the user types /ado-eod-report, or asks for their EOD report, end-of-day report, daily report or daily status from ADO."
---

# ADO EOD report

Builds the user's end-of-day report from Azure DevOps. The script reads the
board with the user's PAT, or with their `az login` session when there is no
PAT or the PAT is rejected. You turn what it returns into a short,
summarised report for leadership — Senior Engineering Manager, Engineering
Director, Sr. Engineering Director and Client Partner — that reads in under a
minute. It summarises the day by user story, yet still names every story,
task and PR with its status.

For the weekly report, use the `ado-weekly-report` skill; it runs this
skill's script.

`<skill>` below is this skill's folder. Run Python as `python3` (`python` on
Windows).

## Arguments

- A date: `today` (default), `yesterday`, or `YYYY-MM-DD`. On a Monday,
  "last Friday" means the date of that Friday.
- Free-text notes, such as "also paired with Minh on the Timetable import".
  These are work ADO doesn't show. Put each note in the section it belongs to.

## 1. Check access

```bash
python3 "<skill>/scripts/ado_eod.py" check
```

`OK <name> <email> <org> (via PAT)` or `(via az login)` means you can go
on. If you get an error line, handle it and run `check` again:

- **`ERROR NO_AUTH`**: no PAT, and no Azure CLI sign-in that works for this
  organization. Offer the two ways, Azure CLI first when the message names
  an `az login` command:
  - **Azure CLI:** they run the `az login ...` command from the error
    themselves. It opens a browser, so suggest typing it as
    `! az login --tenant ... --scope ...` in the prompt. The `--tenant` part
    matters: the organization can sit in another tenant than their default
    `az` account.
  - **PAT:**
  1. Open `https://dev.azure.com/<org>/_usersSettings/tokens` and click
     **New Token**.
  2. Select the organization, and the scopes **Work Items: Read & write**
     and **Code: Read** (Code: Read is only used for PR status).
  3. Save the token in one of these ways, in their own terminal, so it never
     appears in the chat:
     - `export AZURE_DEVOPS_EXT_PAT=<token>` in their shell profile, then
       restart Claude Code; or
     - `python3 "<skill>/scripts/ado_eod.py" config --pat-stdin`, paste the
       token, then press Enter and Ctrl-D. It's saved to
       `~/.config/ado-eod-report/pat` with owner-only access.

  Don't ask for the token in the chat. If the user pastes it anyway, save it
  with `printf '%s' '<token>' | python3 "<skill>/scripts/ado_eod.py" config --pat-stdin`
  and tell them in one line to revoke it later, since it is now in the
  transcript.
- **`ERROR NO_ORG`**: no ADO remote here and none saved. Ask for the
  organization URL or name, then run `config --org <it>`.
- **`ERROR AUTH`**: the PAT expired, was revoked or belongs to another
  organization, and `az login` didn't work either. When the message names an
  `az login --tenant ...` command, offer that first; otherwise, a new PAT (as
  above).
- **`ERROR FORBIDDEN`**: the PAT lacks the Work Items scope, or the `az`
  account has no access to the project. Ask the user to fix the PAT's scopes,
  or to get access to the project.
- **`ERROR NETWORK`**: say that Azure DevOps can't be reached, and stop.

## 2. Collect

```bash
python3 "<skill>/scripts/ado_eod.py" collect --date <date>
```

(`--days N` widens the window to N days from `--date`, and `--week
this|last|YYYY-MM-DD` takes a Monday-to-Sunday week; the weekly report uses
them. The JSON has `date`, `to` and `days` for the window.)

It prints JSON:

- `items`: every work item the user changed that day. It also includes items
  assigned to them that someone else moved, such as QA closing a bug. Each
  item has:
  - `section`, `type` and `title`;
  - `parent`: its `id`, `type`, `title`, `state` and `children`;
  - `children`: for an item that has child items (a story), how far they are,
    counting the ones nobody touched that day: `total`, `done`, and `open`
    with each open child's `id`, `title` and `state`. A child whose PR merged
    counts as done; removed and cut ones aren't counted. `parent.children` is
    the same for the parent. Null when there are no children;
  - `state_start` and `state_end`: the state at the start and end of the day
    (`state_start` is null when the item was created that day);
  - `events`: what changed and when (`date`, `time`). `by_me` says who made the change. Events
    also carry comments, completed hours, board columns, linked PRs and other
    changed fields;
  - `prs`: linked pull requests, with status `active`, `draft`, `completed`
    or `abandoned`, and approval counts;
  - `blocked` and `url`;
  - `merged_today`: the user's PRs, linked to this item, that merged that
    day. Such items are listed even when nobody touched them on the board.
- `merged_prs`: every PR the user authored that merged that day, in any
  project, with its linked `work_items`.
- `open_assigned`: open items assigned to the user that weren't touched that
  day, highest priority first.
- `warnings`, and `first_run`.

Sections come from `~/.config/ado-eod-report/config.json`. An item goes to the
first section whose `match` words appear in its project, area path,
iteration path or tags, as whole words. Anything else goes to Others.

If `items` is empty, say there is no ADO activity for that date. Ask whether
they meant another date, or want a report from their notes only. Don't write
an empty report.

## 3. Write the report

A busy day can hold many items. Don't write one long line per item: summarise
**by user story**. Each story gets one outcome line, and its tasks and PRs are
listed compactly under it, each with its status. Use exactly this layout; keep
every heading, even when it's empty.

```
# EOD Report – <Day DD Mon YYYY>

## Summary
<2-3 sentences in business terms: what was delivered, what is in review,
any risk. Lead with the outcome that matters most.>

## Today Accomplished

### PXP
- **[User Story #100] <short story name>:** <what it delivers, as an outcome> -> **<status>**
  - Tasks: #101 <2-4 word label> **Done** · #102 <label> **In progress**
  - PRs: #45 **Merged** · #46 **In review**
- **[Bug #210] <short name>:** <what was fixed> -> **<status>**

### SXP
- ...

### Timetable
- <what was done>

### Others
- ...

## Upcoming Task(s)
- ...

## Blocker(s)
- None
```

### Grouping

- **A story line**: one line per user story (or other parent: Bug, Feature,
  Product Backlog Item) that had work that day, on itself or on one of its
  tasks. Each item's `parent` says which story it belongs to. A story whose
  own parent is a Feature or Epic is still its own top-level line; it may
  name the feature in a few words.
- **Tasks line**: every task worked on under that story, as
  `#id <label> **<status>**`, separated by ` · `. The label is 2-4 plain
  words. With more than 6 tasks, group them by status instead:
  `Tasks: 5 **Done** (#101, #102, #103, #104, #105) · 1 **In progress** (#106)`.
  Leave the line out when the story has no tasks in the data.
- **PRs line**: every PR linked to the story or its tasks, once each, with its
  status. Leave it out when there are none.
- **Items with no parent** (a standalone task, bug or story) get one line
  each. One PR goes in the status (`-> **In review (PR #46)**`); a `PRs:`
  sub-line only when there are several.
- **The story's status** is its own, from `state_end` and its PRs, by the rules
  below. When the story itself wasn't touched, use `parent.state` and
  `parent.children`: when the story is still open and every child is done,
  **Tasks done, story open**; when some are open, **In progress (2/4 tasks
  done)**, from `done` and `total`. A story that is an item itself has the
  same counts in its own `children`; add "(2/4 tasks done)" to an In progress
  status there too.

### What to write

- **Outcomes, not activity, not ticket titles.** Use the title, the day's
  comments and the changes. For example:
  - title "Retry logic for payment webhook";
  - comment "added exp. backoff, deployed to QA".
  - Write: "Failed payment webhooks now retry with backoff; on QA".

  Don't invent work the data doesn't show. When there is only a title, keep
  the line close to it.
- **The Summary** is written last, from the lines below it, for a reader who
  reads nothing else: 2-3 sentences, no task IDs, at most one story ID for the
  headline item. Add counts when they help, such as "2 stories completed,
  3 PRs merged"; they must match the lines below.
- **Status words**, from `state_end` and `prs`:
  - A merged PR wins over the board: an item with `merged_today`, or a
    `completed` PR, is **Done, PR #n merged** whatever its state says. The
    board often still shows New after a merge. With several merged PRs it is
    **Done, n PRs merged**, and the PRs line names them. In a Tasks or PRs
    line this is just **Done** or **Merged**.
  - Done, Closed or Completed: **Done**.
  - Resolved: **Resolved, awaiting verification**.
  - Active, In Progress or Committed with an active PR: **In review (PR #n)**.
  - Active, In Progress or Committed without a PR: **In progress**. Add
    remaining hours when they're set, for example "In progress (~4h left)".
  - Created that day and still New: **Created and scoped**.
  - Blocked: **Blocked**. The reason goes under Blocker(s).
  - PRs: **Merged**, **In review**, **Draft**, **Abandoned**.
- **Timetable** uses plain lines without the ID prefix:
  `- Supported <who> with <what>`. Add the ID at the end in brackets when
  there is one.
- **Drop noise.** Items where the only changes were the iteration, area,
  priority, tags or board order are planning, not work. If there are three or
  more, add one Others line, such as "Backlog grooming: re-prioritised 6
  items". Otherwise leave them out.
- **Order** each section by importance: Done first, then In review, then In
  progress.
- **Merged PRs with no linked work item** still appear, in the section their
  project matches. Group them by theme, one line per theme:
  `**[PR #716, #717, #718] Build and pipeline fixes:** <what they achieved> -> **Merged**`.
  One PR alone is `**[PR #<n>] <short name>:** ...`.
- **Section headings** are the configured sections, in their order; the
  template's names are the defaults. **An empty section** gets `- No updates`.

### Upcoming Task(s)

Write 2 to 5 lines, inferred from today's work. Most important first:

1. Blocked items: what has to happen to unblock them.
2. In progress: "Continue <work> (#id): <next concrete step>". Take the next
   step from the comments, the remaining tasks under the same parent, or
   "finish and raise PR".
3. In review: "Address review comments on PR #n" or "Merge PR #n once
   approved".
4. Resolved: "Support QA verification of #id".
5. Done items whose story still has open tasks (in `children.open` or
   `parent.children.open`): "Start <task> (#id)" for the next one.
6. Fill any free lines from `open_assigned`, highest priority first: "Start
   <title> (#id)".

Don't list follow-ups for work that is finished and has nothing left.

### Blocker(s)

List items with `blocked: true`, and comments that say blocked, "waiting
on", "pending from" or "depends on". Write one line each: what is blocked, on
whom or what, the impact, and since when if it's known. If there are none,
write `- None`.

### Style, for engineering and client leadership

- Plain business words. Outcomes and impact, not activity. No ADO field
  names, board columns, internal jargon, times of day, or file and function
  names.
- A story line is at most about 20 words before the status. Tasks and PRs
  lines carry only IDs, short labels and statuses.
- No item gets a second mention: a task listed under its story never gets its
  own line; if it's in Accomplished, Upcoming only says what's next for it.

## 4. Print

The user pastes the report into Teams, which shows markdown as raw `#` and
`**`. So also put it on the clipboard as rich text:

1. Write the same report as an HTML fragment to a file in the scratchpad:
   `<p><b>EOD Report – <date></b></p>`; `<p><b>Summary:</b></p>` and the
   summary in a `<p>`; `<p><b>Today Accomplished:</b></p>` with one `<ul>`
   whose items are the sections in bold, each holding a nested `<ul>` of the
   story lines, and each story a further `<ul>` with its Tasks and PRs lines;
   then `<p><b>Upcoming task(s):</b></p>` and `<p><b>Blocker(s):</b></p>`,
   each with a `<ul>`. Bold the `[Type #id] name:` prefix and every status
   word. Escape `&`, `<`, `>` and write `->` as `-&gt;`.
2. On Windows, copy it:
   `powershell -NoProfile -STA -File "<skill>/scripts/copy_html.ps1" <file.html>`.
   Elsewhere, skip this step.

Then print the report in one fenced `text` block too, as a fallback:

````
```text
# EOD Report – ...
...
```
````

Below the block, say first that the report is on the clipboard and pastes
into Teams with Ctrl+V. Then write at most three short lines:
- how many work items were read, how many story lines they rolled up into,
  and anything left out on purpose (planning noise, items only someone else
  touched);
- any `warnings`, such as PR status being unknown;
- on `first_run` only: the section mapping, as
  `PXP ← project/area "PXP"; Others ← "Internal Tools"`, and that they can
  change it. To change it, save the sections with
  `config --sections '[{"name":"PXP","match":["PXP","Payments"]}, ...]'`; the
  section with an empty `match` collects everything else.

Don't write to ADO. The report is only printed; the user copies it.
