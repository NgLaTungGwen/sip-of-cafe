# sip-of-cafe

Claude Code skills.

## bmad-parallel-build

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
claude plugin install bmad-parallel-build@sip-of-cafe
```

### Models

Each step runs on the model it needs:

| Step | Model |
|---|---|
| Split and orchestration | your session's model |
| Code mapping during the split | Sonnet (`Explore`) |
| Worktree setup, install, build | scripts, no model |
| Worker: spec, verification, review triage | Opus (`bmad-pb-worker` agent) |
| Review layers | the worker's model, as bmad-build-auto requires |
| Code, from the spec | Haiku (`haiku-implementer` agent) |
| Synthesis: merge, fix, open the PR | Opus (`bmad-pb-synthesis` agent) |

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
