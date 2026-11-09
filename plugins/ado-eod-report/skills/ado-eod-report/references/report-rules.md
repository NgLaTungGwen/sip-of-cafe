# Report rules: grouping, status words, style

Shared by the EOD and weekly reports. Each skill's SKILL.md gives its own
layout, ordering and follow-up section; these rules apply inside it. "The
window" is the report's day or week.

## Grouping

A busy window can hold many items. Don't write one long line per item:
summarise **by user story**. Each story gets one outcome line, and under it
one plain-language bullet per piece of work, saying what was done.

- **A story line**: one line per user story (or other parent: Bug, Feature,
  Product Backlog Item) that had work in the window, on itself or on one of
  its tasks. Each item's `parent` says which story it belongs to. A story
  whose own parent is a Feature or Epic is still its own top-level line; it
  may name the feature in a few words.
- **Task bullets**: under the story, one bullet per task worked on, written
  for a reader who doesn't know the ticket: what was built, fixed or changed,
  and what it means for the product, in 8-15 plain words. The IDs come after
  the sentence, in brackets, with the task's PR when it has one; the status
  last:
  `- Failed payment webhooks now retry automatically instead of being lost (#101, PR #45) **Done**`.
  Take the wording from the task title, its comments in the window and its
  PR title, never the bare ticket title or a 2-word label like
  "API changes". Don't invent detail the data doesn't show; when only a
  title exists, turn it into a plain sentence close to it.
- **Many tasks**: with more than 6, merge tasks of the same theme into one
  bullet, listing their IDs:
  `- Built the upload, validation and preview steps of the import screen (#101, #102, #103) **Done**`.
  Keep each story to at most 6 bullets.
- **PRs**: a PR goes in the brackets of the task it implements. A PR linked
  only to the story gets its own bullet saying what it changes:
  `- PR #48: removed the old export path now that the new one is live **Merged**`.
  Every PR appears once.
- **Items with no parent** (a standalone task, bug or story) get one line
  each, written the same plain way. Their PR goes in the status
  (`-> **In review (PR #46)**`); with several PRs, one bullet each under it.
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
  Its sub-issues are its task bullets, with the PRs that close them in the
  brackets. An issue with no parent and no sub-issues is a standalone line.
- **Status words:** a closed issue (`COMPLETED`) is **Done**, or
  **Done, PR #n merged** when a merged PR closes it; `NOT_PLANNED` is
  **Closed, not planned**. An open issue with an open PR is
  **In review (PR #n)**; without one, **In progress**.
- **Merged PRs that close no issue** go in their section, grouped by theme
  like ADO PRs with no work item. A PR whose title is the same as an issue's
  goes in that issue's brackets.
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
  **Done, n PRs merged**, and the bullets name them. On a task or PR bullet
  this is just **Done** or **Merged**.
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
- A story line is at most about 20 words before the status; a task bullet
  8-15 words. Write them so someone outside the team understands what changed
  without opening the ticket: no bare ticket titles, no ID-only lists.
- No item gets a second mention: a task listed under its story never gets its
  own line, and the follow-up section only says what's next for an item.
- **Follow-up lines** (upcoming tasks, plan for next week) also say the work
  in plain words first, then the ID: "Finish the retry dashboard so support
  can see failed payments (#122)", not "Continue #122".
- Don't plan follow-ups for work that is finished and has nothing left.
