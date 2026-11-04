# Split PRs

The ending of a `bmad-batch-build-split` run: one PR per unit instead of one
PR for the run. The final synthesis reads this file in place of sections 4,
5 and 5b of `synthesis.md`, after section 3b, once `<ibranch>` is merged,
repaired, reviewed and fully verified. `<ibranch>` stays the single source of
truth: every PR branch is cut from it, and every code fix goes onto it first.

Placeholders come from the synthesis prompt, plus:

- `<split json>`: `<runs>/<slug>-split.json`, which the orchestrator kept
  while the run went: each unit's `from` and `to` commits, `depends_on`, and
  the units left out (format in `<skill>/scripts/split-prs.py`).
- `<footer>`: the line every commit ends with,
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

You may write `<split json>` as well as `<state>`; nothing else outside
`<iwt>`.

First run section 4b of `synthesis.md` (propose checks for repeated
mistakes) as written; it needs no PR, and its proposals go into `RULES:`.

When only one unit is in the run, there is nothing to split: follow sections
4, 5 and 5b of `synthesis.md` as written, and reply with one `PRS:` entry.

## S1. Cut the PR branches

In `<iwt>`, on `<ibranch>`, with everything committed:

```bash
cd <iwt> && python "<skill>/scripts/split-prs.py" cut "<split json>" --footer "<footer>"
```

It gives every file `<ibranch>` changed to the unit that touched it, groups
the units into stacks (units joined by `depends_on` or by a file both
touched), and builds one branch per unit, one commit each: the first PR of a
stack targets `<base>`, each later one the PR branch before it. It then
checks that the branches together hold exactly `<ibranch>` (`CHECK ok`) and
switches `<iwt>` back to `<ibranch>`.

- `UNOWNED <path>` (exit 2): a file only you changed, such as a contract sync
  output or a repair outside every unit's files. Give each to the unit whose
  intent it serves (generated contracts go to the unit that owns
  `packages/contracts`), and run the command again with one
  `--assign <path>=<unit>` per file. A file two independent units both need
  joins their stacks; that is fine.
- `EXISTS <branch>` (exit 3): a local branch from an earlier attempt; add
  `--replace`. A pushed one is never replaced: stop and report it.
- `REJECTED <branch>` (exit 5): a commit hook refused the message. Read its
  rule in the output and run again with `--replace --message "<template>"`
  (`{type}`, `{title}` and `{id}` are filled in per unit). Never
  `--no-verify`.
- `CHECK failed` (exit 6): the branches lost or added something. Read the
  lines, fix the cause (usually a wrong `from`/`to` in `<split json>`, which
  `git log --oneline <base>..<ibranch>` shows), and run again with
  `--replace`.

## S2. Verify every PR branch

The full suite passed on `<ibranch>`. Each PR branch holds only its unit and
those below it in its stack, so check that each still builds on its own: a
unit can use code from another unit without having declared it.

For each PR branch, in stack order, switch `<iwt>` to it and run build and
typecheck, the same way as section 3 of `synthesis.md` with `--no-test`:

```bash
cd <iwt> && git switch -q <pr branch>
bash "<skill>/scripts/bg.sh" run "<iwt>" verify-pr<n> "bash '<skill>/scripts/heavy.sh' --weight 3 \"bash '<skill>/scripts/verify.sh' '<iwt>' [--apps] [--infra] --no-test\""
```

then `bg.sh wait` as usual. The installed `node_modules` come from
`<ibranch>`, which is fine for a build. Switch back with
`git switch -q <ibranch>` when done, and before any fix.

When a PR branch fails on its own:

- **A missing piece of another unit** (an import, a type, a route): the unit
  depends on that one. Add it to the unit's `depends_on` in `<split json>`,
  run S1 again with `--replace`, and verify again. Its PR now stacks on the
  other one.
- **A real fault**: fix it on `<ibranch>` as a `fix(merge): ...` commit, have
  it reviewed as in section 3b, re-run what it touches there, then run S1
  again with `--replace`.

Never commit a fix on a PR branch alone: the next cut would drop it.

## S3. Push and open the PRs

Push every PR branch, in stack order: `git push -u origin <pr branch>` (from
`<iwt>`; never `--force`). With PR tool `none`, stop after pushing and reply
`PRS: none (branches pushed; open the PRs in the browser)`, with each
description written to `<state>`.

Open the PRs in stack order with the PR tool, each from its branch into its
target (`az repos pr create --source-branch <pr branch> --target-branch
<target> ...`, `gh pr create --head <pr branch> --base <target> ...`). Don't
use the `create-pr` skill here: it targets the default branch. Each
description, under 4000 characters:

1. The unit's title as the PR title, in the repository's commit convention.
2. **What changed:** the unit's behaviour, in a few bullets.
3. **Stack:** "1 of 1" for a PR into `<base>` with nothing above it;
   otherwise "Part <k> of <m>: merge after <the PR it targets>", and the
   whole stack in merge order with links once known.
4. A `Story:` line naming the unit's story as `#id`, and a `Work items:`
   line with the unit's task as `#id`.
5. `## Not included`, in the first PR only, when units were left out.
6. `## Open review findings`, in the PR of the unit whose files they are,
   from section 3b.
7. It ends with
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

After a PR in a stack is opened, edit the PR below it to name it, when the
PR tool allows. Record each PR's number and URL in `<state>`.

## S4. Link the tickets

Skip with tickets off, failed, or no PR. One call per PR:

```bash
python "<skill>/scripts/<tickets script>" "<tickets json>" --link-pr <pr id> --merged <unit> --unit-pr
```

It links the unit's task with close-on-complete, so each task closes when its
own PR completes. In Azure DevOps the story is not linked to any PR
(completing a PR transitions every linked item, so the first PR would close
the story); on GitHub the PR refers to it without closing it. Either way the
story stays open for the user to close after the last PR merges: say so in
`NOTES:`. On GitHub a stacked PR closes its task only once it targets the
default branch, which GitHub does itself when the PR below is merged and its
branch deleted.

## S5. Watch every PR's pipelines

As section 5b of `synthesis.md`, once per PR, with the same limits. A fix for
a red pipeline:

1. goes onto `<ibranch>` first (`fix(ci): ...`), reviewed as in section 3b,
   re-run there;
2. then onto the PR branch of the unit that owns the changed files, as a new
   commit: `git switch -q <that branch>` and `git checkout <ibranch> -- <the
   files>`, commit `fix(ci): ...`, push;
3. then up its stack: for each PR branch above it,
   `git switch -q <branch> && git merge --no-edit <the branch below>` and push.
   Never rebase or force-push a pushed branch;
4. `git switch -q <ibranch>` again.

## S6. Reply

The reply format of section 6 of `synthesis.md`, with `PR:` replaced by one
line per PR and the merge order:

```text
RESULT: pr | stuck
PRS: <unit n: url -> target; ...> | none (...)
MERGE ORDER: <each stack in order, for example 1 -> 3 -> 4 | 2>
TICKETS: <per PR: linked #ids, close-on-complete on or off; the story left open; or none>
MERGED: <unit titles>
REPAIRS: ...
VERIFY: <the <ibranch> checks, then each PR branch's build and typecheck>
REVIEW: ...
RULES: ...
CI: <per PR: green | red ... | pending ... | approval ... | none>
NOTES: ...
SKILL ISSUES: ...
```
