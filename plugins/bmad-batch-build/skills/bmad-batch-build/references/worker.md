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
- **Full suite during review.** The worker starts the full DB suite in the
  background as soon as the coder returns and launches the review at once, so
  the suite and the reviewers run side by side. It reads the suite's result
  before triage. One idle gap instead of two in a row, on the agent with the
  largest context.

## Dispatch prompt

Send every worker in **one message**, with one `Agent` call per unit
(`subagent_type: bmad-batch-worker`, `run_in_background: false`, no `model`).
That agent (`.claude/agents/bmad-batch-worker.md`) runs on Opus with a 1-hour
prompt cache, so its context survives the waits on its coder, its reviewers
and the test suite. The worker plans, verifies and triages the review; bmad-build-auto runs
its review subagents at the worker's model, and they keep the default 5-minute
cache. Opus, not Sonnet: Sonnet workers skipped the review in 31 of 47 units
(2026-10-21/22), while Opus workers completed the workflow in 62 of 67. The
code itself is written by the `haiku-implementer` agent that the worktree's
`implementation_handoff` names. Fill in the placeholders:

- `<root>`: the absolute `<root>` from SKILL.md
- `<skill>`: the absolute `<skill>` from SKILL.md
- `<name>`: `<slug>-<n>`
- `<wt>`: `<root>/.claude/worktrees/<name>`
- `<branch>`: the unit's branch
- `<base>`: the wave's base. For wave 1 that is `origin/main`, and the
  `--base` argument is left out. For a later wave it is `<ibranch>` (a local
  branch; worktrees share the repository)

```text
You are a Build Auto worker for one unit of a BMAD batch build.

Project root: <wt>   (a git worktree on branch <branch>, which you create in step 0)
Work only inside <wt>. Never read, edit or run anything elsewhere in <root> (the main checkout) or in another worktree.

0. Prepare the worktree:
   a. As one foreground Bash call with `timeout: 600000`:
      bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<name>" "<branch>" --no-install [--base <base>]
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
1. Run this exactly once, from any directory:
   uv run --no-cache "<wt>/_bmad/scripts/render_skill.py" --project-root "<wt>" --skill "<wt>/.claude/skills/bmad-build-auto"
2. Read the one absolute workflow.md path it prints, and follow it to its end
   with the intent below. Run every step, in order: plan, implement through
   the implementation handoff, review. Subagents you launch must run in the
   foreground, as that workflow requires. A spec written after the code, or a
   skipped review, fails the gate and the unit is not merged.
3. When the workflow halts, reply with exactly these lines and nothing else:
   SPEC: <absolute path of the spec or result file the workflow wrote>
   STATUS: <the status in its frontmatter>
   BLOCKED: <blocking condition, or none>
   HEAD: <output of `git -C <wt> rev-parse HEAD`>
   INTERFACES: <each interface this unit provides or consumes, as built:
   route and payload, type, or method signature, one per `;`. Mark any that
   differ from the intent's agreed shape with "CHANGED". Or "none".>

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
for u in "<wt1>|<branch1>|<spec1>" "<wt2>|<branch2>|<spec2>"; do
  IFS='|' read -r wt br spec <<< "$u"
  printf '%s: ' "$br"; bash "<skill>/scripts/check-unit.sh" "$wt" "$br" "$spec"
done
```

The script prints `READY <commits>` or `NOT READY: <reasons>` for each unit.
It checks the spec status (`done`, `built` or `in-review`), and the workflow's
own traces in the spec: `baseline_revision`, `<intent-contract>` and at least
one `### <date> — Review pass` entry under `## Review Triage Log`. It also
checks for a clean tree, the right branch, and at least one commit ahead of
`origin/main` (or of the base passed as a fourth argument). In a later wave,
pass the wave's base revision as the fourth argument. Synthesis merges
the branch ref, so a worker that moved to another branch leaves nothing to
merge.

| Result | Meaning |
|---|---|
| `READY` | Ready to merge. |
| `NOT READY` | Record the unit, the reasons and the spec path. A missing workflow trace means the worker skipped plan or review: its code is unreviewed. SKILL.md section 6 decides what happens next. |

## Lane mode

The units of a lane wave run one after another in a single worktree, so
nothing is merged and only one worktree is installed. The differences from a
parallel unit:

- `<name>` is `<slug>` and `<branch>` is the run's branch
  `jayson/<type>-<slug>` for every unit, so `<wt>` is the same for all of them.
  In a run that already has `<iwt>` (a lane wave after a parallel one), `<wt>`
  is `<iwt>` and every unit uses the later-unit prompt below.
- Dispatch one worker at a time, in the order the split set, and wait for it.
  Before each dispatch, record `git -C <wt> rev-parse HEAD` (for the first
  unit, `origin/main`) as that unit's base.
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
  `bash "<skill>/scripts/check-unit.sh" "<wt>" "<branch>" "<spec>" "<base>"`.
  A `NOT READY` unit stops the lane, because later units build on it. SKILL.md
  section 6 decides what happens next.
