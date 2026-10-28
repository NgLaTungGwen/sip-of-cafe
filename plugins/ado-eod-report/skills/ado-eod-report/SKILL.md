---
name: ado-eod-report
description: "Write the user's end-of-day (EOD) report from their Azure DevOps board. Reads every work item they changed that day, groups the work into PXP, SXP, Timetable and Others, infers the upcoming tasks from what was done, lists blockers, and prints the report as plain text to copy. Signs in with an Azure DevOps PAT, or with the Azure CLI sign-in (az login) when there is no PAT. Use when the user types /ado-eod-report, or asks for their EOD report, end-of-day report, daily report or daily status from ADO."
---

# ADO EOD report

Builds the user's end-of-day report from Azure DevOps. The script reads the
board with the user's PAT, or with their `az login` session when there is no
PAT or the PAT is rejected. You turn what it returns into a short report a
senior engineering manager can read in under a minute.

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

It prints JSON:

- `items`: every work item the user changed that day. It also includes items
  assigned to them that someone else moved, such as QA closing a bug. Each
  item has:
  - `section`, `type`, `title` and `parent`;
  - `state_start` and `state_end`: the state at the start and end of the day
    (`state_start` is null when the item was created that day);
  - `events`: what changed and when. `by_me` says who made the change. Events
    also carry comments, completed hours, board columns, linked PRs and other
    changed fields;
  - `prs`: linked pull requests, with status `active`, `draft`, `completed`
    or `abandoned`, and approval counts;
  - `blocked` and `url`.
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

Use exactly this layout. Keep every section heading, even when it's empty.

```
# EOD Report

## Today Accomplished

### PXP
- **[<Type> #<id>]:** <what was done> -> <status>

### SXP
- ...

### Timetable
- <what was done>

### Others
- **[<Type> #<id>]:** <what was done> -> <status>

## Upcoming Task(s)
- ...

## Blocker(s)
- None
```

### Accomplished lines

- **One line per piece of work.** When two or more tasks under the same
  parent were worked on, write one line for the parent:
  `**[User Story #100]:** ... (tasks #101, #102) -> <status>`.
- **Say what was done, not what the ticket is called.** Use the title,
  today's comments and the changes. For example:
  - title "Retry logic for payment webhook";
  - comment "added exp. backoff, deployed to QA".
  - Write: "Added retry with backoff for failed payment webhooks; deployed to
    QA".

  Don't invent work the data doesn't show. When there is only a title, keep
  the line close to it.
- **Status words, from `state_end` and `prs`:**
  - Done, Closed or Completed: **Done**. A merged PR adds "PR #n merged".
  - Resolved: **Resolved, awaiting verification**.
  - Active, In Progress or Committed with an active PR: **In review (PR #n)**.
  - Active, In Progress or Committed without a PR: **In progress**. Add
    remaining hours when they're set, for example "In progress (~4h left)".
  - Created that day and still New: **Created and scoped**.
  - Blocked: **Blocked**. The reason goes under Blocker(s).
- **Timetable** uses plain lines without the ID prefix, like the template:
  `- Supported <who> with <what>`. Add the ID at the end in brackets when
  there is one.
- **Drop noise.** Items where the only changes were the iteration, area,
  priority, tags or board order are planning, not work. If there are three or
  more, add one Others line, such as "Backlog grooming: re-prioritised 6
  items". Otherwise leave them out.
- **Order** each section by importance: Done first, then In review, then In
  progress.
- **An empty section** gets `- No updates`.

### Upcoming Task(s)

Write 2 to 5 lines, inferred from today's work. Most important first:

1. Blocked items: what has to happen to unblock them.
2. In progress: "Continue <work> (#id): <next concrete step>". Take the next
   step from the comments, the remaining tasks under the same parent, or
   "finish and raise PR".
3. In review: "Address review comments on PR #n" or "Merge PR #n once
   approved".
4. Resolved: "Support QA verification of #id".
5. Done items with open sibling tasks under the same parent: start the next
   sibling.
6. Fill any free lines from `open_assigned`, highest priority first: "Start
   <title> (#id)".

Don't list follow-ups for work that is finished and has nothing left.

### Blocker(s)

List items with `blocked: true`, and comments that say blocked, "waiting
on", "pending from" or "depends on". Write one line each: what is blocked, on
whom or what, and since when if it's known. If there are none, write
`- None`.

### Style, for a senior engineering manager

- Plain words. Write outcomes, not activity. No ADO field names, no internal
  jargon, no times of day.
- At most about 20 words per line. Never more than one line per item.
- No item gets a second mention: if it's in Accomplished, Upcoming only says
  what's next for it.

## 4. Print

Print the report in one fenced `text` block, so the user sees exactly the
characters they will copy, `**` included:

````
```text
# EOD Report
...
```
````

Below the block, write at most three short lines:
- how many work items were read, and anything left out on purpose (planning
  noise, items only someone else touched);
- any `warnings`, such as PR status being unknown;
- on `first_run` only: the section mapping, as
  `PXP ← project/area "PXP"; Others ← "Internal Tools"`, and that they can
  change it. To change it, save the sections with
  `config --sections '[{"name":"PXP","match":["PXP","Payments"]}, ...]'`; the
  section with an empty `match` collects everything else.

Don't write to ADO. The report is only printed; the user copies it.
