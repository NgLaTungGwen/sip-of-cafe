#!/usr/bin/env bash
# Create an isolated worktree that can run BMAD skills.
#
# usage: prepare-worktree.sh <main-root> <name> <branch> [--apps] [--no-install] [--base <rev>]
#                            [--agents <prefix>]
#
# --agents is the agent name prefix: "bmad-batch-build:" when the agents come
# from the plugin, empty for project or user copies (see SKILL.md).
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
apps=0 install=1 base="origin/$(profile_get "$root" base || echo main)" agents=""
while [ $# -gt 0 ]; do
  case $1 in
    --apps) apps=1 ;;
    --no-install) install=0 ;;
    --base) base=$2; shift ;;
    --agents) agents=$2; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

wt="$root/.claude/worktrees/$name"
if [ -e "$wt" ]; then
  echo "worktree path already exists: $wt" >&2
  exit 1
fi

# The caller fetches once before preparing worktrees in parallel; concurrent
# fetches into one .git race on FETCH_HEAD.
git -C "$root" worktree add -b "$branch" "$wt" "$base" >&2

cp -r "$root/_bmad" "$wt/_bmad"
mkdir -p "$wt/.claude"
# A plugin install may leave the project without .claude/skills of its own.
if [ -d "$root/.claude/skills" ]; then
  cp -r "$root/.claude/skills" "$wt/.claude/skills"
fi
if [ -d "$root/.claude/agents" ]; then
  cp -r "$root/.claude/agents" "$wt/.claude/agents"
fi

# A private test database per worktree. Without one every run shares
# <dev db>_test, and each run's teardown deletes rows the other created
# (api/test/global-cleanup.ts). The test setup creates, migrates and seeds
# <db>_test from DATABASE_URL. *_FILE secrets stay in the main checkout and are
# referenced by absolute path, never copied. 127.0.0.1, not localhost: the
# localhost relay on this machine wedges.
db="hive_pb_$(printf '%s' "$name" | tr -c 'a-zA-Z0-9\n' '_' | tr 'A-Z' 'a-z')"
per_db=1
if profile_get "$root" base > /dev/null; then
  [ "$(profile_get "$root" database.per_worktree)" = True ] || per_db=0
fi
if [ -f "$root/.env" ] && [ "$per_db" = 0 ]; then
  sed -E -e "s#^([A-Z_]+_FILE)=(secrets/.*)#\1=$root/\2#" "$root/.env" > "$wt/.env"
elif [ -f "$root/.env" ]; then
  sed -E \
    -e "s#^(DATABASE_URL|MIGRATION_DATABASE_URL)=(postgres://[^@]*@)[^/]*/.*#\1=\2127.0.0.1:55432/$db#" \
    -e "s#^([A-Z_]+_FILE)=(secrets/.*)#\1=$root/\2#" \
    "$root/.env" > "$wt/.env"
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
heavy="$scripts/heavy.sh" bg="$scripts/bg.sh"

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
if [ -n "$suite_cmd" ]; then
  suite_fact="The spec's Verification section lists the targeted test files, the typecheck of each touched workspace, and the full suite exactly once; the full suite is the only check you run. Run that full suite off the critical path: as soon as the implementation subagent returns, start \`bash $bg run $wt suite \\\"bash $heavy 'cd $suite_dir && $suite_cmd'\\\"\` as a Bash call with run_in_background: true, and launch the review straight away. Before review triage, run \`bash $bg wait $wt suite\` (again while it prints still running) and triage a failure like any other review finding. After a suite failure, send the fix to a new implementation subagent, which re-runs only the failing files."
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
[ -n "$t_test" ] || t_test="the project's test command, limited to those files"
[ -n "$t_tc" ] || t_tc="the project's typecheck or build check"

# Pin bmad-build-auto to this worktree. The file sits in the worktree's private
# copy of _bmad, so the main checkout never sees it.
cat > "$wt/_bmad/custom/bmad-build-auto.toml" <<EOF
[workflow]

persistent_facts = [
  "The project root for this run is $wt. Every file read, edit, test and git command targets that directory. Never touch anything outside $wt: not the main checkout $root, and not any other worktree.",
  "Run shell commands as \`cd $wt && ...\`, or use \`git -C $wt\`.",
  "Source files change only through the implementation subagent, review fixes included. For every fix round, launch a new \`${agents}haiku-implementer\` with the Agent tool in the foreground, giving it the spec path and the fixes. Never resume an earlier one with SendMessage: that runs in the background, and you would end your turn before the fixes land. Never end your turn or hand back while a fix round is running. The parent edits only the spec and files under _bmad-output/.",
  "No single Bash call may run longer than 10 minutes; Claude Code moves a longer one to the background. Start anything that can take longer (the full suite, a build, an install) with \`bash $bg run $wt <name> \"<command>\"\` as a Bash call with run_in_background: true, and wait for it with one \`bash $bg wait $wt <name>\` per Bash call, repeated while it prints still running. Never wrap a wait in a loop, \`timeout\` or \`sleep\`. Logs and scratch files stay inside $wt.",
  "Workflow gaps: if the spec template has no baseline_revision, set it to the output of \`git -C $wt rev-parse HEAD\` before the first change. If _bmad-output/ is gitignored, commit the spec with \`git add -f\`. Exclude _bmad-output from git pathspecs with ':(exclude)_bmad-output'; the short form ':!_bmad-output' fails with 'Unimplemented pathspec magic'.",
  "Dependencies install in the background while you plan; planning needs only the checkout. Before any command that needs node_modules (tests, typecheck, build, lint), run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, halt with its output as the blocking condition.",
  "Testing has one owner per check. The implementation subagent runs the targeted test files and the typecheck of each workspace it touched, after its first pass and after every review fix, and reports the results. You do not re-run them: if it reports one it could not get green, send that back to it. $suite_fact Synthesis builds, so skip the build unless the intent, the spec's Verification or AGENTS.md requires one; then run it once, after the implementation subagent returns, through the queue: \`bash $heavy 'cd $wt && <the project build command>'\`.",
  "file:AGENTS.md",
]

implementation_handoff = """
Launch a subagent with no prior conversation context, using the \`${agents}haiku-implementer\` agent (\`subagent_type: ${agents}haiku-implementer\`), with this prompt:

> The project root is $wt. Every file you read or edit, and every command you run, must be inside that directory. Use absolute paths and \`cd $wt && ...\` for shell commands. Nothing outside $wt may change: not the main checkout $root, and not any other worktree.
>
> Dependencies may still be installing. Before you run any command, run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, stop and report its output.
>
> Read {spec_file} fully and implement it. The spec is the sole source of truth. Load every file listed in its frontmatter \`context:\` before you start.
>
> You own the targeted checks; the parent does not repeat them. $t_clause the typecheck of each workspace you touched through the machine-wide queue (\`bash $heavy 'cd $checks && $t_tc'\`). Run them again after every review fix you are sent. Report each result.
>
> When you're done, report what you changed, how you verified it, and anything left incomplete or risky.
"""
EOF

if [ "$install" = 0 ]; then
  echo "$wt"
  exit 0
fi

# Without --no-install the install runs here, in the foreground, through the
# same bg.sh entry a worker uses, so `bg.sh wait <wt> install` works either way.
bash "$bg" run "$wt" install "bash '$scripts/install-deps.sh' '$wt'$([ "$apps" = 1 ] && echo " --apps")" >&2

echo "$wt"
