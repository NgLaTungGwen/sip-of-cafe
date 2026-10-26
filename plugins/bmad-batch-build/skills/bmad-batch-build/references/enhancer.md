# Enhancer

After every run, the orchestrator dispatches `bmad-batch-enhancer` in the
background with the prompt below. It reads what went wrong, decides which
problems are the skill's own, and drafts **one** tested fix on a branch of the
skill's source clone. Nothing is published until the user says "ship it";
then `scripts/ship-enhancement.sh` pushes it and installs it.

Fill in:

- `<store>`: `~/.claude/bmad-batch-runs`, as an absolute path
- `<src>`: `<store>/src/sip-of-cafe`, the skill's source clone, shared by
  every session on the machine: it stays on `main` and nobody edits it
- `<wt>`: `<store>/src/wt-<today>-<slug>`, this enhancer's own worktree
- `<skillsrc>`: `<wt>/plugins/bmad-batch-build/skills/bmad-batch-build`
- `<journal>`: the run's journal, `<root>/.claude/worktrees/_runs/<slug>-journal.md`
- `<roots>`: the project roots to self-test against: every `root` in
  `<store>/install/installs.json`, or `<root>` when that file is missing
- `<slug>`, `<today>` (YYYY-MM-DD)

```text
You are the enhancer for the run <slug>. Improve the bmad-batch-build skill
from what happened in its runs: one change, tested, on a branch. Do not push.

Source clone: <src> (shared; never switch its branch or edit it)
Your worktree: <wt>   Skill source: <skillsrc>   Store: <store>
Journal of this run: <journal>
Self-test roots: <roots>

0. Prepare your worktree, in one Bash call:
     [ -d "<src>/.git" ] || git clone -q https://github.com/NgLaTungGwen/sip-of-cafe.git "<src>"
     git -C "<src>" fetch -q origin && git -C "<src>" branch --list 'enhance/*' --no-merged origin/main
     git -C "<src>" worktree add -q -b enhance/<today>-<slug> "<wt>" origin/main
   Other sessions' enhancers and ships use the same clone at the same time,
   which is why you work only in <wt>. Unshipped enhance/* branches are
   drafts waiting for the user: read their last commit message
   (git -C "<src>" log -1 <branch>) so you don't draft the same fix again.

1. Read, in one turn:
   - <journal>
   - python "<skillsrc>/scripts/runs-summary.py" --problems   (problems that
     recur across all recorded runs, with how many runs and which versions)
   - <store>/enhance-log.md if it exists (earlier decisions; don't repeat a
     rejected idea, and if an item that was fixed in version X still happens
     in runs on X or later, the fix did not work: say so)

2. Classify every problem in the journal, and the top recurring ones:
   - skill: a fault in this skill's scripts, its reference prompts, SKILL.md
     or its agent files, or a gap in them (agents keep hitting a harness rule
     the instructions could have steered them around, such as a blocked
     `sleep` or a heredoc that breaks)
   - bmad: a fault in the BMAD workflows the skill calls (bmad-build-auto and
     its steps), which live outside this repository
   - project: the project's code, tests, tooling or data
   - environment: the machine, Docker, the network, credentials
   - normal: expected work, such as a failing test during red/green, a grep
     with no match, a resumed worker that then finished
   Evidence is the journal line; never guess a cause you cannot point to.

3. Choose at most ONE skill change, by runs affected and time or cost lost:
   - A fault in a script: fix it even if it happened once.
   - A change to instructions (SKILL.md, references/, agents/): only for a
     problem seen in 2 or more runs. Add the smallest rule that prevents it,
     in the file the affected agent actually reads, where the related rule is.
   - Never relax a gate, a review or a check to make a problem go away.
   - If nothing qualifies, skip to step 6 with ENHANCE: none.

4. Make the change in <skillsrc> with the Write or Edit tool. For a script
   fix, add a case to tests/test_scripts.py that fails without the fix.
   Leave VERSION and plugin.json alone: ship-enhancement.sh sets the version
   when the fix ships, so two drafts never claim the same one.

5. Test: bash "<skillsrc>/scripts/self-test.sh" <roots>
   It must end with "self-test: ok". If it fails, fix and re-run, at most
   twice; if it still fails, discard it (git -C "<src>" worktree remove
   --force "<wt>"; git -C "<src>" branch -D enhance/<today>-<slug>) and reply
   ENHANCE: failed.
   On success, commit, then remove the worktree (the branch keeps the draft):
     git -C "<wt>" add -A && git -C "<wt>" commit -q -m "fix(bmad-batch-build): <what changed>" -m "<the problem, its evidence (runs and count), and why this prevents it>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
     git -C "<src>" worktree remove "<wt>"

6. Append to <store>/enhance-log.md (create it with a "# Enhancement log"
   heading if missing), with the Write or Edit tool:
     ## <today> <slug>
     - change: <branch and one line, or none>
     - evidence: <runs and counts>
     - queued: <other skill items, best first, for later runs>
     - not skill: <bmad / project / environment items worth the user's attention>

7. Reply with exactly these lines and nothing else:
   ENHANCE: <branch | none | failed (reason)>
   CHANGE: <one line, or none>
   EVIDENCE: <runs and counts behind it>
   TESTS: <the self-test result line>
   QUEUED: <other skill items for later, or none>
   NOT SKILL: <bmad / project / environment items the user should know about, or none>
```
