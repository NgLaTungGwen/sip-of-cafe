# sip-of-cafe

Claude Code skills.

## bmad-batch-build

Formerly `bmad-parallel-build`, renamed in 0.6.0 because runs now go in
parallel or in a lane. If you installed the old plugin, uninstall
`bmad-parallel-build@sip-of-cafe` and install `bmad-batch-build@sip-of-cafe`.
Old agent copies `bmad-pb-worker` and `bmad-pb-synthesis` in a project's
`.claude/agents/` can be deleted.

Orchestrates a parallel BMAD build from a meeting transcript or pasted work
items. It splits the material into independent units and, after you approve the
split, runs one `bmad-build-auto` worker per unit in its own git worktree. It
creates the Azure DevOps tickets, then a synthesis agent merges the units into
one branch, gets it green and opens one PR.

### Install

With npx (any agent the Skills CLI supports):

```bash
npx skills add NgLaTungGwen/sip-of-cafe            # into this project
npx skills add NgLaTungGwen/sip-of-cafe -g         # for every project
```

As a Claude Code plugin:

```bash
claude plugin marketplace add NgLaTungGwen/sip-of-cafe
claude plugin install bmad-batch-build@sip-of-cafe
```

### Models

Each step runs on the model it needs:

| Step | Model |
|---|---|
| Split and orchestration | your session's model |
| Code mapping during the split | Sonnet (`Explore`) |
| Worktree setup, install, build | scripts, no model |
| Worker: spec, verification, review triage | Opus (`bmad-batch-worker` agent) |
| Review layers | the worker's model, as bmad-build-auto requires |
| Code, from the spec | Haiku (`haiku-implementer` agent) |
| Synthesis: merge, fix, open the PR | Opus (`bmad-batch-synthesis` agent) |

The worker runs on Opus because Sonnet workers skipped the review step in 31 of
47 measured units, against 5 of 67 on Opus. Opus and Sonnet cost the same per
cache read, which is most of a worker's tokens.

The worker and synthesis agents set `experimental: { cacheTtl: 1h }`, so their
context survives the long waits on installs and test suites instead of being
re-written after five minutes. That needs Claude Code v2.1.248 or later, and it
does not apply while a subscription draws usage credits. The
`haiku-implementer` agent sets `autoCompactWindow: 100000`, so the Haiku coder
compacts at about 67k tokens; that needs v2.1.296 or later and auto-compact on
(`"autoCompactEnabled": true`, the default).

On first run the skill copies the three agents into the project's
`.claude/agents/`; edit those copies to change them. A copy that differs from
the shipped one is kept and reported, so after upgrading the plugin, replace
the copies you haven't customised.

### Lane or parallel, chosen automatically

After the split, the orchestrator estimates each unit's worker time from its
size (S, M or L), and compares two estimates:

- **lane:** the units take turns in one worktree on the run's branch, so
  nothing is installed twice and nothing is merged;
- **parallel:** one worktree per unit, all at once, then synthesis merges.

One unit always runs as a lane, and five or more always run in parallel.
Between those, the faster estimate wins, with ties going to the lane; a busy
machine raises the lane's margin. The approval table shows the choice and both
estimates, and you can switch.

### Where the time and the cache go

Most of a run is waiting on installs and test suites, and an agent that waits
past its cache TTL pays to re-write its whole context. So the flow keeps long
waits off the critical path and out of large contexts:

- A worker checks out its worktree, starts the install in the background and
  plans while it runs. The Haiku coder, whose context is still tiny, waits for
  the install before its first command.
- The worker starts the full DB suite in the background as soon as the coder
  returns, and runs the review alongside it.
- There is no separate integration worktree: synthesis builds the run's
  branch in the first unit's worktree, so one install fewer competes with the
  workers.
- `scripts/merge-units.sh` does the merges, the migration-number check, the
  contracts sync and any reinstall a lockfile needs; `ado_tickets.py
  --link-pr` links the PR in one call. Synthesis spends its Opus turns on
  conflicts, interface drift and failures.
- Workers report the interfaces they actually built, so the next lane unit
  and synthesis check those instead of re-reading the code.
- The orchestrator never reads product code (an `Explore` agent maps it),
  because its context is re-written each time a worker returns.
- A stuck synthesis writes its state to a file. If the user answers after its
  1-hour cache has expired, a fresh agent resumes from that file.

`bg.sh run|wait` carries the background commands; their logs sit in the
worktree's git dir, so the tree stays clean for the gate. Every wait returns
within 4 minutes, under the default 5-minute cache TTL that the coder and the
reviewers use, so a wait never expires their cache.

Each check has one owner. The Haiku coder runs the targeted tests (with a
compact reporter) and the typechecks of what it touched, after every pass. The
worker doesn't repeat them; it runs only the full suite, once. Only synthesis
builds.

Each agent file sets a `maxTurns` budget: worker 300, coder 300, synthesis
400, about twice the longest run that finished. A stuck agent stops there
instead of running up the bill. Claude Code has no per-agent cost cap, so the
turn budget is the limit.

### Many runs on one machine

`scripts/heavy.sh` admits every heavy command (installs, builds, full suites,
typechecks, the synthesis verify) only while the machine has room. It counts
the commands running across all sessions and checks free RAM, so parallel runs
queue instead of crashing each other. Vitest is capped at 3 workers per suite.
CPU and RAM can still run high; that is expected.

The settings are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `BMAD_HEAVY_SLOTS` | CPU count | Commands allowed at once |
| `BMAD_HEAVY_FLOOR_MB` | 2048 | RAM to keep free |
| `BMAD_HEAVY_MAX_WAIT` | 1500 s | Wait before a command starts anyway |
| `BMAD_HEAVY_DIR` | `~/.cache/bmad-heavy` | Where running commands are recorded |

Free RAM is read from `/proc/meminfo` (Git Bash, Linux, WSL). Without that file
only the slot count applies. Waiting commands start in arrival order.

The agents are told to use `heavy.sh`. To make sure a command they type
without it still queues, enable the optional hook yourself; the skill never
turns it on. Add this to the project's `.claude/settings.local.json`, with
the path to your install:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^Bash$",
        "hooks": [
          { "type": "command", "command": "python \"<skill dir>/scripts/heavy_hook.py\"" }
        ]
      }
    ]
  }
}
```

The hook only touches commands of the form
`cd <dir under .claude/worktrees/> && <install | build | full test | typecheck>`.
It rewrites such a command to go through `heavy.sh`, and approves that one
rewritten call. A command chained with anything else, run outside the
worktrees, or a targeted test run is left alone, and goes through your normal
permission rules.

### Gate and measurement

Before synthesis, `scripts/check-unit.sh` rejects a unit whose spec shows that
the workflow did not run: no `baseline_revision`, no `<intent-contract>`, or no
review pass. You then choose whether to re-dispatch it, merge it marked
unreviewed, or leave it out.

`scripts/cost-report.py <session .jsonl>` reports a finished run: cost and
tokens per role, time per role, tool waits, cache rewrites after idle gaps,
first-review findings per unit, and synthesis repair commits. A gap counts as
an idle rewrite when it beats the TTL the write used: 5 minutes, or 1 hour for
the worker and synthesis agents. That figure is an upper bound, because a write
after a gap also holds new content. The report gives the median cost per run
and the mean without outliers (over twice the median cost or time), and counts
parallel tool calls once ("union"). It splits uncached input from output. It
also explains the "rest" time that is not workers or synthesis, and flags any
over an hour.

Every run records itself: at the end the orchestrator runs
`cost-report.py --record <slug>`. That saves the report next to the run file
and appends one row per run to `~/.claude/bmad-batch-runs/runs.jsonl`, across
all projects. `scripts/runs-summary.py [--since YYYY-MM-DD]` compares the
recorded runs by skill version (median cost and time, review coverage,
findings, repairs, 1-hour cache, turn budgets). `cost-report.py --collect`
sweeps every session on the machine that ran the skill and records any without
a row: missed records, crashed runs and runs from before recording existed.
Each `--record` runs it too. Collected runs keep a gzipped copy of their logs
under `~/.claude/bmad-batch-runs/sessions/`, because Claude Code deletes
transcripts after 30 days by default. `--collect --refresh` rebuilds every row
after a metric change. Rows also measure code discovery per role (turns,
context per turn, Read, Grep/Glob and small Bash calls), and Explore counts as
its own role.

When the project has `docs/architecture/system-map.md` (modules, ownership,
data flow, and a glossary from business terms to paths), the split reads it
before sending Explore, and `scripts/check-map.py <root> [--strict]` first
checks that every path it names still exists.

The machine limits are not hardcoded. heavy.sh gives one slot per CPU and
checks free RAM, Vitest gets a quarter of the CPUs per suite (at least 2), and
the "machine is busy" test for the mode choice is a third of the CPUs. The
worker-time estimates (S/M/L) were measured on a 12-CPU, 32 GB machine; every
recorded run carries its machine's CPUs and RAM, so they can be recalibrated
per machine.

### Requirements

- BMAD comes with it: on first run the skill installs `bmad-method` 6.12.0
  into the project (`_bmad/` and the `bmad-*` skills) if it isn't there yet.
  An existing install is left alone; set `BMAD_VERSION` to pin another release.
- Git, bash, Python and `uv`.
- The Azure CLI with the `azure-devops` extension, with `az devops configure`
  defaults set for the organisation and project.
- The worktree scripts assume a Node monorepo with a Postgres test database
  named in `.env` (`DATABASE_URL`).
