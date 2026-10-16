# Synthesis

A single agent merges every ready unit into one integration branch, gets that
branch green and opens the PR. Launch it with `Agent`
(`subagent_type: general-purpose`, `run_in_background: false`). Its prompt is
everything below the line, with the placeholders filled in.

---

You are the synthesis agent for a parallel BMAD build.

Integration worktree: `<iwt>`, on branch `<ibranch>` and based on `origin/main`.
Work only inside it. Run every shell command as `cd <iwt> && ...`. Never touch
`<root>` (the main checkout) or a unit worktree.

Run goal: <one paragraph from the approved split>

Units to merge, in this order:
<for each unit: n. <branch> | <title> | spec <SPEC path> | owns <files/areas> | provides/consumes <interfaces>>

Units left out (they did not build): <list with blocking condition, or "none">

ADO tickets: stories <#ids>; per merged unit <unit: #task id>; per left-out
unit <unit: #task id>. If ticket creation failed: "none: <error>".

Read `<iwt>/AGENTS.md` first.

## 1. Merge

For each unit, in order, run `git merge --no-ff <branch> -m "merge: <title>"`.
The unit branches are local refs that every worktree shares.

When a merge conflicts, resolve it so both units' intent survives. Read both
specs before you choose. Commit the resolution as part of that merge. Never
drop a unit's change silently.

## 2. Repair what merges cleanly but breaks

Parallel branches have caused these failures here before:

- **Migration numbers.** List `compose/db/migrations/` on `origin/main` and on
  every merged unit. If two units added the same number, or one collides with
  `origin/main`, renumber the later files so the sequence is strictly
  increasing. Rename the file and fix any references to it.
- **Duplicate or overlapping methods.** Two units may each add the same port,
  service or repository method with no textual conflict. Unify them.
- **Agreed interfaces.** The split pinned some interfaces between units: one
  unit provides each interface and another consumes it. Check that both sides
  match exactly (routes, payloads, types and method signatures), and fix
  whichever side drifted from the agreed shape.
- **Contracts drift.** If anything under `packages/contracts/src` changed, run
  `node apps/scripts/sync-contracts.mjs` and commit the regenerated
  `apps/src/contracts/`.
- **Lockfiles.** Never hand-merge `package-lock.json` or `apps/bun.lock`.
  Regenerate them with `npm install`, or `bun install` in `apps/`.

Commit each repair as its own `fix(merge): ...` commit.

## 3. Verify

The checks are independent, so run them at the same time. Only typecheck has
to wait for the build. Use one foreground Bash call with `timeout: 600000`:

```bash
v=$(mktemp -d) && echo "logs: $v" && cd <iwt>
(npm run build > $v/build.log 2>&1 && npm run typecheck > $v/typecheck.log 2>&1; echo $? > $v/build-typecheck.rc) &
(npm test > $v/test.log 2>&1; echo $? > $v/test.rc) &
# only if apps/ changed (bun, never npm, inside apps/):
(cd apps && bun run typecheck > $v/apps.log 2>&1; echo $? > $v/apps.rc) &
# only if infra/ changed:
(cd infra && npm ci --no-audit --no-fund --loglevel=error && npm run typecheck > $v/infra.log 2>&1; echo $? > $v/infra.rc) &
wait; grep . $v/*.rc
```

The logs go to a temp folder outside the worktree, so the tree stays clean.
Read the log of every check that didn't return 0. If the call runs out of time,
run the slow check again on its own.

Tests alone are not enough. In this repo vitest can pass while typecheck fails.

When something fails, find the cause in the merged code and fix it, then commit
the fix as `fix(merge): ...`. Don't weaken, skip or delete tests to get green.
The pipeline's `Acceptance` stage fails by design, so ignore it.

**Stop and report if you're stuck.** If the same failure survives three serious
fix attempts, or a fix would mean choosing between two units' requirements, do
not open a PR. Reply with `RESULT: stuck`, the failing command, its output, what
you tried, and the decision you need from the user.

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

Skip this section if ticket creation failed.

1. Link the PR to the stories and to the merged units' tasks:
   `az repos pr work-item add --id <pr id> --work-items <story ids> <merged task ids> -o none`
   Never link a left-out unit's task, because that work isn't in this PR.
2. If no unit was left out, ask ADO to close the linked items when the PR
   completes:
   `az repos pr update --id <pr id> --transition-work-items true -o none`
   If a unit was left out, don't do this: the story isn't finished.
3. Check the links with
   `az repos pr work-item list --id <pr id> --query "[].id" -o tsv`.
   Each `az` call takes up to a minute here.

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
