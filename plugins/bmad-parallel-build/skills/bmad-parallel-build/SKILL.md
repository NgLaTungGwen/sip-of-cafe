---
name: bmad-parallel-build
description: 'Orchestrate a parallel BMAD build from a meeting transcript or pasted work items. Splits the material into independent units and, after the user approves the split, runs one bmad-build-auto worker per unit in its own worktree. It also creates the Azure DevOps tickets (a User Story and one Task per unit, reusing any pasted work items). A synthesis agent then merges the results into one branch, gets it green, and opens one PR linked to those tickets. Use when the user says "parallel build", "fan out bmad-build", "orchestrate the build", or hands over a transcript or a set of work items to implement together.'
---

# BMAD Parallel Build

You are the orchestrator. You split the work, dispatch workers, check what they
return and hand the results to synthesis. You never write product code
yourself.

The flow is: input → split → **user approves** → ADO tickets + worktrees +
workers, all at once → gate → synthesis → one PR linked to the tickets.

The only planned checkpoint is the split approval. You also stop and ask in two
other cases: when a worker doesn't build, and when synthesis reports `stuck`.

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
`<root>/.claude/agents/` when the project doesn't have them: `bmad-pb-worker`,
`bmad-pb-synthesis` and `haiku-implementer`. A project copy that differs from
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

Read enough of the code to know where each change lands. `AGENTS.md` lists
where things are. When you delegate the code mapping, use `Explore` with
`model: sonnet`; it only reads and searches.

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
  for a later round. Each unit should be one session-sized build. If
  everything fits in one unit, say so and recommend plain `bmad-build`.

This machine has 10 cores and 32 GB of RAM. Each unit runs its own `npm ci`,
build, test suite and test database, and about 8 units can run at once in
comfort. Beyond 8, everything still runs in parallel, but each unit is slower.
Say so at approval, along with the token cost (roughly one full build per unit).

For every unit, write:

- a title
- a branch, `jayson/<type>-<slug>-<n>`
- `--apps`, if the unit touches `apps/`
- the files or areas it owns
- a self-contained intent: the goal, the scope, what's out of scope, acceptance
  criteria in Given/When/Then, and the exact excerpt of the source material it
  came from
- its ticket

The worker sees only its intent.

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

Combining tasks is the one way the split loses parallelism. If you put more
than one input task into a single unit, add a **Combined** line under the table
for each such unit: name the tasks and give the reason (a shared file, or an
interface that can't be fixed in advance). The user can then ask for a
different cut.

Ask with `AskUserQuestion` whether to approve, adjust or cancel. Iterate until
the user approves. After approval, run to the end without asking, except for
the two stop cases in sections 6 and 7.

## 4. Launch everything at once

Nothing waits on anything else. Each worker prepares its own worktree and starts
building as soon as that worktree is ready, so units never wait on each other.

0. Preflight, in one Bash call, before anything is created:
   ```bash
   docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 && echo docker ok || echo docker DOWN
   node -e "require('net').connect(55432,'127.0.0.1').on('connect',()=>{console.log('postgres ok');process.exit(0)}).on('error',()=>{console.log('postgres DOWN');process.exit(1)})"
   ```
   If either is down, notify and ask the user to start it, and check again
   before launching. Every worker's DB suite needs Postgres at
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
   - **The integration worktree:** one Bash call with `run_in_background: true`
     and a long timeout. Add `--apps` if any unit has it.
     ```bash
     bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<slug>" "jayson/<type>-<slug>" [--apps]
     ```
   - **Every worker:** one foreground `Agent` call per unit, using the prompt in
     `<skill>/references/worker.md`. Each worker runs `bmad-build-auto`, which
     starts its own review subagents, so the calls must stay in the
     foreground.

The integration worktree is ready long before the workers finish. Its
background-task notification may arrive while they run; just note it.

Each worktree gets its own `.env` that points at a private test database,
`hive_pb_<name>_test`, which the test setup creates. Workers and synthesis then
run the DB suites side by side without deleting each other's rows. Preparing a
worktree takes several minutes (checkout, `npm ci`, build). Never `cd` your own
shell into a worktree.

If the integration worktree fails to prepare, clear it with
`scripts/remove-worktree.sh` and retry once while the workers keep running.

## 5. Wait for the workers

All the `Agent` calls return together. Record each worker's reply in the run
file.

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

## 7. Synthesis

Read the ticket IDs from `<slug>-tickets.json`. If the ticket call is still
running, wait for its notification. Then read `<skill>/references/synthesis.md`,
fill in its placeholders, and launch
one foreground `Agent` with that prompt. List the ready units in dependency
order. If they have no dependencies, put the unit that owns migrations and
contracts first.

- **`RESULT: pr`:** go to step 8.
- **`RESULT: stuck`:** show the failure and the decision it needs. Once the
  user answers, continue the same synthesis agent with `SendMessage`.

## 8. Report

End with a short summary:

- the PR link
- the tickets, with links: the story, each Task and which unit it belongs to,
  what was created and what was reused, and whether ADO will close them when
  the PR completes
- the units that were merged, and any that were left out. A left-out unit's
  Task stays open and isn't linked.
- the repairs synthesis made
- the verification results
- anything the user should look at first

Don't remove the worktrees. Offer to clean them up after the PR merges.

To measure the run, the user can run
`python "<skill>/scripts/cost-report.py" <this session's .jsonl>`. It prints the
cost, tokens and time per role, the tool waits, and the cache rewrites after
idle gaps. The session file is under `~/.claude/projects/<project>/`.

To clean up, run this for each worktree:

```bash
bash "<skill>/scripts/remove-worktree.sh" "<root>" "<name>" "<branch>"
```

The script unlinks the npm junctions first, because a plain
`git worktree remove` follows them on Windows. Run it from `<root>`. A shell
whose working directory is inside the worktree makes the removal fail with
"busy".
