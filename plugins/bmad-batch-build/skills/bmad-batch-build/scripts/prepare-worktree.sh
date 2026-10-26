#!/usr/bin/env bash
# Create an isolated worktree that can run BMAD skills.
#
# usage: prepare-worktree.sh <main-root> <name> <branch> [--apps] [--no-install] [--base <rev>]
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
shift 3
apps=0 install=1 base=origin/main
while [ $# -gt 0 ]; do
  case $1 in
    --apps) apps=1 ;;
    --no-install) install=0 ;;
    --base) base=$2; shift ;;
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
cp -r "$root/.claude/skills" "$wt/.claude/skills"
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
if [ -f "$root/.env" ]; then
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

# Pin bmad-build-auto to this worktree. The file sits in the worktree's private
# copy of _bmad, so the main checkout never sees it.
cat > "$wt/_bmad/custom/bmad-build-auto.toml" <<EOF
[workflow]

persistent_facts = [
  "The project root for this run is $wt. Every file read, edit, test and git command targets that directory. Never touch anything outside $wt: not the main checkout $root, and not any other worktree.",
  "Run shell commands as \`cd $wt && ...\`, or use \`git -C $wt\`.",
  "Source files change only through the implementation subagent, review fixes included: send them to it again. The parent edits only the spec and files under _bmad-output/.",
  "Dependencies install in the background while you plan; planning needs only the checkout. Before any command that needs node_modules (tests, typecheck, build, lint), run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, halt with its output as the blocking condition.",
  "Testing has one owner per check. The implementation subagent runs the targeted test files and the typecheck of each workspace it touched, after its first pass and after every review fix, and reports the results. You do not re-run them: if it reports one it could not get green, send that back to it. The spec's Verification section lists the targeted test files, the typecheck of each touched workspace, and the full DB suite exactly once; the full suite is the only check you run. Run that full suite off the critical path: as soon as the implementation subagent returns, start \`bash $bg run $wt suite \"bash $heavy 'cd $wt && npm test'\"\` as a Bash call with run_in_background: true, and launch the review straight away. Before review triage, run \`bash $bg wait $wt suite\` (again while it prints still running) and triage a failure like any other review finding. After a suite failure, send the fix to the implementation subagent, which re-runs only the failing files. Never run \`npm run build\`; synthesis builds.",
  "file:AGENTS.md",
]

implementation_handoff = """
Launch a subagent with no prior conversation context, using the \`haiku-implementer\` agent (\`subagent_type: haiku-implementer\`), with this prompt:

> The project root is $wt. Every file you read or edit, and every command you run, must be inside that directory. Use absolute paths and \`cd $wt && ...\` for shell commands. Nothing outside $wt may change: not the main checkout $root, and not any other worktree.
>
> Dependencies may still be installing. Before you run any command, run \`bash $bg wait $wt install\`, again while it prints still running. If it prints failed, stop and report its output.
>
> Read {spec_file} fully and implement it. The spec is the sole source of truth. Load every file listed in its frontmatter \`context:\` before you start.
>
> You own the targeted checks; the parent does not repeat them. Run the test files you added or changed, with a compact reporter (\`cd $checks && npx vitest run <files> --reporter=dot\`), and the typecheck of each workspace you touched through the machine-wide queue (\`bash $heavy 'cd $checks && npm run typecheck'\`). Run them again after every review fix you are sent. Report each result.
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
