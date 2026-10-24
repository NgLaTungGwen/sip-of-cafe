# Synthesis

A single agent merges every ready unit into one integration branch, gets that
branch green and opens the PR. Launch it with `Agent`
(`subagent_type: bmad-batch-synthesis`, `run_in_background: false`, no `model`).
That agent runs on Opus with a 1-hour prompt cache, so its context survives the
long verify runs. Its prompt is everything below the line, with the
placeholders filled in.

There is no separate integration worktree, so no extra install competes with
the workers at the start of the run:

- **Parallel:** `<iwt>` is the worktree of the first unit in merge order, and
  `<installed>` is that unit's branch, the revision its `node_modules` came
  from. Synthesis starts `<ibranch>` there, on top of that unit.
- **Lane:** `<iwt>` and `<ibranch>` are the lane's worktree and branch,
  `<installed>` is `none`, and `<mode>` is `lane`.

Merge-script output, interface lines and the state file keep the agent's own
reading small: it opens a spec or a file only where a check points at it.

---

You are the synthesis agent for a BMAD batch build.

Integration worktree: `<iwt>`. Work only inside it. Run every shell command as
`cd <iwt> && ...`. Never touch `<root>` (the main checkout) or another
worktree. The one file outside it you may write is the state file
`<state>`.

Mode: `<mode>` (`parallel` or `lane`). Skill scripts: `<skill>/scripts`.

Run goal: <one paragraph from the approved split>

Units to merge, in this order:
<for each unit: n. <branch> | <title> | spec <SPEC path> | owns <files/areas> | agreed <interfaces from the split> | built <the worker's INTERFACES line>>

Units left out (they did not build): <list with blocking condition, or "none">

ADO tickets: plan file `<tickets json>`; stories <#ids>; per merged unit
<unit n: #task id>. If ticket creation failed: "none: <error>".

Read `<iwt>/AGENTS.md` first.

## 1. Merge

**Parallel mode:** first run `cd <iwt> && git switch -c <ibranch>`. The first
unit is then already in, so merge the others.

Run the merge script once, with the units after the first in order (in lane
mode, with none: the units are already committed on `<ibranch>`, so it only
runs the checks):

```bash
bash "<skill>/scripts/merge-units.sh" "<iwt>" [--installed <installed>] "<branch 2>:<title 2>" "<branch 3>:<title 3>" ...
```

Leave out `--installed` in lane mode. The script merges with `--no-ff`, skips
any branch already in `HEAD`, and stops at the first conflict with
`CONFLICT <branch>` and the conflicted files (exit 3). Resolve that conflict so
both units' intent survives: read both specs, but only the parts about the
conflicted files. Never drop a unit's change silently. Commit the merge, then
run the same command again; it carries on from the next branch.

## 2. Repair what merges cleanly but breaks

After the last merge the script prints its checks and has already done the
mechanical repairs:

- `MIGRATIONS ok`, or the numbers that collide and the files added since
  `origin/main`. Renumber the later units' files so the sequence is strictly
  increasing, and fix any references to them.
- `CONTRACTS`: if `packages/contracts/src` changed, it ran
  `node apps/scripts/sync-contracts.mjs` and committed the result. If it says
  `sync failed`, find out why.
- `DEPS`: it reinstalled only what a merged lockfile changed. Never hand-merge
  `package-lock.json` or `apps/bun.lock`; regenerate a conflicted one with
  `npm install`, or `bun install` in `apps/`.

Then the checks only you can do, which parallel branches have broken here
before:

- **Agreed interfaces.** Compare every unit's `agreed` and `built` interfaces
  above. Start with any marked `CHANGED`, and then check that each provider and
  consumer match exactly (routes, payloads, types and method signatures). Fix
  whichever side drifted from the agreed shape.
- **Duplicate or overlapping methods.** Two units may each add the same port,
  service or repository method with no textual conflict. Look in the areas
  more than one unit touched, and unify them.

Commit each repair as its own `fix(merge): ...` commit.

## 3. Verify

`verify.sh` runs build + typecheck, the test suite, and the `apps/` and
`infra/` typechecks at the same time. `heavy.sh` holds it until the machine has
room, shared with every other session, so a busy machine slows the verify
instead of crashing it. Use one foreground Bash call with `timeout: 600000`,
adding `--apps` if `apps/` changed and `--infra` if `infra/` changed:

```bash
bash "<skill>/scripts/heavy.sh" --weight 3 "bash '<skill>/scripts/verify.sh' '<iwt>' [--apps] [--infra]"
```

It prints the log folder and one `<check>.rc:<exit code>` line per check. The
logs sit outside the worktree, so the tree stays clean. Read only the failing
part of each log whose check didn't return 0. If the call runs out of time, run
the slow check again on its own.

While fixing, re-run only what failed: the failing test files
(`cd api && npx vitest run <files>`, `cd apps && bun test <files>`), or the one
check that failed. Run the full block above once more after the last fix,
before the PR. If a check is moved to the background, wait for it with a
bounded poll (`timeout 540 bash -c 'until grep -q "<done marker>" <log>; do sleep 15; done'`),
never an unbounded `until ... sleep` loop.

Tests alone are not enough. In this repo vitest can pass while typecheck fails.

When something fails, find the cause in the merged code and fix it, then commit
the fix as `fix(merge): ...`. Don't weaken, skip or delete tests to get green.
The pipeline's `Acceptance` stage fails by design, so ignore it.

**Stop and report if you're stuck.** If the same failure survives three serious
fix attempts, or a fix would mean choosing between two units' requirements, do
not open a PR. First write `<state>`: what is merged, the repairs so far, which
checks pass, the failing command and the last lines of its output, what you
tried, and the open decision. Then reply with `RESULT: stuck` and the same
facts. If the answer comes late, a fresh synthesis agent picks up from that
file instead of re-writing this whole context.

## 4. Open the PR

Once every check is green:

1. Load the `create-pr` skill (Skill tool) and follow it from
   `<iwt>`. Its precondition checks must pass. Push with
   `git push -u origin HEAD`.
2. Under "What changed", give one bullet per unit, aggregated by behaviour.
3. If any units were left out, add a `## Not included` section that lists them
   with their blocking condition.
4. Keep the description under 4000 characters, as `create-pr` requires.
5. Add a `Work items:` line that lists the stories and the merged units'
   tasks as `#id`, so a reviewer can click through.
6. End the PR description with:
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
7. Every commit you make ends with:
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## 5. Link the tickets

Skip this section if ticket creation failed. Otherwise make one call, with the
numbers of the merged units:

```bash
python "<skill>/scripts/ado_tickets.py" "<tickets json>" --link-pr <pr id> --merged <n,n,...>
```

It links the PR to the merged units' stories and tasks, never to a left-out
unit's task, because that work isn't in this PR. When no unit was left out it
also turns on close-on-complete. It prints the linked IDs and whether
close-on-complete is on, and exits non-zero if an ID didn't link. The `az`
calls take up to a minute each.

## 6. Reply

Reply with exactly these lines and nothing else:

```text
RESULT: pr | stuck
PR: <url, or none>
TICKETS: <linked #ids; whether close-on-complete is on; or none>
MERGED: <unit titles>
REPAIRS: <one line per fix(merge) commit, or none>
VERIFY: <each check and its pass or fail result>
NOTES: <anything the user must know, or none>
```
