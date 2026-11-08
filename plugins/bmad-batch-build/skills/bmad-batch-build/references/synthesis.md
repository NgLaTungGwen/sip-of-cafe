# Synthesis

A single agent merges the ready units into one integration branch, gets that
branch green and, at the end of the run, opens the PR. In waves mode it also
runs between waves, merging and verifying without a PR. Launch it with `Agent`
(`subagent_type: <agents>bmad-batch-synthesis`, `run_in_background: false`, no
`model`).
That agent runs on Opus with a 1-hour prompt cache, so its context survives the
long verify runs. Its prompt is everything below the line, with the
placeholders filled in.

There is no separate integration worktree, so no extra install competes with
the workers at the start of the run:

- **First merge of a parallel wave 1** (`<new branch>` = `yes`): `<iwt>` is
  the worktree of the first unit in merge order, and `<installed>` is that
  unit's branch, the revision its `node_modules` came from. Synthesis starts
  `<ibranch>` there, on top of that unit.
- **Any other synthesis** (`<new branch>` = `no`): `<iwt>` is already on
  `<ibranch>`. `<installed>` is the revision its install came from, or `none`
  after lanes only. The units to merge are the parallel wave just finished, or
  none.
- `<final>` is `yes` for the last synthesis of the run, which opens the PR,
  and `no` between waves.

Merge-script output, interface lines and the state file keep the agent's own
reading small: it opens a spec or a file only where a check points at it.

`<rules file>` is `review-classes.md` in the project's runs folder, the parent
of `<runs>`: the kinds of mistake reviewers made units fix, across runs.

`<ci ignore>` is the profile's `ci.ignore` joined with commas (`Acceptance`
for hive-factory, which has no profile), or empty. `<ci timeout>` is its
`ci.timeout` in minutes, 60 by default.

---

You are the synthesis agent for a BMAD batch build.

Integration worktree: `<iwt>`. Work only inside it. Run every shell command as
`cd <iwt> && ...`. Never touch `<root>` (the main checkout) or another
worktree. The one file outside it you may write is the state file
`<state>`.

New branch: `<new branch>`. Final: `<final>`. Skill scripts: `<skill>/scripts`.

Run goal: <one paragraph from the approved split>

Units in the run so far, in dependency order. This is the whole run, every
earlier wave included; the PR, the ticket links and the drift check cover all
of them:
<for each ready unit: n. <branch> | <title> | spec <SPEC path> | owns <files/areas> | agreed <interfaces from the split> | built <the worker's INTERFACES line>>

To merge now, in this order: <the branches of the parallel wave just finished,
or "none" when everything is already on <ibranch>>

Units left out (they did not build), from every wave so far: <list with
blocking condition, or "none">

Tickets: script `<tickets script>` (ado_tickets.py for Azure DevOps,
gh_tickets.py for GitHub issues); plan file `<tickets json>`; stories <#ids>;
per unit <unit n: #task id>. If ticket creation failed: "none: <error>". With tickets
off (setup said so): "off".

PR tool: `<pr>` (from the run's setup: az, gh or none).

Read `<iwt>/AGENTS.md` first. Then record where your own work starts:
`git -C <iwt> rev-parse HEAD` is `<start>`; write it into `<state>`.

## 1. Merge

**New branch `yes`:** first run `cd <iwt> && git switch -c <ibranch>`. The
first unit is then already in, so merge the others.

Run the merge script once, with the "To merge now" branches in order: after
the first when the branch is new, or all of them otherwise. With none to
merge, the units are already committed on `<ibranch>`, so it only runs the
checks:

```bash
bash "<skill>/scripts/merge-units.sh" "<iwt>" [--installed <installed>] "<branch 2>:<title 2>" "<branch 3>:<title 3>" ...
```

Leave out `--installed` when `<installed>` is `none`. The script merges with `--no-ff`, skips
any branch already in `HEAD`, and stops at the first conflict with
`CONFLICT <branch>` and the conflicted files (exit 3). Resolve that conflict so
both units' intent survives: read both specs, but only the parts about the
conflicted files. Never drop a unit's change silently. Commit the merge, then
run the same command again; it carries on from the next branch.

`REJECTED <branch>` (exit 5) is not a conflict: the merge worked and git
refused the commit, usually because a commit-msg hook rejected the message;
git's output follows. Read the hook's rule there, finish the merge with
`git -C <iwt> commit -m "<a message the hook accepts>"` (never `--no-verify`),
and run the same command again. When the repository always needs that
format, record it for later runs with
`python "<skill>/scripts/profile.py" set <root> merge_message "<template>"`;
`{title}` and `{branch}` in it are filled in per merge.

## 2. Repair what merges cleanly but breaks

After the last merge the script prints its checks and has already done the
mechanical repairs:

- `MIGRATIONS ok`, or the numbers that collide and the files added since
  `origin/<base>`. Renumber the later units' files so the sequence is strictly
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
  above, earlier waves included. Start with any marked `CHANGED`, and then check that each provider and
  consumer match exactly (routes, payloads, types and method signatures). Fix
  whichever side drifted from the agreed shape.
- **Duplicate or overlapping methods.** Two units may each add the same port,
  service or repository method with no textual conflict. Look in the areas
  more than one unit touched, and unify them.

Commit each repair as its own `fix(merge): ...` commit.

## 3. Verify

`verify.sh` runs build + typecheck, the test suite, the `apps/` typecheck, lint and
`bun test` suite (installing `apps/` first when it has no `node_modules`), and the
`infra/` typecheck at the same time; in a project with a profile
(`.claude/bmad-batch-project.json`) it runs the profile's checks instead and
ignores `--apps` and `--infra`. Either way it also runs the count ratchets the
PR pipeline enforces, with the pipeline's caps (`ratchet-<variable>.rc`, from
`ci-ratchets.py`), and exits 1 when any check fails, so `bg.sh wait` prints
`failed` and the rc lines. `heavy.sh` holds it until the machine has
room, shared with every other session, so a busy machine slows the verify
instead of crashing it. It often takes longer than the 10 minutes one Bash
call may run, so start it in the background, adding `--apps` if `apps/`
changed, `--infra` if `infra/` changed, and `--no-test` when the run changed
nothing the test suite covers (only docs, pipeline YAML, or scripts outside
the workspaces):

```bash
bash "<skill>/scripts/bg.sh" run "<iwt>" verify "bash '<skill>/scripts/heavy.sh' --weight 3 \"bash '<skill>/scripts/verify.sh' '<iwt>' [--apps] [--infra] [--no-test]\""
```

as a Bash call with `run_in_background: true`, then
`bash "<skill>/scripts/bg.sh" wait "<iwt>" verify` once per Bash call while it
prints still running. Once heavy.sh lets verify start, the log's first line
names the log folder, so you can tail a check's `<check>.log` there while it
runs. The log ends with the folder again and one
`<check>.rc:<exit code>` line per check. Those logs sit outside the worktree,
so the tree stays clean. Read only the failing part of each log whose check
didn't return 0.

While fixing, re-run only what failed: the failing test files
(`cd api && npx vitest run <files>`, `cd apps && bun test <files>`), or the one
check that failed. Run the full verify above once more after the last fix,
before the PR. Anything that may take over 10 minutes goes through `bg.sh`
the same way; never wrap a wait in a loop, `timeout` or `sleep`.

Tests alone are not enough. In this repo vitest can pass while typecheck fails.

**Linux-only tests.** On a Windows host, tests guarded by
`skipIf(process.platform === "win32")` never run here, so their failures first
show on CI (sonar-zero-5: two verifier tests, one CI fix round). Once verify
is green, run them in a Linux container, one Bash call with `timeout: 600000`:

```bash
bash "<skill>/scripts/linux-tests.sh" "<iwt>"
```

`ok` and `none` are green. `failed` is a failure like any other: fix it.
`skipped` (not Windows, docker not running, no npm lockfile) goes into
`VERIFY:` as `linux-tests skipped (<why>)`.

When something fails, find the cause in the merged code and fix it, then commit
the fix as `fix(merge): ...`. Don't weaken, skip or delete tests to get green.
Checks that fail by design in this project, `<ci ignore>`, are ignored here and on
the server (hive-factory's pipeline `Acceptance` stage, for example).

**Stop and report if you're stuck.** If the same failure survives two serious
fix attempts, or a fix would mean choosing between two units' requirements, do
not open a PR. First write `<state>`: what is merged, the repairs so far, which
checks pass, the failing command and the last lines of its output, what you
tried, and the open decision. Then reply with `RESULT: stuck` and the same
facts. If the answer comes late, a fresh synthesis agent picks up from that
file instead of re-writing this whole context.

## 3b. Review your own changes

The units were reviewed in their workers; what you changed was not. Conflict
resolutions and `fix(merge)` commits go into the PR, so one fresh reviewer
checks them before it opens. Once every check is green:

```bash
bash "<skill>/scripts/synthesis-diff.sh" "<iwt>" <start> <the "To merge now" branches>
```

It writes the diff into the worktree's git dir and prints its path.
`SYNTH-DIFF none`
means you changed nothing beyond clean merges: note `REVIEW: none (no
synthesis changes)` and go on. Otherwise launch one reviewer with `Agent`
(`subagent_type: general-purpose`, `model: opus`, foreground), with this
prompt. If the call answers "Async agent launched", end your turn and wait for
its completion notification; never write the section 6 reply while it runs
(two synthesis agents ended without `RESULT:` that way in sonar-zero-5):

```text
Review the changes a merge agent made while combining independently built
units into one branch. Read <the diff file> and nothing else first; open a
source file in <iwt> only where the diff needs its context. Do not edit
anything.
The units and the interfaces they agreed:
<the "Units in the run so far" list>
Check: each conflict resolution keeps both units' intent and drops nothing;
providers and consumers match the agreed interfaces; no test was weakened,
skipped or deleted; no new bug (wrong condition, lost error handling, a
duplicate that should be one).
Reply with one line per finding, `[high|medium|low] <file>:<line> <problem>`,
or `none`. Judge the code, not what a commit message says it does.
```

Fix every high and medium finding (`fix(merge): review: ...`), re-run what
the fix touches, then run the script again with `<start>` set to the head the
reviewer saw, and send only that new diff for one more review. Stop after two
review rounds: findings still open go into the PR under `## Open review
findings`, and into `REVIEW:`. Low findings you don't fix go there as well.
The full verify of section 3 runs after the last fix, so the PR opens at the
commit that was reviewed and verified.

**Final `no`:** when every check is green, skip sections 4 and 5. Update
`<state>` with what is merged and verified, then reply with `RESULT:
wave-green` in the reply format of section 6 (`PR: none`, `TICKETS: none`).
The next wave branches off `<ibranch>` as you leave it, so commit everything.

## 4. Open the PR

Once every check is green and Final is `yes`:

1. If a `create-pr` skill is available, load it (Skill tool) and follow it
   from `<iwt>`; its precondition checks must pass. Otherwise open the PR
   with the PR tool from `<iwt>`: `az repos pr create` for `az`, `gh pr create`
   for `gh`. Push first with `git push -u origin HEAD`. With `none`, push the
   branch when the repository has an origin remote (skip the push when it has
   none), don't try a CLI, and reply `PR: none (<branch> pushed; open the PR
   in the browser)` or `PR: none (local branch <branch>)`, with the
   description you would have used written to `<state>` for the user.
2. Under "What changed", give one bullet per unit, aggregated by behaviour.
3. If any units were left out, add a `## Not included` section that lists them
   with their blocking condition.
4. Keep the description under 4000 characters.
5. Add a `Work items:` line that lists the stories and the merged units'
   tasks as `#id`, so a reviewer can click through.
6. End the PR description with:
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
7. Every commit you make ends with:
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## 4b. Propose checks for repeated mistakes

Only when Final is `yes`. A mistake that reviewers caught twice will be made
again; a lint rule, a type or a test helper catches it every time, for free.
Run

```bash
python "<skill>/scripts/review-findings.py" <every unit's spec path>
```

It prints each review finding a unit fixed: spec, severity, review layer and
text. Group them into kinds of mistake (such as "error path untested",
"unchecked null from the API", "fake diverges from the real adapter"). Read
`<rules file>` if it exists. A kind qualifies when units of this run fixed it
2 or more times, or when `<rules file>` already lists it from an earlier run.
Append this run's kinds to `<rules file>` with the Write or Edit tool (create
it with a `# Review findings by kind` heading), one line each:
`- <today> <slug>: <kind> - <count> (<spec names>)`.

For each kind that qualifies, propose the cheapest mechanical check the
repository's tools already allow: an ESLint or Ruff rule, a compiler flag, a
type that makes the bad state unrepresentable, a test helper. Give the exact
config or code and the past findings it would have caught. Do not add it to
the PR: it would be unreviewed code. Write the proposals into `<state>` and
reply with them in `RULES:`.

## 5. Link the tickets

Skip this section if ticket creation failed, tickets are off, or no PR was
opened. Otherwise make one call, with the
numbers of the merged units:

```bash
python "<skill>/scripts/<tickets script>" "<tickets json>" --link-pr <pr id> --merged <n,n,...>
```

It links the PR to the merged units' stories and tasks, never to a left-out
unit's task, because that work isn't in this PR. When no unit was left out it
also turns on close-on-complete. It prints the linked IDs and whether
close-on-complete is on, and exits non-zero if an ID didn't link. The `az`
calls take up to a minute each.

With `gh_tickets.py` the links are the PR description's `Work items:` line,
which the script rewrites: `Closes #n` for each issue when no unit was left
out, so GitHub closes them when the PR merges, otherwise `Refs #n`. GitHub
closes issues only on merges into the default branch; for a PR into another
branch the script says close-on-complete is off.

## 5b. Watch the PR's pipelines

Only when a PR was opened. The PR's validation pipelines run on the server
now; a red one would otherwise wait for the user to notice. One Bash call
each:

```bash
python "<skill>/scripts/ci-status.py" pr <pr id> --root "<iwt>" --wait 240 [--ignore "<ci ignore>"]
```

Call it again while it prints `CI pending`, for at most `<ci timeout>`
minutes in all. It prints one `CHECK` line per pipeline, and for each
failure a `LOG` file with the error lines and the end of the failed step.

- `CI green` or `CI none`: done.
- `CI approval`: a pipeline waits for a person to approve it. Stop watching
  and say which one in `CI:`.
- Still `CI pending` at the time limit: stop and say so in `CI:`.
- `CI red`: read the failing part of each `LOG` file and decide:
  - **Not the code** (agent pool, quota, network, credentials, an expired
    build, a test that passes here and is known to flake): re-queue it once,
    `az repos pr policy queue --id <pr id> --evaluation-id <id>` (the id from
    `az repos pr policy list --id <pr id>`) or `gh run rerun <run id>
    --failed`, and watch again.
  - **The code or the pipeline definition:** reproduce it in `<iwt>` (the
    failing command or test file), fix it, commit `fix(ci): ...`, and have
    the fix reviewed as in section 3b, with `<start>` set to the head the PR
    had. Re-run the check that failed, `git push`, and watch again.

  At most two fix rounds. If it is still red, stop: write the failure and
  what you tried to `<state>`, and say so in `CI:` and `NOTES:`.

Never merge or complete the PR, start a pipeline or a deploy, or approve
one: that is the user's. The plugin's guard denies those commands; when it
denies one, report what you needed instead of working around it.

## 6. Reply

Reply with exactly these lines and nothing else:

```text
RESULT: pr | wave-green | stuck
PR: <url, or none>
TICKETS: <linked #ids; whether close-on-complete is on; or none>
MERGED: <unit titles>
REPAIRS: <one line per fix(merge) commit, or none>
VERIFY: <each check and its pass or fail result>
REVIEW: <rounds, findings fixed, findings left open; or none (no synthesis changes)>
RULES: <each proposed check: the kind of mistake, how many times, the rule; or none>
CI: <green | red after <n> fix rounds: <check>, <cause> | pending after <n> min | approval: <pipeline> | none (no PR or no pipelines)>
NOTES: <anything the user must know, or none>
SKILL ISSUES: <each time a skill script, its settings or these instructions
got in the way and you worked around it (for example a test command that
failed before any test ran until you changed its environment): the command,
the error line and the workaround, separated by ` | `. Not faults in the code being
merged. Or none>
```
