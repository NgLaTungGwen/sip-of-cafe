#!/usr/bin/env bash
# Remove a worktree made by prepare-worktree.sh, and its branch.
#
# usage: remove-worktree.sh <main-root> <name> <branch>
#
# npm workspaces put junctions in node_modules (node_modules/@hive/api -> api/).
# On Windows `git worktree remove` follows them and fails half-way, so every
# junction is unlinked first. `rmdir` on a junction removes the link only,
# never its target. The branch is deleted with -d, so an unmerged branch is
# kept and reported.
set -euo pipefail

root=$1 name=$2 branch=$3
wt="$root/.claude/worktrees/$name"
[ -d "$wt" ] || { echo "no worktree at $wt" >&2; exit 1; }

# Git Bash reports junctions as symlinks; find does not descend into them.
find "$wt" -path "$wt/.git" -prune -o -type l -print | while IFS= read -r link; do
  if [ -d "$link" ]; then
    cmd //c rmdir "$(cygpath -w "$link")"
  else
    rm -f "$link"
  fi
done

git -C "$root" worktree remove "$wt" 2>/dev/null || {
  # Only ignored files such as node_modules and dist are left at this point.
  rm -rf "$wt"
  git -C "$root" worktree prune
}
# The private test database prepare-worktree.sh routed this worktree to.
db="hive_pb_$(printf '%s' "$name" | tr -c 'a-zA-Z0-9\n' '_' | tr 'A-Z' 'a-z')_test"
if grep -q '^DATABASE_URL=postgres' "$root/.env" 2>/dev/null; then
  docker exec hive-postgres psql -U postgres -qc "drop database if exists $db" || echo "could not drop $db" >&2
fi

# -d refuses a branch that is not merged. After a squash-merged PR every unit
# branch reads as unmerged; delete those with -D once the PR is confirmed merged.
git -C "$root" branch -d "$branch"
