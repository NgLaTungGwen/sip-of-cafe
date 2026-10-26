#!/usr/bin/env bash
# usage: ship-enhancement.sh <enhance branch>
#
# Publishes a fix the enhancer drafted, once the user has said "ship it".
# Several sessions share the source clone (~/.claude/bmad-batch-runs/src/
# sip-of-cafe), so this never switches its branch: it takes a lock, builds the
# release in a temporary worktree on top of origin/main, squashes the draft in
# (its own version bump, if any, gives way to origin/main's), sets the next
# patch version, runs the self-test against every installed project, pushes
# main, installs into every project in install/installs.json, and notes it in
# enhance-log.md. Stops before the push on any failure.
set -uo pipefail
branch=${1:?usage: ship-enhancement.sh <enhance branch>}
store="${BMAD_RUNS_DIR:-$HOME/.claude/bmad-batch-runs}"
src="$store/src/sip-of-cafe"
rel="plugins/bmad-batch-build"
py=$(command -v python || command -v python3)

git -C "$src" rev-parse --git-dir >/dev/null 2>&1 || { echo "ship: no source clone at $src"; exit 1; }
git -C "$src" rev-parse --verify -q "$branch" >/dev/null || { echo "ship: no branch $branch"; exit 1; }

# One ship at a time; a lock older than an hour belongs to a dead ship.
lock="$store/ship.lock"
if ! mkdir "$lock" 2>/dev/null; then
  if [ -n "$(find "$lock" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then rmdir "$lock"; mkdir "$lock"
  else echo "ship: another ship is running ($lock); try again in a few minutes"; exit 1; fi
fi
wt="$store/src/wt-ship-$$"
cleanup() { cd "$store"; git -C "$src" worktree remove --force "$wt" 2>/dev/null; rmdir "$lock" 2>/dev/null; }
trap cleanup EXIT

git -C "$src" fetch -q origin || { echo "ship: fetch failed"; exit 1; }
git -C "$src" worktree add -q --detach "$wt" origin/main || { echo "ship: could not create the release worktree"; exit 1; }
cd "$wt" || exit 1

# The draft's changes as one commit on the latest main. Its version files are
# taken from main: the version is set below, so two drafts never collide on it.
msg=$(git log --format='%B' "origin/main..$branch")
[ -n "$msg" ] || { echo "ship: $branch has nothing that is not already on main"; exit 1; }
if ! git merge -q --squash "$branch" 2>/dev/null; then
  git checkout -q --ours -- "$rel/.claude-plugin/plugin.json" "$rel/skills/bmad-batch-build/VERSION" 2>/dev/null
  git add -- "$rel/.claude-plugin/plugin.json" "$rel/skills/bmad-batch-build/VERSION" 2>/dev/null
  if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    echo "ship: $branch conflicts with main in: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"
    echo "ship: ask the enhancer to redo it on the current main; nothing pushed"
    exit 1
  fi
fi
git checkout -q origin/main -- "$rel/.claude-plugin/plugin.json" "$rel/skills/bmad-batch-build/VERSION"
old=$(cut -d' ' -f1 "$rel/skills/bmad-batch-build/VERSION" | tr -d '\r')
new="${old%.*}.$(( ${old##*.} + 1 ))"
printf '%s %s\n' "$new" "$(date +%F)" > "$rel/skills/bmad-batch-build/VERSION"
sed -i "s/\"version\": \"$old\"/\"version\": \"$new\"/" "$rel/.claude-plugin/plugin.json"

roots=()
if [ -f "$store/install/installs.json" ]; then
  # Python on Windows ends lines with \r\n, and a root ending in \r is a path
  # that doesn't exist, so the carriage returns go before bash reads the lines.
  while IFS= read -r r; do roots+=("$r"); done < <("$py" -c "import json,sys; [print(t['root']) for t in json.load(open(sys.argv[1]))['targets']]" "$store/install/installs.json" | tr -d '\r')
fi
bash "$wt/$rel/skills/bmad-batch-build/scripts/self-test.sh" "${roots[@]}" || { echo "ship: self-test failed; nothing pushed"; exit 1; }

git add -A && git commit -q -F - <<EOF || { echo "ship: commit failed"; exit 1; }
$msg

Released as $new.
EOF
git push -q origin HEAD:main || { echo "ship: push failed (main moved?); run it again"; exit 1; }
echo "ship: pushed $(git log --oneline -1) as $new"
git -C "$src" branch -q -D "$branch"
git -C "$src" fetch -q origin
if [ "$(git -C "$src" branch --show-current)" = main ] && [ -z "$(git -C "$src" status --porcelain)" ]; then
  git -C "$src" merge -q --ff-only origin/main 2>/dev/null || true
fi

"$py" "$wt/$rel/skills/bmad-batch-build/scripts/install-skill.py"
code=$?
printf -- '- shipped %s as %s (%s) on %s\n' "$branch" "$(git rev-parse --short HEAD)" "$new" "$(date +%F)" >> "$store/enhance-log.md"
exit $code
