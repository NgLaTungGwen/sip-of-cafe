---
name: bmad-batch-build
description: 'Orchestrate a BMAD batch build from a meeting transcript or pasted work items. Splits the material into independent units and, after the user approves the split, runs one bmad-build-auto worker per unit: in parallel worktrees, in turn in one worktree (lane), or in dependency waves that mix both, chosen automatically from the units' sizes and dependencies. It also creates the Azure DevOps tickets (a User Story and one Task per unit, reusing any pasted work items). A synthesis agent then merges the results into one branch, gets it green, and opens one PR linked to those tickets. Use when the user says "batch build", "parallel build", "fan out bmad-build", "orchestrate the build", or hands over a transcript or a set of work items to implement together.'
---

# BMAD Batch Build

You are the orchestrator. You split the work, dispatch workers, check what they
return and hand the results to synthesis. You never write product code
yourself.

The flow is: input → split → mode → **user approves** → ADO tickets +
worktrees + workers → gate → synthesis → one PR linked to the tickets.

The schedule is chosen automatically from the split's dependencies (section
2, "Schedule the units"):

- **parallel:** every unit gets its own worktree and all workers start
  together, then synthesis merges them.
- **lane:** the units take turns in one worktree on the run's branch, so
  nothing is installed twice and nothing is merged. Small runs and chains of
  dependent units go this way.
- **waves:** when some units need others' code to build, the units run in
  waves. Each wave runs as a lane or in parallel, and later waves start from
  the integration branch that already holds the earlier ones.

The only planned checkpoint is the split approval. You also stop and ask in two
other cases: when a worker doesn't build, and when synthesis reports `stuck`
(after a wave too).

Two rules shape the flow, because most of a run is waiting:

- **Keep long waits out of large contexts.** An agent that sits idle past its
  cache TTL pays to re-write its whole context. So installs and the full suite
  run in the background while the worker plans and reviews, the Haiku coder
  (small context, cheap tokens) does the waiting for the install, and you keep
  your own context thin: you never read product code, `Explore` maps it.
- **Scripts do the mechanical work.** Installs, merges, migration and
  contracts checks, the gate and the ticket links are scripts, so the Opus
  agents spend their turns on judgment: planning, review triage, conflicts and
  failures.

Paths in this skill:

- `<root>` is the project's main checkout, the absolute path printed by
  `git rev-parse --path-format=absolute --show-toplevel` in the directory this
  session started in. Never edit or switch branches in it, because other
  sessions use it.
- `<skill>` is this skill's own directory, the base directory Claude Code
  reported when it loaded this skill (under `.claude/skills/` for a copied
  install, under `~/.claude/plugins/` for a plugin install).

## 0. Make sure BMAD and the agents are installed

Run `bash "<skill>/scripts/ensure-bmad.sh" "<root>"` before anything else. It
leaves an existing BMAD install alone and otherwise installs the pinned
`bmad-method` release into `<root>` (module `bmm`, tool `claude-code`), which
takes a minute or two. It also copies the run's three agents into
`<root>/.claude/agents/` when the project doesn't have them: `bmad-batch-worker`,
`bmad-batch-synthesis` and `haiku-implementer`. A project copy that differs from
the shipped one is kept and reported; pass that line on to the user. If the
script fails, stop and show the user its output. The general-purpose agent is
no substitute: it has no 1-hour cache and no workflow rules.

**Tell the user when the run needs them.** Before every question to the user
(the split approval, a gate failure, a stuck synthesis), send a
`PushNotification` with one line saying what is needed, then ask. Load the tool
with `ToolSearch` (`select:PushNotification`) if it is deferred.

## 1. Read the input

The user provides one of two things:

- **A meeting transcript.** Extract the decisions and action items that ask for
  a code change. Ignore discussion that didn't end in a decision. Note anything
  ambiguous so you can raise it at the split approval.
- **Pasted work items.** These are already analysed. Treat each item's title,
  description and acceptance criteria as the requirement. If an item carries an
  ADO ID (`#1234`, `1234` or a work item URL), record the ID. If you don't know
  its type, read it with
  `az boards work-item show --id <id> --query "fields.\"System.WorkItemType\"" -o tsv`.

Give the run a short kebab-case slug and a conventional type (`feat`, `fix`,
and so on). Record the run at `<root>/.claude/worktrees/_runs/<slug>.md`, which
is gitignored. That file holds the source summary, the units, their branches
and worktrees, and their gate results. Keep it updated after every step so the
run can be resumed.

## 2. Split into units

Find where each change lands without reading the code yourself. Your context
is re-written in full every time a worker, synthesis or the user answers after
a long gap, since this session's cache keeps the session's own TTL, so every
file you read here is paid for again at each of those returns. Read only
`AGENTS.md` and, when the project has one, its system map. First run
`python "<skill>/scripts/check-map.py" "<root>"`:

- `MAP none`: there is no map; go straight to Explore.
- `MAP ok`: read `docs/architecture/system-map.md`. Its glossary turns the
  input's business terms into modules and paths.
- `MAP stale`: read it, but treat the listed paths as moved, and mention them
  in the approval message so the map gets fixed.

Write the `MAP ...` line into the run file. The run's recorded data reads it,
so runs with and without a map can be compared.

Then delegate the code mapping to one `Explore` agent with `model: sonnet`.
When there is a map, tell Explore to start from the modules and paths it names
for each requirement and to search beyond them only where they don't cover the
requirement. Ask it for a compact map, not file contents: for each
requirement, the files it would change, the existing interfaces it touches,
and any shared hotspot (migrations, `packages/contracts`, lockfiles, a file
two requirements both need). Send a second `Explore` only for a gap in that
map.

A unit is a piece of work that one worker can build and test on its own, from
`origin/main`, without seeing any other unit. Rules for splitting:

- **Disjoint files.** Two units must never edit the same file. If they would,
  merge them into one unit.
- **Cut dependencies at an agreed interface.** Workers can't see each other's
  branches. When unit B needs something unit A builds, such as an endpoint, a
  type or a method, don't serialise them. Write the exact interface (the route
  and payload, the type, or the method signature) into both intents. A
  implements it and B codes against it, so both run at the same time. Put the
  two in one unit only when the interface can't be pinned down before
  implementation starts.
- **Shared hotspots get one owner.** A single unit owns all of a run's
  migrations, all `packages/contracts` changes and all lockfile changes. If
  several units need contract changes, the owner unit makes them and the other
  units' intents describe the agreed shape.
- **Record real dependencies, and only those.** When unit B can't build or
  test without unit A's code (it imports A's new module or types at compile
  time, or its tests run A's code), and an agreed interface can't stand in
  for that code, B depends on A. Write `depends_on` with the reason. Prefer an
  agreed interface wherever one works: a dependency moves B to a later wave.
- **No cap on the count.** Split as finely as file ownership allows. Each unit
  should be one session-sized build. A single unit is fine: it runs as a lane
  of one.

Heavy commands (installs, builds, full test suites, the synthesis verify) go
through `scripts/heavy.sh`. It admits a command only while the machine has room
for it, counting every session and run on this machine, so many runs can go at
once without crashing; they queue instead. Waiting commands start in arrival
order. CPU and RAM may still run high, and that is expected. The limits follow
the machine: one slot per CPU, a free-RAM check, and Vitest capped at a quarter
of the CPUs per suite (3 on 12 CPUs).

`scripts/heavy_hook.py` is an optional PreToolUse hook the user can enable. It
routes a heavy command that an agent typed without `heavy.sh` through the
queue anyway. The skill never enables it itself.

For every unit, write:

- a title
- a size, from the files it owns:
  - **S:** up to 3 files, with no migration and no new module;
  - **M:** 4 to 10 files;
  - **L:** more than 10 files, a new module, or a migration plus API plus UI.
- `depends_on`: the units whose code it needs to build or test, each with the
  reason (for example "imports the chart kit types"), or none
- a branch, `jayson/<type>-<slug>-<n>`, used when the unit runs in a parallel
  wave; a unit in a lane wave works on the run's branch
- `--apps`, if the unit touches `apps/`
- the files or areas it owns
- a self-contained intent: the goal, the scope, what's out of scope, acceptance
  criteria in Given/When/Then, and the exact excerpt of the source material it
  came from
- its ticket

The worker sees only its intent.

### Schedule the units

Decide it yourself from the units; don't ask. Write the plan file
`<root>/.claude/worktrees/_runs/<slug>-plan.json`, in the format documented in
`<skill>/scripts/plan-waves.py`, with each unit's id, title, size,
`depends_on` and reason. Then run it, adding `--busy` when the machine is busy:

```bash
n=$(ls ~/.cache/bmad-heavy/run.* 2>/dev/null | wc -l); [ "$n" -ge $(( $(nproc) / 3 )) ] && b=--busy || b=
python "<skill>/scripts/plan-waves.py" "<root>/.claude/worktrees/_runs/<slug>-plan.json" $b
```

It sorts the units into waves: a wave holds the units whose dependencies are
all in earlier waves. Each wave then runs as a lane or in parallel, whichever
is faster for that wave by more than a margin. The margin is 20 min, or 40 min
on a busy machine, and a tie goes to lane, because lane costs less and merges
nothing. The script estimates whole-run times for lane, parallel (only when no
unit depends on another) and waves, and picks the mode:

- **parallel:** one wave, run in parallel. No unit depends on another.
- **lane:** every wave runs as a lane: one unit, small units, or a chain.
- **waves:** anything else.

Unit times come from measured runs once a size has five measurements, and
until then from the first calibration: S 50 min, M 100 min, L 160 min. The
script writes the schedule back into the plan file and prints the **Mode**
line for the approval table. Recording the run later matches each unit's
measured time to its size, so the estimates calibrate themselves.

A worked example: units 1 (chart kit) and 2 (contract fields) are
independent, and units 3, 4 and 5 import both. That makes two parallel waves,
{1, 2} then {3, 4, 5}: about 450 min, against 670 min for lane.

### Tickets

The house pattern on the board is a User Story, such as "Story 7.7 - ...", with
one Task per piece of work. The PR links to the story and its tasks. Map the
run onto that pattern, and **reuse what already exists**:

- **The story.**
  - An existing User Story among the inputs is the story for the units that
    came from it. Several existing stories are fine.
  - Units with no existing story share one new User Story for the run.
  - A transcript always gets one new story.
- **A unit's ticket.**
  - If the unit came from an existing Task or Bug, that item is its ticket. It
    is never edited or re-parented.
  - Otherwise a new Task is created under the unit's story.
- **Don't attach an Epic.** The user links stories to epics by hand.

A new story's title is a plain behaviour statement, like the board's existing
ones. Its description is a short HTML summary of the source and the list of
units. A new Task's title is the unit title, and its description is the unit's
intent as HTML.

## 3. Get approval (the one planned checkpoint)

Show the split as a compact table with these columns: unit, ticket (`#id`
reused, or `new Task`), owns, interfaces (what it provides or consumes), and the
acceptance criteria in brief. Above the table, give the story or stories (`#id`
reused, or `new User Story: <title>`). List the open ambiguities underneath.
Approving the split also approves creating the new tickets.

Add a size column and a **depends on** column (unit ids and the reason, or
`-`) to the table. Under it, paste the **Mode** line that `plan-waves.py`
printed: the chosen mode, every estimate, the wave order and the
dependencies, for example
`Mode: waves (≈450 min; lane ≈670 min, parallel not possible, waves ≈450 min). Order: {1,2} → {3,4,5}.`
This is information, not a question. The user can still change the schedule,
or a dependency, by choosing "adjust". Re-run the script after any change.

Write the same line into the run file. The plan file keeps the sizes and the
waves.

Combining tasks is the one way the split loses parallelism. If you put more
than one input task into a single unit, add a **Combined** line under the table
for each such unit: name the tasks and give the reason (a shared file, or an
interface that can't be fixed in advance). The user can then ask for a
different cut.

Ask with `AskUserQuestion` whether to approve, adjust or cancel. Iterate until
the user approves. After approval, run to the end without asking, except for
the two stop cases in sections 6 and 7.

## 4. Launch

The run's integration branch is `<ibranch>` = `jayson/<type>-<slug>`. Its
worktree `<iwt>` is the first worktree that holds it:

- When wave 1 is a lane, `<iwt>` is `<root>/.claude/worktrees/<slug>`, which
  the first unit's worker prepares on `<ibranch>`.
- When wave 1 is parallel, `<iwt>` is the first unit's worktree. Synthesis
  creates `<ibranch>` there when it merges wave 1. Plain parallel mode is this
  case with one wave.

There is never a separate integration worktree, so no extra install competes
with the workers.

0. Preflight, in one Bash call, before anything is created:
   ```bash
   docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 && echo docker ok || echo docker DOWN
   node -e "require('net').connect(55432,'127.0.0.1').on('connect',()=>{console.log('postgres ok');process.exit(0)}).on('error',()=>{console.log('postgres DOWN');process.exit(1)})"
   ```
   Skip the Postgres check when `<root>/.env` has no `DATABASE_URL`, and the
   Docker check too when the project doesn't use Docker. If either is down,
   notify and ask the user to start it, and check again before launching.
   Every worker's DB suite needs Postgres at `127.0.0.1:55432`; a run that
   started with Docker down once stalled for nearly five hours.
1. Fetch once with `git -C "<root>" fetch origin main --quiet`. Concurrent
   fetches into one `.git` race on `FETCH_HEAD`, which is why the scripts never
   fetch.
2. In **one message**, send all of the following:
   - **The tickets:** write `<root>/.claude/worktrees/_runs/<slug>-tickets.json`
     in the format documented in `<skill>/scripts/ado_tickets.py`. Then make one
     Bash call with `run_in_background: true` and a long timeout:
     `python "<skill>/scripts/ado_tickets.py" "<root>/.claude/worktrees/_runs/<slug>-tickets.json"`.
     The IDs are written back into that file. If the call fails, re-run it once;
     it creates only what is still missing. If it fails again, carry on with the
     build and report the error at the end. Tickets never block the code.
   - **Wave 1's workers:** one foreground `Agent` call per unit for a parallel
     wave, or the first unit's worker for a lane wave, using the prompts in
     `<skill>/references/worker.md`. Each worker runs `bmad-build-auto`, which
     starts its own review subagents, so the calls must stay in the foreground.

The ticket call finishes long before the workers; its notification may arrive
while they run. Just note it.

Each worktree gets its own `.env` that points at a private test database,
`hive_pb_<name>_test`, which the test setup creates. Workers and synthesis then
run the DB suites side by side without deleting each other's rows. Never `cd`
your own shell into a worktree.

## 5. Run the waves

Work through the waves in order. Within a wave:

- **Parallel wave:** all its `Agent` calls go in one message and return
  together. Record each worker's reply in the run file, its `INTERFACES` line
  included: synthesis checks those against the agreed interfaces. Then gate
  every unit (section 6) against the wave's base.
- **Lane wave:** send one worker at a time in `<iwt>`, with the lane prompt.
  When a worker returns, record its reply and gate its unit straight away
  (section 6, with its base) before sending the next.

A wave's **base** is where its units start. For wave 1 that is `origin/main`.
For a later wave it is `<ibranch>` as the previous wave left it: record
`git -C <iwt> rev-parse HEAD` when the wave starts. Workers in a later
parallel wave prepare their worktree from that base (`--base <ibranch>` in
their step 0). Workers in any later wave get the "Already on the branch" list:
each earlier unit's title, spec path and `INTERFACES` line.

**Between waves.** Synthesis (section 7) runs with `<final>` = `no` after a
parallel wave, to merge it, and before a parallel wave, so that the wave
branches off a verified base. It merges what there is to merge, verifies
`<ibranch>` and replies `RESULT: wave-green`. One synthesis covers both cases
when a parallel wave follows another. Between two lane waves the unit gates
are enough, as in a plain lane run. If the wave synthesis replies `stuck`,
stop and ask before starting the next wave.

After the last wave, run the final synthesis (`<final>` = `yes`), which opens
the PR.

**Turn budgets.** Each agent has a `maxTurns` budget in its agent file: worker
300, Haiku coder 300, synthesis 400, about twice the longest run that
finished. A budget stops a stuck agent; it isn't meant to be hit. A worker
reply without its `SPEC:`/`STATUS:` lines means the worker ran out. Gate the
unit anyway, because its branch may hold usable commits, and record "turn
budget" as the reason. Section 6 then decides what happens next.

## 6. Gate

Apply the gate in `references/worker.md` to every unit, in one Bash call with
`scripts/check-unit.sh`, and record the results in the run file.

- **Every unit is ready:** continue (the next unit in a lane wave, or the
  wave's synthesis or the next wave).
- **Some units are not ready:** show which ones and why, with the spec paths.
  Ask whether to continue without them, re-dispatch them, or stop. When a unit
  failed only on workflow traces (no baseline, no intent contract, no review
  pass), its code exists but is unreviewed; also offer to merge it anyway,
  marked unreviewed in the PR.
  To re-dispatch, clear the unit first, because `prepare-worktree.sh` refuses an
  existing path and branch:
  `bash "<skill>/scripts/remove-worktree.sh" "<root>" "<name>" "<branch>"`, then
  `git -C "<root>" branch -D "<branch>"` (the script's `-d` keeps an unmerged
  branch). Then send a fresh worker with the same, or a corrected, intent.
- **A lane unit is not ready:** the lane stops there, because later units
  build on it. A unit that later waves depend on stops the run the same way.
  Ask whether to:
  - keep it marked unreviewed and continue with the next unit;
  - re-dispatch it: reset the lane to the unit's base with
    `git -C <wt> reset --hard <base>`, then send a fresh worker. This discards
    that unit's commits;
  - stop and send what is done to synthesis.

## 7. Synthesis

Synthesis runs once at the end of every run (`<final>` = `yes`), and in
waves mode also between waves (`<final>` = `no`, section 5). Each one is a
fresh agent: a wave can take longer than its 1-hour cache.

For the final one, read the ticket IDs from `<slug>-tickets.json`, and if the
ticket call is still running, wait for its notification. Then read
`<skill>/references/synthesis.md`, fill in its placeholders, and launch one
foreground `Agent` with that prompt. Synthesis gets two lists:

- **Units in the run so far:** every ready unit of every wave up to now, in
  dependency order, with its agreed interfaces from the split, its worker's
  `INTERFACES` line and its Task. With no dependencies, put the unit that owns
  migrations and contracts first. The final synthesis builds the PR, the
  ticket links and the drift check from this list, so it must hold the whole
  run, including a plain lane's units.
- **To merge now:** the ready units of the parallel wave just finished, or
  none (a lane wave, or a verify before a parallel wave).

The units left out are kept across waves too.
- **`<new branch>`** is `yes` only for the first merge of a run whose wave 1
  is parallel. Synthesis then creates `<ibranch>` in `<iwt>`, the first unit's
  worktree, and `<installed>` is that unit's branch. Otherwise it is `no`, and
  `<installed>` is the `<ibranch>` revision `<iwt>`'s install came from: the
  wave's base, or `none` in a run that only had lanes.
- `<state>` is `<root>/.claude/worktrees/_runs/<slug>-synthesis.md`, and
  `<tickets json>` is `<root>/.claude/worktrees/_runs/<slug>-tickets.json`.

Then:

- **`RESULT: wave-green`:** start the next wave.
- **`RESULT: pr`:** go to step 8.
- **`RESULT: stuck`:** show the failure and the decision it needs. Note the
  time with `date +%s`. When the user answers:
  - within 50 minutes, continue the same synthesis agent with `SendMessage`.
    Its 1-hour cache is still warm.
  - later, its cache has expired, and continuing it would re-write its whole
    context. Launch a fresh synthesis agent instead, with the same prompt plus
    one line at the top: `Resume: read <state> first. The merges and the
    repairs it lists are committed on <ibranch>. The user's decision: <answer>.
    Go on from section 3.`
- **No `RESULT:` line:** synthesis ran out of turns. Treat it as stuck. If
  `<state>` doesn't exist, the resume line says `Resume: read
  git log --oneline origin/main..<ibranch> first; the merges and repairs it
  shows are committed.` instead.

## 8. Report

First record the run's data, in one Bash call:

```bash
python "<skill>/scripts/cost-report.py" --record <slug> --root "<root>"
```

It finds this session's log, saves the full report to
`<root>/.claude/worktrees/_runs/<slug>-cost.txt`, and appends one row to
`~/.claude/bmad-batch-runs/runs.jsonl`: cost, time, review and repair counts,
cache and turn-budget checks. Every run is measured this way, so never skip
it. Record the run the same way when it ends early (cancelled, stopped at the
gate, or left stuck). If the call fails, say so in the summary and go on.

Then end with a short summary:

- the PR link
- the tickets, with links: the story, each Task and which unit it belongs to,
  what was created and what was reused, and whether ADO will close them when
  the PR completes
- the units that were merged, and any that were left out. A left-out unit's
  Task stays open and isn't linked.
- the repairs synthesis made
- the verification results
- anything the user should look at first
- the `recorded` line from the call above: cost, time, and whether the 1-hour
  cache took effect and any agent ran out of turns

Don't remove the worktrees. Offer to clean them up after the PR merges.

`python "<skill>/scripts/runs-summary.py"` compares all recorded runs by skill
version.

To clean up, run this for each worktree:

```bash
bash "<skill>/scripts/remove-worktree.sh" "<root>" "<name>" "<branch>"
```

When wave 1 ran in parallel, the first unit's worktree is on the run's branch, so pass
`jayson/<type>-<slug>` for it, then delete that unit's own branch with
`git -C "<root>" branch -d <its branch>`.

The script unlinks the npm junctions first, because a plain
`git worktree remove` follows them on Windows. Run it from `<root>`. A shell
whose working directory is inside the worktree makes the removal fail with
"busy".
