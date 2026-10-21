#!/usr/bin/env bash
# Create an isolated worktree that can run BMAD skills.
#
# usage: prepare-worktree.sh <main-root> <name> <branch> [--apps] [--no-install] [--base <rev>]
#
# --no-install stops after checkout, copies, .env and pin file, so a caller with
# a per-command time limit can run `npm ci`, `npm run build` and `bun install`
# as separate steps.
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
sed -E \
  -e "s#^(DATABASE_URL|MIGRATION_DATABASE_URL)=(postgres://[^@]*@)[^/]*/.*#\1=\2127.0.0.1:55432/$db#" \
  -e "s#^([A-Z_]+_FILE)=(secrets/.*)#\1=$root/\2#" \
  "$root/.env" > "$wt/.env"

# Pin bmad-build-auto to this worktree. The file sits in the worktree's private
# copy of _bmad, so the main checkout never sees it.
cat > "$wt/_bmad/custom/bmad-build-auto.toml" <<EOF
[workflow]

persistent_facts = [
  "The project root for this run is $wt. Every file read, edit, test and git command targets that directory. Never touch anything outside $wt: not the main checkout $root, and not any other worktree.",
  "Run shell commands as \`cd $wt && ...\`, or use \`git -C $wt\`.",
  "file:AGENTS.md",
]

implementation_handoff = """
Launch a subagent with no prior conversation context, using the \`haiku-implementer\` agent (\`subagent_type: haiku-implementer\`), with this prompt:

> The project root is $wt. Every file you read or edit, and every command you run, must be inside that directory. Use absolute paths and \`cd $wt && ...\` for shell commands. Nothing outside $wt may change: not the main checkout $root, and not any other worktree.
>
> Read {spec_file} fully and implement it. The spec is the sole source of truth. Load every file listed in its frontmatter \`context:\` before you start.
>
> When you're done, report what you changed, how you verified it, and anything left incomplete or risky.
"""
EOF

if [ "$install" = 0 ]; then
  echo "$wt"
  exit 0
fi

# api resolves @hive/contracts through packages/contracts/dist, which is gitignored.
(cd "$wt" && npm ci --no-audit --no-fund --loglevel=error >&2 && npm run build >&2)
if [ "$apps" = 1 ]; then
  (cd "$wt/apps" && bun install --frozen-lockfile >&2)
fi

echo "$wt"
