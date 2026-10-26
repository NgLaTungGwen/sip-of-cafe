#!/usr/bin/env bash
# usage: ship-enhancement.sh <enhance branch>
#
# Publishes a fix the enhancer drafted, once the user has said "ship it":
# puts the branch on top of origin/main in the source clone
# (~/.claude/bmad-batch-runs/src/sip-of-cafe), runs the self-test against every
# installed project, pushes main, installs the new version into every project
# in install/installs.json, and notes it in enhance-log.md. Stops before the
# push on any failure, so nothing half-tested is published.
set -uo pipefail
branch=${1:?usage: ship-enhancement.sh <enhance branch>}
store="${BMAD_RUNS_DIR:-$HOME/.claude/bmad-batch-runs}"
src="$store/src/sip-of-cafe"
skill="$src/plugins/bmad-batch-build/skills/bmad-batch-build"
py=$(command -v python || command -v python3)

cd "$src" || { echo "ship: no source clone at $src"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "ship: the source clone has uncommitted changes; stopping"; exit 1; }
git fetch -q origin || { echo "ship: fetch failed"; exit 1; }
git rev-parse --verify -q "$branch" >/dev/null || { echo "ship: no branch $branch"; exit 1; }

# On top of the latest main: a draft made before another shipped fix is rebased.
git switch -q "$branch" && git rebase -q origin/main || {
  git rebase --abort 2>/dev/null
  echo "ship: $branch does not rebase cleanly onto origin/main; ask the enhancer to redo it"
  exit 1
}

roots=()
if [ -f "$store/install/installs.json" ]; then
  # Python on Windows ends lines with \r\n, and a root ending in \r is a path
  # that doesn't exist, so the carriage returns go before bash reads the lines.
  while IFS= read -r r; do roots+=("$r"); done < <("$py" -c "import json,sys; [print(t['root']) for t in json.load(open(sys.argv[1]))['targets']]" "$store/install/installs.json" | tr -d '\r')
fi
bash "$skill/scripts/self-test.sh" "${roots[@]}" || { echo "ship: self-test failed; nothing pushed"; exit 1; }

git switch -q main 2>/dev/null || git switch -q -c main origin/main
git merge -q --ff-only origin/main && git merge -q --ff-only "$branch" || { echo "ship: could not fast-forward main"; exit 1; }
git push -q origin main || { echo "ship: push failed"; exit 1; }
git branch -q -d "$branch"
echo "ship: pushed $(git log --oneline -1)"

"$py" "$skill/scripts/install-skill.py"
code=$?
printf -- '- shipped %s as %s on %s\n' "$branch" "$(git rev-parse --short HEAD)" "$(date +%F)" >> "$store/enhance-log.md"
exit $code
