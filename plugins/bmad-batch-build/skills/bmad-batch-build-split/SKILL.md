---
name: bmad-batch-build-split
description: 'BMAD batch build that ends in one PR per unit instead of one PR for the whole run. Same flow as bmad-batch-build: splits a meeting transcript or pasted work items into independent units, gets the split approved, creates the tickets (Azure DevOps or GitHub), runs one bmad-build-auto worker per unit in parallel worktrees, a lane or dependency waves, and has synthesis merge and verify everything together. Then it cuts the verified result into one branch and PR per unit, stacked where units depend on each other, each PR linked to and closing its own task. Use when the user says "batch build with separate PRs", "one PR per unit", "split PRs", "many PRs", "PR per task", or wants a batch build reviewed and merged unit by unit.'
---

# BMAD Batch Build, one PR per unit

This skill is `bmad-batch-build` with a different ending. Everything up to a
green, reviewed integration branch is the same; then, instead of one PR, the
run opens one PR per unit. Independent units each get a PR into the base
branch. Units that depend on each other form a **stack**: each PR targets the
branch of the PR below it, so every PR shows only its own unit.

The integration branch is still merged and fully verified first, so the PRs
together are known to work. It stays the single source of truth: the PR
branches are cut from it by a script, and every fix goes onto it first.

## How to run it

`<core>` is the `bmad-batch-build` skill beside this one: the directory
`../bmad-batch-build` next to this skill's own base directory (the same
plugin, or the same `.claude/skills/` folder for a copied install). If it is
missing, stop and tell the user this skill needs `bmad-batch-build` installed
beside it.

Read `<core>/SKILL.md` now and run the whole skill as written, with `<skill>`
meaning `<core>` everywhere (its scripts, references and agents), and the
changes below. Where they don't say otherwise, `<core>/SKILL.md` wins.

## Changes to bmad-batch-build

### Section 1: the run file

Write `PR mode: one per unit` into `<runs>/<slug>.md`, so a resumed run keeps
it.

### Section 2: splitting

The rules are the same, and **disjoint files** matter more here: each file
goes into exactly one unit's PR, and a file two independent units both touch
joins them into one stack. Each unit should also be reviewable alone: one
behaviour, one PR a reviewer can approve on its own.

After `plan-waves.py`, run

```bash
python "<core>/scripts/split-prs.py" stacks "<runs>/<slug>-plan.json"
```

It prints where each unit's PR goes (`PR 3 -> on 1`) and a `PRS:` line.

### Section 3: approval

Add a **PR into** column to the table (`base`, or `on <unit id>`), and paste
the `PRS:` line under the Mode line. The approval question counts PRs too,
for example `Approve the split: 4 units, 4 PRs in 2 stacks, about 300 min?`.
The Approve option's preview ends with the `PRS:` line.

After approval, create `<runs>/<slug>-split.json` (format in
`<core>/scripts/split-prs.py`) with `ibranch` and one entry per approved unit
with `id`, `title` and `depends_on`. Re-write it if the split changes later.

**Build directly** works here too: build the units in that one worktree in
dependency order, **one commit series per unit**, with `base`,
`base_branch` and `worktree` recorded as in section 4 below, and each unit's
`from` (the head before it) and `to` (the head after it) in
`<runs>/<slug>-split.json`. Then, instead of one PR, follow
`<core>/references/split-prs.md` yourself in that worktree, from S1, with
`<iwt>` being it and `<ibranch>` its branch, after a full verify of the
branch.

### Section 4: launch

Right after the fetch of step 1, record the base commit,
`git -C "<root>" rev-parse origin/<base>` (or `<base>` with `REMOTE none`),
and write that **sha** into `<runs>/<slug>-split.json` as `base`, and
`<base>` as `base_branch`; a later fetch that moves `origin/<base>` must not
change it. Add `worktree` = `<iwt>`.

### Section 6: record each unit's commits at its gate

As soon as a unit passes its gate (or the user keeps it, unreviewed or
unverified), write its range into `<runs>/<slug>-split.json`:

- **Lane unit:** `from` is the head the worker started from, the base the
  gate used; `to` is `git -C <iwt> rev-parse HEAD` right after the gate.
  Record it before the next lane worker starts: afterwards it can't be told
  apart.
- **Unit of a parallel wave:** `from` is the wave's base commit (for wave 1
  the base sha recorded at launch, never the name `origin/<base>`), `to` is
  `git -C "<root>" rev-parse <its branch>`.

A unit left out goes into `left_out`, and so does every unit that depends on
it. A re-dispatched unit gets its range from the new worker.

### Section 7: the final synthesis

Build the prompt from `<core>/references/synthesis.md` as usual. For the
final synthesis (`<final>` = `yes`) only, add this line at the top of its
prompt:

`Split PRs: this run ends in one PR per unit. After section 3b, skip sections 4, 5 and 5b and follow <core>/references/split-prs.md instead (it runs section 4b too), with the split file <runs>/<slug>-split.json, which you may also write; reply in its format (S6).`

Wave syntheses (`<final>` = `no`) are unchanged.

A `stuck` reply from the split (an unowned file it can't place, a PR branch
already pushed, a stack that fails on its own after two fixes) is handled as
section 7 of `<core>/SKILL.md` describes.

### Section 8: report

In place of the one PR link, list every PR: unit, link, target, and the
merge order of each stack. Then:

- **Stacked PRs** must be merged bottom-up. On GitHub, merging the lower PR
  and deleting its branch retargets the next one to the base by itself. On
  Azure DevOps it doesn't: after the lower PR completes, retarget the next
  one to `<base>` by hand. With squash merges, a stacked PR may show the
  lower PR's changes again until it is retargeted or updated from `<base>`.
- **Tickets:** each task closes when its own PR completes. The story is left
  open: close it after the last PR merges.
- The units left out, and anything else from `<core>/SKILL.md` section 8.

### Section 9: deploys

Check the deploys per merged PR: run `ci-status.py deploy` once for each PR
id. A deploy fix opens one follow-up PR into `<base>`, as in
`<core>/SKILL.md`.

### Clean-up

The PR branches are ordinary local and remote branches named
`<ibranch>-pr<n>`; the integration branch `<ibranch>` is kept locally and not
pushed unless the user asks. Offer to delete both after the PRs merge, with
the worktrees as `<core>/SKILL.md` describes.
