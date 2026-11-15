#!/usr/bin/env bash
# Create an isolated worktree that can run BMAD skills.
#
# usage: prepare-worktree.sh <main-root> <name> <branch> [--apps] [--no-install] [--base <rev>]
#                            [--agents <prefix>] [--db-template <template>]
#                            [--intake <intake.md> --unit <id> [--iface <file>]]
#
# --intake and --unit come from the dispatch's Intake: and Unit: lines. They
# add INTAKE, UNIT and IFACE to scripts.env, and the facts that make the
# worker read the interface broadcast before its tests run and give the intent
# auditor the intake (intake.py unit) instead of the raw prompt. --iface is the
# dispatch's Interface changes: file; it defaults to the intake's
# <runs>/<slug>-interface-changes.md. Without --intake nothing changes.
#
# --db-template is the run's migrated template database (db-template.sh). The
# worktree's test database is cloned from it; see the database section below.
#
# --agents is the agent name prefix: "bmad-batch-build:" when the agents come
# from the plugin, empty for project or user copies (see SKILL.md), or
# "general-purpose" in fallback mode, when the session has not loaded them.
#
# --no-install stops after checkout, copies, .env and pin file. Workers use it
# and then start scripts/install-deps.sh in the background through bg.sh, so
# the install overlaps planning; the pin file tells every agent to wait for it
# (`bg.sh wait <wt> install`) before a command that needs node_modules.
#
# _bmad/ and .claude/ are gitignored, so a fresh worktree has neither the BMAD
# runtime nor the skills. Both are copied in. Dependencies are installed inside
# the worktree; the main checkout's node_modules is never linked in, because a
# later forced removal follows the links (see AGENTS.md / project memory).
#
# Prints the absolute worktree path on the last line of stdout.
set -euo pipefail

root=$1 name=$2 branch=$3
# Forward slashes: the root ends up inside TOML strings below, where a
# backslash (D:\Projects\...) starts an escape sequence and breaks the file.
root=${root//\\//}
shift 3
. "$(dirname "${BASH_SOURCE[0]}")/lib-profile.sh"
. "$(dirname "${BASH_SOURCE[0]}")/lib-db.sh"
apps=0 install=1 base=$(base_ref "$root") agents="" db_template="" intake="" unit="" iface=""
while [ $# -gt 0 ]; do
  case $1 in
    --apps) apps=1 ;;
    --no-install) install=0 ;;
    --base) base=$2; shift ;;
    --agents) agents=$2; shift ;;
    --db-template) db_template=$2; shift ;;
    --intake) intake=$2; shift ;;
    --unit) unit=$2; shift ;;
    --iface) iface=$2; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$intake" ] && [ -z "$unit" ]; then
  echo "--intake needs --unit <id>" >&2
  exit 2
fi
if [ -n "$intake" ]; then
  # Forward slashes, as for the root: these paths go into TOML strings.
  intake=$(cygpath -m "$intake" 2>/dev/null || printf '%s' "$intake")
  intake=${intake//\\//}
  [ -n "$iface" ] || iface="${intake%-intake.md}-interface-changes.md"
  iface=$(cygpath -m "$iface" 2>/dev/null || printf '%s' "$iface")
  iface=${iface//\\//}
  unit=${unit#U}
fi

wt="$root/.claude/worktrees/$name"
if [ -e "$wt" ]; then
  echo "worktree path already exists: $wt" >&2
  exit 1
fi

# The caller fetches once before preparing worktrees in parallel; concurrent
# fetches into one .git race on FETCH_HEAD.
git -C "$root" worktree add -b "$branch" "$wt" "$base" >&2

exclude=$(git -C "$wt" rev-parse --path-format=absolute --git-path info/exclude)
# hide <path>: keep a file this script adds out of the worktree's git status,
# so bmad-build-auto's clean-tree check passes.
hide() {
  if git -C "$wt" ls-files --error-unmatch -- "$1" > /dev/null 2>&1; then
    git -C "$wt" update-index --skip-worktree -- "$1"
  elif ! git -C "$wt" check-ignore -q -- "$1"; then
    mkdir -p "$(dirname "$exclude")"
    grep -qxF "/$1" "$exclude" 2>/dev/null || echo "/$1" >> "$exclude"
  fi
}
# copy_in <path>: bring a gitignored folder of the main checkout into the
# worktree. A project that tracks the folder already has it after checkout,
# where `cp -r` would land inside it (_bmad/_bmad, .claude/skills/skills), so
# then only what git doesn't track is copied in.
copy_in() {
  [ -e "$root/$1" ] || return 0
  if [ ! -e "$wt/$1" ]; then
    mkdir -p "$(dirname "$wt/$1")"
    cp -r "$root/$1" "$wt/$1"
    return 0
  fi
  local p
  while IFS= read -r -d '' p; do
    p=${p%/}
    mkdir -p "$(dirname "$wt/$p")"
    cp -r "$root/$p" "$wt/$p"
    hide "$p"
  done < <(git -C "$root" ls-files --others --directory -z -- "$1")
}
copy_in _bmad
mkdir -p "$wt/.claude"
# A plugin install may leave the project without .claude/skills of its own.
copy_in .claude/skills
copy_in .claude/agents
# The code graph, when the main checkout has one that git doesn't track:
# CLAUDE.md tells every agent to query graphify-out/graph.json "when it
# exists", and in a fresh worktree it never did, so the agents fell back to
# grep (sonar-zero-5). Only the graph and its report; the build cache stays.
if [ -f "$root/graphify-out/graph.json" ] && [ ! -e "$wt/graphify-out/graph.json" ]; then
  mkdir -p "$wt/graphify-out"
  for f in graph.json GRAPH_REPORT.md manifest.json; do
    [ -f "$root/graphify-out/$f" ] && cp "$root/graphify-out/$f" "$wt/graphify-out/$f" && hide "graphify-out/$f"
  done
  [ -d "$root/graphify-out/wiki" ] && cp -r "$root/graphify-out/wiki" "$wt/graphify-out/wiki" && hide graphify-out/wiki
fi

# A private test database per worktree. Without one every run shares
# <dev db>_test, and each run's teardown deletes rows the other created
# (api/test/global-cleanup.ts), which showed up as FK, 409 and duplicate-key
# failures that pass when a suite runs alone. With --db-template the database
# is cloned from the run's migrated template (lib-db.sh, db-template.sh), so
# the suite finds it ready. When the clone fails (Postgres down, a connection
# open on the template), hive-factory's test setup creates, migrates and seeds
# <db>_test from DATABASE_URL as before, and a profiled project keeps the
# shared database in .env, since nothing would create its private one. A
# project with no database gets none of this. *_FILE secrets stay in the main
# checkout and are referenced by absolute path, never copied.
db="hive_pb_$(db_ident "$name")"
dbmode=$(db_mode "$root")
if [ "$dbmode" != none ] && [ -n "$db_template" ]; then
  target="$db$(db_suffix "$root" "$dbmode")"
  if db_clone "$root" "$dbmode" "$db_template" "$target"; then
    echo "test database: $target, cloned from $db_template" >&2
  else
    echo "test database: could not clone $db_template into $target; $([ "$dbmode" = hive ] && echo "the test setup creates and migrates it" || echo "this worktree shares the database in .env")" >&2
    [ "$dbmode" = profile ] && dbmode=none
  fi
elif [ "$dbmode" = profile ]; then
  dbmode=none  # no template: nothing would create the private database
fi
if [ -f "$root/.env" ] && [ "$dbmode" = none ]; then
  sed -E -e "s#^([A-Z_]+_FILE)=(secrets/.*)#\1=$root/\2#" "$root/.env" > "$wt/.env"
elif [ -f "$root/.env" ]; then
  sed -E -e "$(db_url "$dbmode" "$db")" -e "s#^([A-Z_]+_FILE)=(secrets/.*)#\1=$root/\2#" "$root/.env" > "$wt/.env"
fi
# Vitest starts one worker per CPU by default. With several suites running at
# once across worktrees and sessions that crashed workers, so every run in this
# worktree, targeted ones included, is capped (dotenv puts these in process.env
# before vitest reads them). The cap is a quarter of the CPUs, at least 2, as in
# heavy.sh: 3 on a 12-CPU machine. The minimum goes with it: Vitest 2 defaults
# it to CPUs - 1 and refuses to start ("minThreads and maxThreads must not
# conflict") when that is above the maximum.
vt=$(( $(nproc 2>/dev/null || echo 8) / 4 )); [ "$vt" -lt 2 ] && vt=2
printf '\nVITEST_MAX_FORKS=%s\nVITEST_MAX_THREADS=%s\nVITEST_MIN_FORKS=1\nVITEST_MIN_THREADS=1\n' "$vt" "$vt" >> "$wt/.env"

scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
heavy="$scripts/heavy.sh" bg="$scripts/bg.sh" app="$scripts/app-check.sh"
# Python on Windows cannot open Git Bash's /c/... form, so the coverage reader
# gets the mixed C:/... form.
lcov=$(cygpath -m "$scripts/lcov-gaps.py" 2>/dev/null || echo "$scripts/lcov-gaps.py")
# The same paths in a file in the worktree's private git dir, where bg.sh keeps
# its logs. An agent that compacted away its prompt finds them again from the
# worktree path alone; without it, implementers fell back to stale copies and
# the guard refused them 6 times (ti-port). The handoff and the agent files name
# the lookup: cat "$(git -C <wt> rev-parse --absolute-git-dir)/bmad/scripts.env".
scripts_env="$(git -C "$wt" rev-parse --absolute-git-dir)/bmad/scripts.env"
mkdir -p "$(dirname "$scripts_env")"
printf "HEAVY='%s'\nBG='%s'\nLCOV='%s'\nAPP_CHECK='%s'\nSCRIPTS='%s'\n" "$heavy" "$bg" "$lcov" "$app" "$scripts" > "$scripts_env"
[ -z "$intake" ] || printf "INTAKE='%s'\nUNIT='%s'\nIFACE='%s'\n" "$intake" "$unit" "$iface" >> "$scripts_env"
paths_lookup="cat \\\"\$(git -C $wt rev-parse --absolute-git-dir)/bmad/scripts.env\\\""

# A unit dispatched with an intake (Intake: and Unit: lines) reads it through
# intake.py: the intent auditor gets the unit's view of the frozen intake
# (Goal, Non-goals, Shared interfaces, its ACs) instead of the raw intent, and
# the worker re-reads the interface broadcast before each test run, so a pinned
# interface another unit was allowed to change reaches it before its tests do.
intake_fact=""
if [ -n "$intake" ]; then
  intake_py=$(cygpath -m "$scripts/intake.py" 2>/dev/null || echo "$scripts/intake.py")
  unit_cmd="python \\\"$intake_py\\\" unit \\\"$intake\\\" $unit"
  show_cmd="python \\\"$intake_py\\\" iface show \\\"$iface\\\""
  intake_fact="  \"This unit was planned in an intake: $intake, unit $unit. In a lane, later units reuse this worktree: wherever this fact names unit $unit, use the id on your dispatch's Unit: line instead. \`$unit_cmd\` prints its view (Goal, Non-goals, Shared interfaces, this unit's block and the full text of its acceptance criteria); read it before planning and keep the spec's acceptance criteria to those ACs. This replaces step-04's definition of {verbatim_intent} for the Intent Alignment Auditor: run \`$unit_cmd\` just before launching that layer and put its whole output where its prompt says {verbatim_intent}, never the raw intent. A pinned interface (one listed under Shared interfaces) keeps the shape listed there: if the unit cannot be built without changing one, stop and reply STATUS: blocked-interface as your worker prompt says. Before you start the affected tests or a full-suite run, every time, run \`$show_cmd\`: it prints the interface changes the orchestrator approved for other units, or none. Adapt this unit's code to each change that touches an interface it provides or consumes, through the implementation subagent, before the tests start.\","
fi

# A project whose profile names an app gets each unit checked in the running
# app (app-check.sh). Every worktree gets its own port, so parallel units
# never start two dev servers on one.
app_fact=""
if profile_get "$root" app.start > /dev/null; then
  port=$("$PROFILE_PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  printf 'APP_PORT=%s\n' "$port" >> "$wt/.env"
  app_fact="  \"Check the unit in the running app after the last review fix, before you set the final status. With the Write tool, write a drive script at $wt/_bmad-output/evidence/<stem>/drive.sh (or .mjs, .py), where <stem> is the spec's file name without .md. It exercises every acceptance criterion against \$APP_URL (curl for an API, Playwright for a page, saving screenshots in the same folder), prints what it saw for each, and exits non-zero when one fails. Run it with \`bash $bg run $wt app-<stem> \\\"bash $heavy 'bash $app $wt _bmad-output/evidence/<stem>/drive.sh'\\\"\` as a Bash call with run_in_background: true, then \`bash $bg wait $wt app-<stem>\`, again while it prints still running. The script starts the app on this worktree's own port, runs the drive script and stops the app. A failure is a finding: send the fix to a new implementation subagent and run the check again. Add \`## Verification Evidence\` to the spec, one line per acceptance criterion with what the drive script observed, and commit the evidence folder with the spec. The gate reads the result: it rejects the unit when the check failed, never ran, or ran before a later code change.\","
fi

# How the coder is launched: by its agent name, or, with --agents
# general-purpose (a session that has not loaded the run's agents yet, such as
# the first run after a copied install), as a general-purpose agent on Haiku
# that reads the agent's instructions from the skill.
impl_type="${agents}haiku-implementer" impl_extra="" impl_fix=""
if [ "$agents" = general-purpose ]; then
  impl_md="$(cd "$scripts/.." && pwd)/agents/haiku-implementer.md"
  impl_md=$(cygpath -m "$impl_md" 2>/dev/null || printf '%s' "$impl_md")
  impl_type="general-purpose"
  impl_extra=", \`model: haiku\`; begin the prompt with: First read $impl_md and follow it as your standing instructions."
  impl_fix=" agent (\`model: haiku\`, its prompt starting with: First read $impl_md and follow it as your standing instructions.)"
fi

# The implementer's targeted checks run where the project keeps them: api/ in a
# monorepo like hive-factory, the root in a single-package repo like HealthPro.
# Naming api/ in a repo without one sent implementers to a missing directory.
checks="$wt"
[ -d "$wt/api" ] && checks="$wt/api"
suite_dir="$wt" suite_cmd="npm test"
t_test="npx vitest run <files> --reporter=dot" t_tc="npm run typecheck"
# No root package.json (packages in app/ and the like): the first package folder.
if [ ! -d "$wt/api" ] && [ ! -f "$wt/package.json" ]; then
  . "$(dirname "${BASH_SOURCE[0]}")/lib-packages.sh"
  first=$(package_dirs "$wt" | head -1)
  [ -n "$first" ] && checks="$wt/$first"
fi
# The project profile's commands, when it has them.
profiled=""
if profile_get "$root" suite > /dev/null || profile_get "$root" targeted > /dev/null; then profiled=1; fi
if profile_get "$root" base > /dev/null && ! profile_get "$root" suite > /dev/null; then
  suite_cmd=""
elif s=$(profile_get "$root" suite); then
  d=$(printf '%s\n' "$s" | awk -F'\t' '$1=="dir"{print $2}'); c=$(printf '%s\n' "$s" | awk -F'\t' '$1=="cmd"{print $2}')
  [ -n "$c" ] && suite_dir="$wt/$d" suite_cmd="$c"
fi
if s=$(profile_get "$root" targeted); then
  d=$(printf '%s\n' "$s" | awk -F'\t' '$1=="dir"{print $2}')
  checks="$wt/${d:-.}"
  t_test=$(printf '%s\n' "$s" | awk -F'\t' '$1=="test"{print $2}')
  t_tc=$(printf '%s\n' "$s" | awk -F'\t' '$1=="typecheck"{print $2}')
fi
# The suite part of the testing fact, or what to do when there is no suite.
# A unit runs only the tests its change affects (affected-tests.sh, from the
# commit the worktree starts at); the full suite runs once, in synthesis,
# unless the unit touches a path that can break any test.
affected="$scripts/affected-tests.sh" start=$(git -C "$wt" rev-parse HEAD)
# A bun apps/ that declares vitest (threat-intel) runs it through bunx and its
# test script: bun test is bun's own runner and ignores vitest.config (no jsdom,
# no coverage thresholds; ti-port, 5 workers).
# The suite is its own test script whenever it has one, as verify.sh runs it.
# affected.py runner_of makes the same choice for the affected run.
. "$(dirname "${BASH_SOURCE[0]}")/lib-packages.sh"
apps_test="bun test <files>" apps_suite="bun test"
has_script "$wt/apps" test && apps_test="bun run test <files>" apps_suite="bun run test"
if [ -f "$wt/apps/package.json" ] && grep -q '"vitest"' "$wt/apps/package.json"; then
  apps_test="bunx vitest run <files>" apps_suite="bun run test"
fi
apps_full=""
if [ -z "$profiled" ] && [ -f "$wt/apps/package.json" ] && { [ -f "$wt/apps/bun.lock" ] || [ -f "$wt/apps/bun.lockb" ]; }; then
  apps_full=" and \`cd $wt/apps && $apps_suite\`"
fi
if [ -n "$suite_cmd" ]; then
  suite_fact="The spec's Verification section lists the targeted test files, the typecheck of each touched workspace, and the affected tests exactly once; the affected tests are the only check you run. They are the tests this unit's change reaches, not the full suite: synthesis runs the full suite once, for the whole run. Run them off the critical path: as soon as the implementation subagent returns, start \`bash $bg run $wt suite \\\"bash $heavy 'bash $affected $wt $start'\\\"\` as a Bash call with run_in_background: true, and launch the review straight away. It runs \`vitest run --changed $start\` in each touched vitest workspace, in a bun workspace the changed test files and those beside the changed sources, and prints what it chose and why. When the unit touches a path that can break any test (packages/contracts, migrations, test setup, vitest or tsconfig config, a package.json or lockfile, the project profile), it runs the full suite instead (\`cd $suite_dir && $suite_cmd\`$apps_full); do not start the full suite by hand. Before review triage, run \`bash $bg wait $wt suite\` (again while it prints still running) and triage a failure like any other review finding. After a failure, send the fix to a new implementation subagent, which re-runs only the failing files."
else
  suite_fact="This project has no full test suite to run (its project profile lists none, for example because AGENTS.md forbids running tests): skip the full-suite step. A workflow step that asks for a passing test per row, such as the Matrix Test Audit, is met by tracing each row to the code that satisfies it, and the spec says so."
fi
# Without a suite and without a targeted test command the project runs no tests.
if [ -z "$suite_cmd" ] && [ -z "$t_test" ]; then
  t_clause="This project runs no tests (see AGENTS.md); run"
else
  t_run=${t_test:-"the project test command, limited to those files"}
  t_clause="Run the test files you added or changed, with a compact reporter (\`cd $checks && $t_run\`), and"
fi
# An apps/ package on bun (hive-factory) is checked inside apps/ with bun: the
# api/root commands above sent apps units to api/ and npx vitest
# (sonar-zero-4, intake-toolchain-ux). Added to them, since a unit can touch both.
apps_clause=""
# A project profile's commands already say where and how apps/ is checked;
# the clause would contradict them.
if [ -z "$profiled" ] && [ -f "$wt/apps/package.json" ] && { [ -f "$wt/apps/bun.lock" ] || [ -f "$wt/apps/bun.lockb" ]; }; then
  if [ -z "$suite_cmd" ] && [ -z "$t_test" ]; then
    apps_clause=" Files under apps/ get their typecheck inside apps/ with bun, never npm: \`bash $heavy 'cd $wt/apps && bun run typecheck'\`."
  else
    apps_clause=" Files under apps/ are checked inside apps/ with bun, never npm or npx: test files with \`cd $wt/apps && $apps_test\`, and the typecheck through the queue with \`bash $heavy 'cd $wt/apps && bun run typecheck'\`."
  fi
fi
# The root suite never reaches apps/ (sonar-zero-5 units 6 and 7 ran it by
# hand): the affected run covers apps/ changes itself, so there is no second job.
[ -n "$t_test" ] || t_test="the project's test command, limited to those files"
[ -n "$t_tc" ] || t_tc="the project's typecheck or build check"

# Pin bmad-build-auto to this worktree. The file sits in the worktree's private
# copy of _bmad, so the main checkout never sees it.
cat > "$wt/_bmad/custom/bmad-build-auto.toml" <<EOF
[workflow]

persistent_facts = [
  "The project root for this run is $wt. Every file read, edit, test and git command targets that directory. Never touch anything outside $wt: not the main checkout $root, and not any other worktree.",
  "Run shell commands as \`cd $wt && ...\`, or use \`git -C $wt\`. Every command here is bash: with no Bash tool (PowerShell only, on Windows), run each one as bash -c '<command>' through PowerShell, writing a single quote inside it as two, and never translate it into PowerShell.",
  "Source files change only through the implementation subagent, review fixes included. For every fix round, launch a new \`${impl_type}\`${impl_fix} with the Agent tool in the foreground, giving it the spec path and the fixes. Never resume an earlier one with SendMessage: that runs in the background, and you would end your turn before the fixes land. This replaces step-04's instruction to re-engage the same implementation subagent. Never end your turn or hand back while a fix round is running. The parent edits only the spec and files under _bmad-output/.",
  "No single Bash call may run longer than 10 minutes; Claude Code moves a longer one to the background. Start anything that can take longer (the full suite, a build, an install) with \`bash $bg run $wt <name> \"<command>\"\` as a Bash call with run_in_background: true, and wait for it with one \`bash $bg wait $wt <name>\` per Bash call, repeated while it prints still running. Never wrap a wait in a loop, \`timeout\` or \`sleep\`. Logs and scratch files stay inside $wt. The one exception is {diff_file}, which steps 03 and 04 write to the system temp directory: keep it there, because inside $wt it would end up in the next diff.",
  "Every subagent prompt you write yourself (a fix round, a reviewer, a helper) must say, in its own words: the skill's scripts are \`$heavy\`, \`$bg\` and \`$lcov\`, and no other copy (another copy's are refused), and after a compaction \`$paths_lookup\` prints them again (the one file outside $wt anyone may read; you can use it too); never call TaskStop or Monitor, which they cannot use (a stuck command ends at its time limit; wait for it with bg.sh); write files only inside $wt; do not commit. You commit the implementation subagent's work yourself, after it returns, so every commit carries your own trailer. Give one implementation subagent at most about 15 findings or 3 files per round, and run rounds one after another: parallel ones in one worktree share its test database and coverage folder (heavy.sh runs their tests one at a time anyway), and in sonar-zero-5 large rounds came back partial. To read coverage, run \`python $lcov <lcov file> <path part>\` instead of parsing lcov by hand: it handles CRLF, backslash paths and bun's lcov, and exits 2 when the lcov is empty.",
  "Write and change files with the Write or Edit tool. Never write a file or a script through a heredoc in Bash (\`cat > f <<'EOF'\`, \`python - <<'EOF'\`): quotes and backticks inside it break the shell, and the harness blocks the python one. Write the script to a file under $wt/_bmad-output/ with the Write tool, then run it.",
  "Workflow gaps: if the spec template has no baseline_revision, set it to the output of \`git -C $wt rev-parse HEAD\` before the first change. If _bmad-output/ is gitignored, commit the spec with \`git add -f\`. Exclude _bmad-output from git pathspecs with ':(exclude)_bmad-output'; the short form ':!_bmad-output' fails with 'Unimplemented pathspec magic'.",
  "Dependencies install in the background while you plan; planning needs only the checkout. Before any command that needs node_modules (tests, typecheck, build, lint), run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, halt with its output as the blocking condition.",
  "Testing has one owner per check. The implementation subagent runs the targeted test files and the typecheck of each workspace it touched, after its first pass and after every review fix, and reports the results. You do not re-run them: if it reports one it could not get green, send that back to it. $suite_fact Synthesis builds, so skip the build unless the intent, the spec's Verification or AGENTS.md requires one; then run it once, after the implementation subagent returns, through the queue: \`bash $heavy 'cd $wt && <the project build command>'\`.",
  "When the intent starts with \`Kind: bug\`, reproduce the bug before fixing it. The plan's first task is a test that shows the bug; the implementation subagent runs it before the fix, where it must fail, and after, where it must pass, and reports both result lines. Keep a test for each item of the intent's Unchanged behaviour list too. Record the reproduction in the spec under \`## Reproduction\`, one line each: \`RED: <command> -> <the failing line>\` and \`GREEN: <command> -> <the passing line>\`. In a project that runs no tests, reproduce it through the app check when there is one, otherwise write \`RED: traced: <file:line and why it fails>\` and \`GREEN: traced: <what the fix changes there>\`. The gate rejects a bug unit without both lines.",
  "Review is sized by risk. This replaces step-04's rule that all review subagents run at the same model capability as the current session: launch the Blind Hunter (\`blind-hunter\`) and the Intent Alignment Auditor (\`intent-alignment\`) with \`model: sonnet\` in their Agent calls, and the Edge Case Hunter and the Verification Gap Reviewer at the session model (no \`model\`). Each layer's 'Run only when' line reads the \`Risk:\` line of your worker prompt (low, med or high) and, when there is none, its \`Size:\` line (S, M or L; with neither, run every layer), and the diff: count its changed lines, insertions plus deletions, with \`git -C $wt diff --shortstat <baseline_revision>\`. The Edge Case Hunter and the Verification Gap Reviewer always run. Announce each skipped layer with the reason.",
  "Every finding row you write in the spec's Review Triage Log carries the tag of the layer that raised it, right after its verdict and route, as in: - \`[medium]\` \`patch\` [edge-case] <finding> — <evidence>. The tags are [blind-hunter], [edge-case], [verification-gap] and [intent-alignment]; a finding two layers raised carries both tags. A row without a tag cannot be traced to the layer that found it.",
$intake_fact
$app_fact
  "file:AGENTS.md",
]

implementation_handoff = """
Launch a subagent with no prior conversation context, using the \`${impl_type}\` agent (\`subagent_type: ${impl_type}\`${impl_extra}), with this prompt:

> The project root is $wt. Every file you read or edit, and every command you run, must be inside that directory. Use absolute paths and \`cd $wt && ...\` for shell commands; with no Bash tool, only PowerShell, run each as bash -c '<command>'. Nothing outside $wt may change: not the main checkout $root, and not any other worktree.
>
> Dependencies may still be installing. Before you run any command, run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, stop and report its output.
>
> Read {spec_file} fully and implement it. The spec is the sole source of truth. Load every file listed in its frontmatter \`context:\` before you start.
>
> You own the targeted checks; the parent does not repeat them. $t_clause the typecheck of each workspace you touched through the machine-wide queue (\`bash $heavy 'cd $checks && $t_tc'\`).$apps_clause Run them again after every review fix you are sent. Report each result. Leave whole test suites to the parent: it runs the tests your change affects once you return.
>
> Use only these copies of the skill's scripts: \`$heavy\`, \`$bg\` and, to read coverage, \`python $lcov <lcov file> <path part>\`. If you lose these paths, for example after a compaction, read them back with \`$paths_lookup\` (the one file outside $wt you may read); never search the disk for another copy. Never call TaskStop or Monitor; you cannot use them. Write logs and scratch files only inside $wt (under _bmad-output/). Do not commit: the parent commits your work.
>
> When you're done, report what you changed, how you verified it, and anything left incomplete or risky.
"""

# Review layers merge with bmad-build-auto's defaults by id, each replacing the
# whole layer. The blind hunter's finding floor and its "do not stop with an
# empty list" rule are gone: 60% of its findings were low, 3 high in 854. The
# edge-case and verification-gap layers keep their defaults and always run.
[[workflow.review_layers]]
id = "blind-hunter"
name = "Blind Hunter"
when = "your worker prompt has a \`Risk:\` line and it says \`Risk: med\` or \`Risk: high\` (with \`Risk: low\`, skip this layer whatever the size). With no Risk line, the 0.16.0 size rule: the unit is not small and low-risk. It is small and low-risk only when all three hold: your worker prompt says \`Size: S\`; the diff has fewer than 400 changed lines (insertions plus deletions, from \`git -C $wt diff --shortstat <baseline_revision>\`); and it touches no contract, migration or auth code (no changed path in a contracts package or a migrations folder, none with auth, session, permission or role in its path, and no change to an interface the intent agreed on). With no Size line, run it."
instruction = """
Launch a context-free subagent with \`model: sonnet\` (the review-sizing fact replaces step-04's same-model rule for this layer), with this prompt:

Conduct a review of CONTENT.
Look for what's missing, not only what's wrong.
Report only real defects: each one a bad outcome a user or developer would actually meet, at a file and line you cite. Leave out style, naming, taste and speculative hardening. There is no minimum number of findings: if you find no real defect, return an empty list and say so in one line.
Output a Markdown list of findings only, with no severity, priority or ranking.
If the content is empty, stop and say so.

CONTENT: the unified diff at \`{diff_file}\`. Read that file; it is the content under review.

Do not invoke any skill, and do not spawn subagents of your own: you are the reviewer. Return your findings as text in your final message; do not route them through any findings-reporting tool the host may offer.
"""

[[workflow.review_layers]]
id = "intent-alignment"
name = "Intent Alignment Auditor"
when = "the diff changes an interface this unit provides or consumes from the agreed shape (any Risk), or: with a \`Risk:\` line in your worker prompt, it says \`Risk: high\` (skip this layer for \`Risk: low\` and \`Risk: med\` unless an interface changed); with no Risk line, the 0.16.0 rule: your worker prompt says \`Size: L\` (or larger), or the diff changes an interface this unit provides or consumes from the shape the intent agreed (a route, payload, type or method signature you will mark CHANGED in your reply). With no Size line, run it."
instruction = """
Launch a subagent with \`model: sonnet\` (the review-sizing fact replaces step-04's same-model rule for this layer), with this prompt:

You are an intent-alignment auditor. You have no other context about how this change was produced. Here is the verbatim intent this work started from:

{verbatim_intent}

The diff is the unified diff at \`{diff_file}\`. Read that file; it is the change under review.

Your task is strictly descriptive; do not prescribe additional work. Report: (1) the defensible readings of the intent, enumerated; (2) which reading this diff implements; (3) where the readings and the diff diverge, specifically which surface the intent's expectations live at versus which surface the diff's changes and its tests exercise.

Do not invoke any skill, and do not spawn subagents of your own: you are the reviewer. Return your findings as text in your final message; do not route them through any findings-reporting tool the host may offer.
"""
EOF
hide _bmad/custom/bmad-build-auto.toml

if [ "$install" = 0 ]; then
  echo "$wt"
  exit 0
fi

# Without --no-install the install runs here, in the foreground, through the
# same bg.sh entry a worker uses, so `bg.sh wait <wt> install` works either way.
bash "$bg" run "$wt" install "bash '$scripts/install-deps.sh' '$wt'$([ "$apps" = 1 ] && echo " --apps")" >&2

echo "$wt"
