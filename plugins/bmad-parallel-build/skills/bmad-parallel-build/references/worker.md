# Worker

One worker per unit. It runs `bmad-build-auto` inside the unit's worktree and
nowhere else.

## Pinned to its worktree

`scripts/prepare-worktree.sh` writes `<wt>/_bmad/custom/bmad-build-auto.toml`
into the worktree's private copy of `_bmad`. It adds a persistent fact and an
`implementation_handoff` that both name `<wt>` as the only place work may
happen, so the implementation subagent stays there as well.

## Dispatch prompt

Send every worker in **one message**, with one `Agent` call per unit
(`subagent_type: bmad-pb-worker`, `run_in_background: false`, no `model`).
That agent (`.claude/agents/bmad-pb-worker.md`) runs on Opus with a 1-hour
prompt cache, so its context survives the long waits on `npm ci` and the test
suite. The worker plans, verifies and triages the review; bmad-build-auto runs
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

```text
You are a Build Auto worker for one unit of a parallel build.

Project root: <wt>   (a git worktree on branch <branch>, which you create in step 0)
Work only inside <wt>. Never read, edit or run anything elsewhere in <root> (the main checkout) or in another worktree.

0. Prepare the worktree. Run each command below as its own foreground Bash
   call with `timeout: 600000`, in order, and stop at the first failure:
   a. bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<name>" "<branch>" --no-install
   b. cd "<wt>" && npm ci --no-audit --no-fund --loglevel=error
   c. cd "<wt>" && npm --workspace @hive/contracts run build
   d. (only if this unit touches apps/) cd "<wt>/apps" && bun install --frozen-lockfile
   If b, c or d fails, retry it once, because parallel installs can hit EBUSY
   on Windows. If a fails, or a retry fails, reply with:
   SPEC: none
   STATUS: blocked
   BLOCKED: worktree preparation failed: <command and the last lines of output>
   HEAD: none
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

Intent:
<the unit's self-contained intent: goal, scope, the files it owns, acceptance
criteria in Given/When/Then, and the excerpt of the source material it came
from. The worker sees nothing else.>
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
one `### <date> — Review pass` entry under `## Review Triage Log`. It also checks for a clean tree, the right
branch, and at least one commit ahead of `origin/main`. Synthesis merges the
branch ref, so a worker that moved to another branch leaves nothing to merge.

| Result | Meaning |
|---|---|
| `READY` | Ready to merge. |
| `NOT READY` | Record the unit, the reasons and the spec path. A missing workflow trace means the worker skipped plan or review: its code is unreviewed. SKILL.md section 6 decides what happens next. |
