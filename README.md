# sip-of-cafe

Claude Code skills.

## bmad-batch-build

Turns a meeting transcript or a list of work items into one reviewed pull
request. It splits the material into units and shows you the plan. Once you
approve it, the skill runs one `bmad-build-auto` worker per unit in its own git
worktree, creates the Azure DevOps tickets, and has a synthesis agent merge
everything into one branch, get it green and open one PR.

It adapts to your repository. On the first run it works out how your project
installs, builds and tests, shows you those commands with the plan, and
remembers them once you confirm.

Formerly `bmad-parallel-build` (renamed in 0.6.0). If you still have the old
plugin, uninstall `bmad-parallel-build@sip-of-cafe` and install
`bmad-batch-build@sip-of-cafe`. You can delete old `bmad-pb-*` agent copies in
`.claude/agents/`.

### Install

As a Claude Code plugin (recommended; the agents come with it):

```bash
claude plugin marketplace add NgLaTungGwen/sip-of-cafe
claude plugin install bmad-batch-build@sip-of-cafe
```

To update:

```bash
claude plugin marketplace update sip-of-cafe
claude plugin update bmad-batch-build@sip-of-cafe
```

then restart Claude Code.

With npx (copies the skill folder into the project, or with `-g` into your
user folder):

```bash
npx skills add NgLaTungGwen/sip-of-cafe
npx skills add NgLaTungGwen/sip-of-cafe -g
```

Nothing else to set up. The first step of every run, `scripts/setup.sh`,
resolves the rest on its own:
- it installs BMAD when the project has none, and copies the run's agents
  into `<repo>/.claude/agents/`;
- it keeps `.claude/worktrees/` out of git status;
- it finds Python (`python3` or `python`) and the default branch, and
  installs `uv` when it is missing (workers run BMAD's scripts with it);
- it decides where tickets and the PR go: Azure DevOps tickets only with an
  ADO remote and a signed-in `az`, and the PR through `az`, `gh` or, without
  either, a pushed branch.

It stops only for what it can't install itself: Git, Python 3.9+, or
Node.js when BMAD still has to be installed. Optional: `az` (signed in) for
Azure DevOps tickets and PRs, `gh` for GitHub PRs, Docker for projects whose
tests need a database. Claude Code loads a new `.claude/agents` folder at
the next session start, so the first run after an npx install uses
general-purpose agents with the same instructions instead of stopping; it
costs a little more (no 1-hour cache).

### Use

Open Claude Code in the repository and run:

```
/bmad-batch-build <paste the transcript or the work items>
```

(With the plugin install the command is `/bmad-batch-build:bmad-batch-build`.)

The skill checks its agents first and stops early if they are missing. It
maps the code, splits the work into units, and shows one approval table: the
units, their sizes and dependencies, the time estimate for each mode, the
tickets, and the project commands on a first run. After you approve, it runs
to the end without asking again, unless a unit fails the gate or synthesis
gets stuck. At the end you get the PR link, the tickets, the verification
results, and a "What happened" section that lists anything that failed.

### Adapting to your project

The commands the skill runs live in a project profile,
`<repo>/.claude/bmad-batch-project.json`:

```json
{
  "base": "main",
  "install":  [{"dir": "app", "cmd": "npm ci --no-audit --no-fund --loglevel=error"}],
  "checks":   [{"name": "app-typecheck", "dir": "app", "cmd": "npm run typecheck"},
               {"name": "app-test", "dir": "app", "cmd": "npm run test"}],
  "suite":    {"dir": "app", "cmd": "npm run test"},
  "targeted": {"dir": "app", "test": "npx vitest run <files> --reporter=dot",
               "typecheck": "npm run typecheck"},
  "after_merge": [],
  "database": null
}
```

- **First run:** `scripts/profile.py detect` drafts the profile from your
  files:
  - the base branch, from `origin/HEAD`;
  - one install per folder with a lockfile (npm, bun, pnpm or yarn);
  - build, typecheck, lint and test checks from the package scripts;
  - the full test suite, and the targeted test and typecheck commands. The
    test runner is found in workspace members too.
  - whether `.env` names a database.

  Python, Go, Rust and .NET projects get basic defaults. The orchestrator then
  corrects the draft from your AGENTS.md, CLAUDE.md and README: for example, a
  rule like "use bun, never npm" changes every command. You confirm the result
  in the approval table.
- **Later runs:** the confirmed profile is reused. If a lockfile or a package
  script changes, the profile is marked stale and shown to you again.
- **How the commands are used:**
  - **Workers:** install with `install`, and run `targeted` after every change
    and `suite` once per unit.
  - **Synthesis:** runs every `checks` entry at once and `after_merge` after
    merging, for example code generation. Its changes are committed.
- **No tests:** a project whose rules forbid running tests (no `suite`, no
  `targeted.test`) gets instructions to match. Workers skip the full suite, the
  coder runs only typechecks, and test-based workflow steps are met by tracing
  each requirement to the code.
- **Editing:** edit the file by hand whenever you like. Every command is a plain
  string run from its `dir`, so any stack works.

Without a profile file, the scripts fall back to their original behaviour,
which was built for a Node monorepo:
- a root `npm ci`;
- `packages/contracts`, and `apps/` with bun;
- a per-worktree Postgres test database on `127.0.0.1:55432`.

A repository with no lockfile at its root still installs and runs checks per
package folder.

### Lane, parallel or waves, chosen automatically

At the split, every unit gets a size and a `depends_on` list: the units whose
code it needs to build or test, each with a reason. `scripts/plan-waves.py`
sorts the units into waves. A wave holds the units whose dependencies are all
in earlier waves, and each wave runs whichever way is faster for it:

- **lane:** units take turns in one worktree on the run's branch. Nothing is
  installed twice and nothing is merged.
- **parallel:** each unit gets its own worktree, branched from the run's
  branch as the earlier waves left it. Synthesis merges and verifies the wave
  before the next one starts.

Independent units usually run in parallel, and a chain or a few small units
run as a lane; anything else becomes waves. For example, a chart kit and new
contract fields that three other units import make two parallel waves,
{1, 2} then {3, 4, 5}: about 450 minutes, against 670 as a lane.

The approval table shows each estimate. Unit times calibrate from your recorded
runs once a size has five measurements; until then they're S 50, M 100,
L 160 minutes.

No parallel wave has more than 4 workers: 3 when one other run is active on
the machine, and 2 when more are. A larger group of independent units becomes
several waves in a row. Each worker brings a coder and up to four reviewers,
and Claude Code allows 20 subagents at once per session. Larger waves got
launches refused and stalled the machine's build queue.

Branches follow your repository's convention when its AGENTS.md or CLAUDE.md
states one, and `<type>/<slug>` otherwise.

### Models and agents

| Step | Model |
|---|---|
| Split and orchestration | your session's model |
| Code mapping during the split | Sonnet (`Explore`) |
| Worktree setup, install, build | scripts, no model |
| Worker: spec, verification, review triage | Opus (`bmad-batch-worker`) |
| Review layers | the worker's model, as bmad-build-auto requires |
| Code, from the spec | Haiku (`haiku-implementer`) |
| Synthesis: merge, fix, open the PR | Opus (`bmad-batch-synthesis`) |

- **Why Opus for the worker:** Sonnet workers skipped the review step in 31 of
  47 measured units, against 5 of 67 on Opus.
- **Cache:** the worker and synthesis agents keep a 1-hour prompt cache, so
  their context survives long installs and test suites. This needs Claude Code
  v2.1.248 or later.
- **Turn budgets:** each agent has a `maxTurns` budget (worker 300, coder 300,
  synthesis 400), so a stuck agent stops instead of running up the bill.
- **Agent names:** with the plugin, the agents are named
  `bmad-batch-build:<agent>`; with a copied install they are plain
  `<agent>`. The skill detects which one applies. To customise an agent, edit
  its copy in `.claude/agents/`.

### Speed: keeping waits off the critical path

- **Installs:** each worker starts its install in the background and plans
  while it runs.
- **Full test suite:** the worker starts it in the background as soon as the
  code is written, and runs the review alongside it.
- **One owner per check:**
  - the Haiku coder runs the targeted tests and typechecks;
  - the worker runs only the full suite, once;
  - only synthesis builds.
- **Bounded waits:** Claude Code stops any single command after 10 minutes. So
  everything that can take longer, such as a suite, a build or the final
  verify, runs through `bg.sh run` in the background. Agents wait with one
  `bg.sh wait` per call, which returns within 4 minutes, never with loops,
  `timeout` or `sleep`.
- **Review fixes:** each fix round goes to a fresh coder in the foreground,
  so a worker never hands back while fixes are still landing.
- **No extra install for synthesis:** synthesis builds the run's branch in the
  first unit's worktree.
- **Scripts do the mechanical work:**
  - `merge-units.sh` handles merges, migration-number checks and reinstalls;
  - `ado_tickets.py` creates and links the tickets.

### Many runs on one machine

`scripts/heavy.sh` queues every heavy command across all sessions on the
machine: installs, builds, full test suites, typechecks and the final verify.
A command starts only when a slot and enough free RAM are available. Vitest is
capped at a quarter of the CPUs per suite.

| Variable | Default | Meaning |
|---|---|---|
| `BMAD_HEAVY_SLOTS` | CPU count | Commands allowed at once |
| `BMAD_HEAVY_FLOOR_MB` | 2048 | RAM to keep free |
| `BMAD_HEAVY_MAX_WAIT` | 1500 s | Real time a command waits before it starts anyway |
| `BMAD_HEAVY_TIMEOUT` | 2700 s | A command running longer is stopped, so a hung install can't block the machine |
| `BMAD_HEAVY_DIR` | `~/.cache/bmad-heavy` | Where the queue is recorded |

Each command reserves memory by kind unless `--mem` is given: about 1 GB for a
typecheck or lint, 1.5 GB for an install, and 2.5 GB for anything else.
Waiting commands check the queue every 10–14 seconds.

An optional hook routes heavy commands that agents type without `heavy.sh`
through the queue. The skill never turns it on. To enable it, add this to
`.claude/settings.local.json`, using the path to your install:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "^Bash$",
        "hooks": [{ "type": "command", "command": "python \"<skill dir>/scripts/heavy_hook.py\"" }] }
    ]
  }
}
```

It only rewrites `cd <worktree> && <install | build | full test | typecheck>`.
Any other command goes through your normal permission rules.

### What each run records

- **Gate:** `check-unit.sh` rejects a unit whose spec shows the workflow
  didn't run: no baseline revision, no intent contract, or no review pass.
  You then choose to re-dispatch it, merge it marked unreviewed, or leave it
  out.
- **Preflight:** before anything is created, `check-test-env.sh` runs one
  trivial test through the queue. A broken test setup stops the run in
  seconds.
- **Cost and time:** `cost-report.py --record` writes the run's cost, time,
  cache use and review coverage. It adds one row per run to
  `~/.claude/bmad-batch-runs/runs.jsonl`, across all projects and with the
  machine's CPUs and RAM. It also sweeps up any run that wasn't recorded, and
  keeps gzipped logs, because Claude Code deletes transcripts after 30 days.
- **Journal:** `<repo>/.claude/worktrees/_runs/<slug>-journal.md` records what
  happened, failures first:
  - units blocked at the gate;
  - agents that ran out of turns or had to be resumed;
  - commands that failed, including inside the queue, where an agent might
    never see the failure;
  - classifier denials, blocked calls and shell errors;
  - synthesis repairs;
  - the problems the agents report about the skill itself.

  The final summary shows it as "What happened".
- **Comparisons:** `runs-summary.py` compares runs by skill version, and
  `runs-summary.py --problems` lists the problems that recur across runs.
- **Sharing metrics:** nothing leaves your machine on its own. To share
  your runs with the skill's maintainer, run
  `python <skill>/scripts/metrics.py export`. It writes one JSON file of
  numbers: cost, time, tokens, workers, review and failure counts, skill
  version, CPUs and RAM, and how long each heavy command took. It holds no
  host name, paths, commands, branch names or PR links, and project names are
  hashed. `--include-text` adds the agents' reports about the skill, with
  paths, branches and project names replaced. Read the file, then send it.
  The maintainer runs `metrics.py import <file>...`, which merges every
  machine into `~/.claude/bmad-batch-runs/metrics/`; the fields are listed in
  `references/metrics.md`.
- **Dashboard:** `/bmad-batch-dashboard` (`/bmad-batch-build:bmad-batch-dashboard`
  from the plugin) records any runs not yet recorded, refreshes the dataset
  and opens one page of d3 charts: cost per run by role, wall time,
  problems from the journals, cost and time by skill version, heavy command
  times and failures, unit times by size, context per turn, and a table of
  runs. Filters for date range, project, machine and skill version. Files
  from other machines are added with
  `dashboard.py --import <file>...`. The page is
  `~/.claude/bmad-batch-runs/dashboard.html`; it holds its own data and needs
  the network only for d3. `install-skill.py` also copies the dashboard to
  `~/.claude/skills`, so it works in every project.
- **System map:** when `docs/architecture/system-map.md` exists, the split
  reads it, and `check-map.py` first checks that every path it names still
  exists.

### Requirements

- Git, bash (Git Bash on Windows), Python 3 and `uv`.
- BMAD: the first run installs `bmad-method` 6.12.0 into the project if it
  isn't there. An existing install is left alone. Set `BMAD_VERSION` to pin
  another release.
- Tickets and PR:
  - Tickets need the Azure CLI with the `azure-devops` extension and
    `az devops configure` defaults. If ticket creation fails, the run carries
    on and reports it.
  - The PR opens on Azure DevOps (`az repos pr`) or GitHub (`gh pr`).

### Troubleshooting

- **"batch-build did not launch" or "agents are missing":** versions before
  0.12.5 stopped when the session hadn't loaded the run's agents (every
  first run after an npx install). Update; the run now goes ahead with
  general-purpose agents and the next session loads the real ones. Plugin
  versions before 0.10.7 didn't ship their agents at all.
- **A `SETUP blocked` line:** install what it names (Python 3.9+, or Node.js
  20+ for the BMAD install) and run the skill again.
- **No tickets:** they need an Azure DevOps remote and `az login`; setup says
  `TICKETS off (...)` with the reason, and the run carries on without them.
- **`npx skills add` finds no skills:** you have a version between 0.9.0 and
  0.10.3, whose SKILL.md header wasn't valid YAML. Update.
- **Commands wait for a long time:** check `~/.cache/bmad-heavy`.
  - `run.<pid>` files are commands holding a slot; `wait.*` files are queued
    commands.
  - A hung command now stops at `BMAD_HEAVY_TIMEOUT`.
- **A command in the profile is wrong:** edit
  `.claude/bmad-batch-project.json`, or delete it to have it detected again on
  the next run.

### For the maintainer: the improvement loop

This part only runs on a machine with `~/.claude/bmad-batch-runs/install/installs.json`.
Everyone else skips it.

1. **Draft:** after each run, the `bmad-batch-enhancer` agent reads the journal
   and the recurring problems. It drafts at most one fix to the skill in its own
   worktree of the source clone (`~/.claude/bmad-batch-runs/src/sip-of-cafe`).
   That fix must pass `scripts/self-test.sh`, which runs:
   - a syntax check of every script;
   - strict YAML parsing of SKILL.md and the agents;
   - the unit and layout tests;
   - a real Vitest run through the queue.
2. **Ship:** you reply "ship it". `scripts/ship-enhancement.sh <branch>` then:
   - takes a lock;
   - squashes the draft onto the latest `main` and sets the next version;
   - re-runs the self-test;
   - pushes;
   - installs the new version with `install-skill.py`.
3. **Install:** `install-skill.py` installs into every project in
   `installs.json`. It skips a project where a session ran the skill in the
   last 15 minutes; `--force` overrides that. Projects marked `"legacy": true`
   get no profile.

The decisions are logged in `~/.claude/bmad-batch-runs/enhance-log.md`.

### Recent changes

- **0.12.6:** `uv` is installed when missing; a new small project works on
  the first run: a `package.json`
  without a lockfile is detected (targeted tests run as `npm test -- <files>`),
  the profile is corrected with `profile.py set` instead of an edit Claude
  Code would ask about, and a repository without a remote branches from its
  local base and skips fetch and push.
- **0.12.5:**
  - `setup.sh` sets up a new project on its own; the run no longer stops
    when its agents aren't loaded yet (fallback to general-purpose agents);
  - commands are bash everywhere; a Windows session with only PowerShell
    runs them as `bash -c '...'`;
  - tickets and the PR follow the remote: Azure DevOps (organization and
    project read from the remote), GitHub, or a pushed branch.
- **0.12.3:** the `bmad-batch-dashboard` skill. Viewing it no longer counts
  as a run when the installer checks for active runs, and runs started as a
  plugin skill (`/bmad-batch-build:bmad-batch-build`) are now recorded.
- **0.12.2:** `metrics.py export` and `import`: an anonymised metrics file
  to share, and one dataset of every machine's runs for a dashboard.
- **0.12.1:** the Haiku coder reads a file before editing it.
- **0.12.0:**
  - parallel waves are capped at 4 workers, and fewer when other runs are
    active;
  - review fixes go to a fresh foreground coder;
  - long commands always run in the background through `bg.sh`;
  - projects without tests are handled;
  - workarounds for BMAD's missing `baseline_revision`, a gitignored
    `_bmad-output/` and the `:!_bmad-output` pathspec;
  - `verify.sh --no-test`;
  - queue memory estimates per kind of command;
  - exact skill versions are recorded per run;
  - a Windows-style project path (`D:\Projects\...`) no longer breaks the
    workers' pinned settings.
- **0.11.1:** workers may build when the intent or AGENTS.md requires it.
- **0.11.0:**
  - project profile: detected, refined from your docs, confirmed once, then
    used by every script;
  - installs skip projects with an active run.
- **0.10.7:**
  - the plugin ships its agents, and the skill checks them before reading the
    input;
  - installs and checks follow the lockfiles;
  - the queue's wait limit counts real time, and commands have a time limit;
  - branch names follow the repository;
  - the PR step works without a `create-pr` skill.
- **0.10.4–0.10.6:**
  - the SKILL.md header is valid YAML again, so `npx skills add` works;
  - drafts work in their own worktrees;
  - targeted checks no longer assume an `api/` folder.
- **0.10.0:** run journal and the improvement loop.
- **0.9.x:** dependency waves; Vitest worker limits fixed; preflight test
  check.
