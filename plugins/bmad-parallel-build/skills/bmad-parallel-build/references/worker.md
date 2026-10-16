# Worker

One worker per unit. It runs `bmad-build-auto` inside the unit's worktree and
nowhere else.

## Pinned to its worktree

`scripts/prepare-worktree.sh` writes `<wt>/_bmad/custom/bmad-build-auto.toml`
into the worktree's private copy of `_bmad`. It adds a persistent fact and an
`implementation_handoff` that both name `<wt>` as the only place work may
happen, so the implementation subagent stays there as well.

## Dispatch prompt

Send every worker in **one message**, with one `Agent` call per unit
(`subagent_type: general-purpose`, `run_in_background: false`). Fill in the
placeholders:

- `<root>`: the absolute `<root>` from SKILL.md
- `<skill>`: the absolute `<skill>` from SKILL.md
- `<name>`: `<slug>-<n>`
- `<wt>`: `<root>/.claude/worktrees/<name>`
- `<branch>`: the unit's branch

```text
You are a Build Auto worker for one unit of a parallel build.

Project root: <wt>   (a git worktree on branch <branch>, which you create in step 0)
Work only inside <wt>. Never read, edit or run anything elsewhere in <root> (the main checkout) or in another worktree.

0. Prepare the worktree. Run each command below as its own foreground Bash
   call with `timeout: 600000`, in order, and stop at the first failure:
   a. bash "<skill>/scripts/prepare-worktree.sh" "<root>" "<name>" "<branch>" --no-install
   b. cd "<wt>" && npm ci --no-audit --no-fund --loglevel=error
   c. cd "<wt>" && npm run build
   d. (only if this unit touches apps/) cd "<wt>/apps" && bun install --frozen-lockfile
   If b, c or d fails, retry it once, because parallel installs can hit EBUSY
   on Windows. If a fails, or a retry fails, reply with:
   SPEC: none
   STATUS: blocked
   BLOCKED: worktree preparation failed: <command and the last lines of output>
   HEAD: none
1. Run this exactly once, from any directory:
   uv run --no-cache "<wt>/_bmad/scripts/render_skill.py" --project-root "<wt>" --skill "<wt>/.claude/skills/bmad-build-auto"
2. Read the one absolute workflow.md path it prints, and follow it to its end
   with the intent below. Subagents you launch must run in the foreground, as
   that workflow requires.
3. When the workflow halts, reply with exactly these lines and nothing else:
   SPEC: <absolute path of the spec or result file the workflow wrote>
   STATUS: <the status in its frontmatter>
   BLOCKED: <blocking condition, or none>
   HEAD: <output of `git -C <wt> rev-parse HEAD`>

Intent:
<the unit's self-contained intent: goal, scope, the files it owns, acceptance
criteria in Given/When/Then, and the excerpt of the source material it came
from. The worker sees nothing else.>
```

## Gate

Don't trust the reply text alone. For each worker:

1. Read the `SPEC:` file and check `status` in its frontmatter and the
   `## Auto Run Result` section.
2. Run `git -C <wt> status --porcelain`. It should be empty, because the worker
   commits but never pushes.
3. Run `git -C <wt> branch --show-current`. It must print `<branch>`.
   Synthesis merges that ref, so a worker that moved to another branch would
   leave nothing to merge.
4. Run `git -C <wt> log --oneline origin/main..<branch>`. It should list at
   least one commit.

| Result | Meaning |
|---|---|
| `status: built` (or `in-review` with its commits present) and a clean tree | Ready to merge. |
| `blocked`, `dropped`, no commits, or a dirty tree | Not ready. Record the unit, the blocking condition and the spec path. |
