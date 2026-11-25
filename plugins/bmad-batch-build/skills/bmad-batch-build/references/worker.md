# Worker

One worker per unit. It runs `bmad-build-auto` inside the unit's worktree and
nowhere else. A unit in a parallel wave has its own worktree and branch; the
units of a lane wave take turns in one worktree on the run's branch (see "Lane
mode" below). Plain parallel and plain lane runs are one-wave and all-lane
cases of the same thing.

## Pinned to its worktree

`scripts/prepare-worktree.sh` writes `<wt>/_bmad/custom/bmad-build-auto.toml`
into the worktree's private copy of `_bmad`. It adds a persistent fact and an
`implementation_handoff` that both name `<wt>` as the only place work may
happen, so the implementation subagent stays there as well.

The same file takes two waits off the worker's critical path:

- **Install during planning.** The worker starts the install in the
  background and goes straight to planning, which only reads the checkout. The
  Haiku coder waits for the install (`bg.sh wait <wt> install`) before its
  first command. Its context is still tiny then, so a long wait costs little
  even when its cache expires.
- **Affected tests during review.** The worker starts the unit's affected
  tests in the background as soon as the coder returns and launches the
  review at once, so the tests and the reviewers run side by side. It reads
  their result before triage. One idle gap instead of two in a row, on the
  agent with the largest context.
- **Only the tests the change reaches.** `scripts/affected-tests.sh`, pinned
  with the commit the worktree started at, runs `vitest run --changed <start>`
  in each touched vitest workspace (jest: `--changedSince`), and in a bun
  workspace such as `apps/` the changed test files and the ones beside the
  changed sources. It runs the full suite (and `apps/`'s `bun test`) only when
  the unit touches a path that can break any test: `affected.py`'s
  `FORCE_FULL` list (packages/contracts, migrations, test setup, vitest,
  vite, jest or tsconfig config, a package.json or lockfile, the profile), or
  a workspace whose runner it doesn't know. The full suite runs once, in
  synthesis.

## Dispatch prompt

Send every worker in **one message**, with one `Agent` call per unit
(`subagent_type: <agents>bmad-batch-worker`, `run_in_background: false`, no
`model`; `<agents>` is the prefix SKILL.md set).
That agent (`agents/bmad-batch-worker.md`) runs on Opus with a 1-hour
prompt cache, so its context survives the waits on its coder, its reviewers
and the test suite. The worker plans, verifies and triages the review. The
review is sized by risk through the pinned toml's per-layer `when` gates,
which read the `Risk:` line below: `low` gets the edge-case and
verification-gap layers, `med` adds the blind hunter, and `high`, or an
interface changed from the agreed shape, adds the intent auditor. A dispatch
with no `Risk:` line (no intake) keeps the 0.16.0 rules, which read `Size:`:
an S unit under 400 changed lines that touches no contract, migration or auth
code gets only the edge-case and verification-gap layers; the intent auditor
runs only for L units or when an interface changed. Every review layer runs
on Sonnet (a pinned fact replaces step-04's same-model rule); only the plan
needs Opus. All keep the default 5-minute cache. The worker itself stays on
Opus because it writes the plan, and Sonnet workers skipped the review in 31 of 47 units
(2026-10-21/22), while Opus workers completed the workflow in 62 of 67. The
code itself is written by the `<agents>haiku-implementer` agent that the worktree's
`implementation_handoff` names. Fill in the placeholders:

- `<root>`: the absolute `<root>` from SKILL.md
- `<skill>`: the absolute `<skill>` from SKILL.md
- `<agents>`: the agent name prefix from SKILL.md (`bmad-batch-build:` or empty),
  or `general-purpose` in fallback mode
- `<name>`: `<slug>-<n>`
- `<budget>` and `<estimate>`: the unit's `budget` and `p50` under `unit_estimates` in the
  plan file, in minutes
- `<size>`: the unit's size from the split (S, M or L)
- `<intake>`: `<runs>/<slug>-intake.md`, the frozen intake, and `<risk>` the
  unit's `risk` there (low, med or high). `<iface>` is
  `<runs>/<slug>-interface-changes.md`, which may not exist yet. A run without
  an intake leaves out the `Intake:`, `Risk:` and `Interface changes:` lines,
  the `--intake`, `--unit` and `--iface` arguments and the Intake section of
  the prompt.
- `<template>`: the `DB-TEMPLATE` name from SKILL.md section 4; leave out
  `--db-template` when it printed `none`
- `<wt>`: `<root>/.claude/worktrees/<name>`
- `<branch>`: the unit's branch
- `<base>`: the wave's base. For wave 1 that is `origin/<base>` (SKILL.md), and the
  `--base` argument is left out. For a later wave it is `<ibranch>` (a local
  branch; worktrees share the repository)

```text
You are a Build Auto worker for one unit of a BMAD batch build.
Unit: <n>
Size: <size>
Risk: <risk>   (the review layers' `Run only when` lines read this, and Size: when there is no Risk:)
Intake: <intake>
Interface changes: <iface>
Budget: <budget> min (about <estimate> expected). Run `date +%s` first and keep the number.

Project root: <wt>   (a git worktree on branch <branch>, which you create in step 0)
Work only inside <wt>. Never read, edit or run anything elsewhere in <root> (the main checkout) or in another worktree.

0. Prepare the worktree:
   a. As one foreground Bash call with `timeout: 600000`:
      bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<name>" "<branch>" --no-install --agents "<agents>" [--base <base>] [--db-template <template>] [--intake "<intake>" --unit <n> --iface "<iface>"]
      If it fails, reply with:
      SPEC: none
      STATUS: blocked
      BLOCKED: worktree preparation failed: <the last lines of its output>
      HEAD: none
      INTERFACES: none
   b. Start the install as one Bash call with `run_in_background: true`, and
      don't wait for it:
      bash "<skill>/scripts/bg.sh" run "<wt>" install "bash '<skill>/scripts/install-deps.sh' '<wt>' [--apps]"
      (add `--apps` only if this unit touches apps/). It queues through
      heavy.sh and retries each step once. Planning needs only the checkout;
      the workflow's pinned facts say when to wait for the install.
   c. Read your part of the intake, once, before planning:
      python "<skill>/scripts/intake.py" unit "<intake>" <n>
      It prints the Goal, the Non-goals, the Shared interfaces, your unit's
      block and the full text of its acceptance criteria. Those ACs are the
      ones your spec must meet, and they are part of the intent below.
      Shared interfaces are pinned: keep each one in the shape listed there.
      You may not change a pinned interface. If the unit cannot be built
      without changing one, stop (start no further implementation round) and
      reply with:
      SPEC: <the spec path, or none>
      STATUS: blocked-interface
      BLOCKED: <the interface, why it must change, and the shape you need: <old> -> <new>>
      HEAD: <output of `git -C <wt> rev-parse HEAD`>
      INTERFACES: <as in step 3>
      The orchestrator decides. When it approves, it redispatches you with a
      line `Approved interface change: <symbol>: <old> -> <new>`. Then, before
      changing the code, record it for the other units:
      python "<skill>/scripts/intake.py" iface add "<iface>" --unit <n> --iface "<symbol>" --change "<old> -> <new>" --reason "<why>"
      Name the interface exactly as Shared interfaces spells it, here and in
      your INTERFACES line: the gate matches the two.
      Before every affected-test or full-suite run, run
      python "<skill>/scripts/intake.py" iface show "<iface>"
      It prints the changes approved for other units, or `none`. Adapt your
      code to any that touch an interface you provide or consume before the
      tests start (the pinned facts repeat this at that point).
1. Run this exactly once, from any directory:
   uv run --no-cache "<wt>/_bmad/scripts/render_skill.py" --project-root "<wt>" --skill "<wt>/.claude/skills/bmad-build-auto"
2. Read the one absolute workflow.md path it prints, and follow it to its end
   with the intent below. Run every step, in order: plan, implement through
   the implementation handoff, review. Subagents you launch must run in the
   foreground, as that workflow requires: launch them without
   `run_in_background`. Claude Code may still start one in the background
   (the Agent tool answers "Async agent launched"). **Never end your turn
   while an agent you launched is still running**: for you, ending the turn
   hands you back to the orchestrator half done. So, for every subagent
   (the implementation subagent of each round and every reviewer):
   a. Give it a name, such as `impl-1` or `review-1-edge`, and run
      `bash "<skill>/scripts/bg.sh" expect "<wt>" <agent name>` first.
   b. End its prompt with this line: `Your last step, after your report is
      written: run bash "<skill>/scripts/bg.sh" done "<wt>" <agent name>
      (add 1 at the end if you could not finish), then reply with your report.`
   c. If the Agent call answers "Async agent launched", block on
      `bash "<skill>/scripts/bg.sh" wait "<wt>" <agent name>`, one per Bash
      call (each returns within 100 s), again while it prints still running.
      When it prints ok or failed, the agent's report arrives as its
      completion notification; work on that report. `done` runs just before
      the agent's reply, so if the report isn't there yet, call `wait` once
      more (it returns at once) instead of ending your turn. Until then,
      don't start the next workflow step, launch another agent on the same
      files, or write the reply in step 3. An agent can also end without
      running `done` (turn budget, an error): after 40 replies of still
      running in a row (about an hour), or once your budget is used up, stop
      waiting and end your turn; the orchestrator resumes you when the
      agent's notification reaches it.
   A spec written after the code, or a skipped review, fails the gate and the
   unit is not merged.
   Budget: before you start any further fix round, compare `date +%s` with the
   number you kept. Once the budget is used up, start no new round: finish the
   one you are in, record the findings still open in the spec's review log as
   deferred, and list them under REMAINING in your reply. The orchestrator
   builds them as a unit of their own; a unit that runs on past its budget
   holds up everything behind it (sonar-zero-5: 315 min, about 12 coder
   rounds).
3. When the workflow halts, and only once every implementation subagent you
   launched has returned, reply with exactly these lines and nothing else:
   SPEC: <absolute path of the spec or result file the workflow wrote>
   STATUS: <the status in its frontmatter, or blocked-interface (step 0c)>
   BLOCKED: <blocking condition, or none>
   HEAD: <output of `git -C <wt> rev-parse HEAD`>
   INTERFACES: <each interface this unit provides or consumes, as built:
   route and payload, type, or method signature, one per `;`. Mark any that
   differ from the intent's agreed shape with "CHANGED"; a pinned one by its
   Shared interfaces name. Or "none".>
   REMAINING: <the findings or acceptance criteria you did not finish because the
   budget ran out, one per `;`, or "none">
   SKILL ISSUES: <each time a skill script, its settings or these
   instructions got in the way and you worked around it: the command, the
   error line and the workaround, separated by ` | `. Not faults in the project's
   own code. Or "none".>

Intent:
<the unit's self-contained intent: goal, scope, the files it owns, acceptance
criteria in Given/When/Then, and the excerpt of the source material it came
from. The worker sees nothing else. In a later wave, start it with the
"Already on the branch" list: each earlier unit's title, spec path and
INTERFACES line, and the note that this unit builds on that code as it is.>
```

## Gate

Don't trust the reply text alone. Check every unit in **one** Bash call:

```bash
for u in "<wt1>|<branch1>|<spec1>|" "<wt2>|<branch2>|<spec2>|--bug"; do
  IFS='|' read -r wt br spec kind <<< "$u"
  printf '%s: ' "$br"; bash "<skill>/scripts/check-unit.sh" $kind "$wt" "$br" "$spec"
done
```

The fourth field is `--bug` for a unit whose intent starts with `Kind: bug`,
and empty otherwise.

In a run with an intake, add the interface check to each unit's call, with
the worker's `INTERFACES` value:
`--intake "<intake>" --unit <n> --iface-file "<iface>" --reply-interfaces "<INTERFACES>"`.
It fails the unit when an item marked `CHANGED` names an interface listed
under Shared interfaces and `<iface>` has no entry for that unit and
interface (`pinned interface '<symbol>' marked CHANGED with no approved
entry ...`): the worker changed a pinned interface without an approved
exception. A `STATUS: blocked-interface` reply is not gated: it is a failed
unit that needs the user's decision on the interface (SKILL.md section 6).

The script prints `READY <commits>` or `NOT READY: <reasons>` for each unit.
It checks the spec status (`done`, `built` or `in-review`), and the workflow's
own traces in the spec: `baseline_revision`, `<intent-contract>` and at least
one `### <date> — Review pass` entry under `## Review Triage Log`. It also
checks for a clean tree, the right branch, and at least one commit ahead of
`origin/<base>` (or of the base passed as a fourth argument). With `--bug`
it requires the spec's `## Reproduction` to hold a `RED:` and a `GREEN:`
line: the bug reproduced before the fix and gone after it. When the project
profile has an `app`, it requires the unit's check in the running app
(`app-<spec name>`, `scripts/app-check.sh`) to have passed at the code the
branch holds now. In a later wave,
pass the wave's base revision as the fourth argument. Synthesis merges
the branch ref, so a worker that moved to another branch leaves nothing to
merge.

| Result | Meaning |
|---|---|
| `READY` | Ready to merge. |
| `NOT READY` | Record the unit, the reasons and the spec path. A missing workflow trace means the worker skipped plan or review: its code is unreviewed. A missing reproduction or app check means its code is unverified. SKILL.md section 6 decides what happens next. |

## Lane mode

The units of a lane wave run one after another in a single worktree, so
nothing is merged and only one worktree is installed. The differences from a
parallel unit:

- `<name>` is `<slug>` and `<branch>` is the run's branch
  `<run branch>` for every unit, so `<wt>` is the same for all of them.
  In a run that already has `<iwt>` (a lane wave after a parallel one), `<wt>`
  is `<iwt>` and every unit uses the later-unit prompt below.
- Dispatch one worker at a time, in the order the split set, and wait for it.
  Before each dispatch, record `git -C <wt> rev-parse HEAD` (for the first
  unit, `origin/<base>`) as that unit's base.
- The first unit's prompt is the one above. Add `--apps` to its step 0b if
  **any** unit in the lane touches `apps/`. Later units find the install done,
  so their waits return at once.
- Every lane unit's intent, the first one's included, starts with this line,
  so bmad-build-auto's branch check doesn't halt on the shared name:
  `This unit runs on the run's shared branch <branch> on purpose; the branch
  name covers the whole run, not this unit.`
- Every later unit's prompt replaces step 0 with:
  ```text
  0. The worktree is already prepared and on <branch>; earlier units of this
     run are committed on it. Run `git -C <wt> status --porcelain` and
     `git -C <wt> branch --show-current`. If the tree is dirty or the branch is
     not <branch>, reply with:
     SPEC: none
     STATUS: blocked
     BLOCKED: lane worktree not clean or on the wrong branch
     HEAD: none
     INTERFACES: none
  ```
  and its intent starts with a short "Already on the branch" list: for each
  earlier unit, its title, its spec path and the `INTERFACES` line its worker
  returned. The worker builds on those interfaces as built, and opens an
  earlier spec only when it needs more, instead of re-exploring that code.
- Gate each unit as soon as its worker returns, with its base as the fourth
  argument:
  `bash "<skill>/scripts/check-unit.sh" [--bug] "<wt>" "<branch>" "<spec>" "<base>"`.
  A `NOT READY` unit stops the lane, because later units build on it. SKILL.md
  section 6 decides what happens next.
