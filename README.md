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

### Requirements

- BMAD comes with it: on first run the skill installs `bmad-method` 6.12.0
  into the project (`_bmad/` and the `bmad-*` skills) if it isn't there yet.
  An existing install is left alone; set `BMAD_VERSION` to pin another release.
- Git, bash, Python and `uv`.
- The Azure CLI with the `azure-devops` extension, with `az devops configure`
  defaults set for the organisation and project.
- The worktree scripts assume a Node monorepo with a Postgres test database
  named in `.env` (`DATABASE_URL`).
