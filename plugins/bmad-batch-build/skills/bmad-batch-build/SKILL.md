---
name: bmad-batch-build
description: 'Orchestrate a BMAD batch build from a meeting transcript or pasted work items. Splits the material into independent units and, after the user approves the split, runs one bmad-build-auto worker per unit: in parallel worktrees for larger runs, or in turn in one worktree (lane) for small ones, chosen automatically. It also creates the Azure DevOps tickets (a User Story and one Task per unit, reusing any pasted work items). A synthesis agent then merges the results into one branch, gets it green, and opens one PR linked to those tickets. Use when the user says "batch build", "parallel build", "fan out bmad-build", "orchestrate the build", or hands over a transcript or a set of work items to implement together.'
---

# BMAD Batch Build

You are the orchestrator. You split the work, dispatch workers, check what they
return and hand the results to synthesis. You never write product code
yourself.

The flow is: input → split → mode → **user approves** → ADO tickets +
worktrees + workers → gate → synthesis → one PR linked to the tickets.

The mode is chosen automatically from the split (section 2, "Choose the
mode"):

- **parallel:** every unit gets its own worktree and all workers start
  together, then synthesis merges them.
- **lane:** the units take turns in one worktree on the run's branch, so
  nothing is installed twice and nothing is merged. Small runs finish sooner
  this way and load the machine less.

The only planned checkpoint is the split approval. You also stop and ask in two
other cases: when a worker doesn't build, and when synthesis reports `stuck`.

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
- **No cap on the count, and no waves.** Split as finely as file ownership
  allows. Every unit starts in the same message, and no unit is ever held back
  for a later round. Each unit should be one session-sized build. A single
  unit is fine: it runs as a lane of one.

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
- a branch, `jayson/<type>-<slug>-<n>` (parallel mode only; a lane uses the
  run's branch)
- `--apps`, if the unit touches `apps/`
- the files or areas it owns
- a self-contained intent: the goal, the scope, what's out of scope, acceptance
  criteria in Given/When/Then, and the exact excerpt of the source material it
  came from
- its ticket

The worker sees only its intent.

### Choose the mode

Decide it yourself from the units; don't ask. Estimate each unit's worker time
from its size: S 50 min, M 100 min, L 160 min. These numbers were measured
when the worker installed before planning; the install now overlaps planning
and the full suite overlaps review, so real times should come in lower. They
are a first calibration, made on a 12-CPU, 32 GB machine; correct them when
`scripts/runs-summary.py` shows other worker times. Every recorded run carries
its machine's CPUs and RAM, so a slower machine gets its own figures.

```text
lane     = sum(unit minutes) - 10 * (units - 1) + 30   # one preparation, units in turn, one verify
parallel = max(unit minutes) + 80                       # all at once, then synthesis merges and verifies
```

- **1 unit:** lane.
- **2 to 4 units:** lane when `lane <= parallel + margin`, otherwise parallel.
  - The margin is 20 min.
  - It is 40 min when the machine is already busy, meaning the running heavy
    commands number a third of the CPU count or more (4 on 12 CPUs). Check
    with
    `n=$(ls ~/.cache/bmad-heavy/run.* 2>/dev/null | wc -l); [ "$n" -ge $(( $(nproc) / 3 )) ] && echo busy || echo free`.
  - A tie goes to lane, because lane costs less, merges nothing and loads the
    machine less.
- **5 or more units:** parallel.

Examples:

| Units | Lane | Parallel | Result |
|---|---|---|---|
| 2 × S | 120 | 130 | lane |
| 3 × S | 160 | 130 + 20 | parallel |
| 2 × M | 220 | 180 + 20 | parallel |
| L + S | 230 | 240 | lane |

In lane mode the units run in dependency order, owner of migrations and
contracts first. Disjoint files and pinned interfaces still help the review,
but a later unit may build directly on an earlier one.

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

Add a size column to the table, and one **Mode** line under it: the chosen
mode and both estimates, for example
`Mode: lane (≈120 min; parallel ≈130 min)`. This is information, not a
question. The user can still switch modes by choosing "adjust".

Write the same line into the run file, with each unit's size. Sizes and
estimates set against the measured times are how the mode constants get
calibrated.

Combining tasks is the one way the split loses parallelism. If you put more
than one input task into a single unit, add a **Combined** line under the table
for each such unit: name the tasks and give the reason (a shared file, or an
interface that can't be fixed in advance). The user can then ask for a
different cut.

Ask with `AskUserQuestion` whether to approve, adjust or cancel. Iterate until
the user approves. After approval, run to the end without asking, except for
the two stop cases in sections 6 and 7.

## 4. Launch

In parallel mode nothing waits on anything else. Each worker prepares its own
worktree, starts its install in the background and plans while it runs, so
units never wait on each other. There is no separate integration worktree:
synthesis later works in the first unit's worktree, so no extra install
competes with the workers.

In lane mode, steps 0 to 2 are the same, except that only the first unit's
worker is sent, using the lane prompt in `references/worker.md`. Its worktree
is `<root>/.claude/worktrees/<slug>`, on `jayson/<type>-<slug>`.

0. Preflight, in one Bash call, before anything is created:
   ```bash
   docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 && echo docker ok || echo docker DOWN
   node -e "require('net').connect(55432,'127.0.0.1').on('connect',()=>{console.log('postgres ok');process.exit(0)}).on('error',()=>{console.log('postgres DOWN');process.exit(1)})"
   ```
   Skip the Postgres check when `<root>/.env` has no `DATABASE_URL`, and the
   Docker check too when the project doesn't use Docker. If either is down,
   notify and ask the user to start it, and check again before launching. Every worker's DB suite needs Postgres at
   `127.0.0.1:55432`; a run that started with Docker down once stalled for
   nearly five hours.
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
   - **Every worker:** one foreground `Agent` call per unit, using the prompt in
     `<skill>/references/worker.md`. Each worker runs `bmad-build-auto`, which
     starts its own review subagents, so the calls must stay in the
     foreground.

The ticket call finishes long before the workers; its notification may arrive
while they run. Just note it.

Each worktree gets its own `.env` that points at a private test database,
`hive_pb_<name>_test`, which the test setup creates. Workers and synthesis then
run the DB suites side by side without deleting each other's rows. Never `cd`
your own shell into a worktree.

## 5. Wait for the workers

**Parallel:** all the `Agent` calls return together. Record each worker's reply
in the run file, its `INTERFACES` line included: synthesis checks those against
the agreed interfaces.

**Lane:** when a worker returns, record its reply and gate its unit straight
away (section 6, with its base). Then send the next unit's worker with the
lane prompt, whose intent lists the units already on the branch with their
spec paths and `INTERFACES` lines. Go to synthesis after the last unit passes.

**Turn budgets.** Each agent has a `maxTurns` budget in its agent file: worker
300, Haiku coder 300, synthesis 400, about twice the longest run that
finished. A budget stops a stuck agent; it isn't meant to be hit. A worker
reply without its `SPEC:`/`STATUS:` lines means the worker ran out. Gate the
unit anyway, because its branch may hold usable commits, and record "turn
budget" as the reason. Section 6 then decides what happens next.

## 6. Gate

Apply the gate in `references/worker.md` to every unit, in one Bash call with
`scripts/check-unit.sh`, and record the results in the run file.

- **Every unit is ready:** continue to synthesis.
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
  build on it. Ask whether to:
  - keep it marked unreviewed and continue with the next unit;
  - re-dispatch it: reset the lane to the unit's base with
    `git -C <wt> reset --hard <base>`, then send a fresh worker. This discards
    that unit's commits;
  - stop and send what is done to synthesis.

## 7. Synthesis

Read the ticket IDs from `<slug>-tickets.json`. If the ticket call is still
running, wait for its notification. Then read `<skill>/references/synthesis.md`,
fill in its placeholders, and launch one foreground `Agent` with that prompt.
List the ready units in dependency order. If they have no dependencies, put the
unit that owns migrations and contracts first. Give each unit its agreed
interfaces from the split and its worker's `INTERFACES` line.

- **Parallel:** `<iwt>` is the first listed unit's worktree, `<installed>` its
  branch, and `<ibranch>` is `jayson/<type>-<slug>`.
- **Lane:** `<mode>` is `lane`, `<iwt>` and `<ibranch>` are the lane's
  worktree and branch; synthesis then skips the merge.
- `<state>` is `<root>/.claude/worktrees/_runs/<slug>-synthesis.md`, and
  `<tickets json>` is `<root>/.claude/worktrees/_runs/<slug>-tickets.json`.

Then:

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

In parallel mode the first unit's worktree is on the run's branch, so pass
`jayson/<type>-<slug>` for it, then delete that unit's own branch with
`git -C "<root>" branch -d <its branch>`.

The script unlinks the npm junctions first, because a plain
`git worktree remove` follows them on Windows. Run it from `<root>`. A shell
whose working directory is inside the worktree makes the removal fail with
"busy".
