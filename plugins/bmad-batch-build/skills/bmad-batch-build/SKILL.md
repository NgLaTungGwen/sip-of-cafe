---
name: bmad-batch-build
description: 'Orchestrate a BMAD batch build from a meeting transcript or pasted work items. Splits the material into independent units and, after the user approves the split, runs one bmad-build-auto worker per unit: in parallel worktrees, in turn in one worktree (lane), or in dependency waves that mix both, chosen automatically from the sizes and dependencies of the units. It also creates the tickets, in Azure DevOps (a User Story and one Task per unit) or as GitHub issues (a story issue and one sub-issue per unit), reusing any pasted work items. A synthesis agent then merges the results into one branch, gets it green, and opens one PR linked to those tickets. Use when the user says "batch build", "parallel build", "fan out bmad-build", "orchestrate the build", or hands over a transcript or a set of work items to implement together. For one PR per unit instead of one for the run, use bmad-batch-build-split.'
---

# BMAD Batch Build

You are the orchestrator. You split the work, dispatch workers, check what they
return and hand the results to synthesis. You never write product code
yourself.

The flow is: input → intake (draft, readiness, one question round, split
and mode) → **user approves** → freeze → tickets + worktrees + workers →
gate → synthesis → one PR linked to the tickets.

The schedule is chosen automatically from the split's dependencies (section
1, "Schedule the units"):

- **parallel:** every unit gets its own worktree and all workers start
  together, then synthesis merges them.
- **lane:** the units take turns in one worktree on the run's branch, so
  nothing is installed twice and nothing is merged. Small runs and chains of
  dependent units go this way.
- **waves:** when some units need others' code to build, the units run in
  waves. Each wave runs as a lane or in parallel, and later waves start from
  the integration branch that already holds the earlier ones.

The user is asked twice, both before anything is built: one question round
on the intake, and the single approval of the intake, the plan and the gate
policy (section 1). After approval you ask only about real failures: a unit
that is blocked (`STATUS: blocked` or `blocked-interface`) or fails a gate
that needs a decision, a destructive or irreversible decision, and a
synthesis that reports `stuck` (after a wave too). Never ask to clarify a
requirement after approval: the frozen intake is the requirement.

Two rules shape the flow, because most of a run is waiting:

- **Keep long waits out of large contexts.** An agent that sits idle past its
  cache TTL pays to re-write its whole context. So installs and the unit tests
  run in the background while the worker plans and reviews, the Haiku coder
  (small context, cheap tokens) does the waiting for the install, and you keep
  your own context thin: you never read product code, `Explore` maps it.
- **Scripts do the mechanical work.** Installs, merges, migration and
  contracts checks, the gate and the ticket links are scripts, so the
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
- `<runs>` is this run's own folder, where every file of the run goes: the
  absolute path printed by `python "<skill>/scripts/run_dir.py" <slug> --root "<root>"`,
  `~/.claude/bmad-batch-runs/runs/<project>/<slug>/`. Every project's runs are
  kept there, out of the repositories.

## 0. Set up

**The commands in this skill are bash.** Run them with the Bash tool. When
this session has no Bash tool, only PowerShell (Claude Code on Windows can be
set up that way), run each one as `bash -c '<command>'` through PowerShell:
inside PowerShell's single quotes `$(...)`, `&&`, pipes and double quotes
reach bash unchanged; write a single quote inside the command as `''`. Never
translate a command into PowerShell. Tell every worker and synthesis the
same in its prompt: `No Bash tool: run every shell command as bash -c '<command>'
through PowerShell.`

Run `bash "<skill>/scripts/setup.sh" "<root>"` before anything else, as one
Bash call with a 10-minute timeout. It resolves what the run needs and fixes
what it can on its own, so a first run in a new project starts without help:
it installs BMAD when the project has none (a minute or two), copies the run's
agents into `<root>/.claude/agents/`, keeps `.claude/worktrees/` out of git
status, and finds the Python, the default branch, and where tickets and the PR
can go. Keep its lines:

- `PYTHON <cmd>`: type `<cmd>` wherever this skill says `python`.
- `BASE <branch>`: `<base>` when the project has no profile.
- `TICKETS ado`, `TICKETS gh` or `TICKETS off (...)`: with `ado` the
  tickets go to Azure DevOps and `<tickets script>` is `ado_tickets.py`; with
  `gh` they are GitHub issues and `<tickets script>` is `gh_tickets.py`. With
  `off`, skip every ticket step (creating and linking); the approval table
  says "Tickets: off (<reason>)". Pass `<tickets script>` to synthesis too.
- `PR az`, `PR gh` or `PR none (...)`: pass it to synthesis as `<pr>`.
- `AGENTS note: ...`: a project copy of an agent differs from the shipped one
  and was kept; pass that line on to the user.
- `FORK on (...)`: Claude Code's fork mode starts every subagent in the
  background. The run still works (workers and synthesis wait on `bg.sh`
  instead of ending their turn), but put the line's fix in the approval
  message as one line. Never suggest `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
  instead: it also stops the background Bash calls `bg.sh` runs on.
- A line with `BLOCKED` (and `SETUP blocked`): stop and show the user those
  lines; each says what to install. Nothing else stops a run.

**Choose the agents before reading the input.** Look at the agent types the
Agent tool offers you and set `<agents>`, the prefix of every agent name in
this run:

- `bmad-batch-build:bmad-batch-worker` is listed (the plugin install):
  `<agents>` is `bmad-batch-build:`.
- `bmad-batch-worker` is listed (a project or user copy): `<agents>` is empty.
- Neither (the first run after a copied install, before a restart loads
  `<root>/.claude/agents/`, or a session opened outside `<root>`): don't stop.
  Run in **fallback mode**: launch each of the run's agents as
  `subagent_type: general-purpose` with the `model` its file names (`opus`
  for bmad-batch-worker, which writes the plan, and `sonnet` for
  bmad-batch-synthesis and bmad-batch-enhancer), and
  begin its prompt with `First read <skill>/agents/<agent>.md and follow it as
  your standing instructions.` Add to every worker and synthesis prompt:
  `Fallback mode: pass --agents general-purpose to prepare-worktree.sh.` In
  the approval message say, in one line, that this run uses general-purpose
  agents because the run's agents load at the next session start, and that it
  costs somewhat more (no 1-hour cache).

Every agent name below is written with this prefix: `<agents>bmad-batch-worker`,
`<agents>bmad-batch-synthesis`, `<agents>haiku-implementer` and
`<agents>bmad-batch-enhancer`; in fallback mode each is launched as above.

**Branch names follow the repository.** `<run branch>` is the name for a
branch of type `<type>` (feat, fix, ...) about `<slug>`, in the convention the
repository's AGENTS.md or CLAUDE.md states (in hive-factory,
`jayson/<type>-<slug>`). Without a stated convention it is `<type>/<slug>`.

**Adapt to the project.** The scripts install, build and test with the
commands in the project profile, `<root>/.claude/bmad-batch-project.json`
(`<skill>/scripts/profile.py`). Run `python "<skill>/scripts/profile.py" check "<root>"`:

- `PROFILE ok`: nothing to do. The approval table says "Project commands:
  confirmed <date>".
- `PROFILE none`: if `~/.claude/bmad-batch-runs/install/installs.json` lists
  `<root>` with `"legacy": true`, skip the profile; the scripts keep their
  original commands. Otherwise run
  `python "<skill>/scripts/profile.py" detect "<root>" --write`, which drafts
  the profile from the lockfiles and package scripts. Then read the
  repository's AGENTS.md, CLAUDE.md, README and CONTRIBUTING for how it
  installs, builds, tests and typechecks, and correct it with
  `python "<skill>/scripts/profile.py" set "<root>" <key> '<JSON value>'`
  (one call per field, for example `set "<root>" suite '{"dir": ".", "cmd": "npm test"}'`;
  never Edit the file: Claude Code asks before every edit under `.claude/`)
  where they differ (they win: "use bun, never npm" changes every command)
  and fill in what the draft left empty. Every command runs from its `dir`
  without prompting; `targeted.test` keeps the `<files>` placeholder. For a project
  with a database, keep `database.per_worktree` on and set `database.migrate`
  (`{"dir", "cmd"}`, the command that migrates the database DATABASE_URL names)
  and `database.test_suffix` when the suite appends one to that name; every
  worktree then gets a private test database cloned from a migrated template
  (see `profile.py`). Without `migrate` the worktrees share the database. When the project is a web app or an API
  that a script can drive, and the docs say how to start it, set `app`
  (`{"dir", "start", "url"}`, with `{port}` where the start command takes its
  port; see the docstring of `profile.py`): every unit is then checked in the
  running app, and the gate holds it to that. Show `profile.py show "<root>"` in the
  approval table under "Project commands (first run in this repository)",
  and after approval run `python "<skill>/scripts/profile.py" confirm "<root>"`.
  A correction from the user goes in with `profile.py set` before confirming.
- `PROFILE stale`: lockfiles or package scripts changed since it was
  confirmed. Run `profile.py detect "<root>"` without `--write`, update the
  file where the commands changed, show what changed in the approval table,
  and confirm after approval.
- `PROFILE unconfirmed`: show it in the approval table and confirm after
  approval.

`<base>` is the profile's `base` branch; without a profile, setup's `BASE`.
With `REMOTE none`, read every `origin/<base>` in this skill as `<base>`, and
skip every fetch and push: the run ends on a local branch.

**Tell the user when the run needs them.** Before every question to the user
(the intake's question round, the approval, a service that is down, a gate
failure, a stuck synthesis), send a `PushNotification` with one line saying what is needed,
then ask. Load the tool with `ToolSearch` (`select:PushNotification`) if it is
deferred.

**How to ask.** Every question goes through `AskUserQuestion`. Its dialog shows
plain text only and covers the chat while it is open, so a question that
carries the details becomes one unreadable paragraph:

- **Details go in your message**, as markdown, before the call: tables, spec
  paths, error lines, run facts, each on its own line.
- **The question is one short sentence**: what is being decided, with the
  counts that matter. No ids, regexes, file or class names, and never a
  second question inside it.
- **One decision per question.** Separate decisions are separate questions in
  the same call, up to four. A combined option such as "Approve, new
  migration" means two questions were merged.
- **Headers are at most 12 characters, labels 1–5 words**, and each
  description is one plain sentence saying what happens if the user picks
  it, including anything it discards. The default comes first, marked
  `(Recommended)`.
- **Use `preview`** when the user needs to see something to choose: the unit
  list on an approve option, the failing check on a retry option.
- **A question that closes on timeout takes the Recommended option.** When
  `AskUserQuestion` returns without an answer (it timed out or closed
  unanswered), go on with the option marked `(Recommended)`, write
  `TIMEOUT: <header>: took <option>` into the run file, and say so in the
  final report. In the intake's question round, `intake.py defaults` does
  both: it writes the default under the intake's Assumptions and the
  `TIMEOUT:` line into the run file (section 1, Step 3), so don't write it
  again. Where no option is marked, a timeout takes the one that changes
  least: **Leave it out** for a failed unit, **Stop** for a blocker in a lane
  or dependency chain and for a stuck synthesis. Only the approval waits for
  a real answer, since approving starts the spending and the tickets: on a
  timeout there, ask it again once, then stop and record the run.
- **When to ask at all.** Before approval: the one question round and the
  approval, nothing else. After approval: only a real failure (a blocked or
  `blocked-interface` unit, a gate failure that needs a decision, a stuck
  synthesis, a service that is down) or a destructive or irreversible
  decision. A requirement that turns out unclear after approval is decided
  from the frozen intake, its Assumptions and its Open risks; a change the
  run must make anyway goes in with `intake.py amend`, never as a question.

## 1. Intake

The intake, `<runs>/<slug>-intake.md`, is the run's one statement of what it
builds: the user's prompt verbatim, the goal and non-goals, testable
acceptance criteria, the pinned shared interfaces, the units, the mode and
the holdout scenarios. Workers build from it, review and the gate check
against it, and synthesis tests the merged result with its scenarios. It
lives in `<runs>`, never in a worktree, and it is written before any
worktree exists. `<skill>/scripts/intake.py` creates, checks, freezes and
reads it; its docstring gives the format. The intake goes through five steps:
draft, readiness, one question round, the single approval, freeze.

### Step 1. Draft

**Read the input.** The user provides one of two things:

- **A meeting transcript.** Extract the decisions and action items that ask for
  a code change. Ignore discussion that didn't end in a decision. Note anything
  ambiguous as an open question for Step 3.
- **Pasted work items.** These are already analysed. Treat each item's title,
  description and acceptance criteria as the requirement. If an item carries an
  ADO ID (`#1234`, `1234` or a work item URL), or with `TICKETS gh` a GitHub
  issue number (`#123` or an issue URL), record the ID. If you don't know an
  ADO item's type, read it with
  `az boards work-item show --id <id> --query "fields.\"System.WorkItemType\"" -o tsv`.
  A GitHub issue has no type: it is a story if it already has sub-issues
  (`gh api repos/<owner>/<repo>/issues/<n> --jq .sub_issues_summary.total`
  is above 0), otherwise a task.

Give the run a short kebab-case slug and a conventional type (`feat`, `fix`,
and so on). Create the run folder and use the path it prints as `<runs>`:

```bash
python "<skill>/scripts/run_dir.py" <slug> --root "<root>"
```

Save the user's prompt, as given (the transcript or the work items, word for
word, nothing summarised), to `<runs>/<slug>-prompt.txt` with the Write tool,
then create the intake:

```bash
python "<skill>/scripts/intake.py" new "<runs>" <slug> --prompt-file "<runs>/<slug>-prompt.txt"
```

It writes the template with the Source prompt filled in, prints the intake's
path, and writes `INTAKE start` into the run file. It refuses to overwrite an
existing intake: a resumed run keeps its own. Record the run at
`<runs>/<slug>.md`. That file holds the source summary, the units, their
branches and worktrees, and their gate results. Keep it updated after every
step so the run can be resumed.

**Map the code** without reading it yourself. Your context is re-written in
full every time a worker, synthesis or the user answers after a long gap,
since this session's cache keeps the session's own TTL, so every file you
read here is paid for again at each of those returns. Read only `AGENTS.md`
and, when the project has one, its system map. First run
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

**Fill the intake** with the Write or Edit tool, section by section (the
headings stay as they are, in their order; a `<...>` placeholder left in a
field counts as empty):

- **Goal:** one to three sentences. **Non-goals:** what this run does not do.
- **Acceptance criteria:** one `- AC-<n>: ...` line per testable behaviour,
  in Given/When/Then.
- **Shared interfaces:** every interface two units meet at, pinned exactly
  (the route and payload, the type, the method signature), as
  `- <symbol>: <current shape> -> <new shape> | owner U<n>`, or `- none`.
  Workers code against these lines, so pin them here and nowhere else.
- **Units:** one line per unit, as the split rules below say:
  `- U<n> <title> | size <S|M|L|XL> | risk <low|med|high> | depends on: <- or U<n> (reason), ...> | touches: <files> | ACs: <AC ids> | items: <n>`,
  with indented lines under it for its kind and unchanged behaviour (bug
  units), the exact excerpt of the source it came from, `--apps`, its branch
  and its ticket.
- **Coupling map:** which units are independent, which must run in order, and
  why.
- **Architecture (C4):** Mermaid C4 diagrams of what the run changes, each
  in its own ` ```mermaid ` block, drawn from the code map. Give a
  `C4Context` (the system, its users and the external systems it talks to)
  and a `C4Container` (the apps, services and stores, with what this run
  changes in each one's description). Keep labels short and leave out
  double quotes inside them. The approval page draws these, plus a component
  view of the units that it builds itself, so don't draw the units here.
- **Verification scenarios:** `- SC-<n>: Given ... When ... Then ...` lines
  that exercise the acceptance criteria end to end. They are the holdout:
  synthesis runs them on the merged result, and workers never see them.
- **Assumptions and defaults applied**, **Open risks:** what you assumed and
  what may go wrong.
- Leave **Mode decision** to `set-mode` and **Changes after approval** empty.

A unit is a piece of work that one worker can build and test on its own, from
`origin/<base>`, without seeing any other unit. Rules for splitting:

- **Disjoint files.** Two units must never edit the same file. If they would,
  merge them into one unit.
- **Cut dependencies at an agreed interface.** Workers can't see each other's
  branches. When unit B needs something unit A builds, such as an endpoint, a
  type or a method, don't serialise them. Pin the exact interface under
  Shared interfaces with A as its owner. A implements it and B codes against
  it, so both run at the same time. Put the two in one unit only when the
  interface can't be pinned down before implementation starts.
- **Shared hotspots get one owner.** A single unit owns all of a run's
  migrations, all `packages/contracts` changes and all lockfile changes. If
  several units need contract changes, the owner unit makes them and the
  agreed shape is pinned under Shared interfaces for the others.
- **Record real dependencies, and only those.** When unit B can't build or
  test without unit A's code (it imports A's new module or types at compile
  time, or its tests run A's code), and an agreed interface can't stand in
  for that code, B depends on A. Write it in `depends on:` with the reason.
  Prefer an agreed interface wherever one works: a dependency moves B to a
  later wave.
- **No cap on the count.** Split as finely as file ownership allows. Each unit
  should be one session-sized build. A single unit is fine: it runs as a lane
  of one.

For every unit, write:

- a title
- a size, from the files it owns:
  - **S:** up to 3 files, with no migration and no new module;
  - **M:** 4 to 10 files;
  - **L:** more than 10 files, a new module, or a migration plus API plus UI;
  - **XL:** more than one session's work. plan-waves.py schedules it as L
    and prints a Split line for it: split it before the approval.
- a risk, which sizes the review: **high** for migrations, auth,
  payments, deleting or rewriting data, and the owner of a shared interface;
  **med** for a change to a shared module, an API or a contract;
  **low** for a change local to its own files.
- `items`: how many separate things it must fix or build (findings, work
  items, acceptance criteria), and `touches`: the source files they are in.
  **Keep a unit to at most 50 items, and give a file of more than 2500 lines
  its own unit** when it carries real work. One worker's coder cannot hold
  more: in sonar-zero-5 a 64-finding unit with a 6500-line route file took
  315 min and about 12 coder rounds, and held its whole wave.
- its ACs: the ids of the acceptance criteria it delivers. Every AC should be
  delivered by some unit.
- `depends on`: the units whose code it needs to build or test, each with the
  reason (for example "imports the chart kit types"), or `-`
- a branch, `<run branch>-<n>`, used when the unit runs in a parallel
  wave; a unit in a lane wave works on the run's branch
- `--apps`, if the unit touches `apps/`
- its kind: `bug` when it fixes behaviour that is wrong now, otherwise
  `change`. A bug unit's block has the indented line `kind bug`, says what
  happens now and what should happen, and ends with an **Unchanged
  behaviour** list: what nearby must keep working as it does. The worker
  reproduces the bug before fixing it, and the gate (`--bug`) rejects the unit
  without that reproduction.
- its ticket

The worker sees only `intake.py unit <id>` output: the Goal, the Non-goals,
the Shared interfaces, its own unit block and the full text of its ACs. So a
unit's block must say everything its worker needs that the ACs don't.

Heavy commands (installs, builds, full test suites, the synthesis verify) go
through `scripts/heavy.sh`. It admits a command only while the machine has room
for it, counting every session and run on this machine, so many runs can go at
once without crashing; they queue instead. Waiting commands start in arrival
order. CPU and RAM may still run high, and that is expected. The limits follow
the machine: one slot per CPU (a test suite takes as many slots as its Vitest
workers, a quarter of the CPUs, 3 on 12 CPUs), a free-RAM check against an
estimate learned from earlier runs of the same kind of command, and a pause
while the machine is already stalling on memory or CPU (`/proc/pressure`) or
swapping. A command that waits past `BMAD_HEAVY_MAX_WAIT` starts anyway only
if 512 MB is free; otherwise `heavy.sh` exits 75, "overloaded, try again", and
you retry later. Each command runs in its own session, so a killed `heavy.sh`
leaves no orphan: the next call ends it. `heavy.sh --status` shows what runs
and waits.

`scripts/heavy_hook.py` is an optional PreToolUse hook the user can enable. It
routes a heavy command that an agent typed without `heavy.sh` through the
queue anyway. The skill never enables it itself.

### Step 2. Readiness

```bash
python "<skill>/scripts/intake.py" check "<runs>/<slug>-intake.md"
```

- `READY` (exit 0): go on.
- `NOT READY` (exit 2), with one `MISSING: <what>` line per gap: fill what
  you can from the input and the code map, and check again. When a gap needs
  input only the user has (no goal can be read from the prompt, no testable
  behaviour, nothing to build), **stop**: show the `MISSING:` lines in your
  message and ask the user for the missing input. Plan nothing and dispatch
  nothing until a new check prints `READY`.

### Step 3. One question round

Collect every open question into **one batch**: ambiguities in the input,
choices the code map raised (edit a migration or add one, which unit owns a
contract), interface shapes you could not pin. At most about 6. Each gets a
**Recommended** default, the option you would take, first. `AskUserQuestion`
takes up to 4 questions per call, so use at most two calls back to back.
Follow "How to ask" above. First mark the round:

```bash
python "<skill>/scripts/intake.py" asked "<runs>/<slug>-intake.md" --count <n>
```

Write each answer into the intake (Goal, ACs, Units, Shared interfaces or
Assumptions, wherever it belongs). A question that times out takes its
Recommended default:

```bash
python "<skill>/scripts/intake.py" defaults "<runs>/<slug>-intake.md" --question "<header>" --took "<option>"
```

It writes `- TIMEOUT: <header>: took <option>` under Assumptions and defaults
applied and the `TIMEOUT:` line into the run file. With no open question,
skip the round (and `asked`). Then run `check` again: it must print `READY`.
This is the only round of questions about the requirements in the whole run.

### Schedule the units

Decide it yourself from the units; don't ask. Write the plan file
`<runs>/<slug>-plan.json` from the intake:

```bash
python "<skill>/scripts/intake.py" to-plan "<runs>/<slug>-intake.md" "<runs>/<slug>-plan.json"
```

It writes each unit's id, title, size, risk, `depends_on` with the reasons,
`items`, `files` and ACs, in the format documented in
`<skill>/scripts/plan-waves.py`, and keeps the plan's other keys. Then run
plan-waves, adding `--busy` when the machine is busy and `--no-tests` when
the profile has no `suite` or the repository's instructions ban tests:

```bash
n=$(bash "<skill>/scripts/heavy.sh" --count); [ "$n" -ge $(( $(nproc) / 3 )) ] && b=--busy || b=
python "<skill>/scripts/plan-waves.py" "<runs>/<slug>-plan.json" $b [--no-tests] --root "<root>"
```

It ends with a **Set mode:** line, a ready-to-paste `intake.py set-mode`
command with the Mode line and the reason for it. Run it (one Bash call, the
command after `Set mode: `); it fills the intake's Mode decision. Every time
you change the units, run `to-plan`, `plan-waves.py` and the `Set mode`
command again.

When it prints a **Split** line, split that unit as the line says (by file or
by area, each part with its own unit line, ACs and ticket), in the intake,
then run the three commands again, before you show the split for approval.
Parts that edit the same file never run side by side, because they would
conflict at the merge: give each later part a `depends on` the one before, so
they run as a lane. A unit that is only one big file and cannot be cut
further stays one unit: show its Split line in the approval table with the
reason, instead of splitting it.

It also reads each unit's `files` and chains units that share one: the later
gets a `depends_on` on the earlier (a **Chained** line says so), because two
units editing one file cannot be merged side by side. Write the chain into
the intake's unit line too.

It schedules the units in rounds. A round holds up to 4 ready units (all their
dependencies are in earlier rounds), the unit with the longest chain of work
behind it first and then the longest unit, so units of similar length share a
wave and a short unit does not wait behind a long one. A wave's `Agent` calls
return together, so a unit cannot join a wave that is already running. Each
wave then runs as a lane or in parallel, whichever is faster for that wave by
more than a margin. The margin is 20 min, or 40 min on a busy machine, and a
tie goes to lane, because lane costs less and merges nothing. A parallel wave
is stretched by the queue for the machine: its suites wait for `heavy.sh` when
the workers together want more CPUs than there are. The script estimates
whole-run times for lane, parallel (only when no unit depends on another) and
waves, and picks the mode:

- **parallel:** one wave, run in parallel. No unit depends on another.
- **lane:** every wave runs as a lane: one unit, small units, or a chain.
- **waves:** anything else.

No parallel wave has more than 4 workers, 3 when one other run is active on
the machine and 2 when more are, and fewer when the free RAM can't hold that
many units; a larger group of independent units becomes several waves in a
row. Each worker brings a coder and up to four reviewers, and Claude Code
allows 20 subagents at once per session; on 2026-10-26 runs of 12, 9 and 5
parallel workers at the same time got launches refused and stalled the build
queue for hours. The script prints the cap it used.

A unit with `items` takes 40 min plus 2.2 min per item, plus 30 min per 1000
lines of every file over 2500 lines it owns (sonar-zero-5: 21 findings took 85
min, 46 took 140, 64 with a 6500-line file took 315). A unit without `items`
takes its size's minutes: S 50, M 100, L 160. Recorded runs correct every
number from the first one on (three prior runs' worth of weight), first over
all projects and then over this project's own, so give every unit `items` and
`touches`. The script also prints a p80, from the measured spread of recorded
units (1.6 times the estimate until there are five), and a **Critical path**:
the least the run can take however many workers it has. Show the p80 with the
estimate: sonar-zero-5 was estimated at 6 h and took 13.4 h. A **Straggler
risk** line names a unit that would dominate the run. Every unit gets a budget,
twice its estimate (at least 60 min over it), in the plan file under
`unit_estimates`. The script writes the schedule back into the plan file and
prints the **Mode** line for the approval table. Recording the run later keeps
each unit's measured time, items and the estimate, so the estimates calibrate
themselves, and `python "<skill>/scripts/plan-waves.py" --accuracy` says how far
off they were.

A worked example: units 1 (chart kit) and 2 (contract fields) are
independent, and units 3, 4 and 5 import both. That makes two parallel waves,
{1, 2} then {3, 4, 5}: about 450 min, against 670 min for lane.

**Is a batch run worth it?** Every unit pays a fixed cost (worktree, install,
spec, plan, coder, reviewers, gate), and every parallel wave pays a merge and a
verify. That only pays off when units really run side by side. When the work
is a chain, runs at most 2 units at once over 3 or more waves, or has no tests
to verify and runs at most 3 at once, one session that keeps the whole design
in view builds it faster, as does a run of 4 h or more whose units, built one
after another in one session, would take about as long. `plan-waves.py` then
prints an **Advice** line with the reasons. A run whose p80 is 6 h or more also
gets a **Long run** line: offer to run the first wave as a run of its own. Units that share hotspot files (navigation, store, registries,
generated route trees) and so all hang off one foundation unit are the usual
sign.

### Tickets

Only with `TICKETS ado` or `TICKETS gh` from setup; with `TICKETS off` the
run has no tickets and the table's ticket column says `off`.

On GitHub the same pattern uses issues: the story is an issue and each Task
is a sub-issue of it. Below, "User Story" and "Task" mean those issues, and
an existing issue's role follows the rule under "Pasted work items".

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
ones. Its description is a short HTML summary of the intake's Goal and the
list of units. A new Task's title is the unit title, and its description is
the unit's `intake.py unit` output as HTML. With `TICKETS gh`, write both
descriptions in Markdown instead.

### Step 4. The single approval

The user approves on the plan page ("Asking for approval: the plan page"
below), which shows everything listed here. The chat message and
`AskUserQuestion` are the fallback when the page can't be used.

**The intake summary:** the Goal, the Non-goals, the units table, the Mode
line and the Open risks, plus the Assumptions the question round applied by
default.

The units table has these columns: unit, ticket (`#id` reused, or `new
Task`), size, risk, owns (its touches), interfaces (what it provides or
consumes, from Shared interfaces), the ACs it delivers in brief, and **depends
on** (unit ids and the reason, or `-`). Above the table, give the story or
stories (`#id` reused, or `new User Story: <title>`). Approving also approves
creating the new tickets. Mark bug units with `(bug)` after their title.

Under it, paste the **Mode** line that `plan-waves.py` printed: the chosen
mode, every estimate with its p80, the wave order and the dependencies, for
example
`Mode: waves (≈450 min, p80 ≈720 min; lane ≈670 min, parallel not possible, waves ≈450 min). Order: {1,2} → {3,4,5}.`
Paste its **Critical path**, **Straggler risk**, **Chained** and **Long run**
lines with it, when it printed them. This is information, not a question. The
user can still change the schedule, or a dependency, by choosing "adjust".
Write the same line into the run file. The plan file keeps the sizes and the
waves.

Combining tasks is the one way the split loses parallelism. If you put more
than one input task into a single unit, add a **Combined** line under the table
for each such unit: name the tasks and give the reason (a shared file, or an
interface that can't be fixed in advance). The user can then ask for a
different cut.

When `plan-waves.py` printed an **Advice** line, put it above the table and
offer **Build directly (Recommended)** as the first option, ahead of approve,
adjust and cancel. Building directly means:

- run `intake.py freeze` as below, then no workers and no synthesis;
- `git fetch origin`, then create one worktree on `<run branch>` from
  `origin/<base>` (never from the local `<base>`, which may be stale);
- build the units in this session, in dependency order, with `bmad-build`,
  passing each unit's `intake.py unit` output as one work list, and check the
  intake's acceptance criteria and verification scenarios at the end;
- start every subagent prompt for this work (implementation, review) with
  the shell rules this skill's own agents carry, since a general-purpose
  agent has none of them: write and change files with the Write or Edit
  tool, never through a heredoc in Bash (`cat > f <<'EOF'`,
  `python - <<'EOF'`), which breaks with "unexpected EOF while looking for
  matching"; never start a command with `sleep N`, which the harness
  blocks, but run anything long with `bash "<skill>/scripts/bg.sh" run` and
  `wait`;
- open one PR as `<skill>/references/synthesis.md` section 4 describes, link
  the tickets as its section 5 does unless `TICKETS off`, and report as in
  section 8.

Approving it still approves the new tickets.

Otherwise the choice is approve, adjust or cancel. With it, the **gate
policy** is asked once (on the page with `--gate`; in the fallback, in the same
`AskUserQuestion` call) (skip it for a single unit, or
when every unit is in one lane): `If a unit nothing depends on fails its gate,
what then?`, with **Re-dispatch once, then leave it out (Recommended)**, "A
fresh worker builds it once more; if that fails too, its task stays open and
out of the PR", **Merge unverified**, "Keep its code when it failed only on
review traces, the reproduction or the app check; the PR marks it", and **Ask
me**, "Stop and ask about each failed unit". Write the answer into the run file
as `Gate policy: <option>`. The open questions were settled in Step 3, so this
call holds no other question.

**Adjust** is the one way back: change the intake (it is not frozen yet), run
`check`, `to-plan`, `plan-waves.py` and the `Set mode` command again, and ask
again. Iterate until the user approves. The approval waits for a real answer
("How to ask"). **Cancel** ends the run: record it (section 8).

#### Asking for approval: the plan page

The user reviews the plan as a web page with C4 diagrams. They can comment on
any section, AC or unit, and they choose there. Build the page once the Mode
line is set:

```bash
python "<skill>/scripts/plan-review.py" render "<runs>/<slug>-intake.md" --out "<runs>/<slug>-plan.html" --plan "<runs>/<slug>-plan.json" [--gate] [--note "<runs>/<slug>-plan-note.txt"]
```

`--gate` adds the gate-policy choice (leave it out for a single unit, or when
every unit is in one lane). The plan's Advice puts **Build directly** first.
Then serve it, as a Bash call with `run_in_background: true`:

```bash
python "<skill>/scripts/plan-review.py" serve "<runs>/<slug>-plan.html" --result "<runs>/<slug>-plan-review.json" --run "<runs>/<slug>.md" --open
```

It opens the browser and writes the page's URL to `<runs>/<slug>-plan.html.url`.
Read that file, and send a short message: the URL, the story line, the unit
count, the Mode line and the run facts (worktree, branch, base commit,
whether anything is pushed or written to the tracker). Leave the full table
and diagrams to the page. Send a `PushNotification` that the plan is waiting,
then end your turn. The server's exit wakes you, and its output is the
answer:

- `DECISION approve`: the comments, if any, are small changes. Write each
  into the intake (Assumptions, an AC's wording), run `check`, and go on to
  the freeze. A comment that changes a unit, an AC, a dependency or an
  interface makes it `adjust` instead.
- `DECISION adjust`: apply every comment to the intake, then run `check`,
  `to-plan`, `plan-waves.py` and `set-mode` again. Write one line per comment
  into `<runs>/<slug>-plan-note.txt`, saying what you changed or why you
  didn't. Render with `--note` and serve again.
- `DECISION build-directly` and `DECISION cancel`: as below.
- `GATE <option>`: write `Gate policy: <option>` into the run file.
- `TIMEOUT` (4 hours with no answer): ask once with `AskUserQuestion` as
  below. Without an answer, stop and record the run.

When the user answers in the chat instead (they type "approve", or paste the
`DECISION` lines the page copied when it couldn't reach the server), stop the
server with `TaskStop` and use that answer. When the page can't be opened at
all (a cloud session, or no browser), ask with `AskUserQuestion`.

#### Asking for approval in the chat

The fallback, when the page can't be used. Follow "How to ask" above:

- **The message holds the plan**: the intake summary, the story line, the
  table, the Mode line, any Advice, Combined, map and "Project commands"
  lines, the intake's path, and the run facts (worktree, branch, base commit,
  and whether anything is pushed, opened as a PR or written to the ticket
  tracker).
- **The approval question** gives the counts, for example `Approve the split:
  2 units in one lane, about 230 min?`. Options: Approve, Adjust, Cancel,
  with **Build directly** first instead when there is Advice. A correction to
  the project commands comes in through Adjust or the free-text answer.
- **The Approve option's `preview`** has one line per unit, `<id>  <size>
  <risk>  <title>` plus `after <ids>` when it depends on another, then the
  Mode line.

For the run in the example above, the question round (Step 3) and the
approval would be:

```text
Round   Q1  [Migration]  The LiveCounts migration is not merged yet. Edit it or add a new one?
            Edit in place (Recommended) | Add a new migration
Approval Q1 [Split]      Approve the split: 2 units in one lane, about 230 min?
            Approve (Recommended) | Adjust | Cancel
            preview of Approve:
              U1  L  high  Count ids, metrics handler, nullable totals, migration
              U2  S  low   Visitor flow endpoints, 24 h cutoff        after U1
              Mode: lane (≈230 min). Order: 1 → 2.
```

(Both units are in one lane, so the approval has no gate-policy question. A
run with independent units adds it as one more question:
`Q2  [Gate]  If a unit nothing depends on fails its gate, what then?`.)

### Step 5. Freeze

On approval (Approve or Build directly):

```bash
python "<skill>/scripts/intake.py" freeze "<runs>/<slug>-intake.md"
```

It requires `READY`, hashes everything above `## Changes after approval` into
`<slug>-intake.md.sha256`, writes `INTAKE approved` into the run file and makes
the intake read-only. It runs once; a second freeze is refused. From here on
change the intake only with

```bash
python "<skill>/scripts/intake.py" amend "<runs>/<slug>-intake.md" --reason "<why>" --text "<what changed>"
```

which appends a dated line under Changes after approval and leaves the frozen
part, and its hash, as approved. Never Write or Edit the intake after the
freeze: `intake.py verify` then prints `FROZEN changed`.

**After approval** run to the end without asking, except for the real failures
"How to ask" names: a blocked or `blocked-interface` unit, a failed unit that
later units build on (section 6), a failed unit under **Ask me**, a destructive
or irreversible decision, and a stuck synthesis (section 7). Never ask to
clarify a requirement: decide it from the intake, and record a change the run
must make anyway with `amend`.

## 2. Split into units

Folded into section 1 (Step 1, "Schedule the units" and "Tickets").

## 3. Get approval

Folded into section 1 (Steps 4 and 5).

## 4. Launch

The run's integration branch is `<ibranch>` = `<run branch>`. Its
worktree `<iwt>` is the first worktree that holds it:

- When wave 1 is a lane, `<iwt>` is `<root>/.claude/worktrees/<slug>`, which
  the first unit's worker prepares on `<ibranch>`.
- When wave 1 is parallel, `<iwt>` is the first unit's worktree. Synthesis
  creates `<ibranch>` there when it merges wave 1. Plain parallel mode is this
  case with one wave.

There is never a separate integration worktree, so no extra install competes
with the workers.

Before any ticket or worktree is created, the intake must be frozen as
approved:

```bash
python "<skill>/scripts/intake.py" verify "<runs>/<slug>-intake.md"
```

It must print `FROZEN ok`. On `NOT FROZEN` the approval never froze it: go
back to section 1, Step 5. On `FROZEN changed` the approved text was edited:
create nothing; show the user the change and ask whether to restore the
approved intake or approve the new one (a real failure, so it may ask).

0. Preflight, in one Bash call, before anything is created:
   ```bash
   docker info --format '{{.ServerVersion}}' >/dev/null 2>&1 && echo docker ok || echo docker DOWN
   node -e "require('net').connect(55432,'127.0.0.1').on('connect',()=>{console.log('postgres ok');process.exit(0)}).on('error',()=>{console.log('postgres DOWN');process.exit(1)})"
   bash "<skill>/scripts/check-test-env.sh" "<root>"
   ```
   Run the Postgres check only for a project without a profile whose
   `<root>/.env` has `DATABASE_URL`, or whose profile sets
   `database.per_worktree` on; skip the Docker check when the project doesn't
   use Docker. When a profile turns `per_worktree` off, tell the user the
   test suite may need the database running. If either is down,
   notify and ask `Docker is down. Start it, then check again?` (or
   Postgres, or both in one sentence), with **Check again (Recommended)**,
   "I started it; run the check again", and **Stop**, "End the run; nothing
   was created yet". Check again before launching, and ask again while it is
   still down.
   Every worker's DB suite needs Postgres at `127.0.0.1:55432`; a run that
   started with Docker down once stalled for nearly five hours.
   `vitest BROKEN` means the limits the skill's scripts set stop Vitest from
   starting at all, so every unit's tests would fail before running. Do not
   launch: tell the user the error line; it is a fault in the skill, not in
   the project.
1. Fetch once with `git -C "<root>" fetch origin <base> --quiet`. Concurrent
   fetches into one `.git` race on `FETCH_HEAD`, which is why the scripts never
   fetch. Then make the run's template test database, once, in the
   foreground: `bash "<skill>/scripts/db-template.sh" "<root>" "<slug>"`. It
   prints `DB-TEMPLATE <template>`, which every worker's step 0a passes as
   `--db-template <template>`, or `DB-TEMPLATE none: <why>` (no database, no
   migrate command, a main checkout ahead of the base, Postgres down), after
   which the workers leave the flag out and fall back. Record the line in the
   run file. It never blocks the run.
2. In **one message**, send all of the following:
   - **The tickets** (skip with `TICKETS off`): write `<runs>/<slug>-tickets.json`
     in the format documented in `<skill>/scripts/<tickets script>`. Then make
     one Bash call with `run_in_background: true` and a long timeout:
     `python "<skill>/scripts/<tickets script>" "<runs>/<slug>-tickets.json"`.
     The IDs are written back into that file. If the call fails, re-run it once;
     it creates only what is still missing. If it fails again, carry on with the
     build and report the error at the end. Tickets never block the code.
   - **Wave 1's workers:** one foreground `Agent` call per unit for a parallel
     wave, or the first unit's worker for a lane wave, using the prompts in
     `<skill>/references/worker.md`. Each worker runs `bmad-build-auto`, which
     starts its own review subagents, so the calls must stay in the foreground.
     Fill every worker's prompt, each lane unit's included, from the intake,
     with `<intake>` = `<runs>/<slug>-intake.md`, `<iface>` =
     `<runs>/<slug>-interface-changes.md` and `<n>` the unit's plan id (`1`
     for `U1`):
     - the lines `Unit: <n>`, `Size: <size>`, `Risk: <risk>` (the unit's
       risk in the intake), `Intake: <intake>` and
       `Interface changes: <iface>`;
     - in step 0a, `--intake "<intake>" --unit <n> --iface "<iface>"` on
       `prepare-worktree.sh`. A later lane unit skips step 0, so its own
       `Unit:` line is what tells it which unit it is;
     - as the intent, the output of
       `python "<skill>/scripts/intake.py" unit "<intake>" <n>`, word for
       word (after the lane line and the "Already on the branch" list, when
       `references/worker.md` adds them).
     Never paste the source prompt or the verification scenarios into a
     worker's prompt.

The ticket call finishes long before the workers; its notification may arrive
while they run. Just note it.

Each worktree gets its own `.env` that points at a private test database,
`hive_pb_<name>_test`, cloned from the template (or, when that failed, created
and migrated by the test setup). Workers and synthesis then
run the DB suites side by side without deleting each other's rows. Never `cd`
your own shell into a worktree.

## 5. Run the waves

Work through the waves in order. Within a wave:

- **Parallel wave:** all its `Agent` calls go in one message and return
  together. Record each worker's reply in the run file, its `INTERFACES` line
  included: synthesis checks those against the intake's Shared interfaces and
  the interface broadcast (`intake.py iface show
  "<runs>/<slug>-interface-changes.md"`), where a worker that had to change
  an agreed interface recorded it. Copy every
  `SKILL ISSUES` entry other than `none`, from workers and synthesis alike, to
  its own run-file line `SKILL ISSUE: <unit or synthesis>: <entry>`; step 8
  records them for fixing the skill. Then gate
  every unit (section 6) against the wave's base.
- **Lane wave:** send one worker at a time in `<iwt>`, with the lane prompt.
  When a worker returns, record its reply and gate its unit straight away
  (section 6, with its base) before sending the next.

A wave's **base** is where its units start. For wave 1 that is `origin/<base>`.
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
ask as section 7 describes before starting the next wave.

After the last wave, run the final synthesis (`<final>` = `yes`), which opens
the PR.

**Stragglers.** Every unit has a budget in the plan file (`unit_estimates`). Write
the UTC time each worker is sent into the run file. A wave's `Agent` calls return
together, so the orchestrator cannot look in on a parallel wave; there the
worker's own rule applies (`references/worker.md`: past its budget it starts no
new fix round and lists what is left under `REMAINING:`). In a lane, and
whenever you resume a handed-back worker, run
`python "<skill>/scripts/straggler.py" "<runs>/<slug>-plan.json" <unit>=<minutes
since it was sent>`. A **Slow** line is only a note in the run file. After a
**Straggler** line (exit 1), tell that worker with `SendMessage`: "Budget
reached: finish the round you are in, start no other, and reply with what is
left under REMAINING." A reply whose `REMAINING:` is not `none` is not a failed
unit: gate it as usual, then add the remaining items to the plan file as a new
unit (`items`, `files`, `depends_on` the finished unit, and the finished
unit's `risk` and `acs`), run `plan-waves.py` again, and build it in a later
wave, with the finished unit's `intake.py unit` output plus the remaining
items as its intent. Record it with `intake.py amend` (`--reason "straggler
remainder"`). It is inside the scope the user approved; list it in the final
message. sonar-zero-5 had nothing like this: its slowest
unit ran 315 min and held its wave.

**Turn budgets.** Each agent has a `maxTurns` budget in its agent file: worker
300, Haiku coder 300, synthesis 400, about twice the longest run that
finished. A budget stops a stuck agent; it isn't meant to be hit. A worker
reply without its `SPEC:`/`STATUS:` lines means the worker ran out. Gate the
unit anyway, because its branch may hold usable commits, and record "turn
budget" as the reason. Section 6 then decides what happens next.

**Background agents and early hand-backs.** With fork mode on (setup's
`FORK on`), Claude Code starts every subagent in the background, so your
`Agent` calls answer "Async agent launched" and each worker's and synthesis's
reply arrives as its own task notification. For you that is fine: end your
turn and wait for those notifications; never poll for them. For a worker or
synthesis it is not, because a subagent that ends its turn is handed back.
So they never end their turn while an agent they launched still runs: they
wait on a `bg.sh expect`/`done`/`wait` file instead (`references/worker.md`
step 2, and the synthesis agent file). A hand-back should now be rare.
When one still comes (a reply that says `blocked` or `in progress` because a
coder or reviewer it launched is still running, or a synthesis reply without
a `RESULT:` line), it is not a failed unit. Don't gate it and don't ask the
user: wait for the notifications of the agents it says are still running,
then continue it with `SendMessage`, giving it their reports word for word
and what it still has to do, and wait again. Gate only a reply that says
`done` or `ready`, or one that names a real blocking condition. Note each
resume in the run file as `RESUME: <unit or synthesis>: <why>`; the journal
counts them.

## 6. Gate

Apply the gate in `references/worker.md` to every unit, in one Bash call with
`scripts/check-unit.sh`, and record the results in the run file. Every unit's
call carries the interface check:
`--intake "<intake>" --unit <n> --iface-file "<iface>" --reply-interfaces "<INTERFACES>"`,
with `<INTERFACES>` the value of the `INTERFACES:` line of that worker's
reply, word for word (`none` when it says none). A unit that changed a pinned
interface without an approved `intake.py iface add` entry for it fails here.

- **Every unit is ready:** continue (the next unit in a lane wave, or the
  wave's synthesis or the next wave).
- **Some units nothing depends on are not ready:** follow the run file's
  `Gate policy:` line without asking. Write each unit, its reasons and its
  spec path into the run file, and what the policy did with it.
  - **Re-dispatch once, then leave it out** (also the policy when the run
    file has none): re-dispatch the unit once, as below, with the gate's
    reasons added after its `intake.py unit` output. If the second worker's unit is not ready
    either, leave it out: its task stays open and out of the PR.
  - **Merge unverified**: when the unit failed only on workflow traces (no
    baseline, no intent contract, no review pass), keep its code and mark it
    unreviewed; when it failed only on the reproduction or the app check,
    keep it and mark it unverified. Otherwise (no commits, a dirty tree, the
    wrong branch, a blocked reply) treat it as the first policy.
  - **Ask me**: show in your message which units failed and why, with the
    spec paths. Then ask one question per unit, up to four (beyond that, one
    question for the rest), for example `Unit 3 (metrics handler) isn't
    ready. What now?`, with **Leave it out**, "Continue; its task stays open
    and out of the PR", **Re-dispatch**, "A fresh worker builds it again", and
    **Stop**, "End the run here; the branches stay as they are", plus **Merge
    unreviewed** or **Merge unverified** when only the traces, or only the
    reproduction or app check, failed.
  The final report lists every unit the policy re-dispatched, left out or
  merged unreviewed or unverified.
  To re-dispatch, clear the unit first, because `prepare-worktree.sh` refuses an
  existing path and branch:
  `bash "<skill>/scripts/remove-worktree.sh" "<root>" "<name>" "<branch>"`, then
  `git -C "<root>" branch -D "<branch>"` (the script's `-d` keeps an unmerged
  branch). Then send a fresh worker with the same intent, plus the gate's
  reasons. A correction that changes what the unit must do goes into the
  intake first, with `intake.py amend`.
- **`STATUS: blocked-interface`:** the worker found that the agreed interface
  can't work as pinned, and says why. It is a failed unit that needs a
  decision, whatever the gate policy, so ask (it is a real blocker): show the
  reason and the interface line from the intake, and ask whether to change
  the interface (record it with `intake.py amend` and `intake.py iface add`,
  then re-dispatch the unit and any unit that consumes it), leave the unit
  out, or stop.
- **A lane unit is not ready:** the lane stops there, because later units
  build on it. A unit that later waves depend on stops the run the same way.
  These are the only gate failures you always ask about, whatever the gate
  policy. Ask `Unit <n> (<short title>) isn't ready and later units build on it. What
  now?`, with:
  - **Keep, unreviewed**: "Continue with the next unit; the PR marks this one
    unreviewed".
  - **Re-dispatch**: "Discard this unit's commits and build it again". Reset
    the lane to the unit's base with `git -C <wt> reset --hard <base>`, then
    send a fresh worker.
  - **Stop**: "Send the units done so far to synthesis".

## 7. Synthesis

Synthesis runs once at the end of every run (`<final>` = `yes`), and in
waves mode also between waves (`<final>` = `no`, section 5). Each one is a
fresh agent: a wave can take longer than its 1-hour cache.

For the final one, read the ticket IDs from `<slug>-tickets.json`, and if the
ticket call is still running, wait for its notification. Then read
`<skill>/references/synthesis.md`, fill in its placeholders, and launch one
foreground `Agent` with that prompt. Synthesis gets two lists:

- **Units in the run so far:** every ready unit of every wave up to now, in
  dependency order, with its agreed interfaces from the intake, its worker's
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
- `<state>` is `<runs>/<slug>-synthesis.md`,
  `<tickets json>` is `<runs>/<slug>-tickets.json`, and `<rules file>` is
  `review-classes.md` in the parent folder of `<runs>`.
- `<intake>` is `<runs>/<slug>-intake.md` and `<iface>` is
  `<runs>/<slug>-interface-changes.md` (it may not exist). Synthesis reads the
  run goal and the acceptance criteria from the intake, runs its verification
  scenarios (`intake.py scenarios`) on the merged result and writes
  `<runs>/<slug>-scenarios.json`.
- `<ci ignore>` and `<ci timeout>`: from the profile's `ci` (see
  `references/synthesis.md`).
- `<review rounds>`: the rounds of the scored branch review (synthesis
  section 3d), 2 by default and at most 2. When the user names a number ("PR
  review set at 1"), write `Review rounds: <n>` into the run file at the
  intake and pass that number; a number above 2 is passed as 2 (say so in one
  line), and `review-pack.py pack` refuses a third round.

Then:

- **`RESULT: wave-green`:** start the next wave.
- **`RESULT: pr`:** go to step 8.
- **`RESULT: stuck`:** show the failure in your message: what failed, the
  error lines and what synthesis already tried. Ask the decision synthesis
  named as one short question, its choices as the options, and add **Stop**,
  "Keep the branch as it is; no PR". Several decisions are several questions.
  Note the time with `date +%s`. When the user answers:
  - within 50 minutes, continue the same synthesis agent with `SendMessage`.
    Its 1-hour cache is still warm.
  - later, its cache has expired, and continuing it would re-write its whole
    context. Launch a fresh synthesis agent instead, with the same prompt plus
    one line at the top: `Resume: read <state> first. The merges and the
    repairs it lists are committed on <ibranch>. The user's decision: <answer>.
    Go on from section 3.`
- **No `RESULT:` line:** synthesis ran out of turns. Treat it as stuck. If
  `<state>` doesn't exist, the resume line says `Resume: read
  git log --oneline origin/<base>..<ibranch> first; the merges and repairs it
  shows are committed.` instead.

## 8. Report

First record the run's data, in one Bash call:

```bash
python "<skill>/scripts/cost-report.py" --record <slug> --root "<root>"
```

It finds this session's log, saves the full report to
`<runs>/<slug>-cost.txt`, and appends one row to
`~/.claude/bmad-batch-runs/runs.jsonl`: cost, time, review and repair counts,
cache and turn-budget checks. It also writes the run journal,
`<runs>/<slug>-journal.md` (scripts/journal.py): every
failed tool call, every command that failed inside `heavy.sh` or `bg.sh`,
units blocked or failing the gate, agents that ended without their reply or
were resumed, classifier denials, synthesis repairs, and the `SKILL ISSUES`
the agents reported, failures first. Every run is measured this way, so never
skip it. Record the run the same way when it ends early (cancelled, stopped at
the gate, or left stuck). If the call fails, say so in the summary and go on.

Then end with a short summary:

- the PR link
- the tickets, with links: the story, each Task and which unit it belongs to,
  what was created and what was reused, and whether ADO or GitHub will close
  them when the PR completes
- the units that were merged, and any that were left out. A left-out unit's
  Task stays open and isn't linked.
- the repairs synthesis made, and its `REVIEW:` line: what the fresh review
  of those repairs found, fixed and left open
- **Review:** the `SCORE:` line (the scored branch review, synthesis section
  3d), the decision (100 is Approve, anything lower is Request Changes), and
  the findings table from `<runs>/<slug>-review.txt`, with the reason for each
  finding left open. This is the review report for the PR (one per PR in a
  split run); don't run a second review of the same code.
- the verification results, and the `CI:` line: how the PR's pipelines
  ended on the server, and any fix synthesis pushed for them
- **Checks to add:** each `RULES:` proposal from synthesis, with the past
  findings it would have caught. Offer to add the ones the user picks, as a
  separate small change after this PR.
- anything the user should look at first
- the `recorded` line from the call above: cost, time, and whether the 1-hour
  cache took effect and any agent ran out of turns
- **What happened:** the `journal:` line from the call above, then the
  journal's problems, failures first: one line each with who, what and how
  often, at most about ten, and the journal's path for the rest. Include
  every skill issue. Say plainly when nothing went wrong.

Don't remove the worktrees. Offer to clean them up after the PR merges.

### Reviews after the PR opens

The code was reviewed on the integration branch before the PR opened
(synthesis section 3d). Don't review it again unless the user asks for it, or
a fix changes the code after that review. Then keep the review small. In
review-workspace-pr-size, four rounds of per-PR reviews cost $100 of the
run's $318:

- **Review one PR per agent,** each launched by you, the main session. Never
  have a lead reviewer start one reviewer per PR: both levels read the same
  diffs.
- **Give each reviewer a pack, not a patch:**
  `review-pack.py pack "<iwt>" <the PR's target> --out <dir>` on the PR
  branch. The reviewer reads the group file, never a whole diff.
- **Only re-review what changed.** From round 2, pack only
  `<the reviewed head>..<the new head>`, and give the reviewer that PR's
  earlier findings to check. Skip a PR that scored 100, and one whose
  remaining findings are all in its description.
- **Fix description findings yourself.** Fix a wrong AC claim, a stack note or
  a size line in the description with the PR tool (or
  `split-prs.py describe` again), without another review.
- **Work from a file, not from replies.** Keep each PR's findings and score
  in `<runs>/<slug>-review.txt` (`pr:<unit>` locations for descriptions). Ask
  each reviewer for findings lines only, so its reply stays small.

### Safety guard

The plugin's `hooks/hooks.json` runs `scripts/guard.py` before every shell
command and file edit. For the run's agents (and any agent working inside a
batch worktree) it denies what must stay with the user: changing the main
checkout, force pushes or pushes to a protected branch, merging or
completing a PR, starting or approving pipelines and deploys, infrastructure
changes, dropping a non-test database. Your own session is not affected. A
copied (non-plugin) install has no guard. An agent that reports a guard
denial needs the user's decision: pass it on.

## 9. After the merge: the deploys

The deploy pipelines run on `<base>` after someone merges the PR, so they are
checked when the user says the PR is merged or asks how the deploys went
(`/loop 15m check the deploys of <slug>` keeps checking). In one Bash call:

```bash
python "<skill>/scripts/ci-status.py" deploy <pr id> --root "<iwt>" --wait 240 [--ignore "<ci ignore>"]
```

- `CI none`: the PR is not merged yet, or nothing ran for its merge commit.
  Say so.
- `CI pending`: call again, for up to an hour, or tell the user to ask later.
- `CI approval`: name the pipeline that waits for approval; approving it is
  the user's.
- `CI green`: report it.
- `CI red`: report it without a question: the failing pipeline, the stage
  and the error lines from its `LOG` file, then this line: `Reply **fix the
  deploy** and a fresh agent fixes the cause on a new branch and opens a
  follow-up PR; otherwise nothing changes.`

When the user replies that they want it fixed: prepare a worktree from the latest base,
`git -C "<root>" fetch origin <base> --quiet` then
`bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<slug>-deploy-fix" "<run branch>-deploy-fix" --base origin/<base>`
(one Bash call, 10-minute timeout), and launch a fresh synthesis agent
(section 7) on it with Final `yes`, New branch `no`, nothing to merge, and
this line at the top of its prompt:
`Deploy fix: the deploy pipeline <name> failed after PR <pr id> merged. Its log is <LOG file>. There is nothing to merge: read AGENTS.md and the log, find the cause (code, infrastructure code or the pipeline definition), fix it on this branch, then go on from section 3. The PR title starts with "fix(deploy):" and links the same tickets.`
Synthesis verifies, reviews its own change, opens the follow-up PR and
watches its checks. It never re-runs the deploy: tell the user to run it
again once the follow-up PR is merged. Two fix PRs at most per run; after
that, hand the failure to the user.

### Enhance the skill

This step is for the skill's maintainer: run it only when
`~/.claude/bmad-batch-runs/install/installs.json` exists on this machine
(it lists the projects a shipped fix is installed into). Anyone else skips
it; their journal still records the run.

Right after the summary, unless the journal line says no failures or issues
were recorded, dispatch `<agents>bmad-batch-enhancer` in the background with the
prompt in `<skill>/references/enhancer.md`. It drafts at most one tested fix
to the skill on a branch of its source clone and publishes nothing. When it
replies, send a `PushNotification` ("skill fix drafted: <one line>") and tell
the user in a few lines:

- `ENHANCE: <branch>`: the change, its evidence and the test result, then
  "Reply **ship it** to publish it and install it in every project", followed
  by the exact command with absolute paths, so a later session without this
  skill loaded can run it:
  `bash "<home>/.claude/bmad-batch-runs/src/sip-of-cafe/plugins/bmad-batch-build/skills/bmad-batch-build/scripts/ship-enhancement.sh" <branch>`.
  Also name what it queued and anything under `NOT SKILL` worth the user's
  attention.
- `ENHANCE: none` or `failed`: one line saying so, plus `NOT SKILL` items.

When the user says "ship it", run that command and report its output. Run
the source clone's copy, not `<skill>`'s: it is the version being shipped.
It rebases the branch onto the latest source, re-runs the self-test, pushes,
and installs it: on a machine with the plugin installed it updates the
plugin (`claude plugin update`), otherwise it copies the skill into every
project in `~/.claude/bmad-batch-runs/install/installs.json`. Tell the user
to start a new session to load it.
Unshipped drafts are the `enhance/*` branches in
`~/.claude/bmad-batch-runs/src/sip-of-cafe`; the decisions are in
`~/.claude/bmad-batch-runs/enhance-log.md`.

`python "<skill>/scripts/runs-summary.py"` compares all recorded runs by skill
version; with `--problems` it lists the problems that recur across runs.

To clean up, run this for each worktree:

```bash
bash "<skill>/scripts/remove-worktree.sh" "<root>" "<name>" "<branch>"
```

It drops the worktree's test database too. Once every worktree is gone, drop
the run's template with `bash "<skill>/scripts/db-template.sh" "<root>" "<slug>" --drop`.

When wave 1 ran in parallel, the first unit's worktree is on the run's branch, so pass
`<run branch>` for it, then delete that unit's own branch with
`git -C "<root>" branch -d <its branch>`.

The script unlinks the npm junctions first, because a plain
`git worktree remove` follows them on Windows. Run it from `<root>`. A shell
whose working directory is inside the worktree makes the removal fail with
"busy".
