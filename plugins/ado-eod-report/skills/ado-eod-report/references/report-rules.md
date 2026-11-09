# Report rules: grouping, status words, style

Shared by the EOD and weekly reports. Each skill's SKILL.md gives its own
layout, ordering and follow-up section; these rules apply inside it. "The
window" is the report's day or week.

## Grouping

A busy window can hold many items. Don't write one long line per item:
summarise **by user story**. Each story gets one outcome line, and its tasks
and PRs are listed compactly under it, each with its status.

- **A story line**: one line per user story (or other parent: Bug, Feature,
  Product Backlog Item) that had work in the window, on itself or on one of
  its tasks. Each item's `parent` says which story it belongs to. A story
  whose own parent is a Feature or Epic is still its own top-level line; it
  may name the feature in a few words.
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
- **The story's status** is its own, from `state_end` and its PRs, by the
  status words below. When the story itself wasn't touched, use
  `parent.state` and `parent.children`: when the story is still open and
  every child is done, **Tasks done, story open**; when some are open,
  **In progress (2/4 tasks done)**, from `done` and `total`. A story that is
  an item itself has the same counts in its own `children`; add
  "(2/4 tasks done)" to an In progress status there too.

## GitHub work

When the JSON's `github` is set, its work goes in the same report, in the
section each item names, by the same rules:

- **An issue with sub-issues** is a story line: `**[Issue #41] <name>:**`.
  Its sub-issues are its Tasks line, and the PRs that close them its PRs
  line. An issue with no parent and no sub-issues is a standalone line.
- **Status words:** a closed issue (`COMPLETED`) is **Done**, or
  **Done, PR #n merged** when a merged PR closes it; `NOT_PLANNED` is
  **Closed, not planned**. An open issue with an open PR is
  **In review (PR #n)**; without one, **In progress**.
- **Merged PRs that close no issue** go in their section, grouped by theme
  like ADO PRs with no work item. A PR whose title is the same as an issue's
  can go on that issue's PRs line.
- **Counts:** issue stories count as user stories, sub-issues as tasks, and
  `github.merged_prs` add to the PRs merged; say how many came from each
  source when both have work.
- **Follow-ups** also come from `github.open_prs` and
  `github.open_assigned`, the same way as their ADO counterparts.

## What to write

- **Outcomes, not activity, not ticket titles.** Use the title, the window's
  comments and the changes. For example:
  - title "Retry logic for payment webhook";
  - comment "added exp. backoff, deployed to QA".
  - Write: "Failed payment webhooks now retry with backoff; on QA".

  Don't invent work the data doesn't show. When there is only a title, keep
  the line close to it.
- **A section whose `style` is `plain`** (set in the user's config) uses
  plain lines without the ID prefix: `- Supported <who> with <what>`. Add the
  ID at the end in brackets when there is one. Every other section uses story
  lines.
- **Drop noise.** Items where the only changes were the iteration, area,
  priority, tags or board order are planning, not work. If there are three or
  more, add one Others line, such as "Backlog grooming: re-prioritised 6
  items". Otherwise leave them out.
- **Merged PRs with no linked work item** still appear, in the section their
  project matches. Group them by theme, one line per theme:
  `**[PR #716, #717, #718] Build and pipeline fixes:** <what they achieved> -> **Merged**`.
  One PR alone is `**[PR #<n>] <short name>:** ...`.
- **Section headings** come only from the JSON's `sections`, in its order.
  Never use a section name that isn't there: no project is built in. With
  `sections_from: "project"`, each section is an Azure DevOps project the
  user worked in; with `"config"`, it is the user's own list.

## Status words

From `state_end` and `prs`:

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
- Created in the window and still New: **Created and scoped**.
- Blocked: **Blocked**. The reason goes in the blockers section.
- PRs: **Merged**, **In review**, **Draft**, **Abandoned**.

## Blockers

List items with `blocked: true`, and comments that say blocked, "waiting
on", "pending from" or "depends on". Write one line each: what is blocked, on
whom or what, the impact, and since when if it's known. If there are none,
write `- None`.

## Style, for engineering and client leadership

The readers are the Senior Engineering Manager, Engineering Director,
Sr. Engineering Director and Client Partner.

- Plain business words. Outcomes and impact, not activity. No ADO field
  names, board columns, internal jargon, dates or times of individual
  changes, or file and function names.
- A story line is at most about 20 words before the status. Tasks and PRs
  lines carry only IDs, short labels and statuses.
- No item gets a second mention: a task listed under its story never gets its
  own line, and the follow-up section only says what's next for an item.
- Don't plan follow-ups for work that is finished and has nothing left.
