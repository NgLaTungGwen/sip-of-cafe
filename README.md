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
`.claude/agents/`; edit those copies to change them.

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
first-review findings per unit, and synthesis repair commits.

### Requirements

- BMAD comes with it: on first run the skill installs `bmad-method` 6.12.0
  into the project (`_bmad/` and the `bmad-*` skills) if it isn't there yet.
  An existing install is left alone; set `BMAD_VERSION` to pin another release.
- Git, bash, Python and `uv`.
- The Azure CLI with the `azure-devops` extension, with `az devops configure`
  defaults set for the organisation and project.
- The worktree scripts assume a Node monorepo with a Postgres test database
  named in `.env` (`DATABASE_URL`).
